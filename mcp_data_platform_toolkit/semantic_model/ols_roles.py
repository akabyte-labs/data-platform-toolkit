"""
Object-Level Security (OLS) of the semantic model's security roles.

The official Microsoft server (bridge in `powerbi_bridge`) is far more
reliable on Row-Level Security (`tablePermission` with a filter expression)
than on Object-Level Security: tables/columns hidden per role are expressed
in TMDL with the `metadataPermission: none` property, nested inside
`tablePermission` (for the whole table) or inside `columnPermission` (for a
single column). These two tools read and write exactly that structure,
without touching any existing RLS filter.

For a column, metadataPermission is the DEFAULT property of
`columnPermission`, so TMDL accepts two equivalent forms, both handled:
  - inline:   `columnPermission Address = none` (also multi-line or fenced);
  - explicit: `columnPermission Address` + `metadataPermission: none`.
New column permissions follow the form the file already uses.

Supported file layouts (detected automatically):
  - 'definition/roles.tmdl'      -> single file with all the 'role' blocks;
  - 'definition/roles/*.tmdl'    -> one file per role (the more recent layout).
If neither exists, writing creates 'definition/roles.tmdl'.
"""

from __future__ import annotations

import os

from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.tmdl_parser import ElementNotFoundError, TmdlBlock
from mcp_data_platform_toolkit.semantic_model.model_loader import find_table, get_table_file_path, load_semantic_model
from mcp_data_platform_toolkit.semantic_model.project_paths import (
    PathNotAllowedError,
    confined_path,
    find_definition_folder,
)
from mcp_data_platform_toolkit.semantic_model.tmdl_io import file_fingerprint, read_tmdl_lines, write_tmdl_lines

MODEL_PERMISSIONS = {"none", "read", "readrefresh", "refresh", "restricted", "administrator"}


# ---------------------------------------------------------------------------
# Locating the role files
# ---------------------------------------------------------------------------

def _single_roles_file(project_path: str) -> str:
    return os.path.join(find_definition_folder(project_path), "roles.tmdl")


def _roles_folder(project_path: str) -> str:
    return os.path.join(find_definition_folder(project_path), "roles")


def list_role_files(project_path: str) -> list[str]:
    """All the .tmdl files that may contain 'role' blocks, whatever the layout."""
    paths = []
    single_file = _single_roles_file(project_path)
    if os.path.isfile(single_file):
        paths.append(single_file)
    folder = _roles_folder(project_path)
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith(".tmdl"):
                paths.append(os.path.join(folder, name))
    return paths


def check_role_name(role_name: str) -> str:
    """
    A role name that can't act as a path. Separators, NUL and '.'/'..' are
    refused in every layout: with 'definition/roles/<name>.tmdl' a name such
    as '..\\..\\x' wrote outside the project. A colon is allowed ('Sales:
    EMEA' is a valid role name): it used to be refused with the separators,
    and in the folder layout it only needs a safe file name, see
    `_role_file_stem`.
    """
    text = str(role_name or "").strip()
    if not text:
        raise ValueError("The role name cannot be empty.")
    if "/" in text or "\\" in text or "\x00" in text or text in (".", ".."):
        raise PathNotAllowedError(
            f"The role name '{text}' contains a path separator or is not a valid name."
        )
    return text


_WINDOWS_RESERVED_STEMS = {"con", "prn", "aux", "nul"} | {
    f"{device}{number}" for device in ("com", "lpt") for number in range(1, 10)
}


def _role_file_stem(role_name: str) -> str:
    """
    File name (without extension) for a new role file: the role name with the
    characters Windows doesn't allow in a file name replaced by '_'. A colon
    in particular must never reach a path: 'C:x' is a drive-relative path,
    and 'a:b' on NTFS writes an alternate data stream of the file 'a'.
    """
    stem = "".join("_" if char in '<>:"|?*' or ord(char) < 32 else char for char in role_name)
    stem = stem.rstrip(" .") or "_"
    if stem.lower() in _WINDOWS_RESERVED_STEMS:
        stem = f"_{stem}"
    return stem


def _path_for_new_role(project_path: str, role_name: str) -> str:
    """
    Where to create a role that doesn't exist yet, following the layout in use.

    The role name used to end up directly in a file name. With a `role_name`
    such as '..\\..\\..\\Users\\Public\\payload' the server wrote outside the
    project, and since `write_tmdl_lines` calls `os.makedirs`, it even CREATED
    the folder tree: not an overwrite, an arbitrary write. The name is
    therefore validated, turned into a safe file name and the resulting path
    checked again after normalization. A file that already exists (another
    role whose name maps to the same file name) is never reused.
    """
    if os.path.isfile(_single_roles_file(project_path)):
        return _single_roles_file(project_path)
    folder = _roles_folder(project_path)
    if os.path.isdir(folder):
        stem = _role_file_stem(check_role_name(role_name))
        candidate = os.path.join(folder, f"{stem}.tmdl")
        counter = 2
        while os.path.exists(candidate):
            candidate = os.path.join(folder, f"{stem} ({counter}).tmdl")
            counter += 1
        return confined_path(folder, candidate, f"The role '{role_name}'")
    return _single_roles_file(project_path)


def find_role(project_path: str, role_name: str) -> tuple[str, list[str], TmdlBlock] | None:
    """(file_path, lines, role_block) of the role, or None if it isn't in any file."""
    found = _find_role_for_writing(project_path, role_name)
    return None if found is None else (found[0], found[2], found[3])


def _find_role_for_writing(project_path: str, role_name: str):
    """
    (file_path, fingerprint, lines, role_block) of the role, or None.

    The fingerprint is taken BEFORE reading each file. It used to be taken
    after `find_role` had read it: a change landing between the two reads
    was baked into the fingerprint, so the stale lines were written over it
    without the "changed after it was read" refusal.
    """
    target = (role_name or "").strip().lower()
    for file_path in list_role_files(project_path):
        fingerprint = file_fingerprint(file_path)
        lines = read_tmdl_lines(file_path)
        for block in tmdl.find_all_blocks(lines):
            if block.keyword == "role" and block.name.strip().lower() == target:
                return file_path, fingerprint, lines, block
    return None


def _load_roles(project_path: str) -> dict[str, tuple[str, list[str], TmdlBlock]]:
    """
    {lowercase_name: (file, lines, block)} of all roles, in a single pass.

    `get_ols_state` used to call `list_role_names` (which reads every file)
    and then one `find_role` per role, which read them all again: O(n^2)
    reads on the number of roles.
    """
    roles: dict[str, tuple[str, list[str], TmdlBlock]] = {}
    for file_path in list_role_files(project_path):
        lines = read_tmdl_lines(file_path)
        for block in tmdl.find_all_blocks(lines):
            if block.keyword == "role" and block.name:
                roles.setdefault(block.name.strip().lower(), (file_path, lines, block))
    return roles


def list_role_names(project_path: str) -> list[str]:
    names: set[str] = set()
    for file_path in list_role_files(project_path):
        lines = read_tmdl_lines(file_path)
        names.update(b.name for b in tmdl.find_all_blocks(lines) if b.keyword == "role")
    return sorted(names)


# ---------------------------------------------------------------------------
# Reading OLS
# ---------------------------------------------------------------------------

def _direct_properties(lines: list[str], block: TmdlBlock) -> dict[str, str | None]:
    return {
        (child.name or "").lower(): child.value
        for child in tmdl.iter_children(lines, block)
        if child.kind == "property"
    }


def _explicit_permission(lines: list[str], block: TmdlBlock) -> str | None:
    """Value of the `metadataPermission:` property line, lowercase; None if absent."""
    child = tmdl.get_property(lines, block, "metadataPermission")
    return None if child is None else tmdl.normalize_text_value(child.value).lower()


def _inline_permission(lines: list[str], block: TmdlBlock) -> str | None:
    """
    metadataPermission written as the DEFAULT property of a columnPermission
    (`columnPermission Address = none`, also multi-line or fenced), lowercase;
    None if the block doesn't use that form.

    Only for columnPermission: the default property of a tablePermission is
    the RLS filter, and reading it as a permission would turn
    `tablePermission X = [Code] = "none"` into a hidden table.
    """
    if block.keyword != "columnPermission" or block.inline_expression is None:
        return None
    return tmdl.normalize_text_value(tmdl.get_expression(lines, block)).lower()


def _metadata_permission(lines: list[str], block: TmdlBlock) -> str:
    """
    Effective metadataPermission of a tablePermission/columnPermission,
    lowercase ('' if not set), whichever of the two TMDL forms is used.

    Only the explicit `metadataPermission: none` form used to be read: a
    column written as `columnPermission Address = none` (equally valid TMDL,
    since metadataPermission is the default property of columnPermission)
    was reported as visible. If a block declares the permission in BOTH forms
    (invalid, but produced by earlier versions of this server) 'none' wins:
    a restriction must never be reported as missing.
    """
    values = [v for v in (_inline_permission(lines, block), _explicit_permission(lines, block)) if v]
    if "none" in values:
        return "none"
    return values[0] if values else ""


def _declares_permission_twice(lines: list[str], block: TmdlBlock) -> bool:
    return (_inline_permission(lines, block) is not None
            and _explicit_permission(lines, block) is not None)


def _analyze_role_ols(lines: list[str], role_block: TmdlBlock) -> dict:
    props = _direct_properties(lines, role_block)
    model_permission = tmdl.normalize_text_value(props.get("modelpermission")) or None

    tables = []
    for tp in tmdl.child_blocks(lines, role_block, "tablePermission"):
        hidden_table = _metadata_permission(lines, tp) == "none"
        has_row_filter = bool(tmdl.get_expression(lines, tp).strip())

        hidden_columns = []
        ambiguous_columns = []
        for cp in tmdl.child_blocks(lines, tp, "columnPermission"):
            if _metadata_permission(lines, cp) == "none":
                hidden_columns.append(cp.name)
            if _declares_permission_twice(lines, cp):
                ambiguous_columns.append(cp.name)

        tables.append({
            "table": tp.name,
            "hidden_table": hidden_table,
            "has_row_filter": has_row_filter,
            "hidden_columns": sorted(hidden_columns),
            "ambiguous_columns": sorted(ambiguous_columns),
        })

    return {"model_permission": model_permission, "tables": tables}


def get_ols_state(project_path: str, role_name: str = "") -> dict:
    """
    Structured OLS state of one or all roles.
    {"roles": [{"name", "model_permission", "tables": [...]}]}
    """
    loaded = _load_roles(project_path)
    keys = sorted(loaded)
    if role_name:
        target = role_name.strip().lower()
        keys = [k for k in keys if k == target]
        if not keys:
            raise ElementNotFoundError(f"Role '{role_name}' not found in the model.")

    roles = []
    for key in keys:
        _file_path, lines, block = loaded[key]
        state = _analyze_role_ols(lines, block)
        roles.append({"name": block.name, **state})
    return {"roles": roles}


def format_ols_report(state: dict) -> str:
    if not state["roles"]:
        return "No security roles defined in the model."

    lines = ["=== OBJECT-LEVEL SECURITY (OLS) BY ROLE ==="]
    for role in state["roles"]:
        header = f"\nRole: {role['name']}"
        if role["model_permission"]:
            header += f"  (modelPermission: {role['model_permission']})"
        lines.append(header)

        if not role["tables"]:
            lines.append("  (no table/column level restrictions)")
            continue

        for table in role["tables"]:
            if table["hidden_table"]:
                lines.append(f"  Hidden table (OLS): {table['table']}")
                continue
            details = []
            if table["hidden_columns"]:
                details.append("hidden columns (OLS): " + ", ".join(table["hidden_columns"]))
            if table["has_row_filter"]:
                details.append("also has an RLS filter (see get_rls_filters)")
            if table.get("ambiguous_columns"):
                details.append(
                    "metadataPermission declared twice (invalid TMDL, set_ols_permission "
                    "repairs it): " + ", ".join(table["ambiguous_columns"])
                )
            if details:
                lines.append(f"  Table {table['table']}: " + "; ".join(details))
            else:
                lines.append(f"  Table {table['table']}: no OLS restrictions")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Writing OLS
# ---------------------------------------------------------------------------

def _validate_model_permission(value: str | None) -> None:
    if value is not None and value.strip().lower() not in MODEL_PERMISSIONS:
        allowed = ", ".join(sorted(MODEL_PERMISSIONS))
        raise ValueError(f"Invalid modelPermission: '{value}'. Allowed: {allowed}.")


def _create_role(project_path: str, role_name: str, model_permission: str | None):
    """(file_path, fingerprint, lines, role_block) with the new role added (not written yet)."""
    file_path = _path_for_new_role(project_path, role_name)
    fingerprint = file_fingerprint(file_path)          # before reading, see `_find_role_for_writing`
    lines = read_tmdl_lines(file_path) if os.path.isfile(file_path) else []
    unit = tmdl.detect_indent_unit(lines) if lines else "\t"

    new_block = tmdl.build_simple_element_lines(
        keyword="role", name=role_name, indent="", unit=unit,
        properties=[("modelPermission", model_permission or "read")],
    )
    if lines and lines[-1].strip():
        if not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        new_block = ["\n"] + new_block
    lines = lines + new_block

    role_block = tmdl.find_block(lines, role_name, "role")
    return file_path, fingerprint, lines, role_block


def _role_table_permission(lines: list[str], role_block: TmdlBlock, table_name: str) -> TmdlBlock | None:
    target = table_name.strip().lower()
    for tp in tmdl.child_blocks(lines, role_block, "tablePermission"):
        if tp.name.strip().lower() == target:
            return tp
    return None


def _table_column_permission(lines: list[str], tp_block: TmdlBlock, column_name: str) -> TmdlBlock | None:
    target = column_name.strip().lower()
    for cp in tmdl.child_blocks(lines, tp_block, "columnPermission"):
        if cp.name.strip().lower() == target:
            return cp
    return None


def _unchanged_ols_message(subject: str, role_name: str, role_created: bool,
                           permission_changed: bool) -> str:
    """
    Message for the branches where the subject's OLS doesn't change. The base
    text stays the same, but if the role was created or its modelPermission
    updated in the same call it says so: that change used to vanish silently
    together with the file that was never written.
    """
    text = f"No changes: {subject} is already visible by default for role '{role_name}'."
    if role_created:
        return text + f" Role '{role_name}' was created."
    if permission_changed:
        return text + " The role's modelPermission was updated."
    return text


def set_ols_permission(
    project_path: str,
    role_name: str,
    table_name: str = "",
    column_name: str = "",
    hidden: bool = True,
    model_permission: str | None = None,
    create_role_if_missing: bool = True,
) -> str:
    """Applies the OLS write logic. See the `set_ols_permission` tool for details."""
    role_name = (role_name or "").strip()
    table_name = (table_name or "").strip()
    column_name = (column_name or "").strip()
    # Validated HERE and not only where it becomes a file name: with the
    # 'definition/roles/<name>.tmdl' layout the check fired, with the single
    # 'roles.tmdl' layout it didn't. A defense that depends on the project's
    # layout is a defense that sooner or later isn't there.
    check_role_name(role_name)
    if column_name and not table_name:
        raise ValueError("To hide/show a column 'table_name' must be provided as well.")

    _validate_model_permission(model_permission)

    if table_name:
        # Raises if the table or the column doesn't exist. The permission is
        # written with the names AS DECLARED: 'sales' / 'cost' used to be
        # written as typed by the caller, next to a table declared 'Sales'.
        get_table_file_path(project_path, table_name)      # path validation, "not found" error
        meta = find_table(load_semantic_model(project_path), table_name)
        if meta is None:
            raise FileNotFoundError(f"Table '{table_name}' not found in the semantic model.")
        table_name = meta["name"]
        if column_name:
            declared = next((c for c in meta["columns"] if c.strip().lower() == column_name.lower()), None)
            if declared is None:
                raise ElementNotFoundError(
                    f"No declaration found for column '{column_name}' in table '{table_name}'.")
            column_name = declared

    found = _find_role_for_writing(project_path, role_name)
    permission_changed = False
    if found is None:
        if not create_role_if_missing:
            raise ElementNotFoundError(
                f"Role '{role_name}' not found. Call again with create_role_if_missing=True to create it."
            )
        file_path, fingerprint, lines, role_block = _create_role(project_path, role_name, model_permission)
        role_created = True
    else:
        file_path, fingerprint, lines, role_block = found
        role_created = False
        if model_permission is not None:
            lines, permission_changed = tmdl.set_property(
                lines, role_block, "modelPermission", model_permission)
            if permission_changed:
                role_block = tmdl.find_block(lines, role_name, "role")

    # `lines` may already contain a change (role created, or modelPermission
    # updated on an existing role) even when the OLS part changes nothing.
    # Without this flag the "no changes" branches below returned without
    # writing, throwing away the new modelPermission.
    role_needs_saving = role_created or permission_changed

    if not table_name:
        # Only the role itself: the file is written only if it changed. An
        # existing role with nothing to change used to be rewritten as it was
        # and reported as "updated".
        if role_created:
            message = f"Role '{role_name}' created in '{os.path.basename(file_path)}'."
        elif permission_changed:
            message = f"Role '{role_name}' updated in '{os.path.basename(file_path)}'."
        else:
            message = f"No changes: role '{role_name}' already exists with these settings."
        ols_changed = False
    else:
        subject = f"'{column_name}' ({table_name})" if column_name else f"'{table_name}'"
        lines, message, ols_changed = _apply_ols(
            lines, role_name, role_block, table_name, column_name, hidden,
            subject, role_created, permission_changed,
        )

    # SINGLE write point of the function. There used to be ten calls to
    # write_tmdl_lines, one per branch, and it was enough for one branch to
    # forget it to silently lose the changes already applied to `lines`: that
    # is exactly how the modelPermission bug was born.
    if ols_changed or role_needs_saving:
        write_tmdl_lines(file_path, lines, expected_fingerprint=fingerprint)
    return message


def _uses_inline_column_permissions(lines: list[str]) -> bool:
    """
    True if the file's columnPermissions mostly use the inline form
    (`columnPermission X = none`): new ones follow the style already in use,
    so a file serialized by Power BI Desktop doesn't end up with mixed forms.
    """
    inline = explicit = 0
    for block in tmdl.find_all_blocks(lines):
        if block.keyword != "columnPermission":
            continue
        if block.inline_expression is not None:
            inline += 1
        elif _explicit_permission(lines, block) is not None:
            explicit += 1
    return inline > explicit


def _column_permission_lines(lines: list[str], column_name: str, indent: str, unit: str) -> list[str]:
    """A new columnPermission hiding the column, in the file's style."""
    if _uses_inline_column_permissions(lines):
        return [f"{indent}columnPermission {tmdl.quote_object_name(column_name)} = none\n"]
    return tmdl.build_simple_element_lines(
        keyword="columnPermission", name=column_name, indent=indent, unit=unit,
        properties=[("metadataPermission", "none")],
    )


def _hide_column_permission(lines: list[str], cp_block: TmdlBlock) -> list[str]:
    """
    Brings an existing columnPermission to `metadataPermission = none`,
    keeping the form it already uses. A block declaring the permission in
    both forms (the duplicate earlier versions wrote by adding the property
    under `= none`) is repaired: the explicit line goes, the inline value stays.
    """
    inline = _inline_permission(lines, cp_block)
    if inline is None:
        lines, _changed = tmdl.set_property(lines, cp_block, "metadataPermission", "none")
        return lines

    lines, _removed = tmdl.remove_property(lines, cp_block, "metadataPermission")
    cp_block = tmdl.make_block(lines, cp_block.start)
    # Rewritten only if needed: an already single-line `= none` keeps its
    # original spelling (quoting of the name, casing), so no spurious diffs.
    if inline != "none" or cp_block.inline_expression == "":
        lines = tmdl.set_default_value(lines, cp_block, "none")
    return lines


def _clear_column_permission(lines: list[str], cp_block: TmdlBlock) -> list[str]:
    """Removes the metadataPermission of a columnPermission, in both forms."""
    lines, _removed = tmdl.remove_property(lines, cp_block, "metadataPermission")
    cp_block = tmdl.make_block(lines, cp_block.start)
    if cp_block.inline_expression is not None:
        lines = tmdl.set_default_value(lines, cp_block, None)
    return lines


def _apply_ols(
    lines: list[str],
    role_name: str,
    role_block,
    table_name: str,
    column_name: str,
    hidden: bool,
    subject: str,
    role_created: bool,
    permission_changed: bool,
) -> tuple[list[str], str, bool]:
    """
    The OLS part proper: returns (lines, message, changed) and NEVER writes
    to disk. Saving is up to the caller alone, which has a single write
    point: this way a branch that exits without changes can no longer throw
    away those already present in `lines`.
    """
    unit = tmdl.detect_indent_unit(lines)
    tp_block = _role_table_permission(lines, role_block, table_name)

    unchanged = _unchanged_ols_message(subject, role_name, role_created, permission_changed)

    if tp_block is None:
        if not hidden:
            return lines, unchanged, False

        indent = tmdl.child_indent(lines, role_block)
        table_properties = None if column_name else [("metadataPermission", "none")]
        tp_lines = tmdl.build_simple_element_lines(
            keyword="tablePermission", name=table_name, indent=indent, unit=unit,
            properties=table_properties,
        )
        lines = tmdl.append_child_block(lines, role_block, tp_lines)
        role_block = tmdl.find_block(lines, role_name, "role")
        tp_block = _role_table_permission(lines, role_block, table_name)

        if column_name:
            cp_lines = _column_permission_lines(
                lines, column_name, tmdl.child_indent(lines, tp_block), unit)
            lines = tmdl.append_child_block(lines, tp_block, cp_lines)

        return lines, f"OLS set: {subject} hidden in role '{role_name}'.", True

    if not column_name:
        if hidden:
            if _metadata_permission(lines, tp_block) == "none":
                return lines, f"No changes: {subject} was already hidden in role '{role_name}'.", False
            lines, _changed = tmdl.set_property(lines, tp_block, "metadataPermission", "none")
            return lines, f"Table '{table_name}' hidden (OLS) in role '{role_name}'.", True

        lines, removed = tmdl.remove_property(lines, tp_block, "metadataPermission")
        if not removed:
            return lines, unchanged, False

        role_block = tmdl.find_block(lines, role_name, "role")
        tp_block = _role_table_permission(lines, role_block, table_name)
        if tmdl.is_block_empty(lines, tp_block):
            lines = tmdl.remove_block(lines, tp_block)
        return lines, f"Table '{table_name}' made visible (OLS) in role '{role_name}'.", True

    cp_block = _table_column_permission(lines, tp_block, column_name)
    hidden_message = f"Column '{column_name}' hidden (OLS) in '{table_name}' in role '{role_name}'."

    if hidden:
        if cp_block is None:
            cp_lines = _column_permission_lines(
                lines, column_name, tmdl.child_indent(lines, tp_block), unit)
            lines = tmdl.append_child_block(lines, tp_block, cp_lines)
            return lines, hidden_message, True

        updated = _hide_column_permission(lines, cp_block)
        if updated == lines:
            return lines, f"No changes: {subject} was already hidden in role '{role_name}'.", False
        return updated, hidden_message, True

    if cp_block is None:
        return lines, unchanged, False

    # The permission is cleared in both forms and the block removed only if
    # nothing else is left in it (annotations, extended properties).
    updated = _clear_column_permission(lines, cp_block)
    role_block = tmdl.find_block(updated, role_name, "role")
    tp_block = _role_table_permission(updated, role_block, table_name)
    cp_block = _table_column_permission(updated, tp_block, column_name)
    if tmdl.is_block_empty(updated, cp_block):
        updated = tmdl.remove_block(updated, cp_block)
        role_block = tmdl.find_block(updated, role_name, "role")
        tp_block = _role_table_permission(updated, role_block, table_name)
        if tmdl.is_block_empty(updated, tp_block):
            updated = tmdl.remove_block(updated, tp_block)
    if updated == lines:
        return lines, unchanged, False
    return updated, f"Column '{column_name}' made visible (OLS) in '{table_name}' in role '{role_name}'.", True
