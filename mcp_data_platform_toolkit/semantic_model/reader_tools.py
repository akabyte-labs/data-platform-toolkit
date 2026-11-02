"""
Read-only tools for the Power BI semantic model (TMDL).

All functions raise exceptions on error: the `tool_error_handler` decorator
turns them into MCP responses with `isError=True`.
"""

from __future__ import annotations

import os

from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.mcp_common.errors import register_tools
from mcp_data_platform_toolkit.mcp_common.output_shaping import MAX_TOOL_OUTPUT_CHARS, shape_lines
from mcp_data_platform_toolkit.semantic_model import model_search, ols_roles, perspectives, rls, translations
from mcp_data_platform_toolkit.semantic_model import description_rules
from mcp_data_platform_toolkit.semantic_model.description_guidelines import guidelines_for, with_short_guidelines
from mcp_data_platform_toolkit.mcp_common.folder_choice import FirstChoice
from mcp_data_platform_toolkit.semantic_model.description_rules import GuidelinesQuestion, settle_first_choice
from mcp_data_platform_toolkit.semantic_model.autodoc import gather_autodoc_state, prepare_autodoc_preview
from mcp_data_platform_toolkit.semantic_model.dax_lineage import build_dax_lineage, format_dax_lineage
from mcp_data_platform_toolkit.semantic_model.model_loader import (
    find_table,
    get_table_file_path,
    load_semantic_model,
    locate_element,
    read_table_lines,
)
from mcp_data_platform_toolkit.semantic_model.relationships import cardinality_symbol, is_fact_table, parse_relationships
from mcp_data_platform_toolkit.semantic_model.documentation_coverage import documentation_coverage
from mcp_data_platform_toolkit.semantic_model.table_summary import table_summary

SUPPORTED_AUTODOC_MODES = ("merge", "append", "overwrite", "skip")


def list_tables(project_path: str) -> str:
    """
    Lists all tables of the Power BI semantic model, with the name declared
    in TMDL (the one Power BI Desktop shows). When the file name differs
    (names with characters not allowed in a file name) it is shown too.
    USE THIS TOOL first to get an overview of the available tables before
    running more specific queries ('get_table_summary' for one table,
    'search_model' to find an object without knowing its table).

    Args:
        project_path: Absolute path to the .pbip file or the Power BI project folder.
    """
    # The return type is `str` and not `list[str]` on purpose: MCPServer
    # derives an output schema from the annotation and validates it with
    # pydantic. With `list[str]` the response returned by `AmbiguousModelError`
    # (a textual question, not an error) failed validation and the client
    # received "Error executing tool list_tables" instead of the question.
    names = []
    for name, meta in sorted(load_semantic_model(project_path).items(), key=lambda item: item[0].lower()):
        files = [f"{part['file_stem']}.tmdl" for part in meta["parts"]]
        if len(files) > 1:
            names.append(f"{name}  (declared in {len(files)} files: {', '.join(files)})")
        else:
            names.append(name if name == meta["file_stem"] else f"{name}  (file: {files[0]})")
    if not names:
        return "No tables found in the semantic model."
    header = f"=== TABLES ({len(names)}) ==="
    return shape_lines("\n".join([header] + names), MAX_TOOL_OUTPUT_CHARS)


def read_table_definition(project_path: str, table_name: str) -> str:
    """
    Reads the full TMDL definition of a single table, DAX and M included.
    For the list of its columns, measures and their settings use
    'get_table_summary' instead: it is many times smaller.

    Args:
        project_path: Absolute path to the .pbip file or the Power BI project folder.
        table_name: Exact name of the table to read.
    """
    file_path, lines = read_table_lines(project_path, table_name)
    meta = find_table(load_semantic_model(project_path), table_name)
    tables_in_file = sum(1 for b in tmdl.find_all_blocks(lines) if b.keyword == "table" and not b.indent)
    if meta is None or (len(meta["parts"]) == 1 and tables_in_file <= 1):
        name = os.path.splitext(os.path.basename(file_path))[0]
        text = f"--- TMDL DEFINITION OF TABLE: {name} ---\n\n{''.join(lines)}"
    else:
        # A table split across files, or sharing its file with other tables
        # (partial declarations): every declaration of THIS table, and only
        # those, each with the file it comes from.
        sections = []
        for number, part in enumerate(meta["parts"], start=1):
            block = part["table_block"]
            sections.append(
                f"--- TMDL DEFINITION OF TABLE: {meta['name']} (declaration {number} of "
                f"{len(meta['parts'])}, file '{part['file_stem']}.tmdl') ---\n\n"
                + "".join(part["lines"][block.doc_start:block.end]).rstrip() + "\n"
            )
        text = "\n".join(sections)
    return shape_lines(text, MAX_TOOL_OUTPUT_CHARS, note="for a single measure use 'get_dax_measure'")


def get_relationships(project_path: str, table_name: str = "") -> str:
    """
    Returns the relationships (active and inactive) of the Power BI semantic
    model in compact form: one line per relationship, from side first, with
    its cardinality ('*--1' many-to-one, '1--1' one-to-one, '*--*'
    many-to-many). Never returns the full dump of model.tmdl.

    Args:
        project_path: Absolute path to the .pbip file or the Power BI project folder.
        table_name: Optional filter: only show relationships involving this table.
    """
    relationships = parse_relationships(project_path)
    if not relationships:
        return "No relationships found in 'relationships.tmdl' or 'model.tmdl'."

    table_filter = (table_name or "").strip().lower()
    if table_filter:
        relationships = [
            rel for rel in relationships
            if table_filter in (rel["from_table"].lower(), rel["to_table"].lower())
        ]
        if not relationships:
            return f"No relationships found involving table '{table_name}'."

    lines = [f"=== RELATIONSHIPS ({len(relationships)}) ==="]
    for rel in relationships:
        state = "active" if rel["is_active"] else "inactive"
        lines.append(
            f"{rel['from_table']}[{rel['from_column']}] {cardinality_symbol(rel)} "
            f"{rel['to_table']}[{rel['to_column']}]"
            f" | {state} | {rel['cross_filtering']}"
        )
    return shape_lines(
        "\n".join(lines), MAX_TOOL_OUTPUT_CHARS,
        note="narrow down with 'table_name'",
    )


def get_dax_measure(project_path: str, table_name: str, measure_name: str) -> str:
    """
    Extracts the full DAX definition of a measure (declaration, multi-line
    expression, properties and associated '///' comments).

    Args:
        project_path: Absolute path to the .pbip file or the Power BI project folder.
        table_name: Name of the table that contains the measure.
        measure_name: Name of the measure to extract.
    """
    _file, _fingerprint, lines, block = locate_element(
        project_path, table_name, measure_name, ["measure", "calculationItem"])
    return shape_lines(
        "".join(lines[block.doc_start:block.end]).rstrip() + "\n",
        MAX_TOOL_OUTPUT_CHARS,
    )


def get_dax_lineage(project_path: str, target_measure: str = "") -> str:
    """
    Analyzes the semantic model lineage by mapping DAX dependencies.
    Covers measures, physical columns and calculation items of calculation groups.

    On large models the output is reduced progressively (tree, then one line
    per element, then counts, then inventory) but the name of every measure
    and calculation item is always kept. Measures that reference nothing
    (constants, BLANK(), COUNTROWS(Table)) are listed separately at the end.

    Args:
        project_path: Absolute path to the .pbip file or the Power BI project folder.
        target_measure: Optional measure or calculation item to restrict the analysis to.
    """
    # No `shape_lines` here: `format_dax_lineage` has its own reduction ladder
    # that never loses an element, whereas cutting by lines dropped the tail
    # of the list (sorted by table) with no way to tell "depends on nothing"
    # from "was past the cut".
    return format_dax_lineage(
        build_dax_lineage(project_path, target_measure or None),
        max_chars=MAX_TOOL_OUTPUT_CHARS,
    )


def get_smart_description_preview(
    project_path: str,
    table_name: str,
    element_name: str,
    base_description: str = "",
    element_type: str = "measure",
    mode: str = "merge",
    is_table: bool = False,
    choice: FirstChoice = None,
    user_guidelines: GuidelinesQuestion = None,
) -> str:
    """
    Previews the '///' description of a TMDL element: existing text, proposed
    text and the result after writing with `mode`. Also shows the element's
    technical context (DAX dependencies), which is NOT written: use it to
    understand the element before composing 'base_description'. Can be
    called with an empty 'base_description' to get that context first.
    Does not write anything to the project. The first description ever
    prepared may come back with what to do first: the user is asked, once,
    whether they have rules of their own (saved in their own folder).

    Args:
        project_path: Root path of the Power BI project / .SemanticModel.
        table_name: Name of the table that contains the element.
        element_name: Name of the element to document.
        base_description: Business description to write (may be empty for a first look).
        element_type: Element type (measure | column | dimension | calculationGroup | calculationItem | table).
        mode: overwrite = replace the existing description; merge = keep it,
            drop lines auto-generated by earlier versions, add the new text;
            append = add the new text after the existing one; skip = write
            only if there is no description yet.
        is_table: Compatibility flag (if True, sets element_type='table').
    """
    if is_table:
        element_type = "table"

    if mode not in SUPPORTED_AUTODOC_MODES:
        raise ValueError(f"Invalid mode '{mode}'. Allowed: {', '.join(SUPPORTED_AUTODOC_MODES)}.")

    get_table_file_path(project_path, table_name)  # early path validation
    # Once per user, before the first description is prepared: their own rules.
    if description_rules.needs_guidelines_question():
        stop, notice = description_rules.settle_guidelines_question(user_guidelines, choice)
        if stop:
            return stop
    else:
        notice = settle_first_choice(choice)
    state = gather_autodoc_state(project_path, table_name, element_name, element_type, base_description)

    if state["block"].keyword != "table" and tmdl.is_hidden(state["lines"], state["block"]):
        return (
            (f"{notice}\n" if notice else "")
            + f"Protected: element '{element_name}' is hidden (isHidden) "
            f"and would not be modified."
        )

    preview = prepare_autodoc_preview(
        project_path, table_name, element_name, element_type, base_description, mode, state
    )
    # Said here too, so the protection doesn't first show up as a refusal of
    # the write that follows a preview reporting "changes available". A
    # relationships file that can't be read doesn't fail the preview: the
    # write checks again, and fails closed.
    if state["block"].keyword == "column":
        try:
            fact = is_fact_table(project_path, table_name)
        except (OSError, tmdl.TmdlError):
            fact = False
        if fact:
            preview += ("\nNote: this is a column of a fact table, protected by default: "
                        "write_smart_description needs allow_fact_table_columns=True.")
    # Which rules the description must follow, and where they are: the
    # user's own are read in full by get_description_guidelines.
    rules = description_rules.rules_line(description_rules.rule_sources(project_path))
    return f"{rules}\n" + (f"{notice}\n" if notice else "") + preview


def get_ols_permissions(project_path: str, role_name: str = "") -> str:
    """
    Reads the Object-Level Security (OLS) of the security roles: which tables
    and columns are hidden (metadataPermission: none) for each role.
    Unlike the official Microsoft server (more focused on RLS), this tool
    correctly interprets both tables hidden as a whole and individual hidden
    columns nested inside 'tablePermission'. If a role also has an RLS filter
    it is flagged: 'get_rls_filters' shows it.

    Args:
        project_path: Absolute path to the .pbip file or the Power BI project folder.
        role_name: Exact name of a role to restrict the analysis to (default: all roles).
    """
    state = ols_roles.get_ols_state(project_path, role_name)
    return shape_lines(
        ols_roles.format_ols_report(state), MAX_TOOL_OUTPUT_CHARS,
        note="narrow down with 'role_name'",
    )


def search_model(
    project_path: str,
    query: str,
    search_in: list[str] | None = None,
    object_types: list[str] | None = None,
    table_name: str = "",
    regex: bool = False,
) -> str:
    """
    Searches the whole semantic model and returns one line per matching
    object, with the line that matched: far cheaper than reading the tables.
    USE THIS TOOL to find a measure or column by part of its name, every
    object whose DAX or M mentions something (a column, a function, a source
    table), or the objects whose '///' description talks about a topic.

    Searched objects: tables, columns, measures, hierarchies, calculation
    items, partitions, shared expressions and parameters, DAX functions, RLS
    filters of the roles. "expression" means: the DAX of measures, calculated
    columns, calculation items, functions and RLS filters (dynamic format
    strings included), the sourceColumn of data columns, the M or DAX source
    of partitions, the M of shared expressions.

    Args:
        project_path: Absolute path to the .pbip file or the Power BI project folder.
        query: Text to search for, case-insensitive (a substring unless `regex`).
        search_in: Where to search: any of "name", "expression", "description"
            (default: all three).
        object_types: Optional filter: any of table, column, measure, hierarchy,
            calculationItem, partition, expression, function, role (RLS filters).
        table_name: Optional: only the objects of this table.
        regex: Treat `query` as a regular expression (Python syntax).
    """
    result = model_search.search_model(project_path, query, search_in, object_types, table_name, regex)
    return shape_lines(
        model_search.format_search(result, (table_name or "").strip()), MAX_TOOL_OUTPUT_CHARS,
        note="narrow down with 'object_types', 'search_in' or 'table_name'",
    )


def get_table_summary(project_path: str, table_name: str, include_descriptions: bool = False) -> str:
    """
    One table in a few lines, without DAX or M: its kind (fact table,
    calculation group, calculated table, hidden), its partitions with the
    source each one reads, its columns (type, calculated, hidden, key,
    display folder, format, sort by), measures (display folder, format
    string, or which ones have none), calculation items, hierarchies with
    their levels, relationships, and how many visible elements have a '///'
    description. USE THIS TOOL instead of 'read_table_definition' to know
    what a table contains; read the DAX of one measure with 'get_dax_measure'.

    Args:
        project_path: Absolute path to the .pbip file or the Power BI project folder.
        table_name: Name of the table.
        include_descriptions: Also show the first 100 characters of each
            element's '///' description.
    """
    return shape_lines(
        table_summary(project_path, table_name, include_descriptions), MAX_TOOL_OUTPUT_CHARS,
        note="call again without 'include_descriptions'",
    )


def get_documentation_coverage(project_path: str, table_name: str = "") -> str:
    """
    How much of the semantic model has a '///' description, exactly: measures,
    columns, and tables / hierarchies / calculation items, each as documented
    out of total, VISIBLE elements apart from HIDDEN ones (the columns of a
    hidden fact table are read by measures, not by people, so they are not
    what a Data Dictionary has to explain). Then the tables with the most
    visible elements still undocumented. USE THIS TOOL for any question like
    "how documented is the model?", "what is missing?", instead of estimating
    from a few tables or listing the model by hand. With 'table_name' it lists
    the names of that table's visible elements without a description.

    Args:
        project_path: Absolute path to the .pbip file or the Power BI project folder.
        table_name: Optional table: the names of its undocumented visible elements.
    """
    return shape_lines(documentation_coverage(project_path, table_name), MAX_TOOL_OUTPUT_CHARS,
                       note="ask for one table with 'table_name'")


def get_rls_filters(project_path: str, role_name: str = "", table_name: str = "") -> str:
    """
    Reads the Row-Level Security of the security roles: the DAX filter of
    each role on each table, the columns and measures it reads (resolved
    against the model, unresolved references flagged), whether it depends on
    the user (USERPRINCIPALNAME, USERNAME, CUSTOMDATA...), and the other
    tables it restricts through the relationships: active ones, from the
    one side to the many side, and back only where the relationship applies
    the security filter in both directions (securityFilteringBehavior).
    Tables and columns hidden per role (OLS) are in 'get_ols_permissions'.

    Args:
        project_path: Absolute path to the .pbip file or the Power BI project folder.
        role_name: Exact name of a role to restrict the analysis to (default: all roles).
        table_name: Optional: only the filters on this table or reaching it
            through relationships ("which roles restrict table X?").
    """
    return shape_lines(
        rls.format_rls_report(rls.load_role_security(project_path), role_name, table_name),
        MAX_TOOL_OUTPUT_CHARS, note="narrow down with 'role_name' or 'table_name'",
    )


def get_perspectives(project_path: str, perspective_name: str = "") -> str:
    """
    Lists the perspectives of the model and what each one shows: per table,
    the measures, columns and hierarchies it includes, or the whole table
    (includeAll). Members that don't exist in the model any more (renamed or
    deleted objects) are flagged. Without 'perspective_name' it also lists
    the tables that are in no perspective.

    Args:
        project_path: Absolute path to the .pbip file or the Power BI project folder.
        perspective_name: Exact name of one perspective (default: all).
    """
    state = perspectives.perspective_state(project_path, perspective_name)
    all_tables = list(load_semantic_model(project_path))
    return shape_lines(
        perspectives.format_perspectives(state, all_tables, filtered=bool(perspective_name)),
        MAX_TOOL_OUTPUT_CHARS, note="narrow down with 'perspective_name'",
    )


def get_translations(project_path: str, culture: str = "", table_name: str = "") -> str:
    """
    Reads the translations of the model (the 'cultures' folder): without
    'culture', each language with how many visible objects (tables, columns,
    measures, hierarchies) have a translated caption, plus translated
    descriptions and display folders; with 'culture', every translation of
    that language, the visible objects still without a caption, and the
    translations of objects that no longer exist. Use it for a Data
    Dictionary in several languages or to check a translation is complete.

    Args:
        project_path: Absolute path to the .pbip file or the Power BI project folder.
        culture: Culture name, e.g. 'en-GB' (default: the summary of all cultures).
        table_name: With 'culture', only this table.
    """
    return shape_lines(
        translations.translations_report(project_path, culture, table_name),
        MAX_TOOL_OUTPUT_CHARS, note="narrow down with 'table_name'",
    )


def get_description_guidelines(project_path: str = "", choice: FirstChoice = None) -> str:
    """
    Returns the full guidelines (with examples) for writing descriptions meant
    for a business Data Dictionary, understandable by business users: the
    built-in rules followed by the user's own, for every project and for
    this one, each under the file it comes from. Where they disagree the
    more specific one wins.
    USE THIS TOOL ONCE at the start of a documentation session, before
    composing the 'base_description' values to pass to
    'get_smart_description_preview' / 'write_smart_description'.
    There is no need to call it again for each element.

    The first line names the files of the user's rules: tell the user where
    they are when they want to change them. The first time, the server may
    ask the user where to keep their files; any message about it is in the
    second line, pass it on.

    Args:
        project_path: The .pbip file or folder of the project being
            documented, to include its rules. Without it, only the built-in
            rules and the user's general ones.
    """
    return guidelines_for(project_path, settle_first_choice(choice))


def register_reader_tools(mcp):
    """Registers the read-only tools on the MCP server."""
    register_tools(mcp, {
        "list_tables": list_tables,
        "get_table_summary": get_table_summary,
        "get_documentation_coverage": get_documentation_coverage,
        "search_model": search_model,
        "read_table_definition": read_table_definition,
        "get_relationships": get_relationships,
        "get_dax_measure": get_dax_measure,
        "get_dax_lineage": get_dax_lineage,
        "get_ols_permissions": get_ols_permissions,
        "get_rls_filters": get_rls_filters,
        "get_perspectives": get_perspectives,
        "get_translations": get_translations,
        "get_description_guidelines": get_description_guidelines,
        "get_smart_description_preview": get_smart_description_preview,
    }, descriptions={
        "get_smart_description_preview": with_short_guidelines(get_smart_description_preview.__doc__),
    })
