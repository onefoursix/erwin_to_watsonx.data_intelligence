---
name: wxdi-metadata-import
description: Import a database schema's tables and views into an IBM watsonx.data intelligence (wxdi / DI) project with a metadata import, either without lineage (assets, primary and foreign keys, descriptions) or with lineage (also a data source definition and a lineage scan), from PostgreSQL or SQL Server. It can first create the schema from a SQL script (such as the one erwin-model-export writes) and a read-only login, and it sets up the project and a platform connection through that login. Use this whenever someone wants to run a metadata import, onboard or catalog a schema or erwin model's tables into wxdi, import with or without lineage, or create the database objects for an erwin model before cataloguing them. Step 2 of the erwin skills (after erwin-model-export, before erwin-wxdi-governance).
---

# Metadata import into watsonx.data intelligence (with or without lineage)

Step 2 of three skills for an erwin model:
1. **erwin-model-export** writes `work/<schema>/`: the SQL for each dialect, the viewer, `<schema>.metadata.json`.
2. **wxdi-metadata-import** (this one): the tables and views in a database, then in a wxdi project.
3. **erwin-wxdi-governance**: glossary, publishing to the catalog, term assignments, classifications.

It works without the other two as well: for any schema, skip `deploy`.

## Settings

Everything runs from the project home (the folder with `.env` and `work/`; the scripts find it by walking up from the skill, or set `ERWIN_WXDI_HOME`). `.env` is never printed or put in commands; refer to keys by name.

| `.env` key | Used for |
|---|---|
| `ibm_cloud_api_key`, `di_service_url` | wxdi (SaaS) |
| `sqlserver_host`, `sqlserver_port`, `sqlserver_db`, `sqlserver_user`, `sqlserver_password` | `--db sqlserver`: the admin login (creates the schema and the reader) |
| `postgres_host`, `postgres_port`, `postgres_database`, `postgres_user`, `postgres_password`, `postgres_sslmode`, `postgres_cert` | `--db postgres` (`postgres_cert`: the provider's CA certificate when it isn't publicly trusted, e.g. Aiven) |
| `<schema>_reader_password` | the read-only login's password; `reader` generates it into `.env` |

Run the script with the project's venv, which has the database drivers (`python3 -m venv venv && venv/bin/pip install psycopg2-binary pymssql` if it's missing):

```bash
venv/bin/python <skill-dir>/scripts/mdi.py [--dry-run] <step> --db sqlserver|postgres --schema <schema> [--project <name>] ...
```

On this Mac the sandbox can't reach the database host; run database steps with the sandbox disabled.

## Ask first

1. **Which database** (`sqlserver` or `postgres`, from `.env`), **schema**, and **project** (default: the model or schema name). Several wxdi accounts can exist side by side; `check` prints the account the key belongs to, so confirm it's the right one.
2. **With or without lineage.** Without is quick (a few minutes) and gives assets, keys and descriptions. With lineage adds a data source definition and a scan that can take much longer. On SQL Server, the lineage scan completes, but view lineage hasn't produced edges in testing (see Lineage below), so say so before the user picks it for views.
3. **Whether to create the schema** (`deploy`), if `check` says it doesn't exist or is empty. That runs DDL against their database: name the host and database and get a yes first.

## Steps

`run-all` does them in order, and every step is safe to repeat: it reads what exists and only adds what's missing.

```bash
venv/bin/python <skill-dir>/scripts/mdi.py check   --db sqlserver --schema emovies --project emovies
venv/bin/python <skill-dir>/scripts/mdi.py run-all --db sqlserver --schema emovies --project emovies [--deploy] [--lineage]
```

| Step | What it does |
|---|---|
| `check` | Read-only. The admin login connects; whether the schema exists and how many tables and views it holds; whether the reader login exists; Postgres TLS trust; the wxdi key and account; the project and its connections; the data source definition. Settle every `FAIL` before going on. |
| `deploy --sql FILE` | Runs the SQL script as one transaction (all or nothing). SQL Server: creates the database first if it's missing. Skipped when the schema already holds objects. Default file in `run-all`: `work/<schema>/<schema>.<db>.sql`. |
| `reader` | Creates `<schema>_reader`: read-only on the schema, including view definitions (Postgres: `SELECT` + default privileges; SQL Server: `SELECT` and `VIEW DEFINITION` on the schema). Generates its password into `.env` as `<schema>_reader_password`, then proves it can read the schema and can't create a table. |
| `project` | Creates the project on the Cloud Object Storage the account's projects already use. |
| `connection --connection NAME` | Creates a platform connection (in the Platform assets catalog) through the reader login, tested, and adds it to the project as a reference, as the UI does. The import goes through it, so the catalog never sees an admin login when the assets are published later. |
| `datasource` | Only for lineage: a data source definition for the database's host, port and database. |
| `import --connection NAME [--lineage]` | Creates and runs the metadata import (scope `/<schema>`, primary and foreign keys; re-imports update names and descriptions and never delete) and waits for it. With `--lineage`: lineage and discovery through the data source definition, lineage scope `/<database>/<schema>`. Default names: `<schema>_mdi`, `<schema>_mdil`. |
| `lineage-check --metadata FILE` | For each view in `metadata.json`: the lineage edges and the tables they come from. |
| `status` | The project's assets, connections and metadata imports. |

Afterwards, report: how many assets the project has (tables + views), and that keys and descriptions came across. Foreign keys show on the table they reference (in its `foreign_keys`), not on the referencing table.

## Lineage

What we learned on real SaaS accounts (Nov 2026), and the script builds in:

- **Scanner names** (`/gov_lineage/v2/enums` lists them): `PostgreSQL`, and `Microsoft SQL Server`, not `MSSQL`, which is only its connection type. A wrong name fails the lineage part at once ("Unknown scanner"), but the job stays `Running` forever.
- **Input reader:** `parallel` made Postgres view lineage work. With it, the SQL Server extractor fails before it connects ("Job failed during extraction"); `scenarioReaderPrefetch` (the UI default) works.
- **SQL Server view lineage:** with every setting right, the scan completes and reads the view definitions and dependencies, but no view-to-table edges appear. This is still unexplained (an IBM support case is the next step). Tables and views still arrive as assets.
- **One lineage import per data source definition and scanner.** Creating a second one fails with "…already exists": change or delete the first.
- **Scanner identity:** the scanner records the server under the address and port SQL Server reports about itself. In a Docker container that's the container's internal IP and port, and ports above 32767 come back negative (35428 → -30108). Prefer host networking and a port below 32768. Changing this didn't make view lineage appear, though.
- **Order of changes:** a connection links to the data source definition whose endpoint matches its host, port and database. Change the definition's endpoint first, then the connection; otherwise the link drops, and runs fail with "The data source definition asset related to connection … was not found".
- **A stuck run** (`Running` with a failed lineage part, or `Canceling` for good): `PATCH /v2/jobs/{job}/runs/{run}?project_id=…` with `[{"op":"replace","path":"/entity/job_run/state","value":"Failed"}]`, `Content-Type: application/json`. The script prints this when it sees it.
- **The scan's real error** isn't in the API (no findings, the log isn't downloadable). Ask the user to download the run log from the job run page in the UI.

## Undo

Deleting the metadata import (`DELETE /v2/metadata_imports/{id}?project_id=…`) keeps its assets; delete those separately. Deleting the project removes its connections (references), imports and assets, but not the platform connection or the data source definition. Delete the definition last: connections and imports that still point at it break. Confirm with the user before deleting anything.

## Files

- `scripts/mdi.py`: the steps above.
- `scripts/wxdi_common.py`: settings, the REST client and lookups, shared with erwin-wxdi-governance (a copy of the repository's `shared/wxdi_common.py`; edit it there and run `tools/sync.py`).
