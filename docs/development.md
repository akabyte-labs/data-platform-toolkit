# Development

[Back to the README](../README.md) · [Contributing](../CONTRIBUTING.md)

## Project structure

```
mcp_data_platform_toolkit/          the package
  __init__.py                       the version (read by the build, declared to clients)
  __main__.py                       python -m mcp_data_platform_toolkit
  server.py                         entry point (access mode, bridge, tool registration, --check)
  rules_cli.py                      the --guidelines, --lineage-config and --user-folder commands
  mcp_common/                       shared error handling, output reduction, the user's folder
                                    (user_folder.py) and its first-time choice (folder_choice.py)
  semantic_model/
    tmdl_parser.py                  TMDL blocks, properties, expressions, validation of written values
    tmdl_io.py                      file encoding, line endings, atomic writes, change detection
    project_paths.py                path confinement, allowed roots, .pbip / model / report folders
    model_loader.py                 the whole 'definition' folder, cached; where each element is declared
    relationships.py                relationships and fact tables
    dax_lexer.py, dax_lineage.py    DAX references and their lineage
    model_writer.py, autodoc.py     property and element writes, '///' descriptions
    ols_roles.py                    Object-Level Security
    rls.py                          Row-Level Security: filters, what they read, where they propagate
    model_search.py                 search by name, DAX / M, description
    table_summary.py                a table in a few lines, without DAX or M
    documentation_coverage.py       how much of the model has a '///' description, visible apart from hidden
    perspectives.py, translations.py   perspectives, and the 'cultures' folder
    power_query.py                  the M of the partitions: sources and the steps of each column
    description_guidelines.md       the built-in rules for Data Dictionary descriptions
    description_guidelines.py, description_rules.py   the built-in rules, and the user's own
    reader_tools.py, writer_tools.py   the MCP tools
  reports/                          PBIR report analysis (pbir_references.py, report_tools.py)
  sql_project/                      lineage of a SQL Server database project (no MCP dependency)
    sql_files.py                    the .sqlproj, its .sql files, encodings
    tsql_scanner.py                 GO batches, OPENROWSET, statements inside procedures, dynamic SQL
    config.py                       lineage.yaml: layers, rules, technical columns
    analyzer.py                     objects, names, object and column edges, rules, temporary tables
    project.py                      the two graphs with their layers, the checks, the cache
  lineage/
    graph.py                        generic graph: nodes, edges, navigation, rendering within the budget
    model_lineage.py                the semantic model: Power Query and Direct Lake sources, DAX
    report_lineage.py               visuals, filters, bookmarks, report-level measures
    end_to_end.py                   the three parts joined, the checks, the targets
    suggestions.py, export.py       rules inferred from the names, Mermaid / Markdown / CSV / JSON
    config_draft.py                 a first lineage.yaml drafted from the project, and its questions
    draft_notice.py, config_review.py   what marks a generated file as a draft, and the form that shows its warning
    structure.py                    the structure proposed for a project with no configuration: the form, and the save
    lineage_config_guide.md         the guide to write lineage.yaml
    lineage_tools.py                the MCP tools
  powerbi_bridge/                   optional bridge to the official Microsoft server
tests/                              pytest tests, the self-contained suite (self_test.py),
                                    manual checks of the bridge (test_bridge_*.py)
examples/                           client configuration and lineage.yaml templates
docs/                               the documentation linked from the README
  img/                              the README's lineage image and its Mermaid source
CHANGELOG.md                        changes by version
.github/
  workflows/                        CI (tests.yml) and the PyPI release (publish.yml)
  scripts/check_dist.py             checks what the wheel and the source archive carry
  ISSUE_TEMPLATE/, pull_request_template.md, dependabot.yml
```

## Testing

The tests build synthetic PBIP projects and report workspaces in temporary folders, among them a model written the way Power BI Desktop serializes it:

```bash
pip install -e ".[dev]"     # from a clone: the package, editable, with pytest and ruff
python -m pytest            # isolated tests + the self-contained suite
ruff check .                # lint (real defects only, see pyproject.toml)
```

With uv: `uv run --extra dev pytest` and `uv run --extra dev ruff check .`, on the Python of `.python-version`.

The self-contained suite also runs on its own, with only the package installed: `python tests/self_test.py`. The tests never touch your own folder (your description rules and lineage configurations): they point the settings folder and Documents to temporary folders.

GitHub Actions (`.github/workflows/tests.yml`) runs on every push and pull request:

- lint, on Linux;
- the tests on Windows and Linux, with the oldest and the newest supported Python (3.10 and 3.13);
- a package job that builds the wheel and the source archive, checks what they carry (`.github/scripts/check_dist.py`) and runs the installed command outside the repository.

Two manual scripts check the bridge against the real official executable, on Windows. pytest doesn't collect them; they are skipped or fail fast if the executable is not installed:

```bash
python tests/test_bridge_write.py                              # synthetic throwaway project
python tests/test_bridge_connection.py "C:\path\to\Project.pbip"
```

To inspect the tools interactively, use the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector python -m mcp_data_platform_toolkit
```
