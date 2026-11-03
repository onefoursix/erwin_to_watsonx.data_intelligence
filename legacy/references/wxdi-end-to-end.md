# erwin model → watsonx.data intelligence, end to end

This is the full sequence that loads an erwin model into IBM watsonx.data intelligence (wxdi, SaaS). It was worked out on a real account with the eMovies sample model:
- 9 tables and 2 views, 66 business terms in 4 subject-area categories
- 123 term assignments and 52 classified columns
- everything published to a catalog through a read-only connection

Commands use `wxdi_import.py`. Each step can be re-run safely, and every step supports `--dry-run`.

## Contents

0. Readiness check
1. Before you start
2. Generate the files
3. Create the schema and a read-only login
4. Connection and metadata import
5. Glossary
6. Term assignments
7. Descriptions
8. Classifications
9. Publish to the catalog
10. Lineage (optional)
11. Pitfalls found on a real account
12. Undo a load

## 0. Readiness check

Run `check` first; it reads everything and changes nothing:

```bash
$W check --name eMovies
```

On the account it was written against (a new trial account), it found that the Aiven database's private CA isn't trusted by default, that the account has no authorization for the catalog service to use its storage (so the catalog is created in the UI), and that publishing is "Automatic publishing". SKILL.md has the full list of checks and how to settle each warning.

`run-all --name eMovies --model MODEL.erwin` runs sections 2 to 9 below in order for one model, with lineage, and stops at the two points where the user publishes in wxdi (SKILL.md, "The full load"). The sections below are the individual steps it calls, for running or repairing one on its own.

## 1. Before you start

Settle these with the user:

- **wxdi account and service URL**, e.g. `https://api.ca-tor.dai.cloud.ibm.com`. They come from `ibm_cloud_api_key` and `di_service_url` in the project's `.env`. People often have several accounts; `status` and the list of projects show which one a key belongs to.
- **Project:** where the metadata import lands. **Catalog:** where business users will find the assets.
- **Postgres database and schema** for the model. Keep it out of `public` if `public` already holds other objects. erwin names like `customer` collide easily.
- **Root glossary category.** The user may have created it already. If so, use its exact name, because case matters.

Run everything from the project home; outputs go in `work/<schema>/`. `wxdi_import.py` reads `.env` there (Postgres and wxdi settings), so no `export` or key file is needed:

```bash
cd <project home>
W="venv/bin/python scripts/wxdi_import.py"     # in zsh, use a function: W() { venv/bin/python scripts/wxdi_import.py "$@"; }
```

## 2. Generate the files

```bash
python3 scripts/erwin2postgres.py MODEL.erwin --schema emovies --out work/emovies/emovies.sql \
        --glossary work/emovies/glossary --category emovies
```

Before going further, review these with the user:
- the view `REVIEW` notes in the SQL
- definitions in `glossary.csv`: template text, copy-paste errors, empty ones
- the `glossary:` notes about one name having two definitions
- `classification_suggestions.csv`, which becomes the classification mapping once it is agreed

## 3. Create the schema and a read-only login

The user runs the SQL script, or you run it if they ask:

```bash
$W run-sql --file work/emovies/emovies.sql        # the .env Postgres; or psql … -v ON_ERROR_STOP=1 -f
```

Then create the read-only login. Generate its password straight into `.env` so it never appears in chat or logs, and let `reader-sql` fill it in (the database defaults to `postgres_database`):

```bash
printf '\nemovies_reader_password=%s\n' "$(openssl rand -hex 16)" >> .env
$W reader-sql --schema emovies --password-env emovies_reader_password | $W run-sql --file -
```

Test the login before using it. It should be able to read the schema's tables and views, and be refused for other schemas, writes and `CREATE`:

```bash
echo "select count(*) from emovies.movie" | $W run-sql --file - --user emovies_reader --password-env emovies_reader_password
echo "create table emovies.x(i int)"      | $W run-sql --file - --user emovies_reader --password-env emovies_reader_password   # must fail
```

## 4. Connection and metadata import

Create a connection that uses the read-only login, then import the schema through it. `connection` creates it as a **platform connection** (in the Platform assets catalog) and adds it to the project as a reference to that, the way the UI does when you add a platform connection to a project. Lineage needs this: through a project-only connection the SQL Server lineage scan failed in `mssqlExtractorStep` ("Job failed during extraction") without ever connecting to the database, while the same import through a platform connection scanned fine. Re-running `connection` reuses a platform connection of the same name. Without `--copy-from` the connection points at the `.env` Postgres host, port and database (SSL on unless `postgres_sslmode` is `disable`; set `postgres_ssl_certificate_file` if wxdi must trust a private CA). With `--copy-from` it copies an existing connection's settings instead. Importing through the read-only connection matters: catalog assets are tied to the connection they were imported with, and publishing them requires publishing that connection too (see section 9).

```bash
$W connection --project MY_PROJECT --name emovies_reader --username emovies_reader --password-env emovies_reader_password
#   or copy an existing connection's host and database: --copy-from postgres [--from-project OTHER_PROJECT]
$W import --project MY_PROJECT --schema emovies --connection emovies_reader --name emovies_reader_mdi
```

`import` creates the metadata import with primary and foreign keys, runs it, and waits. The descriptions arrive from the Postgres comments.

## 5. Glossary

```bash
$W glossary --glossary-dir work/emovies/glossary --dry-run
$W glossary --glossary-dir work/emovies/glossary
```

How the step works:
- It imports categories first, waits for them to become searchable, then imports terms. It uses merge option `specified`, so an existing root category is not changed.
- It skips categories and terms that already exist.
- Imported terms are **drafts**. The user publishes them, through the import's workflow in the task inbox.
- After publishing, run `glossary` again. It then adds the secondary categories from `extra_secondary_categories.csv`, which the CSV import can't take. Each one creates a small draft to publish.

Check the result yourself. The import reports success even when it rejects rows, and lists at most 10 errors.

## 6. Term assignments

Terms must be published first.

```bash
$W assign-terms --project MY_PROJECT --connection emovies_reader --catalog emovies --schema emovies \
               --glossary-dir work/emovies/glossary [--remove-other-terms]
```

- **What it writes:** table terms go in each asset's `asset_terms`; column and view-column terms go in `column_info.<column>.column_terms`.
- **What it leaves alone:** existing terms are kept, so an earlier metadata enrichment's automatic terms from other glossaries stay. Add `--remove-other-terms` to drop them, but ask the user first.
- **Catalog assets:** published assets are copies, so assign in both places, or assign before publishing.

## 7. Descriptions

```bash
$W descriptions --project MY_PROJECT --connection emovies_reader --catalog emovies --schema emovies --sql work/emovies/emovies.sql
```

This makes table, view and column descriptions match the script's `COMMENT`s:
- **Sets or clears asset descriptions** to match, so a view with no erwin definition ends up blank rather than with invented text.
- **Fills column descriptions** that are missing or different.

Use it when the database was created from an older script, or when descriptions were edited by hand and should go back to the model's. Keep the Postgres comments in step too, or the next metadata import brings the old text back.

## 8. Classifications

erwin models don't usually contain classifications. Treat them as derived: agree the mapping with the user first.

```bash
$W classify --project MY_PROJECT --connection emovies_reader --catalog emovies --schema emovies \
           --glossary-dir work/emovies/glossary --classifications reviewed_classifications.csv
```

The mapping file needs `Name` and `Classifications` columns. A reviewed `classification_suggestions.csv`, with `Classifications` filled or emptied, is fine as the mapping file.

The step does three things:
- **Adds the classification to each term as a draft** of the existing term (no duplicates); the user publishes it.
- **Writes `column_classifications` on each column** that has one of those terms, in the project and the catalog. wxdi does **not** copy a term's classification onto its columns; without this the columns show none.
- **Tags those assets `classifications-derived-not-modelled`.** On `publish`, pass `--note` to explain the tag in the catalog description.

## 9. Publish to the catalog

```bash
$W publish --project MY_PROJECT --catalog emovies --schema emovies --connection emovies_reader \
          --expect-user emovies_reader --note "…what came from erwin and what was derived…"
```

How publishing works:
- **Connection first:** the catalog refuses the assets until the connection they were imported through is in the catalog (error ATTSV3055E).
- **Credentials:** the published connection is a **reference connection** (and the project connection is itself a reference to the platform connection, section 4; publishing through that chain is untested). It always uses the project connection's current login, and its credentials can't be changed in the catalog. So the only way to keep an admin login out of the catalog is to import through a read-only connection (section 4). `--expect-user` refuses anything else.
- **Matching:** assets are published by ID, so a same-named table in another schema can't be picked up by mistake.

Once published, the catalog copies have their own terms, descriptions and classifications. Later changes to the project assets need applying to the catalog too, which the steps above do when given `--catalog`.

## 10. Lineage (optional)

View-to-table lineage needs a metadata import with the "lineage and discovery" goal and a data source definition, which is best set up in the UI.
- **Lineage scope** is `/<database>/<schema>`.
- **How to tell it worked:** each view gets a `<view>_BODY` node feeding it. If the lineage step `postgresqlExtractedDataflowStep` shows `n/a` inputs, the scanner didn't read the view SQL. That has happened on one account with identical settings; it's a support case.
- **Avoid duplicates:** if an existing lineage import also lists the schema's tables in its discovery scope, remove them from that scope and keep the lineage scope. Otherwise its next run recreates duplicate assets through its own connection.

## 11. Pitfalls found on a real account

- **Lineage import body:** the data source definition goes in `datasource_definition_info.data_source_asset_id` (snake case). `dataSourceAssetId` is accepted with 200 and silently dropped, and the lineage scan then fails at `dictionaryInitializationStep`. `PATCH /v2/metadata_imports/{id}` takes a merge object.
- **Small Postgres servers:** lineage extraction opens many connections, and wxdi keeps an idle pool of them after an import. On a server with 20 connections (Aiven's small plans) a reader limited to 10 failed with "too many connections for role". `run-all` sizes the limit from `max_connections` and ends the reader's idle sessions before each run.
- **View lineage and the read-only login:** Postgres shows `information_schema.views.view_definition` only to the view's owner, so a read-only login sees NULL there (it can still read `pg_views`). With the import running as the reader, the scan completed but `postgresqlExtractedDataflowStep` had no inputs and the views had no edges. Not yet proven to be the cause: an admin login on another account gave the same result.

- **Content types:** `PATCH` on connections, assets and catalogs needs `Content-Type: application/json-patch+json`; attribute patches (`/attributes/column_info`) take `application/json`.
- **Re-importing terms:** a glossary re-import of a term that is still a draft creates a second draft with the same name. Don't use test imports on a real glossary.
- **Secondary categories:** a comma or `|` in the Secondary Categories cell is read as one unknown path.
- **Pending drafts:** a published term can have only one pending draft. Adding a category or classification to the published version while a draft exists fails with WKCBG2356E. The script checks for a draft (`versions?status=DRAFT`) and adds to it, so one draft per term collects all the changes. Each draft is still its own publish task, so expect one task per changed term.
- **Import status:** the import status's `messages` are a page (`count` is the real total). `operations_count` shows what was created per artifact type, and is the quickest check.
- **New catalogs:** creating one through the API needs a Cloud Object Storage bucket. Create catalogs in the UI.
- **Search IDs:** the search API returns categories, terms and classifications with `artifact_id` as `<repository>_<id>_<version>` and doesn't include the published `version_id`; read the versions list for that.
- **Data classes:** "No data class detected" on empty tables is normal, because data classes come from profiling actual values.
- **Escaped commas:** erwin escapes commas in definitions as `\,`. `erwin2postgres.py` unescapes them; databases created from older scripts keep the backslash, and `descriptions` fixes the wxdi side.

## 12. Undo a load

Use this after a test run, or to start over. Work from the narrowest scope (one category, one project, one schema), and check before each delete that the targets belong to this load and nothing else. In particular, confirm that no term or category IDs overlap with a glossary you keep.

1. **Pending drafts:** discard each with `DELETE /v3/glossary_terms/{id}/versions/{draft_version}`. Find a term's draft with `versions?status=DRAFT`. This takes effect at once.
2. **Published terms:** `DELETE /v3/glossary_terms/{id}/versions/{published_version}` doesn't delete anything yet. It creates a "marked for deletion" draft, one publish task per term, which someone has to approve.
3. **Categories:** `DELETE /v3/categories/{id}`, children before the parent. wxdi refuses while any term still uses the category as primary or secondary category.
4. **Project:** `DELETE /transactional/v2/projects/{id}` removes the project with its connections, metadata imports and assets.
5. **Catalog assets and connections:** delete the assets first, then the connection (`DELETE /v2/connections/{id}?catalog_id=…`). Check first that no remaining asset uses the connection.
6. **Postgres:** `DROP SCHEMA <schema> CASCADE; DROP OWNED BY <reader>; DROP ROLE <reader>;`

Claude can't approve the deletion tasks: the wxdi workflow tools leave approvals to a person.

