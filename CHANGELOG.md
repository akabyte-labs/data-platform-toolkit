# Changelog

All notable changes to this project. The version is the one the server
declares to MCP clients and the version of the package
(`mcp_data_platform_toolkit/__init__.py`, `__version__`).

Versions before 1.5.0 were developed in a private repository: their history
and tags are not part of this one.

## Unreleased

Documentation only: the description on PyPI will show what the README shows
on GitHub. The server itself does not change.

### Changed

- **README**: every link is absolute, so it also works on the PyPI page, and
  the lineage example is an image (`docs/img/lineage-example.png`), because
  PyPI does not render Mermaid. Its source is `docs/img/lineage-example.mmd`,
  which the tests still compare with the real export.
- **README**: new "How it was built" section; the licence and trademark line
  at the end is gone (the licence is in `LICENSE`).

### Added

- Issue templates for bug reports and feature requests, with a link to the
  private vulnerability report, and a pull request template.
- Dependabot configuration for the pip dependencies and the GitHub Actions.

## 1.5.0

The server becomes Data Platform Toolkit, an installable package, and the
files that belong to the user (the rules for the descriptions, the lineage
configurations) move to a folder of their own, outside the projects.

It also infers the structure of a warehouse whose schemas are not called
bronze, silver and gold, instead of showing an empty lineage, and follows the
files a pipeline writes instead of stopping at them in silence.

### Added

- **Installable package** `mcp-data-platform-toolkit`: `uvx --python 3.13
  mcp-data-platform-toolkit` runs it with nothing to install by hand (on
  the Python CI tests; uv downloads it if needed, and a newer one may lack
  a package for a dependency: PyYAML had none for Python 3.15 yet), `pip
  install mcp-data-platform-toolkit` installs the command of the same name,
  and `python -m mcp_data_platform_toolkit` runs it from any environment
  that has it. `--check` prints a ready-to-paste client configuration for
  uvx and one for the Python environment it runs in.
- **Your folder**, outside the projects' repositories:
  `%APPDATA%\data-platform-toolkit` (`~/.config/data-platform-toolkit` on
  macOS and Linux) by default, with a folder per project under `projects/`,
  named after its repository. The first time a tool needs it, the user
  chooses where to keep it (settings folder, Documents or another folder) in
  a form shown by the client (MCP elicitation); clients that can't show it
  keep the default, and the response says where it is and how to move it.
  `mcp-data-platform-toolkit --user-folder [folder]` moves it, to the folder
  given or to the one chosen from a menu; `set_user_folder` (full access)
  between the settings folder and Documents only; `MCP_USER_FOLDER` sets it
  for a whole office.
- **Your own rules for the descriptions**, added to the built-in ones:
  `description_guidelines.md` in your folder for every project,
  `projects/<repository>/description_guidelines.md` for one. A
  `description_guidelines.md` committed in a project's repository is read
  too, as the team's rules. They add up from the most general; a file
  starting with `<!-- replace -->` replaces the rules before it.
  `save_description_guidelines` (full access) saves the rules the user asks
  for, to the project's file by default, to the repository's only when
  asked; `mcp-data-platform-toolkit --guidelines [project]` opens them in
  the system's editor.
- **The first description asks for your rules**: before the first
  description is ever prepared or written, `get_smart_description_preview`
  or `write_smart_description` (whichever comes first) asks whether you
  have explicit rules to add, in a form of the client (MCP elicitation) or,
  when the client can't show one, through the assistant. The answer goes to
  your rules for every project. Asked once per user (`settings.json`
  remembers it), and never when your rules already have something.
- **The folder of a project is always created**: `projects/<repository>/`
  in your folder, with the template of its `description_guidelines.md`,
  the first time a tool works on the project (once your folder is chosen),
  so you always find where its files go.
- **Your own lineage configuration**:
  `projects/<repository>/<database project>.lineage.yaml` in your folder.
  `mcp-data-platform-toolkit --lineage-config <database project>` opens the
  configuration in use, or creates yours from a draft of the project; a
  generated file comes with a warning to check it before relying on it (a
  wrong layer makes the whole lineage wrong, with no error), and opens right
  after. `save_lineage_config` shows the same warning to the user as it is,
  in a form of the client (MCP elicitation) with the choice to open the file
  in an editor; the file is saved whatever they answer, and when they didn't
  see the form the answer carries the warning for the assistant to pass on.
  A generated file says `draft: true` (a new key of `lineage.yaml`) and
  stays a draft, whoever edits it, until the user removes the line once
  they checked it; until then every lineage answer says the configuration
  is a draft, and `--lineage-config` reminds it. A file written by hand gets
  no warning. The answer of a save also says how many objects the file
  leaves in the lineage, warns when it leaves out most of them, and counts
  the items `test_lineage_config` would mark (schemas in no layer, rules
  matching nothing, tables nothing loads, flows going backwards). Opening
  the editor doesn't wait for it, and is skipped where there is no
  graphical session.
- **The structure of a project with no configuration.** When
  `get_sql_project_lineage`, `get_end_to_end_lineage` or `export_lineage` meet a
  database project with no `lineage.yaml` (or a file of the user's that says
  nothing) and the standard layers place less than half of its objects, they
  infer its structure from the SQL (the layers in flow order, the schemas to
  leave out, the rules, the ETL columns; with the model, the schemas it reads
  are the last layer), show it in a form of the client and ask: save it, change
  it or not now. On *save* it is written as the user's own configuration, in
  their folder, outside the repository, also in read-only mode (the answer
  doesn't go through the language model). The repository's own file is never
  replaced. A client that can't show forms gets the proposal through the
  assistant, once. With *not now*, or a declined form, the lineage uses the
  structure for the session, and every response says
  `PROVISIONAL, not saved`.
- **Files written by a pipeline.** `test_lineage_config` flags the objects that
  only read files nothing in the project writes when another object is named
  like them (a Parquet exported from `etl.vw_fact_x` and read by `dwh.fact_x`
  through `OPENROWSET`): the lineage stopped at the file and the check said
  "Nothing to fix". `suggest_lineage_rules` and `draft_lineage_config` propose
  the rule that connects them. A file people drop (a CSV of targets) has no such
  object and stays the source.
- Every lineage response warns when the layers of the configuration in use
  place less than half of the project's objects.
- **`get_documentation_coverage`.** How much of the model has a `///`
  description, for the whole model or one table: measures, columns and
  tables / hierarchies / calculation items, the visible ones apart from the
  hidden ones, then the tables with the most undocumented visible elements (the
  names of a table's, with `table_name`). Until now the assistant could only
  estimate from a few tables. A test keeps its figures equal to those of
  `get_table_summary`.
- **The server tells its assistant what to say** when a client connects
  (`SERVER_INSTRUCTIONS`): answer from the tools and don't rebuild a lineage by
  hand; say what they report they did not cover; say when a lineage is
  provisional; write descriptions only from what the model supports.
- **Descriptions say only what the model supports.** The built-in guidelines
  have a GROUNDING section (no business agreements, thresholds or causes that no
  file shows, company terms explained only if defined, the facts of an existing
  description kept, what was inferred listed); the short text of the tools says
  it; the preview repeats it, and warns when `overwrite` would replace an
  existing description. The files of your rules invite a glossary.

### Fixed in the draft of the lineage configuration

- A schema whose data flows only between its own objects (a warehouse all in
  `dbo`, with the layers in the names of the tables) is no longer proposed as
  `ignore`, which left the lineage empty: it forms a single layer, and the
  question says so (layers are given to whole schemas).
- An object the SQL reads but the project doesn't define (a synonym, another
  database) in a schema of no layer is shown as a source, not as an object
  in no layer; the draft lists such objects in a comment.
- Measured on Microsoft's WideWorldImporters, WideWorldImportersDW and
  AdventureWorksDW:
  - Flows between two schemas both ways are told apart: the direction that
    only UPDATEs existing rows and moves fewer columns is lookups (staging
    taking the keys of a dimension), not where the data comes from.
    `test_lineage_config` judges them the same way: lookups are listed, not
    marked as flows going backwards (19 of them in WideWorldImportersDW).
  - A schema that nothing in the SQL loads and that feeds the others is
    proposed as the first layer even when its name is a weak guess for
    another (`Integration` is staging in WideWorldImportersDW).
    `integration` is now a weak word; `dimension`, `fact` and `dim` point
    to gold.
  - A schema whose name says nothing takes the layer most of its objects
    are named after (`DimCustomer`, `FactInternetSales`: gold); objects
    named like different layers in one schema are explained in the question.

### Fixed in the lineage

- A `CREATE TABLE` the parser can't read (a storage clause inside a
  constraint, a masked column) is still defined, from its column list,
  instead of vanishing and turning up as an object outside the project: 9
  tables in WideWorldImporters and WideWorldImportersDW. Listed in
  `test_lineage_config` as tables read from their column list only.
- The CTEs of an `UPDATE`, a `MERGE` or an `INSERT` are no longer taken for
  tables: the tables they read are the sources (the slowly changing
  dimensions of WideWorldImportersDW).

### Changed

- `save_lineage_config` saves the configuration in your folder by default;
  next to the `.sqlproj`, in the repository, only with `scope='repo'`. The
  repository's `lineage.yaml` still wins when it exists, and every lineage
  response says which file it used (and the one it didn't).
- `get_description_guidelines` takes `project_path`, to include the rules
  of the project. It and `get_smart_description_preview` start with the
  line naming every file the rules come from.
- The code lives in the `mcp_data_platform_toolkit` package; the server
  introduces itself to MCP clients as "Data Platform Toolkit".
- The dependencies are declared in `pyproject.toml`: `requirements.txt` and
  `requirements-dev.txt` are gone (`pip install -e ".[dev]"` from a clone).
- The README is a first screen: what the server does, a lineage diagram, a command to
  try it, and links. The reference moved to `docs/` (installation, configuration,
  tools, lineage, known limitations, development); each page is organised by what
  the reader is looking for, and the Mermaid diagram of the lineage is kept equal to
  the export by a test in both the README and `docs/lineage.md`.
- The documentation was edited for readability: `docs/configuration.md` explains
  your folder, the description rules and the lineage configuration once each
  (they were described twice); the long tool descriptions in `docs/tools.md` and
  the lineage configuration steps in `docs/lineage.md` are split into short lists.
- The README explains how the server fits with Microsoft's Power BI Modeling MCP
  Server (a short comparison, and the bridge that uses part of it) instead of
  presenting it as an alternative. Its lineage diagram drops the edge labels and
  breaks long names on two lines, so it stays readable in GitHub's column;
  `docs/lineage.md` draws the full diagram top to bottom. A test checks both
  against the export.
- The package metadata (`pyproject.toml`) points to the same repository as the
  README, `akabyte-labs/data-platform-toolkit`.
- The end-to-end lineage and the exports flag the reports they leave out
  (PBIR-Legacy, no definition) on a line of their own at the top, `[!] N
  report(s) NOT analyzed`, and the `Coverage` line counts them; before, they
  were the tail of the line of the reports analyzed. Reports of another model
  are listed apart: they are not a gap.
- The rules suggested for a table never take as its source an object that reads
  it, directly or through others.
- Comparing the names of hundreds of tables with the objects of the project is
  several times faster (`difflib` instead of a table of lengths in Python, with
  the same results): about 2 seconds instead of 10 on 660 objects.
- `elicitation_supported` answers "no" outside a request (a tool called without
  a client) instead of raising.

### Upgrading from 1.4.0

- **Client configuration**: replace the path of `python.exe` and
  `server.py` with `"command": "uvx", "args": ["--python", "3.13",
  "mcp-data-platform-toolkit"]`
  (or `-m mcp_data_platform_toolkit` with the interpreter of a clone).
- **If you edited `description_guidelines.md`** in your clone: the file is
  now the built-in rules, replaced at every update. Run
  `mcp-data-platform-toolkit --guidelines` and copy your additions into the
  file it opens.
- **A `lineage.yaml` next to a `.sqlproj`** keeps working as before. To
  make it yours instead of the repository's, move it to the file
  `--lineage-config` names.

## 1.4.0

Safer file writes, the server at the root of its repository, and a README
that shows what to ask and what the lineage looks like.

### Security

- `save_lineage_export` no longer writes anywhere on the disk when
  `MCP_ALLOWED_ROOTS` is not set: the file goes inside the folder of the
  Power BI project (where its `.pbip` is) or of the database project the
  export describes. With `MCP_ALLOWED_ROOTS` set, anywhere inside those
  folders, as before.
- The file must have the extension of its format: `.md` or `.markdown`
  for markdown, `.mmd`, `.mermaid` or `.md` for mermaid, `.csv`, `.json`.
  A name such as `run.bat` is refused.
- A new description containing ```` ``` ```` is refused (`write_smart_description`,
  the `description` of `create_measure` and `create_calculation_item`), as
  `SECURITY.md` states. Descriptions already in the model are kept as they are.

### Changed

- The server lives at the root of its repository, `powerbi-tmdl-mcp-server`:
  clone it and run `pip install -r requirements.txt` in the folder of the
  clone, no subfolder.
- The README shows example requests with the tools behind them, the lineage
  of a visual as a Mermaid diagram and as text, how the server relates to
  Microsoft's Power BI Modeling MCP Server, the supported platforms, and the
  configuration of Claude Desktop and Claude Code.

## 1.3.0

Read tools that answer without the whole TMDL, and the security,
perspectives and translations of the model.

### Added

- `search_model`: searches tables, columns, measures, hierarchies,
  calculation items, partitions, shared expressions, DAX functions and RLS
  filters by name, by what their DAX or M contains (dynamic format strings
  and the `sourceColumn` of data columns included) or by their `///`
  description. Substring or regular expression, case-insensitive, filtered
  by object type and table; one line per match, with the line that matched.
- `get_table_summary`: a table in a few lines without DAX or M: its kind,
  partitions and the source each one reads, columns with type and flags,
  measures with display folder and format string, calculation items,
  hierarchies, relationships, and how many visible elements are documented.
- `get_rls_filters`: the RLS filter of each role on each table, the columns
  and measures it reads, the user functions that make it dynamic, and the
  tables it restricts through active relationships (one side to many side,
  back only with `securityFilteringBehavior: bothDirections`).
- `find_measure_usage_in_reports` also reads the RLS filters of the local
  models behind the reports: a column only a role reads is no longer "NO
  usages found". When no local model is available, the response says the
  RLS was not checked.
- `get_perspectives`: what each perspective shows, the members that don't
  exist in the model any more, the tables in no perspective.
- `get_translations`: the `cultures` folder, read only when asked: per
  culture, how many visible objects have a translated caption; for one
  culture, every translation, the visible objects still missing a caption, and the
  translations of objects that no longer exist.

### Changed

- The TMDL parser recognizes `cultureInfo` (the keyword Power BI Desktop
  writes for a culture) and `translations`: a bare `translations` line was
  read as a boolean property.
- Relationships carry their `securityFilteringBehavior`.
- `get_ols_permissions` points to `get_rls_filters` for the RLS filter of a
  role, instead of saying it is not managed.

## 1.2.0

The end-to-end lineage: from the source columns of the database project
that feeds the semantic model, across its medallion layers and Power
Query, to the measures and the report visuals, and back.

### Added

- `get_sql_project_lineage`: upstream and downstream lineage of a database
  project (`.sqlproj`, T-SQL), for objects and columns, as edges, a tree or
  JSON. It reads:
  - views and inline functions, `INSERT ... SELECT`, `MERGE`,
    `UPDATE ... FROM`, `SELECT INTO`, `CREATE TABLE AS SELECT`;
  - stored procedures with control flow, following temporary tables and
    table variables through;
  - files read with `OPENROWSET(BULK ...)`, external tables and their
    `LOCATION`, `APPLY`, computed columns.
- `lineage.yaml`:
  - the medallion layers, in flow order;
  - the loads the SQL doesn't show, as rules (a regular expression on the
    loaded objects and a template for their source), external source
    systems included;
  - the columns created by the ETL.

  Without the file, the standard medallion applies (schemas bronze, silver,
  gold). `get_lineage_config_guide` returns the guide to write it
  (`lineage_config_guide.md`); a commented example is in
  `examples/lineage.example.yaml`.
- `get_end_to_end_lineage`, from any node (`[Measure]`, `Table[Column]`, a
  database object or column, `Report/Page/Visual`), at column or object
  level, with the coverage of the model. The chain it follows:
  - **the database project**, as in `get_sql_project_lineage`;
  - **the Power Query of every partition**: navigation, native queries
    (parsed against the project's columns), Direct Lake, other connectors,
    renames, added, duplicated, split and combined columns, groupings,
    merges, appends, parameters and query functions;
  - **the DAX of calculated columns, measures, calculation groups,
    calculated tables and field parameters**;
  - **the reports**: visuals, filters, drillthrough fields, bookmarks and
    report-level measures of the PBIR reports bound to the model.
- `test_lineage_config` checks the file against the project, so it can be
  written by trial. With `project_path` it also checks the model:
  - tables reading from a layer other than the last;
  - sources not in the project;
  - columns whose source is not traced, with the Power Query step that
    stops them;
  - native queries that can't be read;
  - visuals that reach no source.
- `suggest_lineage_rules`: rules for the tables nothing loads, inferred from
  the object names. Each one is tried on the project and returned as YAML.
  Numbered target names become one rule: `tmp.t01_customers` and
  `tmp.t02_orders`, loaded from `etl.vw_customers` and `etl.vw_orders`,
  give `tmp\.t\d{2}(?P<name>\w+)` from `etl.vw{name}`.
- `draft_lineage_config` drafts `lineage.yaml` from the database project
  and asks what the SQL can't tell, each question with a proposed answer:
  - the layer of each schema, from its name, the flows between schemas
    and, with `project_path`, the schemas the Power BI model reads;
  - the schemas to ignore, the rules for the tables nothing loads (or,
    when no name explains a table, what loads it), the ETL columns.

  Called again with the answers, it applies them and asks only what is
  still open. `save_lineage_config` (full access) saves the file next to
  the `.sqlproj` after checking it.
- `export_lineage`: Mermaid flowchart by layer, Markdown page, CSV or JSON.
  `save_lineage_export` (full access) writes the whole export to a file.
- Dependencies: `sqlglot` (major version pinned) and `PyYAML`. `--check`
  shows their versions.

### Fixed

- A line of an unfenced DAX or M expression ending with ``` and containing
  '=' (`VAR x = 1 // see ````) was read as an opening fence: every object
  after it disappeared and looking it up failed with "not found". The
  opening fence is recognized only right after the '=' of a declaration or
  of an expression property.

## 1.1.0

Fixes and groundwork from the code review that precedes the end-to-end
lineage.

### Changed

- The whole `definition` folder is loaded (relationships, shared
  expressions, DAX functions, roles, perspectives), not only `tables/`, and
  TMDL partial declarations are supported: a table split across files, or
  several tables in one file. Every element is read from, and written to,
  the file that declares it.
- A `.pbip` file identifies its own semantic model (the one named after it,
  or the one its report is bound to), even with other projects in the same
  folder. A report connected to a published model no longer borrows a
  neighbour's model.
- Qualified DAX references must exist in the model: unknown tables, columns
  and table-variable columns are reported as unresolved instead of
  becoming physical columns. Names are reported as declared.
- Properties are written only where TOM has them: the format of a
  calculation item is written as `formatStringDefinition`, display folder
  and visibility are refused for calculation items and tables. Making an
  element visible removes `isHidden`; a true boolean is written in the
  shorthand form, as Power BI Desktop does.
- Fact-table columns are protected by default, with an override
  (`allow_fact_table_columns`).
- `TmdlError` is a `ValueError`.

### Added

- `MCP_ALLOWED_ROOTS`: the folders the server may access.
- Tool failures are logged to stderr, with the stack trace when they are
  not an expected error. `--check` shows the installed `mcp` version and
  the allowed roots.
- pytest tests, among them a model written the way Power BI Desktop
  serializes it; ruff; GitHub Actions on Windows and Linux, Python 3.10
  and 3.13.

### Fixed

- A ``` line or a control character in a DAX expression, a description or
  a property value corrupted the file (every object after it disappeared)
  while the tool reported success.
- An expression starting on the declaration line and continuing below was
  read as its first line only; a '///' line ending an expression became the
  description of the next object; the first line of a new expression lost
  its indentation.
- Bridge: `displayFolder` was refused as a path; a nested failure failed the
  whole call and the successful writes were not saved; timeouts and
  transport errors lost their cause; the read-only and forbidden operation
  lists contradicted each other; result retrieval accepted any budget and
  negative offsets.
- OLS: the roles file fingerprint was taken after reading it; permissions
  were written with the names as typed; role names with ':' were refused;
  an unchanged role was rewritten.
- Output shaping cached small responses, flattened text, dropped all but the
  last of several errors, and could exceed the budget by a character.
- Reports: a namesake model in another folder counted as the same model;
  every name in a report-level measure counted as a certain measure.
- The test suite failed on Linux/macOS and on Python 3.10/3.11.

## 1.0.0

First public version.
