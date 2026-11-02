# Known limitations

[Back to the README](../README.md)

> **Status:** the model and report tools come from a version used on a real Power BI project, where the end-to-end lineage was first built for one data warehouse. This version makes the lineage generic: any naming convention can be described in `lineage.yaml`. The generic engine is tested on synthetic projects, so other conventions may need tuning. Bug reports with an anonymized snippet are welcome.

## Model and reports

- Reports in the **PBIR-Legacy** format (`report.json` at the root of the `.Report` folder) are not analyzed: `find_measure_usage_in_reports` lists them as not analyzed, and fails if no report could be read. Upgrade them to PBIR.
- Hierarchy levels used in a report are traced to their column only when the report's semantic model is available locally (`byPath` binding, or the `semantic_model` argument); otherwise they are flagged "to verify".
- When an output exceeds `MCP_MAX_OUTPUT_CHARS`, the server **reduces the detail, not the list**: `get_dax_lineage` keeps the name of every measure and calculation item as long as the names alone fit the budget (on very large models it falls back to per-table counts, and says so), and `find_measure_usage_in_reports` always shows the usage count of every searched field. Tools whose output is already one line per element are cut instead, and the response then **starts** with `[!] INCOMPLETE RESPONSE`: narrow the request before concluding that something "doesn't exist" or "is not used".
- The whole `definition` folder is read (tables, relationships, shared expressions, DAX functions, roles, perspectives), except `cultures/`: translations and linguistic metadata say nothing about how the model is computed, and only `get_translations` reads them. TMDL partial declarations are supported: a table split across files, several tables in one file. New measures go into the table's main file (the one named after it in `tables/`).
- **Row-Level Security**: the tables a filter restricts are read from the relationships, as the engine propagates security: active relationships only, from the one side to the many side, and back only where the relationship sets `securityFilteringBehavior: bothDirections`. Role members are counted only when they are in the files (they are usually assigned in the Power BI service). RLS filters are read, never written.
- **Translations** cover the captions, descriptions and display folders of tables, columns, measures and hierarchies; the translations of hierarchy levels and the linguistic metadata (Q&A synonyms) are not read.
- The project search goes at most 6 folder levels below the given path, so a too generic path (the OneDrive root, a drive root) fails fast instead of scanning the whole disk. Point to the project, not to a folder far above it.
- Power BI Desktop doesn't reload files changed outside it: close and reopen the project to see the changes.

## Lineage, database side

- It reads T-SQL only, from the files of a `.sqlproj`. Deployment scripts and files the project doesn't build are skipped.
- Dynamic SQL, `INSERT ... EXEC`, triggers and multi-statement functions are reported as not analyzed. Describe what they load with a rule.
- A pipeline that copies data into files read by the warehouse is not in the SQL: the lineage stops at the file until a rule says what writes it. The server proposes it only when it finds a similarly named object; otherwise the file is the source.
- `OUTPUT ... INTO` clauses (audit copies of the written rows) are not followed.
- The first analysis of a large project takes a few seconds: about 10 seconds for 1,000 objects with 30 columns each. Later calls reuse it until a file changes. The model and report side is computed in under a second on 100 tables and 100 visuals.

## Lineage, Power BI side

- **Power Query.** It is read, not run. It follows navigation, native queries, renames, added, duplicated, split and combined columns, groupings, merges (`NestedJoin` + `ExpandTableColumn`), appends, parameters and query functions with literal arguments.
  - Unpivot, pivot, flat joins and functions it doesn't know stop the trace of a column, and the reason names the step.
  - A native query built at refresh time can't be read.
- **Matching the database.** A model table is matched to the database project by `schema.object`: the server and the database name of the connection are shown, not compared.
- **Calculated tables.** Field parameters are followed exactly. For any other calculated table, a column without a namesake in its DAX is linked to every column the DAX reads, marked "to verify".
- **Reports.** Only bindings count: a name that appears only in a title or a `queryRef` is not a dependency (`find_measure_usage_in_reports` still reports it as "to verify").

## Microsoft bridge

The limits of the optional bridge (no DAX queries, refresh or transactions in folder mode, Windows only) are in [tools.md](tools.md#the-microsoft-bridge).
