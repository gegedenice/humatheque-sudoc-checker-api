"""FastAPI service for detecting possible duplicate thesis records in Sudoc."""

from __future__ import annotations

import math
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.concurrency import run_in_threadpool
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

load_dotenv()


SUDOC_SRU_ENDPOINT = os.getenv("SUDOC_SRU_ENDPOINT", "https://www.sudoc.abes.fr/cbs/sru/")
USER_AGENT = os.getenv("SUDOC_USER_AGENT", "humatheque-sudoc-check-api/0.1")
API_KEY = os.getenv("SUDOC_API_KEY", os.getenv("API_KEY", ""))
RETRIED_STATUS = {429, 500, 502, 503, 504}

DEFAULT_TIMEOUT = float(os.getenv("SUDOC_HTTP_TIMEOUT", "30.0"))
DEFAULT_RETRIES = int(os.getenv("SUDOC_MAX_RETRIES", "2"))
DEFAULT_BACKOFF = float(os.getenv("SUDOC_BACKOFF_BASE", "1.0"))
DEFAULT_MAX_RECORDS_PER_QUERY = int(os.getenv("SUDOC_MAX_RECORDS_PER_QUERY", "10"))
DEFAULT_MAX_CANDIDATES = int(os.getenv("SUDOC_MAX_CANDIDATES", "20"))
DEFAULT_DUPLICATE_THRESHOLD = float(os.getenv("SUDOC_DUPLICATE_THRESHOLD", "0.78"))
DEFAULT_AMBIGUOUS_THRESHOLD = float(os.getenv("SUDOC_AMBIGUOUS_THRESHOLD", "0.62"))

SRW_NS = {"srw": "http://www.loc.gov/zing/srw/"}

app = FastAPI(
    title="Humatheque Sudoc Check API",
    version="0.1.0",
    description=(
        "Search Sudoc SRU for thesis/dissertation records from VLM-extracted metadata, "
        "score possible duplicates, and keep electronic-version evidence separate from "
        "printed-record duplicate decisions."
    ),
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: str | None = Security(api_key_header)) -> None:
    if API_KEY and api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class SudocCheckRequest(BaseModel):
    title: str = Field(..., description="Extracted main title.")
    subtitle: str = Field("", description="Extracted subtitle.")
    author: str = Field("", description="Extracted author name.")
    degree_type: str = Field("", description="Extracted degree or document type.")
    discipline: str = Field("", description="Extracted discipline.")
    granting_institution: str = Field("", description="Extracted granting institution.")
    co_tutelle_institutions: list[str] = Field(default_factory=list)
    doctoral_school: str = Field("", description="Extracted doctoral school.")
    defense_year: int | str | None = Field(None, description="Extracted defense year.")
    advisor: str = Field("", description="Extracted advisor name.")
    jury_president: str = Field("", description="Extracted jury president.")
    reviewers: str | list[str] = Field("", description="Extracted reviewers, string or list.")
    committee_members: str | list[str] = Field("", description="Extracted committee members, string or list.")
    language: str = Field("", description="ISO 639-2 language code if available.")
    confidence: float | None = Field(None, ge=0.0, le=1.0)

    max_records_per_query: int = Field(DEFAULT_MAX_RECORDS_PER_QUERY, ge=1, le=100)
    max_candidates: int = Field(DEFAULT_MAX_CANDIDATES, ge=1, le=100)
    duplicate_threshold: float = Field(DEFAULT_DUPLICATE_THRESHOLD, ge=0.0, le=1.0)
    ambiguous_threshold: float = Field(DEFAULT_AMBIGUOUS_THRESHOLD, ge=0.0, le=1.0)
    timeout: float = Field(DEFAULT_TIMEOUT, gt=0.0, le=120.0)
    retries: int = Field(DEFAULT_RETRIES, ge=0, le=10)
    backoff: float = Field(DEFAULT_BACKOFF, ge=0.0, le=30.0)
    include_unimarc_xml: bool = Field(False, description="Include raw record XML in candidate output.")


@dataclass
class SudocRecord:
    ppn: str
    title: str = ""
    subtitle: str = ""
    authors: list[str] = field(default_factory=list)
    contributors: list[dict[str, str]] = field(default_factory=list)
    institutions: list[str] = field(default_factory=list)
    year: str | None = None
    language: str | None = None
    nnt: str | None = None
    thesis: dict[str, Any] | None = None
    urls: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    unimarc_xml: str | None = None
    carrier: str = "unknown"
    carrier_evidence: list[str] = field(default_factory=list)
    matched_queries: list[str] = field(default_factory=list)
    score: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)


def normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\x98", " ").replace("\x9c", " ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compact_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\x98", "").replace("\x9c", "")
    return re.sub(r"\s+", " ", text).strip(" /,.;:")


def token_set(value: Any, min_len: int = 3) -> list[str]:
    seen = set()
    tokens = []
    for token in normalize_text(value).split():
        if len(token) < min_len or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def name_similarity(left: Any, right: Any) -> float:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm and not right_norm:
        return 1.0
    if not left_norm or not right_norm:
        return 0.0
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    overlap = left_tokens & right_tokens
    token_f1 = 2 * len(overlap) / (len(left_tokens) + len(right_tokens)) if left_tokens and right_tokens else 0.0
    char_ratio = SequenceMatcher(None, left_norm, right_norm).ratio()
    compact_ratio = SequenceMatcher(None, left_norm.replace(" ", ""), right_norm.replace(" ", "")).ratio()
    return max(0.65 * token_f1 + 0.35 * char_ratio, compact_ratio)


def text_vector(value: str) -> dict[str, float]:
    vector: dict[str, float] = {}
    for token in token_set(value):
        vector[token] = vector.get(token, 0.0) + 1.0
    return vector


def cosine_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    common = set(left) & set(right)
    dot = sum(left[token] * right[token] for token in common)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def lexical_similarity(left: str, right: str) -> float:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    cosine = cosine_similarity(text_vector(left_norm), text_vector(right_norm))
    ratio = SequenceMatcher(None, left_norm, right_norm).ratio()
    containment = 0.0
    if left_norm in right_norm or right_norm in left_norm:
        containment = min(len(left_norm), len(right_norm)) / max(len(left_norm), len(right_norm))
    return max(cosine, 0.65 * ratio + 0.35 * containment)


def split_people(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raw_items = re.split(r"[|;]", str(value))
    return [compact_text(item) for item in raw_items if compact_text(item)]


def build_context(payload: SudocCheckRequest) -> str:
    people = [payload.author, payload.advisor, payload.jury_president]
    people.extend(split_people(payload.reviewers))
    people.extend(split_people(payload.committee_members))
    parts: list[Any] = [
        payload.title,
        payload.subtitle,
        payload.degree_type,
        payload.discipline,
        payload.granting_institution,
        payload.doctoral_school,
        payload.defense_year,
    ]
    parts.extend(payload.co_tutelle_institutions)
    parts.extend(people)
    return " ".join(str(part) for part in parts if part)


def encode_sudoc_query(query: str) -> str:
    encoded = quote_plus(query, safe="()*")
    return encoded.replace("%3D", "%3D")


def sru_url(query: str, maximum_records: int, start_record: int = 1) -> str:
    params = {
        "operation": "searchRetrieve",
        "version": "1.1",
        "query": encode_sudoc_query(query),
        "startRecord": str(start_record),
        "maximumRecords": str(maximum_records),
        "recordSchema": "unimarc",
    }
    return f"{SUDOC_SRU_ENDPOINT}?{urlencode(params, safe='%+()*')}"


def request_xml(url: str, timeout: float, retries: int, backoff: float) -> tuple[ET.Element | None, str | None]:
    last_error = None
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml"})
            with urlopen(request, timeout=timeout) as response:
                payload = response.read()
            return ET.fromstring(payload), None
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}: {exc.reason}"
            if exc.code not in RETRIED_STATUS:
                break
        except URLError as exc:
            last_error = f"URL error: {exc.reason}"
        except ET.ParseError as exc:
            last_error = f"XML parse error: {exc}"
            break
        except Exception as exc:
            last_error = str(exc)
        if attempt < retries:
            time.sleep(backoff * (2**attempt))
    return None, last_error


def field(record: ET.Element, tag: str) -> list[ET.Element]:
    return [item for item in record.findall("datafield") if item.attrib.get("tag") == tag]


def control(record: ET.Element, tag: str) -> str | None:
    item = next((node for node in record.findall("controlfield") if node.attrib.get("tag") == tag), None)
    return compact_text(item.text) if item is not None and item.text else None


def subfields(datafield: ET.Element, code: str | None = None) -> list[str]:
    values = []
    for subfield in datafield.findall("subfield"):
        if code is None or subfield.attrib.get("code") == code:
            if subfield.text and compact_text(subfield.text):
                values.append(compact_text(subfield.text))
    return values


def first_subfield(datafield: ET.Element, code: str) -> str | None:
    values = subfields(datafield, code)
    return values[0] if values else None


def person_label(datafield: ET.Element) -> str | None:
    last = first_subfield(datafield, "a")
    first = first_subfield(datafield, "b")
    if last and first:
        return f"{first} {last}"
    return last or first


def parse_year(record: ET.Element, thesis: dict[str, Any] | None) -> str | None:
    if thesis and thesis.get("year"):
        return str(thesis["year"])
    for tag in ("214", "210"):
        for item in field(record, tag):
            date = first_subfield(item, "d")
            if date:
                match = re.search(r"\b(18|19|20)\d{2}\b", date)
                if match:
                    return match.group(0)
    coded = first_subfield(field(record, "100")[0], "a") if field(record, "100") else None
    if coded:
        match = re.search(r"[defghijklmnpqrstu](18|19|20)\d{2}", coded)
        if match:
            return match.group(0)[1:]
    return None


def parse_thesis(record: ET.Element) -> dict[str, Any] | None:
    for item in field(record, "328"):
        thesis = {
            "type": first_subfield(item, "b"),
            "discipline": first_subfield(item, "c"),
            "institution": first_subfield(item, "e"),
            "year": first_subfield(item, "d"),
            "raw": " ; ".join(subfields(item)),
        }
        if any(value for key, value in thesis.items() if key != "raw"):
            return thesis
    return None


def classify_carrier(record: ET.Element) -> tuple[str, list[str]]:
    evidence = []
    has_online_url = bool(field(record, "856"))
    has_electronic_135 = bool(field(record, "135"))
    source_values = [value.lower() for item in field(record, "035") for value in subfields(item, "a")]
    field_182 = " ".join(value.lower() for item in field(record, "182") for value in subfields(item))
    field_183 = " ".join(value.lower() for item in field(record, "183") for value in subfields(item))
    field_105_b = "".join(first_subfield(item, "b") or "" for item in field(record, "105"))

    if has_online_url:
        evidence.append("856 URL present")
    if has_electronic_135:
        evidence.append("135 electronic-resource field present")
    if any(value.startswith("star") for value in source_values):
        evidence.append("035 STAR source identifier")
    if "c" in field_182 or "ceb" in field_183:
        evidence.append("RDA media/carrier indicates online computer resource")
    if "m" in field_105_b:
        evidence.append("105$b m: original thesis")
    if "v" in field_105_b:
        evidence.append("105$b v: reproduction or other edition")

    if has_online_url or has_electronic_135 or any(value.startswith("star") for value in source_values):
        return "electronic", evidence
    if evidence:
        return "printed_or_physical", evidence
    return "unknown", evidence


def parse_record(record: ET.Element, include_unimarc_xml: bool = False) -> SudocRecord:
    ppn = control(record, "001") or ""
    thesis = parse_thesis(record)
    title_field = field(record, "200")[0] if field(record, "200") else None
    title = first_subfield(title_field, "a") if title_field is not None else ""
    subtitle = first_subfield(title_field, "e") if title_field is not None else ""
    statement = subfields(title_field, "f") + subfields(title_field, "g") if title_field is not None else []
    authors = [label for item in field(record, "700") for label in [person_label(item)] if label]
    contributors = []
    for item in field(record, "701") + field(record, "702"):
        label = person_label(item)
        if label:
            contributors.append({"name": label, "role": first_subfield(item, "4") or ""})
    institutions = []
    for item in field(record, "711") + field(record, "712"):
        institution = first_subfield(item, "a")
        if institution:
            institutions.append(institution)

    carrier, carrier_evidence = classify_carrier(record)
    urls = [url for item in field(record, "856") for url in subfields(item, "u")]
    identifiers = [value for tag in ("017", "029", "035") for item in field(record, tag) for value in subfields(item)]
    notes = statement + [value for tag in ("300", "304", "314", "330") for item in field(record, tag) for value in subfields(item, "a")]
    lang_values = [value for item in field(record, "101") for value in subfields(item, "a")]

    return SudocRecord(
        ppn=ppn,
        title=title or "",
        subtitle=subtitle or "",
        authors=authors,
        contributors=contributors,
        institutions=institutions,
        year=parse_year(record, thesis),
        language=lang_values[0] if lang_values else None,
        nnt=first_subfield(field(record, "029")[0], "b") if field(record, "029") else None,
        thesis=thesis,
        urls=urls,
        identifiers=identifiers,
        notes=notes,
        carrier=carrier,
        carrier_evidence=carrier_evidence,
        unimarc_xml=ET.tostring(record, encoding="unicode") if include_unimarc_xml else None,
    )


def response_records(root: ET.Element, include_unimarc_xml: bool) -> tuple[int, list[SudocRecord], list[str]]:
    total_text = root.findtext("srw:numberOfRecords", namespaces=SRW_NS) or "0"
    diagnostics = [
        compact_text(" ".join(node.itertext()))
        for node in root.findall(".//{http://www.loc.gov/zing/srw/diagnostic/}diagnostic")
    ]
    records = []
    for data in root.findall(".//srw:recordData", SRW_NS):
        record = data.find("record")
        if record is not None:
            parsed = parse_record(record, include_unimarc_xml=include_unimarc_xml)
            if parsed.ppn:
                records.append(parsed)
    return int(total_text) if total_text.isdigit() else 0, records, diagnostics


def title_terms(title: str, max_terms: int = 7) -> str:
    stop = {
        "une",
        "des",
        "les",
        "aux",
        "dans",
        "pour",
        "avec",
        "sans",
        "sur",
        "sous",
        "question",
        "these",
        "doctorat",
        "memoire",
    }
    terms = [token for token in token_set(title, min_len=4) if token not in stop]
    return " ".join(terms[:max_terms])


def author_terms(author: str) -> str:
    tokens = token_set(author, min_len=3)
    if len(tokens) <= 2:
        return " ".join(tokens)
    return " ".join([tokens[0], tokens[-1]])


def build_queries(payload: SudocCheckRequest) -> list[str]:
    main_title = title_terms(" ".join([payload.title, payload.subtitle]))
    short_title = title_terms(payload.title, max_terms=4)
    author = author_terms(payload.author)
    nth_terms = " ".join(
        token_set(
            " ".join(
                [
                    payload.discipline,
                    payload.granting_institution,
                    payload.doctoral_school,
                    str(payload.defense_year or ""),
                ]
            ),
            min_len=4,
        )[:6]
    )
    queries = []
    if main_title and author:
        queries.append(f"mti={main_title} and aut={author} and tdo=y")
    if short_title and author:
        queries.append(f"mti={short_title} and aut={author} and tdo=y")
    if main_title and nth_terms:
        queries.append(f"mti={main_title} and nth={nth_terms} and tdo=y")
    if short_title:
        queries.append(f"mti={short_title} and tdo=y")
    if author and nth_terms:
        queries.append(f"aut={author} and nth={nth_terms} and tdo=y")
    if nth_terms:
        queries.append(f"nth={nth_terms} and tdo=y")

    deduped = []
    seen = set()
    for query in queries:
        if query not in seen:
            seen.add(query)
            deduped.append(query)
    return deduped


def search_sudoc(
    query: str,
    max_records: int,
    timeout: float,
    retries: int,
    backoff: float,
    include_unimarc_xml: bool,
) -> dict[str, Any]:
    url = sru_url(query, max_records)
    root, error = request_xml(url, timeout, retries, backoff)
    if error or root is None:
        return {"query": query, "url": url, "total_found": 0, "records": [], "error": error, "diagnostics": []}
    total, records, diagnostics = response_records(root, include_unimarc_xml)
    for record in records:
        record.matched_queries.append(query)
    return {
        "query": query,
        "url": url,
        "total_found": total,
        "records": records,
        "error": None,
        "diagnostics": diagnostics,
    }


def score_candidate(payload: SudocCheckRequest, record: SudocRecord) -> None:
    expected_title = " : ".join(part for part in [payload.title, payload.subtitle] if part)
    record_title = " : ".join(part for part in [record.title, record.subtitle] if part)
    title_score = lexical_similarity(expected_title, record_title)

    author_score = max((name_similarity(payload.author, author) for author in record.authors), default=0.0)
    people = [item["name"] for item in record.contributors]
    advisor_score = name_similarity(payload.advisor, " ".join(people)) if payload.advisor else 0.0

    thesis_text = " ".join(
        [
            record.thesis.get("raw", "") if record.thesis else "",
            " ".join(record.institutions),
            " ".join(record.notes),
        ]
    )
    expected_thesis = " ".join(
        [
            payload.degree_type,
            payload.discipline,
            payload.granting_institution,
            payload.doctoral_school,
        ]
        + payload.co_tutelle_institutions
    )
    thesis_score = lexical_similarity(expected_thesis, thesis_text)

    expected_year = str(payload.defense_year or "")
    year_score = 1.0 if expected_year and record.year == expected_year else 0.0
    language_score = 1.0 if payload.language and record.language == payload.language else 0.0
    context_score = lexical_similarity(build_context(payload), " ".join([record_title, thesis_text, " ".join(people)]))

    final = (
        0.42 * title_score
        + 0.22 * author_score
        + 0.16 * thesis_score
        + 0.10 * year_score
        + 0.05 * language_score
        + 0.05 * max(context_score, advisor_score)
    )
    record.score = {
        "final": round(final, 4),
        "title": round(title_score, 4),
        "author": round(author_score, 4),
        "thesis_note": round(thesis_score, 4),
        "year": round(year_score, 4),
        "language": round(language_score, 4),
        "context": round(context_score, 4),
        "advisor": round(advisor_score, 4),
    }
    record.evidence = {
        "record_title": record_title,
        "authors": record.authors,
        "contributors": record.contributors,
        "institutions": record.institutions,
        "thesis": record.thesis,
        "carrier_evidence": record.carrier_evidence,
        "matched_queries": record.matched_queries,
    }


def candidate_to_json(record: SudocRecord, include_unimarc_xml: bool) -> dict[str, Any]:
    payload = {
        "source": "sudoc",
        "ppn": record.ppn,
        "url": f"https://www.sudoc.fr/{record.ppn}",
        "title": " : ".join(part for part in [record.title, record.subtitle] if part) or None,
        "authors": record.authors,
        "contributors": record.contributors,
        "institutions": record.institutions,
        "year": record.year,
        "language": record.language,
        "nnt": record.nnt,
        "thesis": record.thesis,
        "carrier": record.carrier,
        "counts_as_print_duplicate": record.carrier != "electronic",
        "urls": record.urls,
        "identifiers": record.identifiers,
        "score": record.score,
        "evidence": record.evidence,
    }
    if include_unimarc_xml:
        payload["unimarc_xml"] = record.unimarc_xml
    return payload


def status_for_candidates(
    ranked: list[SudocRecord],
    duplicate_threshold: float,
    ambiguous_threshold: float,
) -> tuple[str, SudocRecord | None, float]:
    print_ranked = [record for record in ranked if record.carrier != "electronic"]
    electronic_ranked = [record for record in ranked if record.carrier == "electronic"]
    if print_ranked and print_ranked[0].score["final"] >= duplicate_threshold:
        return "duplicate_found", print_ranked[0], print_ranked[0].score["final"]
    if print_ranked and print_ranked[0].score["final"] >= ambiguous_threshold:
        return "ambiguous_print_candidate", print_ranked[0], print_ranked[0].score["final"]
    if electronic_ranked and electronic_ranked[0].score["final"] >= duplicate_threshold:
        return "electronic_only", None, 0.0
    return "no_print_duplicate_found", None, print_ranked[0].score["final"] if print_ranked else 0.0


def check_sudoc(payload: SudocCheckRequest) -> dict[str, Any]:
    queries = build_queries(payload)
    if not queries:
        raise HTTPException(status_code=400, detail="Unable to build Sudoc query from submitted metadata.")

    searches = []
    candidates_by_ppn: dict[str, SudocRecord] = {}
    for query in queries:
        result = search_sudoc(
            query,
            payload.max_records_per_query,
            payload.timeout,
            payload.retries,
            payload.backoff,
            payload.include_unimarc_xml,
        )
        searches.append({key: value for key, value in result.items() if key != "records"})
        for record in result["records"]:
            existing = candidates_by_ppn.get(record.ppn)
            if existing:
                existing.matched_queries.extend(q for q in record.matched_queries if q not in existing.matched_queries)
                continue
            candidates_by_ppn[record.ppn] = record

    for record in candidates_by_ppn.values():
        score_candidate(payload, record)

    ranked = sorted(candidates_by_ppn.values(), key=lambda item: item.score["final"], reverse=True)
    ranked = ranked[: payload.max_candidates]
    status, best_print_candidate, duplicate_score = status_for_candidates(
        ranked,
        payload.duplicate_threshold,
        payload.ambiguous_threshold,
    )
    electronic_candidates = [record for record in ranked if record.carrier == "electronic"]

    return {
        "source": "sudoc_sru_thesis_check",
        "query": {
            "title": payload.title,
            "subtitle": payload.subtitle,
            "author": payload.author,
            "degree_type": payload.degree_type,
            "discipline": payload.discipline,
            "granting_institution": payload.granting_institution,
            "doctoral_school": payload.doctoral_school,
            "defense_year": payload.defense_year,
            "language": payload.language,
        },
        "sru": {
            "endpoint": SUDOC_SRU_ENDPOINT,
            "indexes": ["MTI", "AUT", "NTH", "TDO"],
            "doc_type_filter": "tdo=y",
            "queries": searches,
        },
        "score_weights": {
            "title": 0.42,
            "author": 0.22,
            "thesis_note": 0.16,
            "year": 0.10,
            "language": 0.05,
            "context_or_advisor": 0.05,
        },
        "status": status,
        "duplicate_score": round(duplicate_score, 4),
        "best_print_candidate": candidate_to_json(best_print_candidate, payload.include_unimarc_xml)
        if best_print_candidate
        else None,
        "best_electronic_candidate": candidate_to_json(electronic_candidates[0], payload.include_unimarc_xml)
        if electronic_candidates
        else None,
        "candidates": [candidate_to_json(record, payload.include_unimarc_xml) for record in ranked],
    }


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "humatheque-sudoc-check-api",
        "version": app.version,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/sru/search")
async def sru_search_endpoint(
    query: str = Query(..., description="Raw Sudoc CQL query, for example mti=hygiene and aut=gani and tdo=y."),
    max_records: int = Query(DEFAULT_MAX_RECORDS_PER_QUERY, ge=1, le=100),
    timeout: float = Query(DEFAULT_TIMEOUT, gt=0.0, le=120.0),
    retries: int = Query(DEFAULT_RETRIES, ge=0, le=10),
    backoff: float = Query(DEFAULT_BACKOFF, ge=0.0, le=30.0),
    include_unimarc_xml: bool = Query(False),
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    result = await run_in_threadpool(search_sudoc, query, max_records, timeout, retries, backoff, include_unimarc_xml)
    return {
        "source": "sudoc_sru",
        "query": query,
        "url": result["url"],
        "total_found": result["total_found"],
        "returned": len(result["records"]),
        "results": [candidate_to_json(record, include_unimarc_xml) for record in result["records"]],
        "diagnostics": result["diagnostics"],
        "error": result["error"],
    }


@app.post("/check/thesis")
async def check_thesis_endpoint(
    payload: SudocCheckRequest,
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    return await run_in_threadpool(check_sudoc, payload)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8003)
