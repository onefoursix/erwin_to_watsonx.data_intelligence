---
name: erwin-model-export
description: Open an erwin Data Modeler model (.erwin file) without erwin installed and export it - an interactive entity-relationship viewer page with every table, column, key, relationship and view and all the model's metadata (definitions, logical names, business terms, subject areas, data stewards and other user-defined properties, valid values, suggested classifications), plus one JSON file with all that metadata and SQL scripts (PostgreSQL and/or SQL Server) that create the model's tables, keys, foreign keys, descriptions and views. Use this whenever someone wants to open, view, diagram, browse or "see what's in" an .erwin model, wants DDL or a database schema from it, wants its metadata exported, or is starting to load an erwin model into watsonx.data intelligence (this is step 1; wxdi-metadata-import and erwin-wxdi-governance follow).
---

# erwin model export: viewer, metadata JSON, SQL

Step 1 of three skills that take an erwin model into IBM watsonx.data intelligence (wxdi):

1. **erwin-model-export** (this one): read the `.erwin` file, show it, export its metadata and SQL.
2. **wxdi-metadata-import**: create the tables and views in a database and import them into a wxdi project, with or without lineage.
3. **erwin-wxdi-governance**: the model's glossary in wxdi: categories, business terms, publishing, catalog, term assignments, classifications.

The parser reads erwin's binary format (r9 and later) directly, so it works on a Mac with only Python 3: no erwin, no license, no Windows.

## 1. Ask which model

Ask the user for the **name of the model** if they haven't given it: the name people know it by, e.g. `eMovies`. The name becomes the root glossary category and the catalog name later, so use the user's spelling and case. Then find the file:

| Model | File | Published viewer |
|---|---|---|
| eMovies (EMOVIES 15.2) | `~/Documents/My Models/eMovies.erwin` | https://claude.ai/artifact/HFduSU5kjru73Jopt4DWsN |
| PublicationSystem | `~/Documents/My Models/PublicationSystemSample.erwin` | not published yet |

If the name isn't in the table, look in `~/Documents/My Models/` for a matching `.erwin` file, and ask for the path if there is none. Older `.er1` files and XML exports must be re-saved as `.erwin` from erwin first. When you publish a new model's viewer, add a row here.

Also settle the **schema** (default: the name in snake case, `eMovies` → `emovies`) and the **SQL dialect** (`postgres`, `sqlserver`, `databricks`, `both` = postgres + sqlserver, the default, or `all`). Ask which database the user will create the tables in; if they don't know yet, write both.

## 2. Export

```bash
python3 -I <skill-dir>/scripts/export_model.py MODEL.erwin --name eMovies [--schema emovies] [--dialect both]
```

It writes into `work/<schema>/` in the project home (the folder with `.env` and `work/`, found by walking up from the skill; set `ERWIN_WXDI_HOME` to choose another, or pass `--out-dir`). The next two skills read from there:

| File | Contents |
|---|---|
| `<schema>.metadata.json` | Everything in the model, in one file: tables with columns, data types, keys, definitions; relationships; views with their sources, joins and expressions; domains; and `glossary`: the subject-area categories and business terms in wxdi's glossary import format, the term for every table, column and view column, classification suggestions, valid-value lists and where they're used, and user-defined properties such as Data Steward |
| `<schema>.postgres.sql`, `<schema>.sqlserver.sql` | `CREATE SCHEMA`, tables, primary and alternate keys, foreign keys, the erwin definitions as table and column descriptions, and views. Each script is one transaction: if anything fails, nothing is created |
| `<schema>.databricks.sql` | The same for Databricks Unity Catalog: backtick identifiers, Delta types, definitions as `COMMENT` clauses, informational (not enforced) primary and foreign keys. Databricks has no UNIQUE constraint and no foreign key to an alternate key, so those stay as comments; there are no transactions, so run it into an empty schema after `USE CATALOG` |
| `<Name>.diagram.html` | The viewer page |

The command prints a one-line summary per output and the parser's review notes. Pass the notes on (for example: an attribute name used with two different definitions, where the first one was kept).

## 3. Show the viewer

Publish `<Name>.diagram.html` with the Artifact tool: it is a complete page body (title, styles, inline data; no external scripts besides Google Fonts). For a model in the table above, republish to its existing URL so the link stays the same (read it first, then publish with `url`). For a new model, publish a new artifact, use the model name as the title, and add the link to the table. If the user would rather have the file, give them the local path.

What the page shows, so you can explain it:
- **Diagram** in IE notation: each table's columns in key order (PK above the divider, PK/FK tags, data types), data steward and subject areas on the header, relationships with erwin's verb phrases (solid = identifying, dashed = non-identifying), and views as dashed boxes listing their source tables.
- **Markers:** a coloured dot is a suggested classification (red sensitive personal, amber personally identifiable, blue personal, hollow = needs review); `VV` means the column has a valid-value list.
- **Inspector** (click a table, column or view): definition, business term and abbreviation, subject areas, user-defined properties, keys, relationships, and per column the logical name, definition, term, classification suggestion and valid values. For views: sources, joins and each column's expression.
- **Filters:** by subject area, and highlight classified, key, valid-value or undefined columns.

Layouts: `assets/layouts/<name in snake case>.json` holds a hand-tuned layout (eMovies has one); otherwise tables are auto-placed, parents before children and views last. To tune one, run `scripts/erwin_diagram.py MODEL.erwin --save-layout FILE.json`, edit the `{"table": [x, y]}` positions (boxes are 250px wide; leave ~110px between columns), and save it into `assets/layouts/`.

## 4. Review before anyone uses the SQL or the glossary

- **Views:** erwin stores which tables and columns a view uses but not how they join, so joins are inferred from the model's migrated keys. `-- REVIEW:` comments in the SQL mark guesses (a CROSS JOIN, an ambiguous column). `-- erwin also stores this user-defined SQL` is the modeler's own SQL, kept as a comment because it uses erwin's names: compare it with the generated view. Show these to the user and edit only with their agreement.
- **Definitions** become catalog descriptions and term definitions. Skim them for template text or copy-paste errors.
- **Classifications are suggestions,** made by matching term names and erwin domains against patterns. erwin models normally hold none. Each has a confidence: `high` (the name matches), `medium` (only the domain matches), `review` (a person decides: organisations, public credits such as actors, or codes). Say so if the user treats them as model content.

Then tell the user what's next: **wxdi-metadata-import** creates the tables in their database (or uses ones that exist) and imports them into wxdi; **erwin-wxdi-governance** loads the glossary.

## What the SQL holds, and the limits

| erwin | SQL |
|---|---|
| Entity, attribute, data type | Table and column (snake case; types mapped per dialect; unknown types become varchar(255)) |
| Primary key / alternate key | `PRIMARY KEY` / `UNIQUE` |
| Relationship | `FOREIGN KEY`, when it references the parent's full primary or alternate key |
| Definition | Table and column comments (Postgres `COMMENT ON`, SQL Server `MS_Description`) |
| View | `CREATE VIEW` from the column mappings, inferred joins and expressions |

Not in the SQL, but in `metadata.json`: subject areas, logical names, business terms, valid values, user-defined properties. Not exported anywhere: indexes other than keys, triggers, stored procedures.

## Files

- `scripts/export_model.py`: runs the parser for each output and assembles `metadata.json`.
- `scripts/erwin_diagram.py` + `assets/template.html`: the viewer.
- `scripts/erwin2postgres.py`, `scripts/erwinbin.py`: the parser and SQL generator (`erwin2postgres.py --help` for running it alone). These are copies of the repository's `shared/` sources; fix them there and run `tools/sync.py`.
