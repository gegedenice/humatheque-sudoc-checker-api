# Humatheque Sudoc Checker API

FastAPI service for checking whether VLM-extracted thesis or dissertation
metadata already has a Sudoc bibliographic record.

The API is designed for a printed-thesis cataloguing pipeline. It searches all
Sudoc thesis records via the Sudoc SRU with `tdo=y`, returns both printed/physical and electronic
candidates, but only printed/physical candidates count toward the duplicate
decision. Electronic Sudoc/STAR records are kept as evidence because they may
describe the same intellectual work without blocking creation of a printed
bibliographic record.

## Sudoc SRU Strategy

The service uses the public Sudoc SRU endpoint:

```text
https://www.sudoc.abes.fr/cbs/sru/
```

It relies on the ABES SRU guide indexes [https://abes.fr/wp-content/uploads/2023/05/guide-utilisation-service-sru-catalogue-sudoc.pdf](https://abes.fr/wp-content/uploads/2023/05/guide-utilisation-service-sru-catalogue-sudoc.pdf):

| Index | Purpose |
|---|---|
| `MTI` | title words |
| `AUT` | author words |
| `NTH` | thesis note, including degree, discipline, institution, year |
| `TDO` | document-type limitation |

`tdo=y` is always added for the thesis/dissertation search space. According to
the Sudoc SRU guide, `TDO y` covers thesis records across print and electronic
manifestations, so carrier detection is done after UNIMARC parsing.

## Duplicate Logic

`POST /check/thesis` runs several recall-oriented SRU queries, merges records by
PPN, parses UNIMARC XML, and scores candidates with deterministic lexical
components:

```text
final =
  0.42 * title
+ 0.22 * author
+ 0.16 * thesis_note
+ 0.10 * year
+ 0.05 * language
+ 0.05 * context_or_advisor
```

Candidate `carrier` is classified as:

| Carrier | Meaning |
|---|---|
| `electronic` | `856`, `135`, STAR source, or online RDA carrier evidence present |
| `printed_or_physical` | physical thesis evidence detected and no electronic marker |
| `unknown` | no decisive carrier marker |

Only candidates whose carrier is not `electronic` are counted as printed
duplicates.

Statuses:

| Status | Meaning |
|---|---|
| `duplicate_found` | printed/physical candidate score is at or above threshold |
| `ambiguous_print_candidate` | printed/physical candidate exists but score is below duplicate threshold |
| `electronic_only` | strong match exists only for electronic candidates |
| `no_print_duplicate_found` | no strong printed/physical candidate found |

## Endpoints

### `GET /health`

```json
{"ok": true}
```

### `GET /sru/search`

Debug endpoint for a raw Sudoc SRU query:

```bash
curl "http://localhost:8000/sru/search?query=mti%3Dhygiene%20and%20aut%3Dgani%20and%20tdo%3Dy"
```

### `POST /check/thesis`

Example:

```bash
curl -X POST "http://localhost:8000/check/thesis" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "La question de l'\''hygiène aux Indes-Néerlandaises",
    "subtitle": "Les enjeux médicaux, culturels et sociaux",
    "author": "Gani Achmad JAE LANI",
    "degree_type": "Thèse de doctorat",
    "discipline": "Histoire et civilisations",
    "granting_institution": "École des Hautes Études en Sciences Sociales",
    "doctoral_school": "École doctorale de l'\''EHESS",
    "defense_year": 2017,
    "advisor": "Gérard JORLAND",
    "committee_members": "Romain BERTRAND|Patrice BOURDELAIS|Charles ILLOUZ|Annick OPINEL|Patrick ZYLBERMAN|Gérard JORLAND",
    "language": "fre"
  }'
```

Response shape:

```jsonc
{
  "source": "sudoc_sru_thesis_check",
  "status": "electronic_only",
  "duplicate_score": 0.0,
  "best_print_candidate": null,
  "best_electronic_candidate": {
    "ppn": "229969437",
    "url": "https://www.sudoc.fr/229969437",
    "title": "La question de l'hygiène aux Indes-Néerlandaises : les enjeux médicaux,culturels et sociaux.",
    "carrier": "electronic",
    "counts_as_print_duplicate": false,
    "score": {"final": 0.9}
  },
  "candidates": []
}
```

## Run

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

With `uv` from this workspace:

```bash
uv run uvicorn app:app --reload
```

## Authentication

Authentication is optional. Set `SUDOC_API_KEY` or `API_KEY`; clients must then
send:

```text
X-API-Key: <key>
```
