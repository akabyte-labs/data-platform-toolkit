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
- **Token-efficient output**: compact text responses within a character budget (`MCP_MAX_OUTPUT_CHARS`). Large outputs lose detail before they lose elements; when a list has to be cut, the response says so at the top ([details](limitations.md#model-and-reports)).
- **Optional bridge** to Microsoft's official [Power BI Modeling MCP Server](https://github.com/microsoft/powerbi-modeling-mcp) for the edits not covered natively (relationships, partitions, hierarchies, RLS filters…). It is restricted to local PBIP folders; cloud operations are blocked. See [The Microsoft bridge](#the-microsoft-bridge).

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
| `get_dax_lineage` | Maps the DAX dependencies of all measures or of one target. [Details](#get_dax_lineage). |
| `find_measure_usage_in_reports` | Finds where measures and columns are used across PBIR reports, and which RLS roles read them. [Details](#find_measure_usage_in_reports). |
| `get_ols_permissions` | Shows which tables and columns each role hides (`metadataPermission: none`), optionally for one `role_name`. |
| `get_rls_filters` | Shows each role's RLS filter on each table, what it reads and which tables it restricts. [Details](#get_rls_filters). |
| `get_perspectives` | Lists what each perspective shows, and the members that no longer exist. [Details](#get_perspectives). |
| `get_translations` | Shows the translations of each culture, and what is still untranslated. [Details](#get_translations). |
| `get_description_guidelines` | Returns the rules for Data Dictionary descriptions: the built-in ones followed by [your own](configuration.md#description-rules). Call it once per documentation session. [Details](#get_description_guidelines-and-get_smart_description_preview). |
| `get_smart_description_preview` | Previews the `///` description that would be written, with the element's technical context. [Details](#get_description_guidelines-and-get_smart_description_preview). |

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

Searches `query` in the names, the expressions or the descriptions of the model (`search_in`: any of `name`, `expression`, `description`):

- `expression` covers the DAX of measures, calculated columns, calculation items, functions and RLS filters (dynamic format strings included), the `sourceColumn` of data columns, and the M or DAX of partitions and shared expressions.
- Substring or `regex`, case-insensitive, optionally restricted by `object_types` and `table_name`.
- One line per object, with the line that matched.

#### `find_measure_usage_in_reports`

- Searches measures (`measure_name`, `measure_names`) and columns (`column_names`, as `Table[Column]`).
- Covers visuals, report-, page- and visual-level filters, drillthrough and tooltip fields, bookmarks and report-level measures.
- With `semantic_model`, only reports bound to that model (per `definition.pbir`) are counted.
- Also reads the RLS filters of the local models behind the reports (or of the model given as a path in `semantic_model`): a field a role reads is reported with the role.
- PBIR-Legacy reports are not analyzed, and are listed as such.

#### `get_dax_lineage`

- Without `target_measure`, every measure and calculation item; with it, one target.
- Measures that reference nothing (constants, `BLANK()`) are listed apart.
- Calculation items that use `SELECTEDMEASURE()` are marked.
- References to objects that don't exist are listed as unresolved.

#### `get_rls_filters`

- Each role's DAX filter on each table, with the columns and measures it reads (unresolved references flagged).
- The user functions that make a filter dynamic (`USERPRINCIPALNAME`, `CUSTOMDATA`…).
- The tables a filter restricts through active relationships ([how they are found](limitations.md#model-and-reports)).
- Optionally for one `role_name`, or only the filters on or reaching a `table_name`.

#### `get_perspectives`

- Per table, the measures, columns and hierarchies each perspective shows, or the whole table (`includeAll`).
- Members that no longer exist in the model (renamed or deleted objects).
- The tables in no perspective. Optionally for one `perspective_name`.

#### `get_translations`

- Without `culture`: each culture, how many visible objects have a translated caption, and its translated descriptions and display folders.
- With `culture`: every translation, the visible objects still without a caption, and the translations of objects that no longer exist. Optionally for one `table_name`.

#### `get_description_guidelines` and `get_smart_description_preview`

- `get_description_guidelines` returns the built-in rules followed by yours, for every project and, with `project_path`, for that project.
- Both start with a line naming every file the rules come from.
- The preview shows the `///` description that would be written, plus the element's technical context, which is not written.
- The first description ever prepared first asks whether you have rules of your own.

### Full access only

| Tool | Description |
|---|---|
| `create_measure` | Creates a DAX measure inside the table block, optionally with its display folder, format string, `///` description and hidden flag. |
| `create_calculation_item` | Creates a calculation item in a calculation group, optionally with its format string (written as `formatStringDefinition`) and `///` description. Display folder and visibility are refused, because calculation items don't have them. |
| `set_display_folder` | Sets `displayFolder` on a measure, column or hierarchy. |
| `set_item_visibility` | Hides a table, measure, column or hierarchy (`isHidden`), or shows it again by removing the property. |
| `format_measure` | Sets `formatString` on a measure or column; on a calculation item it writes `formatStringDefinition` instead. |
| `write_smart_description` | Writes the business description as `///`. [Details](#write_smart_description). |
| `set_ols_permission` | Hides or shows a whole table or a single column for a role, creating the role if needed; can also set the role's `modelPermission`. Never touches RLS filters. |
| `save_description_guidelines` | Saves the description rules you ask for, by default in your rules for the project. [Details](#save_description_guidelines). |
| `set_user_folder` | Moves [your folder](configuration.md#your-folder) between the two standard locations, `settings` and `documents`. Any other folder is chosen in a terminal, with `--user-folder`. |

#### `write_smart_description`

- Modes: `overwrite` replaces the description; `merge` keeps the existing text and removes lines auto-generated by earlier versions; `append` adds after it; `skip` writes only if there is none.
- Columns of fact tables and hidden elements are protected by default; `allow_fact_table_columns` and `allow_hidden` lift the protection.
- The first description ever written first asks whether you have rules of your own.

#### `save_description_guidelines`

- `scope`: `project` (the default), your rules for the project; `general`, your rules for every project; `repo`, the repository's, shared with the team, only when you ask for it.
- `mode`: `append` (the default) adds the rules, `overwrite` replaces the whole file.

### Microsoft bridge

| Tool | Access | Description |
|---|---|---|
| `list_official_powerbi_tools` | always, unless the bridge is turned off (`MCP_MSFT_BRIDGE=off`) | Lists the tools exposed by the official server. If the executable is not installed, explains what is missing and how to install it. |
| `get_official_result_detail` | always, when the bridge is available | Retrieves part of a previously reduced bridge response. |
| `call_official_powerbi_tool` | full access, when the bridge is available | Calls an official tool on a local PBIP folder. Path arguments must point inside the project's semantic model. |

### Lineage

All lineage tools are always available, except `save_lineage_export` and `save_lineage_config`.

Shared arguments:

- `sql_project_path` is the `.sqlproj` file or the folder of the database project (a folder with several projects is refused, and the projects are listed). `project_path` is the Power BI project, as for the model tools.
- `config_path` uses a lineage configuration kept somewhere else instead of the one found ([which file is used](configuration.md#which-file-is-used)). `reports_path` points to the reports when they are not next to the model.
- `target`, `direction`, `level` and `depth` select part of a lineage, as described for [`get_end_to_end_lineage`](#get_end_to_end_lineage). The level is `column` by default only there; `get_sql_project_lineage`, `export_lineage` and `save_lineage_export` start at `object`.

| Tool | Description |
|---|---|
| `get_end_to_end_lineage` | The whole chain for a Power BI project; add `sql_project_path` to follow the database back to its sources. [Details](#get_end_to_end_lineage). |
| `get_sql_project_lineage` | The database project alone, for one object, one column or the whole project. [Details](#get_sql_project_lineage). |
| `test_lineage_config` | Checks `lineage.yaml` against the database project and, with `project_path`, the model. [Details](#test_lineage_config). |
| `suggest_lineage_rules` | Proposes rules for the tables nothing loads, inferred from the object names. Each rule is tried on the project and returned as ready-to-paste YAML. |
| `draft_lineage_config` | Drafts `lineage.yaml` from the project and returns the questions that complete it. [Details](#draft_lineage_config). |
| `save_lineage_config` | Full access only. Checks and saves the lineage configuration, by default in [your folder](configuration.md#your-folder). [Details](#save_lineage_config). |
| `get_lineage_config_guide` | Returns the guide to writing `lineage.yaml`. |
| `export_lineage` | Exports a lineage as Mermaid, Markdown, CSV or JSON. [Details](#export_lineage-and-save_lineage_export). |
| `save_lineage_export` | Full access only. Saves the whole export to a file. [Details](#export_lineage-and-save_lineage_export). |

#### `get_end_to_end_lineage`

- **Target**: `[Measure]`, `Table[Column]`, `Table`, `schema.object[.column]`, `Report`, `Report/Page` or `Report/Page/Visual` (title, type or id). Without a target, the whole lineage.
- **Direction**: `upstream`, `downstream` or `both` (the default).
- **Level**: `column` (the default), or `object` (tables and views, model tables, measures, report pages). `depth` limits the steps followed from the target (0, the default, follows them all).
- **Format** (`output_format`): `edges` (the default), `tree` or `json`.
- The header gives the coverage: model columns traced to their source, and visuals that reach a column outside the model.
- With a database project that has no structure yet, it first asks you to confirm [the one inferred from its SQL](lineage.md#when-the-project-has-no-structure-yet).

#### `get_sql_project_lineage`

- **Target**: one object (`schema.object`), one column (`schema.object.column`), or none for the whole project. The other options are those of `get_end_to_end_lineage`, at `object` level by default.
- Every edge says how it was found: `view`, `merge`, `insert`… with file and line, or `rule '<name>'`. Every node says its layer.
- A project with no structure first asks you to confirm [the one inferred from its SQL](lineage.md#when-the-project-has-no-structure-yet).

#### `draft_lineage_config`

- Drafts every section of `lineage.yaml` from the project and returns the questions only you can answer, each with a proposed answer: the layer of a schema whose name says nothing or contradicts its flows, rules inferred from names, what loads a table nothing explains, the columns the ETL creates.
- Called again with the `answers`, it applies them and asks only what is still open.
- With `project_path`, the schemas the model reads are proposed as the last layer.

#### `save_lineage_config`

- Checks and saves the configuration: the draft, or a file edited by hand.
- By default in [your folder](configuration.md#your-folder), outside the repository; next to the `.sqlproj`, shared with the team, only with `scope='repo'`.
- An existing file is replaced only with `overwrite`.

#### `export_lineage` and `save_lineage_export`

- **Formats** (`export_format`): a Mermaid flowchart with one box per layer, a Markdown page with the diagram and the edge table, CSV or JSON. `export_lineage` returns Mermaid by default, `save_lineage_export` saves Markdown by default.
- Both work on the end-to-end lineage (with `project_path`) or on the database project alone.
- A diagram too large for one response is never cut: `export_lineage` says how to narrow it, or `save_lineage_export` writes it whole.
- The file gets the extension of its format (`.md`, `.mmd`, `.csv`, `.json`), inside the folder of the Power BI project (where its `.pbip` is) or of the database project, or anywhere in `MCP_ALLOWED_ROOTS` when set. The folder must exist, and an existing file is replaced only with `overwrite`.
- Like the other lineage tools, `export_lineage` asks you to confirm the structure of a project that has none.

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

- answer from what the tools return, and don't rebuild a lineage by reading the SQL or TMDL files: where the tools stop, say where, and what would describe the missing step (a rule in `lineage.yaml`);
- always mention what the tools report they did **not** cover: the `Coverage` line, reports `NOT analyzed`, `to verify` matches, unresolved references, an `[!] INCOMPLETE RESPONSE`; never say "all the reports" or "not used" without that qualification;
- say when a lineage is `PROVISIONAL`;
- for descriptions, read the rules first (`get_description_guidelines`), write only what the DAX, the lineage, the existing description or you support, and ask for the rest;
- write formulas as plain text.

The end-to-end lineage flags the reports it left out at the top of its response, on a line of its own, and counts them in the `Coverage` line.

## The Microsoft bridge

Microsoft's [Power BI Modeling MCP Server](https://github.com/microsoft/powerbi-modeling-mcp) loads a model into Microsoft's Tabular Object Model, from Power BI Desktop, a Fabric workspace or a PBIP folder, and edits it there. This server reads the PBIP files directly and adds what the model alone can't tell: where each field is used in the reports and by which security roles, the lineage from the database project to the visual, and compact responses within a character budget.

The two work together. When the official server is installed (the executable named in `POWERBI_MODELING_MCP_PATH`, on `PATH`, or in the VS Code extension `analysis-services.powerbi-modeling-mcp`, searched in this order), the bridge exposes its modeling tools on the local PBIP folder (relationships, partitions, hierarchies, RLS filters…) through this server. The rest is left out on purpose: what needs a running model (DAX queries, refresh, traces) and anything that reaches the cloud. `MCP_MSFT_BRIDGE` turns the bridge on or off ([options](configuration.md#options)).

The bridge always connects to the PBIP folder on disk (`ConnectFolder`), never to Power BI Desktop or the cloud, so no Entra ID authentication is needed and the project must **not** be open in Power BI Desktop. It exposes only an allow-list of official tools and blocks cloud, deploy and connection operations. The allow-list was checked against version 0.4.0 of the official server: with another version, `list_official_powerbi_tools` names the expected tools it doesn't find.

- **Not available in folder mode**: DAX queries, data refresh and traces (they answer `not supported on offline connections`), and transactions, because every call uses its own session.
- **Writes re-serialize the whole `definition` folder**, not just the file you changed. In practice untouched files usually come back identical, but use the bridge on a version-controlled project or a backup.
- The bridge works only on Windows, because it looks for a `.exe` executable.
