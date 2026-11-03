# Importing the generated schema into watsonx.data intelligence (manual)

Use this once the user has run the SQL script and the schema exists in Postgres. Everything here is done in the watsonx.data intelligence (wxdi / DI) UI. Give the user only the steps that fit their goal.

## Metadata only (tables, columns, keys, descriptions)

1. In a project, add a **PostgreSQL connection** to the database (host, port, database, a user that can read the schema).
2. Create a **metadata import**:
   - Goal: **Discover** (metadata only).
   - Scope: the new schema, for example `/emovies`. Selecting individual tables and views also works.
   - Turn on **Include primary keys** and **Include foreign keys**; they are off by default.
3. Run it. Expect one asset per table and view. The erwin definitions arrive as the table and column descriptions, primary keys are flagged on the columns, and foreign keys appear as relationships between the table assets.

Notes:
- Newly created schemas sometimes don't show up in the import's schema browser right away. Typing the path into the scope, or selecting the tables, still works.
- Foreign keys are not lineage. They show as asset relationships, not as data flows.

## Metadata plus lineage (views → source tables)

Lineage needs the import goal **Get lineage** together with discovery ("lineage and discovery"), plus a **data source definition** for the Postgres server. Set this up in the UI; changing an existing import's type through the API is not reliable.

- Lineage scope is `/<database>/<schema>`, for example `/postgres/emovies`.
- Only views produce lineage, because their SQL is parsed into column-level flows. Plain tables appear as lineage nodes with no edges.
- **How to tell it worked:** each view should get a companion node named `<view>_BODY` (type CreateViewScript) feeding the view, with edges back to the source tables. In the job run details, the lineage step `postgresqlExtractedDataflowStep` should show one input per view. If it shows `n/a` and the views have no `_BODY` node, the scanner did not read the view definitions. That has been seen on some instances even with identical settings, and is a case for IBM support (send them the job log).

## Governance metadata from `--glossary`

Do these after the metadata import, in this order. Terms must exist before they can be assigned.

### 1. Business terms and categories (`glossary.csv`)

- **UI:** Governance → Business terms → **Import** (or Categories → Import), then upload `glossary.csv`. Pick the merge option. *Replace all values* is right for a first import; *Replace only empty values* is safer when re-importing over terms people have edited.
- **wxdi MCP server:** use the glossary CSV import tool with the file contents, first with `validate_only=true`, then for real.
- **Category already exists?** Generate with `--category <its exact name>`, delete the root category's row from `glossary.csv`, and choose *Replace only specified values* (MCP: `merge_option=specified`).
- **`extra_secondary_categories.csv`:** add these secondary categories after the import, either on each term's page or with the glossary API (`POST /v3/glossary_terms/{id}/versions/{version}/relationships` with `{"categories":[{"id":"<category id>"}]}`).
- **Check the result:** the import reports success even when some rows were rejected, and lists at most 10 errors. Look at a few terms, especially those with several categories.
- Imported terms normally arrive as **drafts**. Someone with the right role must publish them, through the governance workflow if one is configured. Assignments in the next step only work with published terms.
- To add stewards, fill the `Stewards` column with wxdi user IDs or emails before importing. The erwin Data Steward values in `custom_properties.csv` are first names, so they need mapping to real users first.

### 2. Term assignments (`term_assignments.csv`)

The file lists the exact term for each table, column and view column, taken from the model. Ways to apply it:
- **Table- and view-level terms:** the wxdi MCP `update_asset_metadata` tool takes `business_terms` for an asset (by name, in the project or catalog). That's one call per table.
- **Column-level terms:** assign in the asset's Columns view, or run **metadata enrichment** with term assignment on the imported assets. Enrichment suggests terms; check them against `term_assignments.csv` and accept the matches. Because the term names come straight from the column's logical names, the suggestions usually match.
- The file is also the reference when reviewing, since it shows what erwin says each column means.

### 3. Reference data (`reference_data/*.csv`)

- Governance → Reference data → **New reference data set** → import the CSV, mapping `code` → Code and `value` → Value.
- `reference_data_usage.csv` lists the columns that use each list (e.g. `movie.genre`). Relate the reference data set to that column's term, or use it in a data quality rule that checks the column's values against the list.

### 4. Custom properties (`custom_properties.csv`)

User-defined properties, such as Data Steward, Create Date or Color, have no standard home in wxdi. Either:
- map them to asset owners or term stewards, or
- create custom asset properties in wxdi and fill them in, or
- leave them out.

Ask the user which of these they actually need.
