---
name: erwin-wxdi-governance
description: Load an erwin model's governance metadata into IBM watsonx.data intelligence (wxdi / DI) - a top-level glossary category named after the model, nested categories for its subject areas, the business terms in them, publishing the terms (completing the Publish task), publishing the model's tables, columns and views to a catalog, assigning the business terms to those tables, columns and views, and assigning classifications such as PII to the columns - all from the metadata JSON that erwin-model-export writes, onto the assets wxdi-metadata-import imported. Use this whenever someone wants an erwin model's glossary, business terms, subject areas, term assignments or classifications in wxdi, wants the model's assets published to a catalog, or says "do the catalog import" / "the governance part" for an erwin model. Step 3 of the erwin skills.
---

# erwin governance into watsonx.data intelligence

Step 3 of three skills for an erwin model:
1. **erwin-model-export** wrote `work/<schema>/<schema>.metadata.json` (and the viewer and SQL).
2. **wxdi-metadata-import** put the tables and views into a wxdi project through a read-only connection.
3. **erwin-wxdi-governance** (this one): glossary, publishing, catalog, term assignments, classifications.

If the metadata file or the project's assets are missing, run the earlier skill first. `check` says which.

## Settings and running

Runs from the project home (the folder with `.env` and `work/`; found by walking up from the skill, or `ERWIN_WXDI_HOME`). It needs `ibm_cloud_api_key` and `di_service_url` in `.env`, plus Python 3 and curl; no database access. Never print `.env` values.

```bash
python3 <skill-dir>/scripts/governance.py [--dry-run] <step> --schema <schema> [--project <project>] [--catalog <catalog>]
```

`--schema` finds `work/<schema>/<schema>.metadata.json` (or pass `--metadata FILE`). The catalog defaults to the model's name, matched without regard to case (`eMovies` finds `emovies`).

## Ask first

1. **Model and project:** which model (its metadata file), and the project holding its imported tables and views. Confirm the wxdi account `check` prints.
2. **Catalog:** the catalog to publish to, by default the model's name. If it doesn't exist, the user creates it in the UI (Catalogs → New catalog; the API can't create one without storage authorisation on new accounts). Wait until they say it's done.
3. **Publishing the terms.** Importing creates the terms as drafts, and with automatic publishing wxdi leaves a **Publish** task in the user's task inbox. This skill can complete that task, so the terms get published, but it's the user's governance decision. Before `approve --yes` (or `run-all --approve`), tell them what will be published (the categories and the number of terms; `approve` without `--yes` lists each task) and get a clear yes in this conversation. The approval covers this model and this run only.
4. **Classifications** come from the model's suggestions, not from erwin: by default only `high` confidence (the term's name itself matches a pattern). Say how many columns that covers and list what was left out (`review` and `medium` rows) for the user to decide.

## Steps

`run-all` does them in this order. Each step is safe to re-run: it reads what exists and adds only what's missing. `--dry-run` shows what it would do.

| Step | What it does |
|---|---|
| `check` | Read-only: the metadata file (tables, views, categories, terms, assignments, classifications); the project's assets; whether the catalog exists; how much of the glossary is published; whether the classification names exist in wxdi; whether publishing is automatic or goes through approvers. |
| `glossary` | One stage per run: **(1)** the top-level category (the model's name) and one nested category per erwin subject area. These are created directly, with no draft or task. **(2)** the business terms, each filed under its subject area, with the erwin definition, abbreviations and one secondary category. They arrive as drafts with one Publish task. **(3)** once they're published, the extra secondary categories (terms in three or more subject areas), each a draft of the published term with its own Publish task. It refuses to import while earlier drafts still wait, so a re-run never duplicates terms. |
| `approve [--yes]` | Lists, or with `--yes` completes (claim, then Publish), the open Publish tasks that are assigned to the API key's own user **and** whose every draft is one of this model's categories or terms under its top-level category. A task for anyone else, any other glossary, or any mix is never touched. |
| `publish --connection NAME --expect-user LOGIN` | Publishes the import's connection, then the schema's tables and views, to the catalog. `--expect-user` refuses unless the connection logs in as the read-only user, because a published connection shares its credentials with catalog users. |
| `assign-terms [--project P]` | Puts the model's terms on the catalog's tables, columns and view columns (and, with `--project`, on the project's), as the model maps them: table → entity term, column → attribute term, foreign-key column → the term of the attribute it migrated from, view column → its source column's term. |
| `classify [--project P] [--min-confidence high] [--terms]` | Puts the suggested classification on each column whose term has one (wxdi's `PII` for the model's "Personally Identifiable Information"), and tags those tables `classifications-derived-not-modelled`. `--terms` also classifies the terms themselves; each becomes a draft with its own Publish task, so approve again afterwards. |
| `status` | Glossary counts, pending tasks, and per container the assets, term assignments and column classifications. |

```bash
python3 <skill-dir>/scripts/governance.py check   --schema emovies --project emovies
python3 <skill-dir>/scripts/governance.py run-all --schema emovies --project emovies --connection emovies_reader [--approve]
```

Without `--approve`, `run-all` stops with exit code 3 at the first Publish task and lists it. Show the user, and when they agree, run it again with `--approve`. It goes through categories → terms → approve → secondary categories → approve → publish → assign-terms → classify → status.

For eMovies on the wxdi255 account this gave: 5 categories, 66 terms published (one task, then 15 for the secondary categories), 11 tables and views in the catalog, 123 term assignments and 48 column classifications in both the catalog and the project.

## Things to know

- **Approval workflows:** `check` reports how publishing is set up. With automatic publishing, the only task is Publish. If the account routes glossary changes through approvers, those tasks belong to the approvers: tell the user who has to act, and don't complete them.
- **Published terms are needed** for term assignments; `assign-terms` refuses until every term it uses is published.
- **Workflow artifacts:** a workflow lists only its first 10 drafts; the script reads the full list (`/v3/workflows/{id}/artifacts`) before deciding a task is the model's.
- **Catalog and project copies are separate:** later changes to project assets need applying to the catalog too, which is why `assign-terms` and `classify` do both when given `--project`.
- **Undo** has a fixed order: discard waiting drafts; mark published terms for deletion (each creates a task for a person, which this skill doesn't complete); delete categories children first; delete the catalog's assets, then its connection. Confirm with the user before deleting anything.

## Files

- `scripts/governance.py`: the steps above.
- `scripts/wxdi_common.py`: settings, REST client and lookups (a copy of the repository's `shared/wxdi_common.py`; edit it there and run `tools/sync.py`).
