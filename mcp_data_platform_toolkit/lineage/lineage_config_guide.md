# How to write lineage.yaml

The lineage reads the SQL of a database project and finds by itself where every table, view and column comes from. `lineage.yaml` adds the two things the SQL can't say:

1. **The medallion layers**: which schemas form each layer, in flow order.
2. **The loads the SQL doesn't show**: a pipeline or a generic procedure fills a table from another object, and no statement in the project says so.

Everything else needs no configuration.

## Where it goes

- **Yours**, by default: `projects/<repository>/<database project>.lineage.yaml` in your own folder (`%APPDATA%\data-platform-toolkit` on Windows, `~/.config/data-platform-toolkit` elsewhere, or the folder you chose), outside the repository. `mcp-data-platform-toolkit --lineage-config <database project>` opens it.
- **The team's**, when you want to share it: `lineage.yaml` (or `lineage.yml`) next to the `.sqlproj`, under version control with the SQL. When it exists it wins over yours.
- The tools take `config_path` for a file kept somewhere else. Every lineage response says which file it used.
- **Without a file**, the standard medallion applies: the schemas `bronze`, `silver` and `gold` are the three layers, and there are no rules.

## Start from a draft

`draft_lineage_config` writes a first version of the file from the project itself, and returns the questions only the user can answer:

- **Layers:** each schema is placed by its name (`raw`, `staging` -> bronze; `curated`, `clean` -> silver; `mart`, `reporting` -> gold), by the flows between schemas in the SQL and, with `project_path`, by what the Power BI model reads (the last layer). A name that says nothing, a weak one (`dwh`, `integration`, `pbi`) or one the flows contradict becomes a question. When the name says nothing, the names of its objects count (`DimCustomer`, `FactSales`: gold). Flows going both ways between two schemas are told apart: the direction that only UPDATEs existing rows and moves fewer columns is lookups (staging taking the keys of a dimension), not where the data comes from; `test_lineage_config` lists them apart from the flows going backwards.
- **Ignored schemas:** the ones no data flows through, or named as configuration, logging or tests (`cfg`, `audit`, `test`).
- **Rules:** proposed for the tables nothing loads, and for the views that only read files a pipeline writes (a Parquet exported from `etl.vw_fact_x` and read by `dwh.fact_x`), as `suggest_lineage_rules` does; each one is a yes/no question. A table no name explains is asked directly: what loads it.
- **Technical columns:** the columns no source fills that look created by the ETL (`etl_...`, `..._sk`, load dates, run ids, hashes).

Each question has a key and a proposed answer. Ask them all together, then draft again with **every** answer given so far:

| Key | Answer |
|---|---|
| `schema.<schema>` | a layer (`bronze`, `silver`, `gold` or a new name) or `ignore` |
| `layer_order` | the layers in flow order, e.g. `bronze, silver, gold, semantic` |
| `rule.<name>` | `yes` or `no` |
| `load.<schema.table>` | its source `schema.object`, `external:<system.table>`, or `none` |
| `technical_columns` | `yes`, `no`, or the regular expressions to keep |
| `default_schema` | the schema of names written without one (by default the `.sqlproj` one, or `dbo`) |

Unanswered questions keep their proposed answer, so the draft is always a complete file. The lineage tools start it by themselves when a project has no configuration and the standard layers place less than half of its objects: they show the draft to the user and save it, as the user's own, only when the user says so; until then it is provisional. When nothing is open, `save_lineage_config` (full access) saves it, after checking it: in your folder, or next to the `.sqlproj` with `scope='repo'`; then run `test_lineage_config`. The steps below explain each section, to review the draft or change it by hand.

**A draft says `draft: true`.** Every lineage answer then says the configuration is a draft, not checked yet. Keep the line when you change the file: only the user removes it (or sets it to `false`), once they have checked the layers, the ignored schemas and the rules.

## What the lineage finds by itself

It finds these without any configuration:

- **Definitions:** views, inline table functions, `CREATE TABLE ... AS SELECT`, and external tables (`CREATE EXTERNAL TABLE ... AS SELECT`) together with the files at their `LOCATION`.
- **Loads:** `INSERT ... SELECT`, `MERGE`, `UPDATE ... FROM` and `SELECT ... INTO`, also inside stored procedures. Temporary tables and table variables are followed through.
- **Files read with `OPENROWSET(BULK ...)`**, with the columns of their `WITH (...)` clause.
- **Computed columns.**

It reports what it can't read, and never guesses:

- dynamic SQL (`EXEC(@sql)`, `sp_executesql`);
- `INSERT ... EXEC`;
- triggers;
- multi-statement functions;
- names without schema that match several objects.

## Step by step

Call `test_lineage_config` after every step: it says what is still missing.

### 1. Assign the schemas to the layers

```yaml
layers:
  bronze: raw
  silver: [clean, curated]
  gold: mart
```

- **Order matters:** list the layers in the direction the data flows. The first one is the closest to the sources.
- **Recognizing the layers:**
  - **bronze** holds the data as it arrives from the sources;
  - **silver** holds it cleaned, conformed and integrated;
  - **gold** holds it ready to be consumed by reports and models.
- **Names:** layer names are free. Three layers are the standard, but a project can have more, for example a `semantic` layer after `gold`.
- **One schema, one layer:** a schema belongs to a single layer.
- **Leftovers:** with no file, `test_lineage_config` lists every schema that is in no layer.

### 2. Ignore the schemas that hold no data flow

```yaml
ignore: [config, audit, test]
```

Objects in these schemas are left out of the lineage: configuration, logging, test objects.

### 3. Describe the loads the SQL doesn't show

`test_lineage_config` lists the **tables nothing loads**: tables after the first layer that no statement of the project writes. Each one is loaded by something outside the SQL. Describe it with a rule. `suggest_lineage_rules` proposes them from the object names, already tried on the project: start from its proposals and keep the right ones.

```yaml
rules:
  - name: history-tables
    match: 'curated\.(?P<entity>\w+)_history'
    from: 'curated.{entity}'
```

The rule reads: *every object whose `schema.name` matches `match` is loaded from the object in `from`*.

- **`match`** is a regular expression on the whole name `schema.object`. It doesn't need `^` or `$`, and upper and lower case don't matter.
- **`from`** is the name of the source, built with the named groups of `match` written as `{group}`. `{schema}` and `{name}` (the parts of the matched name) are always available.
- **Columns** are linked by name. If the target's columns are unknown (a `SELECT *` over files), it takes the source's columns.
- **Order:** the first rule that matches an object wins.
- **A source not found** in the project is reported, never created.

**Columns with a different name:**

```yaml
  - name: history-tables
    match: 'curated\.(?P<entity>\w+)_history'
    from: 'curated.{entity}'
    columns:
      ValidFrom: LoadedAt        # target column: source column
```

**Source systems.** When the name of a bronze table says which system and table it comes from, an external rule adds the source system as the start of the lineage:

```yaml
  - name: source-systems
    match: 'raw\.(?P<system>[a-z]+)_(?P<table>\w+)'
    from: '{system}.{table}'
    external: true
```

`raw.erp_customers` then comes from `erp.customers`, in the `source` layer. Without such a rule, the lineage starts at the bronze layer.

### 4. Mark the columns the ETL creates

```yaml
technical_columns: ['etl_\w+', '\w+_sk', 'row_hash']
```

These are surrogate keys, hashes, load dates and validity dates. They come from no source column: listing them tells the check they are expected, so they are not reported.

### 5. Check again

Run `test_lineage_config` until the items marked `[!]` are only the expected ones. With `project_path` it checks the Power BI model too, for example a model table that reads from a layer other than the last one. The items to look at:

- **a layer with no objects:** a schema name is wrong;
- **schemas in no layer:** add them to a layer or to `ignore`;
- **a rule that matches nothing:** its `match` doesn't cover the whole name, or a dot isn't escaped;
- **a source not in the project:** the template in `from` builds a name that doesn't exist;
- **tables nothing loads:** a rule is missing;
- **flows going backwards:** a later layer feeds an earlier one, so a schema is probably in the wrong layer.

## Regular expressions in short

| Write | To match |
|---|---|
| `\.` | the dot between schema and object (a bare `.` matches any character) |
| `\w+` | a name: letters, digits, `_` |
| `[a-z]+` | letters only |
| `\d{2}` | exactly two digits |
| `(?P<entity>\w+)` | a part of the name to reuse in `from` as `{entity}` |

- **Alternatives:** `(history|archive)` matches either word.
- **Quoting:** write regular expressions between single quotes in YAML (`'...'`), so backslashes need no doubling.
- **Be precise:** a pattern that is too broad matches objects it shouldn't. `curated\.\w+` would make *every* object of `curated` loaded from somewhere. Keep the fixed parts of the name in the pattern: prefixes, suffixes, digits.

## Reference

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | number | `1` | Format of the file. |
| `draft` | true/false | `false` | `true` in a generated file until the user checked it: every lineage answer says so. |
| `layers` | mapping layer -> schema or list of schemas | `bronze`, `silver`, `gold` | The layers in flow order. `source` is reserved. |
| `ignore` | list of schemas | none | Schemas left out of the lineage. |
| `default_schema` | schema | `dbo` | The schema of names written without one. |
| `rules` | list | none | Loads the SQL doesn't show. |
| `rules[].name` | text | required | Shown on every edge the rule creates. |
| `rules[].match` | regular expression | required | The objects loaded this way (whole `schema.object`). |
| `rules[].from` | template | required | Their source: `schema.object`, or any name with `external: true`. |
| `rules[].columns` | mapping | none | Target column -> source column, when the names differ. |
| `rules[].external` | true/false | `false` | The source is a system outside the project. |
| `technical_columns` | list of regular expressions | none | Columns created by the ETL (whole column name). |

## Complete examples

**The standard medallion, all loads in SQL.** No file at all, or:

```yaml
layers:
  bronze: bronze
  silver: silver
  gold: gold
```

**Four layers, technical schemas and ETL columns:**

```yaml
layers:
  bronze: landing
  silver: [clean, integration]
  gold: mart
  semantic: reporting
ignore: [config, audit]
technical_columns: ['dw_\w+', '\w+_key']
```

**Loads outside the SQL:**

```yaml
layers:
  bronze: raw
  silver: [clean, curated]
  gold: mart
rules:
  # A generic procedure keeps a history copy of each curated table.
  - name: history-tables
    match: 'curated\.(?P<entity>\w+)_history'
    from: 'curated.{entity}'
    columns:
      ValidFrom: LoadedAt
  # A pipeline copies each clean table into curated with a 'c_' prefix.
  - name: pipeline-copies
    match: 'curated\.c_(?P<entity>\w+)'
    from: 'clean.{entity}'
  # Bronze tables are named after their source system.
  - name: source-systems
    match: 'raw\.(?P<system>[a-z]+)_(?P<table>\w+)'
    from: '{system}.{table}'
    external: true
```
