# Humatheque Sudoc Check API

Service FastAPI permettant de vérifier si des métadonnées de thèse ou de
mémoire extraites par VLM correspondent déjà à une notice bibliographique du
Sudoc.

L'API est conçue pour s'intégrer dans un pipeline de catalogage de thèses imprimées. Elle
interroge les notices de thèses via le SRU du Sudoc avec `tdo=y`, retourne les candidats
imprimés et électroniques, mais seuls les candidats imprimés
sont pris en compte dans la décision de doublon. Les notices électroniques
Sudoc/STAR sont conservées comme indices, car elles peuvent décrire la même
oeuvre intellectuelle sans bloquer la création d'une notice de document
imprimée.

## Stratégie SRU Sudoc

Le service utilise le point d'accès public SRU du Sudoc :

```text
https://www.sudoc.abes.fr/cbs/sru/
```

Il s'appuie sur les index du guide SRU de l'ABES [https://abes.fr/wp-content/uploads/2023/05/guide-utilisation-service-sru-catalogue-sudoc.pdf](https://abes.fr/wp-content/uploads/2023/05/guide-utilisation-service-sru-catalogue-sudoc.pdf) :

| Index | Usage |
|---|---|
| `MTI` | mots du titre |
| `AUT` | mots de l'auteur |
| `NTH` | note de thèse, incluant diplôme, discipline, établissement, année |
| `TDO` | limitation par type de document |

`tdo=y` est toujours ajouté pour restreindre la recherche aux thèses et
mémoires. D'après le guide SRU du Sudoc, `TDO y` couvre les notices de thèses
imprimées et électroniques ; la détection du support est donc réalisée ensuite,
après analyse de l'UNIMARC.

## Logique de doublon

`POST /check/thesis` exécute plusieurs requêtes SRU orientées rappel, fusionne
les notices par PPN, analyse l'UNIMARC XML et score les candidats avec des
composants lexicaux déterministes :

```text
final =
  0.42 * title
+ 0.22 * author
+ 0.16 * thesis_note
+ 0.10 * year
+ 0.05 * language
+ 0.05 * context_or_advisor
```

Le champ `carrier` d'un candidat est classé ainsi :

| Carrier | Signification |
|---|---|
| `electronic` | présence de `856`, `135`, d'une source STAR ou d'un indice de support RDA en ligne |
| `printed_or_physical` | indice de thèse physique détecté sans marqueur électronique |
| `unknown` | aucun marqueur de support décisif |

Seuls les candidats dont le `carrier` n'est pas `electronic` sont comptés comme
doublons imprimés.

Statuts :

| Statut | Signification |
|---|---|
| `duplicate_found` | le score d'un candidat imprimé atteint ou dépasse le seuil |
| `ambiguous_print_candidate` | un candidat imprimé existe, mais son score est sous le seuil de doublon |
| `electronic_only` | une correspondance forte existe seulement pour des candidats électroniques |
| `no_print_duplicate_found` | aucun candidat imprimé fort n'a été trouvé |

## Endpoints

### `GET /health`

```json
{"ok": true}
```

### `GET /sru/search`

Endpoint de debug pour une requête SRU Sudoc brute :

```bash
curl "http://localhost:8000/sru/search?query=mti%3Dhygiene%20and%20aut%3Dgani%20and%20tdo%3Dy"
```

### `POST /check/thesis`

Exemple :

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

Forme de réponse :

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

## Lancer le service

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Avec `uv` depuis cet espace de travail :

```bash
uv run uvicorn app:app --reload
```

## Authentification

L'authentification est optionnelle. Définir `SUDOC_API_KEY` ou `API_KEY` ;
les clients devront alors envoyer :

```text
X-API-Key: <clé>
```
