# Tools

[Back to the README](../README.md)

- [What you can ask](#what-you-can-ask)
- [Features](#features)
- [Tool reference](#tool-reference): [always available](#always-available), [full access only](#full-access-only), [Microsoft bridge](#microsoft-bridge), [lineage](#lineage)
- [What the server tells the assistant](#what-the-server-tells-the-assistant)
- [The Microsoft bridge](#the-microsoft-bridge)

## What you can ask

Give the assistant the path of the `.pbip` file (and, for the lineage, of the database project), then ask in plain language. For example:

| Request | Tools the assistant uses |
|---|---|
| "Summarize the Sales table: which measures have no format string, and what is still undocumented?" | `get_table_summary` |
| "Which measures use `USERELATIONSHIP` or read `Sales[Discount]`?" | `search_model` |
| "Where is [Total Sales] used in the reports, and does any RLS role read it?" | `find_measure_usage_in_reports` |
| "Which source columns does [Gross Margin %] come from?" | `get_end_to_end_lineage`, upstream |
| "If I drop `mart.sales.Discount`, which measures and visuals are affected?" | `get_end_to_end_lineage`, downstream |
| "Draft the lineage.yaml of this database project." | `draft_lineage_config`, then the questions it returns |
| "Write a business description for every visible measure of Sales." | `get_description_guidelines`, `get_smart_description_preview`, then `write_smart_description` (full access) |
| "In this project's descriptions, call the customers 'clients'." | `save_description_guidelines` (full access): your rules for the project, kept outside its repository |
| "Hide the Cost column from the Reseller role." | `set_ols_permission` (full access) |

## Features

- **Explore a model**: list the tables; summarize a table in a few lines, without its DAX or M (columns with type and flags, measures with folder and format, hierarchies, partitions and their sources, relationships, documentation status); read a table's TMDL; list relationships one per line.
- **Search the model**: find tables, columns, measures, hierarchies, calculation items, partitions, parameters, DAX functions and RLS filters by name, by the content of their DAX or M, or by their description. One line per match, showing the line that matched.
- **Understand DAX**: get the full definition of a measure or calculation item and map its dependencies (measures, physical columns, calculation items). References are resolved against the model: case-insensitive, unqualified columns traced to their table, qualified ones checked to exist. Anything that can't be attributed to a single existing object is shown as "unresolved", never guessed.
- **Impact analysis**: find which reports, pages, visuals, filters, drillthrough pages, bookmarks and report-level measures use given measures or columns, and which security roles read them in their RLS filters. Real bindings are reported as certain; matches found only in a text field (title, `queryRef`) are flagged "to verify". You can restrict the search to reports bound to a given semantic model.
- **End-to-end lineage**: from a source column to every visual that shows it, and back, across the database project, Power Query, the semantic model and the reports. Details: [lineage.md](lineage.md#what-the-lineage-follows).
- **Row-Level Security**: read each role's DAX filter on each table, the columns and measures it reads, whether it depends on the user (`USERPRINCIPALNAME`, `CUSTOMDATA`…), and which other tables it restricts through relationships.
- **Object-Level Security**: read and write the OLS of security roles (tables and individual columns hidden per role) without touching existing RLS filters.
- **Perspectives and translations**: what each perspective shows, including members that no longer exist; each culture's translated captions, descriptions and display folders, how many visible objects are translated, and which are still missing.
- **Data Dictionary documentation**:
  - measure how much of the model is documented (`get_documentation_coverage`);
  - built-in writing rules that allow only what the model supports: no invented agreements, thresholds or causes, and the facts of an existing description are kept;
  - [your own rules](configuration.md#description-rules) and a glossary of your company's terms, for every project or for one, kept in [your folder](configuration.md#your-folder) outside the projects' repositories;
  - a preview step that also shows the element's technical context;
  - `///` descriptions written with safeguards: fact-table columns and hidden elements are protected by default, and each protection can be lifted per call. Only the business text you provide is written.
- **Safe edits** (opt-in): create measures and calculation items; set display folders, visibility and format strings, only on objects that have those properties in Power BI. Writes are atomic, preserve line endings and BOM, refuse to overwrite a file changed in the meantime, reject values that would break the TMDL file (line breaks in a single-line value, control characters, ```` ``` ````), and never leave the project folder.
- **Token-efficient output**: compact text responses. Large outputs are reduced progressively (less detail, never fewer elements), and a response that had to be cut says so at the top.
- **Optional bridge** to Microsoft's official [Power BI Modeling MCP Server](https://github.com/microsoft/powerbi-modeling-mcp) for operations not covered natively. It is restricted to local PBIP folders; cloud operations are blocked.

### Working with Microsoft's server

Microsoft's [Power BI Modeling MCP Server](https://github.com/microsoft/powerbi-modeling-mcp) loads a model into Microsoft's Tabular Object Model, from Power BI Desktop, a Fabric workspace or a PBIP folder, and edits it there. This server reads the PBIP files directly, without Power BI Desktop, and adds what the model alone can't tell:

- where each measure and column is used in the reports, and which security roles read it;
- the lineage from the source column of the database project to the visual;
- compact responses within a character budget, so a large model doesn't fill the assistant's context.

The two work together: the optional [bridge](#the-microsoft-bridge) exposes part of the official server, its modeling tools on the local PBIP folder (relationships, partitions, hierarchies, RLS filters…), through this server. The rest of it is left out on purpose: what needs a running model (DAX queries, refresh, traces) and anything that reaches the cloud.

## Tool reference

All model tools take `project_path`: the absolute path to a `.pbip` file, to the project folder or to the `.SemanticModel` folder.

- A `.pbip` file identifies its own model (the one named after it, or the one its report is bound to), even when other projects sit in the same folder.
- If a folder contains several semantic models, the tool doesn't guess: it asks which one you mean.
- `find_measure_usage_in_reports` takes `reports_workspace_path` instead: the folder that contains the report projects, each with its `.Report` folder (a single project folder, or a workspace with several projects).

### Always available

| Tool | Description |
|---|---|
| `list_tables` | Lists the tables of the semantic model by the name declared in TMDL, plus the file name when it differs (or the files, when a table is split across several). |
| `get_table_summary` | One table in a few lines, without DAX or M. [Details](#get_table_summary). |
| `get_documentation_coverage` | How much of the model has a `///` description, with visible and hidden elements counted apart. [Details](#get_documentation_coverage). |
| `search_model` | Searches the whole model by name, expression or description. [Details](#search_model). |
| `read_table_definition` | Returns the full TMDL of one table (every declaration of it, when it is split across files). |
| `get_relationships` | Lists relationships, optionally for one table, one line each, with their cardinality (`*--1`, `1--1`, `*--*`). |
| `get_dax_measure` | Returns the full definition of a measure or calculation item. |
| `get_dax_lineage` | Maps the DAX dependencies of all measures or of one target. Measures that reference nothing (constants, `BLANK()`) are listed apart, calculation items using `SELECTEDMEASURE()` are marked, and references to objects that don't exist are listed as unresolved. |
| `find_measure_usage_in_reports` | Finds where measures and columns are used across PBIR reports, and which RLS roles read them. [Details](#find_measure_usage_in_reports). |
| `get_ols_permissions` | Shows which tables and columns each role hides (`metadataPermission: none`), optionally for one `role_name`. |
| `get_rls_filters` | Shows each role's RLS filter on each table: the columns and measures it reads (unresolved references flagged), the user functions that make it dynamic, and the tables it restricts through active relationships. Optionally for one `role_name`, or only the filters on or reaching a `table_name`. |
| `get_perspectives` | Lists each perspective with the measures, columns and hierarchies it shows per table (or the whole table, `includeAll`), its members that don't exist in the model, and the tables in no perspective. |
| `get_translations` | Without `culture`: each culture, how many visible objects have a translated caption, and its translated descriptions and display folders. With `culture`: every translation, the visible objects still without a caption, and translations of objects that no longer exist (optionally for one `table_name`). |
| `get_description_guidelines` | Returns the rules for Data Dictionary descriptions: the built-in ones followed by [your own](configuration.md#description-rules), for every project and, with `project_path`, for that project. The first line names every file they come from. Call it once per documentation session. |
| `get_smart_description_preview` | Previews the `///` description that would be written, plus the element's technical context (which is not written). It starts with the line naming the rule files. The first description ever prepared first asks whether you have rules of your own. |

#### `get_table_summary`

- The table's kind: fact table, calculation group, calculated table, hidden.
- Partitions, with the source each one reads.
- Columns: type, calculated, hidden, key, folder, format, sort by.
- Measures: folder, and format string (or its absence).
- Calculation items, hierarchies with their levels, relationships.
- How many visible elements have a `///` description. `include_descriptions` adds the start of each description.

#### `get_documentation_coverage`

Counts measures, columns, and tables / hierarchies / calculation items with a `///` description, separating **visible** elements from **hidden** ones (the columns of a hidden fact table are read by measures, not by people). It then lists the tables with the most visible elements still undocumented. With `table_name`, it lists the names of that table's undocumented visible elements. Its figures for a table match those of `get_table_summary`.

#### `search_model`

Searches by `name`, `expression` or `description`:

- `expression` covers the DAX of measures, calculated columns, calculation items, functions and RLS filters (dynamic format strings included), the `sourceColumn` of data columns, and the M or DAX of partitions and shared expressions.
- Substring or `regex`, case-insensitive, optionally restricted by `object_types` and `table_name`.
- One line per object, with the line that matched.

#### `find_measure_usage_in_reports`

- Searches measures (`measure_name`, `measure_names`) and columns (`column_names`, as `Table[Column]`).
- Covers visuals, report-, page- and visual-level filters, drillthrough and tooltip fields, bookmarks and report-level measures.
- With `semantic_model`, only reports bound to that model (per `definition.pbir`) are counted.
- Also reads the RLS filters of the local models behind the reports (or of the model given as a path in `semantic_model`): a field a role reads is reported with the role.
- PBIR-Legacy reports are not analyzed, and are listed as such.

### Full access only

| Tool | Description |
|---|---|
| `create_measure` | Creates a DAX measure inside the table block. |
| `create_calculation_item` | Creates a calculation item in a calculation group. Its format string is written as `formatStringDefinition`; display folder and visibility are refused, because calculation items don't have them. |
| `set_display_folder` | Sets `displayFolder` on a measure, column or hierarchy. |
| `set_item_visibility` | Hides a table, measure, column or hierarchy (`isHidden`), or shows it again by removing the property. |
| `format_measure` | Sets `formatString` on a measure or column; on a calculation item it writes `formatStringDefinition` instead. |
| `write_smart_description` | Writes the business description as `///`. [Details](#write_smart_description). |
| `set_ols_permission` | Hides or shows a whole table or a single column for a role, creating the role if needed; can also set the role's `modelPermission`. Never touches RLS filters. |
| `save_description_guidelines` | Saves the description rules you ask for: by default your rules for the project (`scope='project'`), or for every project (`'general'`), or the repository's, shared with the team (`'repo'`, only when you ask for it). `mode='append'` adds them, `'overwrite'` replaces the whole file. |
| `set_user_folder` | Moves [your folder](configuration.md#your-folder) between the two standard locations, `settings` and `documents`. Any other folder is chosen in a terminal, with `--user-folder`. |

#### `write_smart_description`

- Modes: `overwrite` replaces the description; `merge` keeps the existing text and removes lines auto-generated by earlier versions; `append` adds after it; `skip` writes only if there is none.
- Columns of fact tables and hidden elements are protected by default; `allow_fact_table_columns` and `allow_hidden` lift the protection.
- The first description ever written first asks whether you have rules of your own.

### Microsoft bridge

| Tool | Access | Description |
|---|---|---|
| `list_official_powerbi_tools` | always | Lists the tools exposed by the official server. If the executable is not installed, explains what is missing and how to install it. |
| `get_official_result_detail` | always (bridge available) | Retrieves part of a previously reduced bridge response. |
| `call_official_powerbi_tool` | full access (bridge available) | Calls an official tool on a local PBIP folder. Path arguments must point inside the project's semantic model. |

### Lineage

All lineage tools are always available, except `save_lineage_export` and `save_lineage_config`.

`sql_project_path` is the `.sqlproj` file or the folder of the database project (a folder with several projects is refused, and the projects are listed). `project_path` is the Power BI project, as for the model tools.

| Tool | Description |
|---|---|
| `get_end_to_end_lineage` | The whole chain for a Power BI project; add `sql_project_path` to follow the database back to its sources. [Details](#get_end_to_end_lineage). |
| `get_sql_project_lineage` | The database project alone, for one object (`schema.object`), one column (`schema.object.column`) or the whole project, with the same options. Every edge says how it was found (`view`, `merge`, `insert`… with file and line, or `rule '<name>'`), and every node its layer. A project with no structure first asks you to confirm [the one inferred from its SQL](lineage.md#when-the-project-has-no-structure-yet). |
| `test_lineage_config` | Checks `lineage.yaml` against the database project and, with `project_path`, the model. [Details](#test_lineage_config). |
| `suggest_lineage_rules` | Proposes rules for the tables nothing loads, inferred from the object names. Each rule is tried on the project and returned as ready-to-paste YAML. |
| `draft_lineage_config` | Drafts `lineage.yaml` from the project and returns the questions that complete it, each with a proposed answer: the layer of a schema whose name says nothing or contradicts its flows, rules inferred from names, what loads a table nothing explains, the columns the ETL creates. Called again with the `answers`, it applies them and asks only what is still open. With `project_path`, the schemas the model reads are proposed as the last layer. |
| `save_lineage_config` | Full access only. Checks and saves the lineage configuration (the draft, or a file edited by hand): by default in [your folder](configuration.md#your-folder), outside the repository; next to the `.sqlproj`, shared with the team, only with `scope='repo'`. An existing file is replaced only with `overwrite`. |
| `get_lineage_config_guide` | Returns the guide to writing `lineage.yaml`. |
| `export_lineage` | Exports a lineage (end-to-end or database only) as a Mermaid flowchart with one box per layer, a Markdown page with the diagram and the edge table, CSV or JSON. A diagram too large for one response is never cut: the tool says how to narrow it. Like the other lineage tools, it asks you to confirm the structure of a project that has none. |
| `save_lineage_export` | Full access only. Saves the whole export to a file with the extension of its format (`.md`, `.mmd`, `.csv`, `.json`), inside the folder of the Power BI project (where its `.pbip` is) or of the database project, or anywhere in `MCP_ALLOWED_ROOTS` when set. |

#### `get_end_to_end_lineage`

- **Target**: `[Measure]`, `Table[Column]`, `Table`, `schema.object[.column]`, `Report`, `Report/Page` or `Report/Page/Visual` (title, type or id).
- **Direction**: `upstream`, `downstream` or `both`.
- **Level**: `column`, or `object` (tables and views, model tables, measures, report pages), with an optional `depth`.
- **Format**: `edges`, `tree` or `json`.
- The header gives the coverage: model columns traced to their source, and visuals that reach a column outside the model.
- With a database project that has no structure yet, it first asks you to confirm [the one inferred from its SQL](lineage.md#when-the-project-has-no-structure-yet).

#### `test_lineage_config`

On the database project, it reports:

- problems in the file, with their line;
- objects per layer, and schemas in no layer;
- what each rule matches, and the sources it doesn't find;
- tables nothing loads, and views that only read files a similarly named object may have written (a pipeline);
- flows going backwards between layers;
- columns that reach no source, and statements that couldn't be analyzed.

With `project_path`, it also checks the model: tables that read from a layer other than the last one or from objects outside the project, columns whose source is not traced (with the Power Query step that stops them), native queries, and visuals that reach no source.

## What the server tells the assistant

When a client connects, the server gives its assistant a few rules (`SERVER_INSTRUCTIONS` in `server.py`):

- answer from what the tools return, and don't rebuild a lineage by reading the SQL;
- always mention what the tools report they did **not** cover: the `Coverage` line, reports `NOT analyzed`, `to verify` matches, an `[!] INCOMPLETE RESPONSE`;
- say when a lineage is `PROVISIONAL`;
- write descriptions only from what the model, the lineage or you support.

The end-to-end lineage flags the reports it left out at the top of its response, on a line of its own, and counts them in the `Coverage` line.

## The Microsoft bridge

The bridge always connects to the PBIP folder on disk (`ConnectFolder`), never to Power BI Desktop or the cloud, so no Entra ID authentication is needed and the project must **not** be open in Power BI Desktop. It exposes only an allow-list of official tools and blocks cloud, deploy and connection operations. The allow-list was checked against version 0.4.0 of the official server: with another version, `list_official_powerbi_tools` names the expected tools it doesn't find.

- **Not available in folder mode**: DAX queries, data refresh and traces (they answer `not supported on offline connections`), and transactions, because every call uses its own session.
- **Writes re-serialize the whole `definition` folder**, not just the file you changed. In practice untouched files usually come back identical, but use the bridge on a version-controlled project or a backup.
- The bridge works only on Windows, because it looks for a `.exe` executable.
