"""
Writing properties and new elements (measures, calculation items) into the
tables' TMDL files, only where TOM really has those properties.
"""

from __future__ import annotations

import os

from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.model_loader import (
    find_table,
    get_table_file_path,
    load_semantic_model,
    locate_element,
    table_blocks_named,
)
from mcp_data_platform_toolkit.semantic_model.tmdl_io import file_fingerprint, read_tmdl_lines, write_tmdl_lines
from mcp_data_platform_toolkit.semantic_model.tmdl_parser import ElementNotFoundError, TmdlError


# Objects that really have each property in TOM. Writing a property where it
# doesn't exist produced a file Power BI Desktop refuses to open, while the
# tool reported success: a calculation item has no isHidden, displayFolder or
# formatString (its format is the DAX expression 'formatStringDefinition'),
# and a table has no displayFolder or formatString.
PROPERTY_TARGETS = {
    "displayfolder": ("measure", "column", "hierarchy"),
    "ishidden": ("table", "measure", "column", "hierarchy"),
    "formatstring": ("measure", "column"),
}

# Boolean properties whose TOM default is false: Power BI Desktop writes them
# only when true, so "false" means removing the line, not writing ': false'.
_FALSE_BY_DEFAULT = {"ishidden"}


def dax_string_literal(text: str) -> str:
    """A DAX string literal: double quotes, an inner quote doubled."""
    return '"' + str(text).replace('"', '""') + '"'


def check_property_target(keyword: str, property_name: str) -> None:
    """Raises if the object type `keyword` has no such property."""
    allowed = PROPERTY_TARGETS.get(property_name.lower())
    if allowed is None or keyword in allowed:
        return
    hint = ""
    if keyword == "calculationItem" and property_name.lower() == "formatstring":
        hint = " Use format_measure: for a calculation item it writes 'formatStringDefinition'."
    raise TmdlError(
        f"'{property_name}' doesn't exist on a {keyword} (it is a property of: "
        f"{', '.join(allowed)}): Power BI Desktop would refuse the file.{hint}"
    )


def _apply_property(lines: list[str], block: tmdl.TmdlBlock, property_name: str,
                    value) -> tuple[list[str], bool, str]:
    """(new lines, changed, name of the property actually written)."""
    if block.keyword == "calculationItem" and property_name.lower() == "formatstring":
        new_lines, changed = tmdl.set_expression_property(
            lines, block, "formatStringDefinition", dax_string_literal(value))
        # A 'formatString:' line written on a calculation item by earlier
        # versions is invalid TMDL: it goes with the rewrite.
        refreshed = tmdl.make_block(new_lines, block.start)
        new_lines, removed = tmdl.remove_property(new_lines, refreshed, "formatString")
        return new_lines, changed or removed, "formatStringDefinition"

    check_property_target(block.keyword, property_name)
    if (property_name.lower() in _FALSE_BY_DEFAULT
            and tmdl.format_property_value(property_name, value) == "false"):
        new_lines, removed = tmdl.remove_property(lines, block, property_name)
        return new_lines, removed, property_name
    new_lines, changed = tmdl.set_property(lines, block, property_name, value)
    return new_lines, changed, property_name


def set_element_property_in_file(
    project_path: str,
    table_name: str,
    element_name: str,
    element_type: str,
    property_name: str,
    property_value,
    fallback_types: list[str] | None = None,
) -> str:
    types = [element_type] + [t for t in (fallback_types or []) if t]
    file_path, fingerprint, lines, block = locate_element(project_path, table_name, element_name, types)

    new_lines, changed, written = _apply_property(lines, block, property_name, property_value)
    if not changed:
        return f"No changes: '{written}' is already set to this value for '{element_name}'."

    write_tmdl_lines(file_path, new_lines, expected_fingerprint=fingerprint)
    return f"Updated '{written}' for '{element_name}' in '{os.path.basename(file_path)}'."


def _check_measure_name_is_free(project_path: str, host_table: str, measure_name: str) -> None:
    """
    A measure name must be unique in the WHOLE model, and must not match a
    column of the table that hosts it.

    The check used to look only at the target file: a 'Total Sales' created
    in another table was reported as "Created", the model no longer opened in
    Power BI Desktop, and the lineage attributed the dependencies of both to
    whichever table came first alphabetically.
    """
    target = str(measure_name or "").strip().lower()
    for table_name, meta in load_semantic_model(project_path).items():
        for measure in meta["measures"]:
            if measure.name.strip().lower() == target:
                raise TmdlError(
                    f"A measure named '{measure.name}' already exists in table '{table_name}': "
                    f"measure names must be unique across the whole model."
                )
        if table_name.lower() == host_table.lower():
            for column in meta["columns"]:
                if column.strip().lower() == target:
                    raise TmdlError(
                        f"Table '{table_name}' has a column named '{column}': a measure "
                        f"cannot have the same name as a column of its table."
                    )


def _creation_target(meta: dict, keyword: str) -> tuple[str, object, list[str], tmdl.TmdlBlock]:
    """
    (file_path, fingerprint, fresh lines, parent block) where a new element
    is created: the table's main declaration for a measure, the declaration
    that holds the calculation group for a calculation item. The parent is
    looked up by NAME: the first block of the file used to be taken, the
    wrong one when a file declares several tables.
    """
    parts = meta["parts"] if keyword == "calculationItem" else meta["parts"][:1]
    for part in parts:
        fingerprint = file_fingerprint(part["file_path"])
        lines = read_tmdl_lines(part["file_path"])
        for table_block in table_blocks_named(lines, meta["name"]):
            if keyword != "calculationItem":
                return part["file_path"], fingerprint, lines, table_block
            group = next((b for b in tmdl.find_all_blocks(lines)
                          if b.keyword == "calculationGroup"
                          and table_block.start < b.start < table_block.end), None)
            if group is not None:
                return part["file_path"], fingerprint, lines, group
    if keyword == "calculationItem":
        raise TmdlError(
            f"Table '{meta['name']}' does not contain a 'calculationGroup' block: "
            f"cannot create a calculationItem."
        )
    raise TmdlError(f"The declaration of table '{meta['name']}' changed on disk: repeat the operation.")


def create_tmdl_element(
    project_path: str,
    table_name: str,
    element_name: str,
    element_type: str,
    dax_expression: str,
    display_folder: str | None = None,
    format_string: str | None = None,
    description: str | None = None,
    is_hidden: bool | None = None,
) -> str:
    keyword = tmdl.normalize_element_type(element_type)
    if keyword not in ("measure", "calculationItem"):
        raise TmdlError(f"Creation is not supported for type '{element_type}'.")

    get_table_file_path(project_path, table_name)      # path validation, "not found" error
    meta = find_table(load_semantic_model(project_path), table_name)
    if meta is None:
        raise FileNotFoundError(f"Table '{table_name}' not found in the semantic model.")

    # Already declared in ANY file of the table (a table can be split across
    # files): looking only at one file let a duplicate through.
    try:
        existing_file, _fp, _lines, _block = locate_element(project_path, table_name, element_name, [keyword])
        raise TmdlError(
            f"Element '{element_name}' of type '{element_type}' already exists in "
            f"'{os.path.basename(existing_file)}'."
        )
    except ElementNotFoundError:
        pass

    if keyword == "measure":
        _check_measure_name_is_free(project_path, meta["name"], element_name)

    file_path, fingerprint, lines, parent = _creation_target(meta, keyword)

    properties: list[tuple[str, object]] = []
    expression_properties: list[tuple[str, str]] = []
    if keyword == "calculationItem":
        # A calculation item has neither displayFolder nor isHidden, and its
        # format is a DAX expression: writing them as on a measure produced a
        # file Power BI Desktop refuses.
        if str(display_folder or "").strip():
            check_property_target(keyword, "displayFolder")
        if is_hidden:
            check_property_target(keyword, "isHidden")
        if str(format_string or "").strip():
            expression_properties.append(("formatStringDefinition", dax_string_literal(format_string)))
    else:
        properties = [
            ("formatString", format_string),
            ("displayFolder", display_folder),
            # False is the default: Power BI Desktop doesn't write it.
            ("isHidden", True if is_hidden else None),
        ]

    unit = tmdl.detect_indent_unit(lines)
    indent = tmdl.child_indent(lines, parent)
    new_block = tmdl.build_element_lines(
        keyword=keyword,
        name=element_name,
        expression=dax_expression,
        indent=indent,
        unit=unit,
        properties=properties,
        doc_lines=[description] if description else None,
        expression_properties=expression_properties,
    )

    insert_at = tmdl.find_insertion_index(lines, parent, keyword)
    if insert_at > 0 and lines[insert_at - 1].strip():
        new_block = ["\n"] + new_block
    if insert_at < len(lines) and lines[insert_at].strip():
        new_block = new_block + ["\n"]

    new_lines = tmdl.insert_lines(lines, insert_at, new_block)
    write_tmdl_lines(file_path, new_lines, expected_fingerprint=fingerprint)
    return f"Created '{element_name}' ({keyword}) in '{os.path.basename(file_path)}'."
