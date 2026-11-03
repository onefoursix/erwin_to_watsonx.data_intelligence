# erwin → watsonx.data intelligence

Three Claude skills that take an **erwin Data Modeler** model (`.erwin`) into **IBM watsonx.data intelligence** (wxdi, SaaS), without erwin installed. Use them in order, or each on its own:

| # | Skill | What it does |
|---|---|---|
| 1 | [`erwin-model-export`](skills/erwin-model-export/SKILL.md) | Asks for the model, reads the `.erwin` file, and writes an interactive **viewer** page, **all the model's metadata as one JSON file**, and **SQL** (PostgreSQL and/or SQL Server) for its tables, keys, descriptions and views |
| 2 | [`wxdi-metadata-import`](skills/wxdi-metadata-import/SKILL.md) | Optionally creates the schema and a read-only login in the database, then runs a wxdi **metadata import, with or without lineage**, through a platform connection |
| 3 | [`erwin-wxdi-governance`](skills/erwin-wxdi-governance/SKILL.md) | Creates a top-level **category** named after the model and nested categories for its subject areas, imports the **business terms**, **publishes** them (completing the Publish task, with the user's OK), publishes the tables, columns and views to the **catalog**, **assigns the terms**, and assigns **classifications** such as PII to the columns |

Ask Claude in plain words, e.g. *"open the eMovies model"*, *"import the emovies schema from SQL Server into wxdi without lineage"*, *"do the governance import for eMovies"*. Each skill checks first, explains what it will change, and asks before anything that needs your decision (running DDL, which account, publishing terms, which classifications).

## How they connect

```
.erwin ──(1)──► work/<schema>/<schema>.metadata.json ───────────────(3)──► glossary, catalog, terms, classifications
                work/<schema>/<schema>.<dialect>.sql ──(2)──► database ──► wxdi project assets ─┘
                work/<schema>/<Name>.diagram.html  (viewer, published as an Artifact)
```

`work/` and `.env` live in the project home: this repository's root, found by walking up from each skill (`ERWIN_WXDI_HOME` overrides it). Git ignores both.

## Setup

- Python 3.8+ and `curl`. Skill 1 and skill 3 need nothing else. Skill 2 needs database drivers: `python3 -m venv venv && venv/bin/pip install psycopg2-binary pymssql`.
- Copy `.env.example` to `.env` and fill in the IBM Cloud API key and wxdi URL, and the admin login for the database you use (Postgres or SQL Server).
- Install the skills by linking them into Claude's skills folder:

```bash
for s in erwin-model-export wxdi-metadata-import erwin-wxdi-governance; do ln -s "$PWD/skills/$s" ~/.claude/skills/$s; done
```

## Repository layout

| Path | Contents |
|---|---|
| `skills/<name>/` | Each skill, self-contained: `SKILL.md` and `scripts/` (skill 1 also has `assets/`) |
| `shared/` | The sources more than one skill uses: the erwin parser (`erwin2postgres.py`, `erwinbin.py`) and the wxdi client (`wxdi_common.py`) |
| `tools/sync.py` | Copies `shared/` into the skills. Edit `shared/`, then run `python3 tools/sync.py` (`--check` fails if a copy is stale) |
| `legacy/` | The earlier single skill (`erwin-wxdi-import`) these replace, kept for reference |

## Tested

On eMovies (EMOVIES 15.2: 9 tables, 75 columns, 13 foreign keys, 2 views), AWS RDS Postgres , wxdi SaaS (ca-tor), November 2026:

- **Skill 1:** both SQL scripts, the viewer, and `metadata.json` (5 categories, 66 terms, 123 term assignments, 30 classification suggestions).
- **Skill 2:** deployed into SQL Server and created a read-only login (proved read-only); the metadata import gave 11 assets with every primary key, all 13 foreign keys and every description.
- **Skill 3:** 66 terms published, 11 assets in the catalog, 123 term assignments and 48 column classifications.
