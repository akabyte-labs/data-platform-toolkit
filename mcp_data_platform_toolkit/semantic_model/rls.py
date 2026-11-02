"""
Row-Level Security of the semantic model: the DAX filter each role puts on
each table, what that filter reads, and the other tables it restricts
through the relationships.

Object-Level Security (tables and columns hidden per role) lives in
`ols_roles`, which also writes it. This module only reads: an RLS filter is
DAX, and the server never writes DAX into a role.

The tables a filter reaches are read from the relationships, the way the
engine propagates security: through ACTIVE relationships only, from the
"one" side to the "many" side, and back only where the relationship says
`securityFilteringBehavior: bothDirections`. Cross filtering in both
directions is not enough: Power BI applies the security filter one way
unless the relationship is told otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcp_data_platform_toolkit.semantic_model import dax_lexer
from mcp_data_platform_toolkit.semantic_model import tmdl_parser as tmdl
from mcp_data_platform_toolkit.semantic_model.dax_lineage import build_model_index, resolve_dax_dependencies
from mcp_data_platform_toolkit.semantic_model.model_loader import load_definition
from mcp_data_platform_toolkit.semantic_model.relationships import parse_relationships
from mcp_data_platform_toolkit.semantic_model.tmdl_parser import ElementNotFoundError

# Functions that make a filter depend on who is looking: the same role shows
# different rows to different users.
DYNAMIC_FUNCTIONS = ("USERPRINCIPALNAME", "USERNAME", "USEROBJECTID", "CUSTOMDATA", "USERCULTURE")


@dataclass
class RlsFilter:
    role: str
    table: str                      # as declared in the model, or as written if not found
    table_exists: bool
    expression: str
    file_path: str
    line: int
    dependencies: dict              # see `resolve_dax_dependencies`
    dynamic: list[str]
    restricts: list[str]            # other tables it filters through relationships


@dataclass
class RoleSecurity:
    name: str
    model_permission: str
    members: int
    filters: list[RlsFilter] = field(default_factory=list)
    ols_only_tables: list[str] = field(default_factory=list)   # tablePermission without a filter


def _propagation(project_path: str) -> dict[str, list[str]]:
    """{table (lowercase): [tables a security filter on it reaches directly]}."""
    reach: dict[str, list[str]] = {}

    def add(source: str, target: str) -> None:
        if source and target and source.lower() != target.lower():
            reach.setdefault(source.lower(), []).append(target)

    for rel in parse_relationships(project_path):
        if not rel["is_active"]:
            continue
        cardinalities = (rel["from_cardinality"], rel["to_cardinality"])
        # The filter flows from the "one" side; with 1:1 and *:* the 'to'
        # side filters the 'from' side, as TOM's single direction does.
        if cardinalities == ("one", "many"):
            one_side, many_side = rel["from_table"], rel["to_table"]
        else:
            one_side, many_side = rel["to_table"], rel["from_table"]
        add(one_side, many_side)
        if rel["security_filtering"].lower() == "bothdirections":
            add(many_side, one_side)
    return reach


def _reached(table: str, reach: dict[str, list[str]]) -> list[str]:
    """Every table a filter on `table` restricts, transitively, itself excluded."""
    seen = {table.lower()}
    found: list[str] = []
    queue = [table]
    while queue:
        current = queue.pop(0)
        for target in reach.get(current.lower(), []):
            if target.lower() not in seen:
                seen.add(target.lower())
                found.append(target)
                queue.append(target)
    return sorted(found, key=str.lower)


def _members(lines: list[str], role_block) -> int:
    """Role members written in the file (usually none: they are assigned in the service)."""
    return sum(1 for child in tmdl.iter_children(lines, role_block)
               if child.kind == "other" and lines[child.index].strip().lower().startswith("member "))


def load_role_security(project_path: str) -> list[RoleSecurity]:
    """Every role of the model with its RLS filters, sorted by name."""
    definition = load_definition(project_path)
    tables = definition["tables"]
    by_lower = {name.lower(): name for name in tables}
    index = build_model_index(tables)
    reach = _propagation(project_path) if definition["roles"] else {}

    roles: list[RoleSecurity] = []
    for obj in sorted(definition["roles"].values(), key=lambda o: o.name.lower()):
        permission = tmdl.get_property(obj.lines, obj.block, "modelPermission")
        role = RoleSecurity(
            name=obj.name,
            model_permission=tmdl.normalize_text_value(permission.value) if permission else "",
            members=_members(obj.lines, obj.block),
        )
        for tp in tmdl.child_blocks(obj.lines, obj.block, "tablePermission"):
            expression = tmdl.get_expression(obj.lines, tp).strip()
            declared = by_lower.get(tp.name.strip().lower())
            table = declared or tp.name.strip()
            if not expression:
                role.ols_only_tables.append(table)
                continue
            # A filter is evaluated row by row on its table: an unqualified
            # column is that table's first, as in a calculated column.
            dependencies = resolve_dax_dependencies(expression, table, "column", index)
            functions = dax_lexer.analyze(expression).functions
            role.filters.append(RlsFilter(
                role=obj.name, table=table, table_exists=declared is not None,
                expression=expression, file_path=obj.file_path, line=tp.start + 1,
                dependencies=dependencies,
                dynamic=[f for f in DYNAMIC_FUNCTIONS if f in functions],
                restricts=_reached(table, reach) if declared else [],
            ))
        roles.append(role)
    return roles


def _blocks_every_row(expression: str) -> bool:
    return "".join(expression.split()).upper() in ("FALSE()", "FALSE", "0")


def format_rls_report(roles: list[RoleSecurity], role_name: str = "", table_name: str = "") -> str:
    if role_name:
        target = role_name.strip().lower()
        roles = [r for r in roles if r.name.strip().lower() == target]
        if not roles:
            raise ElementNotFoundError(f"Role '{role_name}' not found in the model.")
    if not roles:
        return "No security roles defined in the model."

    table_filter = (table_name or "").strip().lower()

    def concerns(item: RlsFilter) -> bool:
        return (not table_filter or item.table.lower() == table_filter
                or any(t.lower() == table_filter for t in item.restricts))

    shown = [(role, [f for f in role.filters if concerns(f)]) for role in roles]
    if table_filter:
        shown = [(role, filters) for role, filters in shown if filters]
        if not shown:
            return f"No RLS filter is on table '{table_name}' or reaches it through relationships."

    total = sum(len(filters) for _role, filters in shown)
    lines = [f"=== ROW-LEVEL SECURITY ({len(shown)} role{'s' if len(shown) != 1 else ''}, "
             f"{total} filter{'s' if total != 1 else ''}"
             + (f" on or reaching '{table_name}'" if table_filter else "") + ") ==="]
    for role, filters in shown:
        details = [f"modelPermission: {role.model_permission or 'not set'}"]
        if role.members:
            details.append(f"{role.members} member{'s' if role.members != 1 else ''} in the file")
        header = f"\nRole: {role.name}  ({', '.join(details)})"
        if not filters:
            lines.append(header + ": no RLS filter, every row is visible")
        else:
            lines.append(header)
        for item in filters:
            lines.extend(_filter_lines(item, (table_name or "").strip()))
        if role.ols_only_tables and not table_filter:
            lines.append(f"  (OLS only, no row filter, on: {', '.join(role.ols_only_tables)}: "
                         f"see get_ols_permissions)")
    return "\n".join(lines)


def _filter_lines(item: RlsFilter, table_name: str) -> list[str]:
    flags = []
    if item.dynamic:
        flags.append("dynamic: " + ", ".join(item.dynamic))
    if _blocks_every_row(item.expression):
        flags.append("hides every row")
    if not item.table_exists:
        flags.append("[!] table not in the model")
    if table_name and item.table.lower() != table_name.lower():
        flags.append(f"reaches '{table_name}' through relationships")
    title = f"  {item.table}" + (f" [{'; '.join(flags)}]" if flags else "")

    expression_lines = item.expression.splitlines()
    if len(expression_lines) == 1:
        out = [f"{title}: {expression_lines[0].strip()}"]
    else:
        out = [f"{title}:"] + [f"      {line.rstrip()}" for line in expression_lines]

    deps = item.dependencies
    reads = deps["columns"] + deps["measures"]
    if reads:
        out.append("    reads: " + ", ".join(reads))
    if deps["unresolved"]:
        out.append("    [!] unresolved: " + "; ".join(deps["unresolved"]))
    if item.restricts:
        out.append("    also restricts (relationships): " + ", ".join(item.restricts))
    return out


def rls_references(project_path: str) -> list[dict]:
    """
    What each RLS filter reads, for impact analysis:
    [{"role", "table", "columns": ["T[C]"], "measures": ["T[M]"]}].
    """
    return [{"role": item.role, "table": item.table,
             "columns": item.dependencies["columns"], "measures": item.dependencies["measures"]}
            for role in load_role_security(project_path) for item in role.filters]
