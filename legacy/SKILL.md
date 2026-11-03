---
name: erwin-wxdi-import
description: Convert an erwin Data Modeler model file (.erwin) into a PostgreSQL script that creates the model's schema - tables, keys, foreign keys, comments from the erwin definitions, and views - without erwin installed, export its governance metadata (business terms, subject-area categories, term-to-column assignments, valid values, data stewards, classification suggestions), and load all of it into IBM watsonx.data intelligence step by step - metadata import, glossary, term assignment, descriptions, classifications and publishing to a catalog through a read-only connection. Use this whenever someone has a .erwin file and wants Postgres DDL, wants to create or forward-engineer the schema from the erwin model, or wants an erwin model's tables, descriptions, business glossary, subject areas, valid values or classifications in watsonx.data intelligence (wxdi / DI) or another catalog. Also use it to inspect what tables, columns, keys, relationships and views an .erwin file contains.
---

# erwin model → PostgreSQL → watsonx.data intelligence

`scripts/erwin2postgres.py` reads the binary `.erwin` file directly (erwin r9 and later) and writes one SQL script. The user runs that script themselves to create the schema, and can then point a catalog's metadata import at the database. Your job is to run the converter, review what it produced, and hand the user a script they can trust plus clear next steps.

Needs Python 3.8+ and nothing else (standard library only). The wxdi step (section 6) also needs `curl` and an IBM Cloud API key; running SQL from here (`run-sql`) needs `psql` or `psycopg2`. Connection details and the key live in the project's `.env` (section 0).

The whole journey, for orientation: **generate** the SQL and governance files (sections 1, 5) → user **creates the schema** (3) → **load into wxdi** with `scripts/wxdi_import.py` (6). Users often want only part of it; do the part they ask for.

## 0. Project home and settings

The skill folder is also the project home: a git repo (`~/.claude/skills/erwin-wxdi-import` may be a symlink to it; `realpath` shows where). Work there:

- **Outputs** go in `work/<schema>/` under the project home (git ignores `work/`): the SQL script, `--glossary` and `--metadata` folders, reviewed classification files. Pass these paths explicitly; the converter's own default is next to the model file.
- **Settings** are in `.env` at the project root (git ignores it; `.env.example` lists the keys). `wxdi_import.py` reads it by itself, so you don't need `export`s or `--apikey-file`:

| Key | Used for |
|---|---|
| `postgres_host`, `postgres_port`, `postgres_database`, `postgres_user`, `postgres_password`, `postgres_sslmode` (default `require`) | `run-sql`, `reader-sql`, and `connection` without `--copy-from` |
| `postgres_cert` (a pasted PEM, may span lines) or `postgres_ssl_certificate_file` | CA certificate the wxdi connection should trust, for a private CA |
| `ibm_cloud_api_key`, `di_service_url` | every wxdi step |
| any other name, e.g. `emovies_reader_password` | passwords given by name with `--password-env` |

Environment variables of the same name (upper case) and command-line flags override `.env`. Never print `.env` values or put them in commands; refer to them by key. `.env` also says which wxdi account to use (the API key's account); the `check` step below shows it.

- **Python:** `erwin2postgres.py` needs only the standard library. `run-sql` needs `psql` or the `psycopg2` module; the project's `venv/` has it (`python3 -m venv venv && venv/bin/pip install psycopg2-binary` if it is missing), so run `wxdi_import.py` with `venv/bin/python`.

## Start here: readiness check (`check`)

Before generating or creating anything for a load into Postgres and wxdi, run the read-only check with the model's name (the name used for the project, catalog and root category; the schema defaults to it in snake case, as the converter names it):

```bash
venv/bin/python scripts/wxdi_import.py check --name eMovies [--schema emovies]
```

It changes nothing and takes about 20 seconds. Each line is `ok`, `WARN` or `FAIL`, and it exits non-zero on any failure. It checks:

| Area | What it looks at |
|---|---|
| tools | `curl`, and `psycopg2` or `psql` for `run-sql` |
| postgres | the `.env` login connects; the schema is absent or empty; the login may create the read-only login; whether the server's TLS certificate is publicly trusted |
| wxdi | the API key, its account and user; the account's catalog service and Cloud Object Storage, and their plans (trial plans expire) |
| project | projects of this account only (others the user can see are ignored); whether one with the name exists, and what it already holds; which storage a new one would use |
| catalog | whether the catalog exists; if not, whether the API can create it (the catalog service needs an authorization to the account's storage) or the user must create it in the UI |
| glossary | whether the root category exists; that the PII, Personal Information and Sensitive Personal Information classifications are there |
| workflow | whether publishing is automatic (one Publish action per import) or goes through approvers |
| lineage | the lineage service answers; PostgreSQL data source definitions are supported |

Show the user the result and settle every `WARN` and `FAIL` before going on. Typical ones:
- **TLS not trusted** (managed databases such as Aiven use a private CA): wxdi's connection will fail until the CA certificate is given. Ask the user to download it from the provider's console (Aiven: the service's overview page, "CA certificate") and paste it into `.env` as `postgres_cert=` (BEGIN to END lines), or set `postgres_ssl_certificate_file`. `check` then verifies the server against it. Don't take it from the TLS handshake instead; that trusts whatever answered.
- **Catalog can't be created by API:** this is the manual step below.
- **Name already used** (project, category, or a schema with objects): ask whether to add to it, use another name, or clean it up first.
- **Trial plans:** mention that they expire, so the load is for a demo or test.

### Manual step: the user creates the catalog

When `check` reports that the catalog doesn't exist and can't be created by API (the account has no authorization for the catalog service to use its Cloud Object Storage, as on new trial accounts), the user creates it once in the UI. Ask them to, with these settings, and wait until they say it's done:

1. In watsonx.data intelligence (the same account and region as the API key: `check` prints both), open **Catalogs → View all catalogs → New catalog**.
2. **Name:** the model's name exactly as passed to `--name` (e.g. `eMovies`). The steps find the catalog by name, ignoring case.
3. **Enforce data policies:** the user's choice. With it on, data protection rules apply to the published assets; it can't be switched later.
4. **Duplicate asset handling:** leave the default (update original assets).
5. Leave **Allow reporting on asset metadata** as the user prefers, and select **Create**. The UI sets up the storage and its authorization.

Then re-run `check --name <model>`. The catalog line should now read `catalog <name> exists and will be used`. If it still says it doesn't exist, the catalog is probably in another account or region, or has a different name: compare with what `check` prints. Creating the catalog doesn't depend on any other step, so it can be done while the rest runs, as long as it exists before the publish step.

The API key may belong to a user who is a member of several IBM Cloud accounts. The projects API lists all their projects, so every step only uses projects in the key's own account; a same-named project in another account is never picked up.

## The full load: `run-all`

When the user wants the whole thing (the model in Postgres and in wxdi, with glossary, lineage, terms, classifications and a catalog), follow this order. `run-all` does it in one command that stops whenever the user has to act and picks up where it left off when run again:

1. **Ask for the model's location** if the user hasn't given it (sample models are usually in `~/Documents/My Models/`), and the name to use (default: the file name, e.g. `eMovies`).
2. **`check --name <name>`**, and settle its warnings (above), including the manual catalog step.
3. **`run-all --name <name> --model <file>`**. The first time, it extracts the model into `work/<schema>/` and prints the plan: database, schema and login, wxdi account, project, catalog, categories, number of terms, and how many classifications it will apply. It then stops (exit code 2). Show the plan to the user; when they confirm, run it again with `--confirm`.
4. It then runs on its own:
   - creates the schema and its tables and views from the SQL script (it refuses a schema that already holds objects)
   - creates the read-only login `<schema>_reader` (its password is generated into `.env` as `<schema>_reader_password`) and tests that it can read and cannot create
   - creates the project (on the account's existing Cloud Object Storage), the read-only connection (a platform connection with the CA certificate from `.env`, added to the project as a reference: lineage scans fail through a project-only connection), and a data source definition for the database in the Platform assets catalog
   - runs a metadata import with lineage and discovery (primary and foreign keys, lineage scope `/<database>/<schema>`), then reports whether each view has lineage from its tables
5. **Pause 1 (exit code 3):** the glossary import leaves the categories and terms as drafts. Tell the user how many terms there are and ask them to publish the import's task in wxdi. **Never publish or approve for them**, with the MCP workflow tools or the API. When they say it's done, run `run-all` again; it checks that every term is published before going on.
6. It adds the extra secondary categories and the **high-confidence** classifications to the terms; both arrive as drafts of the published terms.
7. **Pause 2 (exit code 3):** ask the user to publish those term changes (one task per changed term). Run again when they're done; it waits until no term has a pending draft. It also lists the suggestions it did not apply (`medium` and `review` ones) for the user to decide.
8. On the project assets it assigns the terms (tables, columns, view columns), adds the column classifications and the `classifications-derived-not-modelled` tag, and makes descriptions match the SQL comments. Doing this before publishing means the catalog copies arrive complete.
9. Publishes the connection and the assets to the catalog (it refuses unless the connection logs in as the read-only user), then prints the final `status`.

Progress is kept in `work/<schema>/state.json`. Steps that are not safe to repeat (running the SQL script, the glossary import, the import run, the term changes) are recorded there and skipped on later runs; everything else re-checks what exists. If the user wants a step redone, remove its key from `state.json`. `--dry-run` (before `run-all`) extracts the model, prints the plan and the readiness check, and stops before the first change.

Classification confidence comes from `erwin2postgres.py`: `high` means the term's name itself matches a pattern, with no caveat; `medium` means only its erwin domain matches; `review` means a person has to decide (data about an organisation, public credits such as actors, or a code rather than the value). Only `high` is applied automatically.

To undo a load, `teardown --name <name> --schema <schema> [--datasource] [--glossary-dir DIR]` lists what it would delete; with `--yes` it deletes the catalog's copies of the assets and the reader connection, the project, the reader's platform connection, and (with `--datasource`) the data source definition, and with `--drop-schema` too the Postgres schema and the login. It keeps the catalog and never deletes glossary terms: deleting a published term creates an approval task for a person (see `references/wxdi-end-to-end.md` section 12). Confirm with the user before running it with `--yes`.

## 1. Generate the script

```bash
python3 <skill-dir>/scripts/erwin2postgres.py MODEL.erwin [--schema NAME] [--out FILE.sql] [--metadata DIR]
```

- `--schema`: the Postgres schema to create. Defaults to the model file name in snake_case (`eMovies.erwin` → `emovies`). Ask the user if they have a schema name in mind; otherwise use the default and tell them.
- `--out`: defaults to `<schema>.postgres.sql` next to the model file. In the project home, use `work/<schema>/<schema>.postgres.sql` unless the user wants it elsewhere.
- `--metadata DIR`: also writes `model.json` and `tables.csv` / `columns.csv` / `relationships.csv`. Useful for answering questions about the model or reviewing definitions, so use it when the user wants to see what is in the model.

The script prints a one-line summary (tables, columns, foreign keys, views) and any review notes.

## 2. Review the output before handing it over

The tables are a direct translation and need little checking. Views need a look, because erwin stores which tables and columns a view uses but not how the tables join:

- **Joins are inferred** from the model's migrated keys, i.e. the foreign-key columns that link the view's source tables (including indirect links, such as a key that migrated through an intermediate table). This matches what erwin's own forward engineering would need a modeler to write, and is usually right.
- **`-- REVIEW:` comments** mark guesses: a source table with no key linking it to the others (it is CROSS JOINed, which is almost never intended), or an expression column name that exists in more than one source table. Show these to the user and suggest the fix (for example a join on a shared business column); edit the script only if they agree.
- **`-- erwin also stores this user-defined SQL`** means the modeler typed SQL for the view in erwin. It is kept as a comment because it uses erwin's own names, which can differ from the generated table names. Compare it with the generated view and point out meaningful differences (a WHERE clause, a different join) to the user.
- **Views reported as `skipped`** have no source tables in the model (some metamodel views hold only placeholders). There is nothing to generate; just mention them.
- Derived columns (erwin view expressions such as `rental_rate * 1.5`) are qualified with the right table alias and get a `Derived: ...` column comment.

Also skim the definitions in the column comments if the user plans to catalogue the result: erwin samples sometimes contain copy-paste errors or template text, and whatever is in the comments becomes the catalog description.

## 3. Tell the user how to run it

The script runs as one transaction (`BEGIN` … `COMMIT`), so if anything fails nothing is created:

```bash
psql "host=HOST port=5432 dbname=DB user=USER" -v ON_ERROR_STOP=1 -f emovies.postgres.sql
```

or, against the database in `.env` (prints the user and database it ran as):

```bash
venv/bin/python scripts/wxdi_import.py run-sql --file work/emovies/emovies.postgres.sql
```

`run-sql` also takes `--file -` for SQL on stdin, which is handy for checks (`select count(*) …`) and for piping `reader-sql` into it (section 6). It runs the script as written, so the script's own `BEGIN`/`COMMIT` still make it all-or-nothing.

Point out that:
- The script starts with `CREATE SCHEMA IF NOT EXISTS`, so an empty schema the user created beforehand is fine. Tables are created with plain `CREATE TABLE` (no `IF NOT EXISTS`), so running it against a schema that already holds these tables fails cleanly. To recreate from scratch they would drop the schema first, which deletes its data, so that is their call.
- The objects go into the chosen schema, so pick one that does not collide with existing tables (for example don't use `public` if it already has a `customer` table).
- Don't run the script against the user's database yourself unless they ask you to. This skill exists so they can create the schema on their own terms. When they do ask, `run-sql` uses the `.env` connection; say which host and database it will hit before you run it.

## 4. Next step: catalog metadata import

For IBM watsonx.data intelligence, section 6 automates the import. If the user prefers to click through the UI, or wants lineage (which needs a data source definition set up in the UI), read `references/wxdi-metadata-import.md` and give them the checklist that fits their goal.

## 5. Governance metadata: business terms, categories, reference data (`--glossary`)

Use this when the user wants more than descriptions in the catalog, e.g. "import the business terms", "build a glossary from the erwin model", "bring the subject areas / valid values / data stewards across".

```bash
python3 <skill-dir>/scripts/erwin2postgres.py MODEL.erwin --schema NAME --glossary OUT_DIR [--category ROOT]
```

It writes, alongside the SQL script:

| File | Contents |
|---|---|
| `glossary.csv` | One category per erwin subject area under a root category (`--category`, default: the model file name), plus one business term per logical entity and per logical attribute, with the erwin definition as the description. In the IBM watsonx.data intelligence glossary CSV import format: `Name, Artifact Type, Category, Description, Abbreviations, Secondary Categories`; category paths use `Root >> Child`. |
| `term_assignments.csv` | `schema, table, column, asset_type, term`: which term goes on each table, column, view column. Foreign-key columns get the term of the attribute they migrated from (e.g. `employee.supervisor` → *Employee Number*), and view columns get their source column's term. |
| `reference_data/<list>.csv` + `reference_data_usage.csv` | erwin valid-value lists as `code, value`, and which columns use them. |
| `custom_properties.csv` | User-defined property values set in the model (e.g. Data Steward, Create Date). |
| `classification_suggestions.csv` | Keyword-based suggestions (Sensitive Personal Information / Personally Identifiable Information / Personal Information) for attribute terms, with confidence (`high`: the name matches; `medium`: only the erwin domain matches; `review`: needs a decision), erwin domains, reason, definition and columns. erwin models normally hold no classifications, so these are derived. Rows marked `review`, and terms about organisations (store address) or public figures (actors, directors), need a human decision. Review them with the user before using the file as the classification mapping. |
| `extra_secondary_categories.csv` | Only when a term belongs to three or more subject areas: the secondary categories beyond the first one. The wxdi CSV import takes one path per Secondary Categories cell (a comma-separated list is rejected as one unknown path), so these are added after the import. |

How the terms are built, so you can explain the result:
- Logical names become title case when erwin has them all upper or all lower case ("MOVIE RENTAL RECORD" → "Movie Rental Record").
- Attributes with the same logical name in different entities become one term. If their definitions differ, the script keeps the first and prints a `glossary:` note; show those notes to the user.
- An entity in several subject areas is filed under the one whose name it contains, otherwise the first; the others become secondary categories.
- Naming-standard abbreviations from the model's NSM file fill the Abbreviations column where a word matches a term name exactly.

Before importing, look through `glossary.csv` with the user for bad definitions (template text, copy-paste errors) and terms with no description. They are easier to fix in the CSV or the model than after import.

If the wxdi MCP server is available, you can check the file with its glossary CSV import tool using `validate_only=true`. Only run the real import when the user asks, because it creates governance artifacts that other people see. Lessons from real imports:
- Validation is a local format check. The real import can still reject rows (e.g. unknown category paths) while reporting `import_status: SUCCEEDED`, and its error list stops at 10 rows. After importing, check the terms rather than trusting the error list.
- If the root category already exists in wxdi, pass `--category` with its exact name (case matters), remove the root category's own row from `glossary.csv`, and import with merge option `specified` so the existing category is left unchanged.
- Don't use test imports to try out formats on a real glossary: every import run creates a new draft, so a test row duplicates a term that already exists. The import steps (publish drafts, assign terms, reference data) are in `references/wxdi-metadata-import.md`.

## 6. Load everything into watsonx.data intelligence (`wxdi_import.py`)

Use this when the user wants the model in wxdi, not just files. Read `references/wxdi-end-to-end.md` first: it has the full command sequence, what each step checks, and the pitfalls found on a real account. In short:

```bash
cd <project home>
venv/bin/python scripts/wxdi_import.py [--dry-run] <step> [options]     # URL and API key come from .env
```

`--dry-run` goes before the step name. Without a `.env`, pass `--url` and `--apikey-file`, or set `DI_SERVICE_URL` and `DI_APIKEY`.

| Step | What it does |
|---|---|
| `status` | Shows the project's and catalog's assets for the schema, catalog connections and their logins, the glossary categories and published term count |
| `run-sql` | Runs a SQL file (or `-` for stdin) against the `.env` Postgres; `--user` with `--password-env` to run as another login, e.g. to test the read-only one |
| `reader-sql` | Prints SQL for a Postgres login that can only read the schema (for the catalog connection); `--password-env NAME` fills in the password from `.env` instead of `CHANGE_ME` |
| `connection` | Creates a platform connection (Platform assets catalog) to the `.env` Postgres host and database (or, with `--copy-from`, copying an existing connection's), logging in as another user, and adds it to the project as a reference |
| `project` | Creates the project in the key's account, on the storage its projects already use |
| `datasource` | Creates a data source definition for the `.env` Postgres (lineage needs one) |
| `import` | Creates and runs a metadata import of the schema with primary and foreign keys, and waits; `--lineage` makes it lineage and discovery through the data source definition |
| `lineage-check` | Reports, for each view in the SQL script, the lineage edges and source tables wxdi shows |
| `publish-status` | How many of the model's terms are published, waiting with changes, or not published yet |
| `glossary` | Imports categories, then terms, skipping ones that already exist; after the terms are published, adds the extra secondary categories |
| `assign-terms` | Puts every term from `term_assignments.csv` on the project and/or catalog assets; `--remove-other-terms` drops automatic terms from other glossaries |
| `descriptions` | Makes asset and column descriptions match the SQL script's comments |
| `classify` | Adds the reviewed classifications to the terms (as drafts to publish) and to the columns, and tags those assets `classifications-derived-not-modelled`; `--min-confidence high` takes only those rows |
| `publish` | Publishes the connection and the assets to a catalog; `--expect-user` refuses a connection with any other login |
| `run-all` | The full load in order (see "The full load"); stops for the user and resumes |
| `teardown` | Lists, or with `--yes` deletes, what a load created (not glossary terms) |

Every step reads the current state and only adds what is missing, so re-running is safe and a completed setup reports nothing to do. Always run a step with `--dry-run` first and show the user what it will change.

Things that need the user's decision, so ask rather than assume:
- **Account, project, catalog and root category.** Several wxdi accounts can exist side by side; confirm which one.
- **The catalog connection's login.** Publishing a connection shares its credentials with catalog users, and the catalog copy always follows the project connection. Recommend a separate read-only login and connection (`reader-sql`, `connection`), and import the schema through that connection, so the catalog never sees an admin login. Keep its password out of chat: generate one straight into `.env` (e.g. `printf '\nemovies_reader_password=%s\n' "$(openssl rand -hex 16)" >> .env`), then `reader-sql --schema emovies --password-env emovies_reader_password | … run-sql --file -` and `connection … --password-env emovies_reader_password`.
- **Classifications.** They are judgement, not model content. Use `classification_suggestions.csv` as a draft, agree the mapping with the user, then apply.
- **Publishing.** Terms, secondary categories and term classifications arrive as drafts. The user (or their workflow) publishes them; assignments only work with published terms.

Limits to state up front: SaaS only, since it authenticates with an IBM Cloud API key; `datasource` and `import --lineage` follow the API the wxdi MCP server uses but had not yet run against an account when written, so check the first result (the UI is the fallback for both); reference data and stewards are files to import by hand. `publish` has been run by hand with the same API calls, but not yet by the script against a catalog, so check its result in the catalog the first time.

**Approvals are the user's.** The wxdi workflow tools don't let Claude approve or publish governance tasks, and they shouldn't. When a step leaves drafts, tell the user how many tasks to expect and wait for them to say they're done.

**Undoing a load or a test** has a fixed order: discard drafts, mark published terms for deletion (the user approves), delete categories children-first, then the project and the Postgres schema and login. The details are in `references/wxdi-end-to-end.md` (section 12). Confirm the targets belong only to that load before deleting anything.

## What gets generated, and the limits

| erwin | PostgreSQL |
|---|---|
| Entity / physical table name | `CREATE TABLE`, name in snake_case (truncated to 63 chars, de-duplicated) |
| Attribute / column, datatype | Column with mapped type (DATETIME→timestamp, NUMBER/DECIMAL→numeric, MONEY→numeric(19,4), BLOB/IMAGE→bytea, ...). Unknown or missing types become varchar(255) |
| Primary key | `PRIMARY KEY`; PK columns are `NOT NULL` |
| Alternate key (AKn) | `UNIQUE` constraint |
| Relationship | `FOREIGN KEY`, only when it references the parent's full primary or alternate key (erwin allows partial references that Postgres would reject) |
| Entity / attribute definition | `COMMENT ON TABLE` / `COMMENT ON COLUMN` (attributes inherit from their domain or migrated parent when they have none of their own) |
| View | `CREATE VIEW` from column mappings, inferred joins and expressions; view columns inherit their source column's definition as a comment |

Not in the SQL: subject areas, valid values, user-defined properties and logical names. These belong in the catalog rather than in DDL, so `--glossary` exports them as files (section 5). Also not exported anywhere: domains as Postgres domains, indexes other than keys, triggers and stored procedures.

The `.erwin` reader was reverse-engineered from erwin 15.x sample models. It handles r9+ binary files; older `.er1` files or XML exports need to be re-saved as `.erwin` from erwin first. If a file yields no entities, the script says so.
