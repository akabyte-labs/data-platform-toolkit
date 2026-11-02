"""
Progressive reduction of verbose outputs, with full recovery on request.

Problem: the official Microsoft server can return very large payloads
(hundreds of columns, each with lineageTag, annotations, timestamps).
Truncating them to N characters or N items throws away information without
knowing whether that was the information actually needed.

Three-step strategy:

  1. NOISE REMOVAL (no useful loss)
     Keys like lineageTag, changedProperties, internal timestamps are never
     useful for reasoning about the semantic model, and on their own they
     often account for most of the payload.

  2. REDUNDANCY FACTORING (no loss)
     If 50 elements share the same value for a field, that value is declared
     once in `_shared` instead of 50 times.

  3. STATISTICAL PROFILE (lossy, but controlled)
     Used only if the previous steps aren't enough. Even here, the
     identifiers (name/id) of ALL elements are preserved in full: they are
     the densest information and the one needed for the next call.

In every case the original payload stays cached in-process and can be
retrieved in full or in part with `resolve_path` (exposed as the
`get_official_result_detail` tool). No information is ever truly lost: it is
only deferred, and the token cost is paid only for what is actually needed.

Cross-cutting invariant: diagnostic fields (error, warning, message, status)
are NEVER compressed, at any level. Losing an error message is the worst
possible failure for a fallback tool.
"""

from __future__ import annotations

import json
import numbers
import os
import time
import uuid
from collections import Counter, OrderedDict
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Default budget of the tools' text outputs
# ---------------------------------------------------------------------------
# A mid-sized enterprise model (80 tables, 1,200 measures) produces well over
# 230,000 characters of DAX lineage on its own: in a single response that
# means saturating the client's context window and making the conversation
# unusable. Every tool whose output can grow with the size of the project is
# therefore bounded by this budget. It can be raised by anyone working on small
# models who wants the full output.


def _int_from_env(name: str, default: int) -> int:
    """Positive integer from an environment variable; an unreadable value stops nothing."""
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _budget_from_env(default: int = 20000) -> int:
    return _int_from_env("MCP_MAX_OUTPUT_CHARS", default)


MAX_TOOL_OUTPUT_CHARS = _budget_from_env()

# ---------------------------------------------------------------------------
# Vocabularies (comparisons are always lowercase)
# ---------------------------------------------------------------------------

# Noise: internal TOM/TMDL metadata, of no value for reasoning.
NOISE_KEYS = {
    "lineagetag", "sourcelineagetag", "changedproperties", "changedproperty",
    "modifiedtime", "structuremodifiedtime", "refreshedtime", "lastprocessed",
    "lastschemaupdate", "lastupdate", "createdtimestamp", "modifiedtimestamp",
    "annotations", "annotation", "extendedproperties", "queryscopedmeasures",
    "objectversion", "schemaversion", "state", "etag",
}

# Never compressed, at any reduction level.
PRESERVE_KEYS = {
    "error", "errors", "iserror", "errormessage", "message", "messages",
    "warning", "warnings", "status", "detail", "details", "exception",
    "reason", "code", "success", "failed",
}

# Fields that identify an element: preserved in full within profiles.
IDENTITY_KEYS = (
    "name", "id", "tablename", "measurename", "columnname", "objectname",
    "displayname", "key", "path",
)


@dataclass
class ShapeConfig:
    drop_noise: bool = False
    drop_empty: bool = False
    factor_shared: bool = False
    profile_lists: bool = False
    sample_items: int = 3
    identity_limit: int = 250     # how many identifiers to list in full
    distinct_limit: int = 8
    # `None` = strings are never cut. The first rungs of the ladder use it
    # because they return WITHOUT a note: with a limit active, a 9,000-character
    # DAX measure was cut to 4,000 and the response presented itself as
    # complete, without even mentioning the result_id to recover it from. That
    # is exactly the failure this module exists to prevent.
    str_limit: int | None = 4000
    list_threshold: int = 8
    noise_keys: set = field(default_factory=lambda: set(NOISE_KEYS))
    preserve_keys: set = field(default_factory=lambda: set(PRESERVE_KEYS))


def _is_empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _cap(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "..."


def _identity_key(items: list[dict]) -> str | None:
    """First identity field present in (almost) every element."""
    for candidate in IDENTITY_KEYS:
        for key in items[0]:
            if key.lower() == candidate:
                if sum(1 for item in items if key in item) >= max(1, len(items) * 0.8):
                    return key
    return None


# ---------------------------------------------------------------------------
# Field profile
# ---------------------------------------------------------------------------

def profile_field(values: list, distinct_limit: int, str_limit: int = 200) -> dict:
    non_null = [v for v in values if v is not None]
    profile: dict = {"non_null": len(non_null), "null": len(values) - len(non_null)}
    if not non_null:
        return profile

    if all(isinstance(v, bool) for v in non_null):
        profile["type"] = "boolean"
        profile["values"] = {str(k): c for k, c in Counter(non_null).items()}
        return profile

    if all(isinstance(v, numbers.Real) and not isinstance(v, bool) for v in non_null):
        profile["type"] = "number"
        profile["min"] = min(non_null)
        profile["max"] = max(non_null)
        return profile

    if all(isinstance(v, str) for v in non_null):
        distinct = sorted(set(non_null))
        profile["type"] = "string"
        # The full list of distinct values is more compact than the
        # "distinct_count + examples" form when there are few of them: we
        # only switch to examples when there would genuinely be more shown,
        # otherwise the reduction would make the output grow.
        n_examples = max(1, min(5, distinct_limit))
        if len(distinct) <= max(distinct_limit, n_examples):
            profile["values"] = [_cap(v, str_limit) for v in distinct]
        else:
            profile["distinct_count"] = len(distinct)
            profile["examples"] = [_cap(v, str_limit) for v in distinct[:n_examples]]
        return profile

    profile["type"] = "mixed"
    profile["examples"] = [
        v if isinstance(v, (int, float, bool)) else _cap(str(v), str_limit) for v in non_null[:5]
    ]
    return profile


# ---------------------------------------------------------------------------
# Recursive transformation
# ---------------------------------------------------------------------------

def _shared_fields(items: list[dict]) -> dict:
    """Scalar fields that hold the same value across all elements."""
    if len(items) < 3:
        return {}
    shared = {}
    first = items[0]
    for key, value in first.items():
        if not isinstance(value, (str, int, float, bool, type(None))):
            continue
        if all(key in item and item[key] == value for item in items):
            shared[key] = value
    return shared


_SHARED_WRAPPER_COST = 32   # roughly '{"_shared":{},"_count":N,"items":[]}'


def _worth_factoring(shared: dict, count: int) -> bool:
    """
    Estimates whether extracting the shared fields actually shrinks the output.

    This used to serialize TWICE with `json.dumps`, both the candidate and the
    original list, for every list, at every nesting level, at every rung of
    the ladder. On large payloads it was the dominant cost of the whole
    module. The estimate is arithmetic and serializes nothing: each shared
    field takes about len(key)+len(value)+4 bytes and is written once instead
    of `count` times.
    """
    per_item = sum(len(str(k)) + len(str(v)) + 4 for k, v in shared.items())
    saving = (count - 1) * per_item
    return saving > per_item + _SHARED_WRAPPER_COST


def _summarize_list(items: list, cfg: ShapeConfig) -> dict:
    """Profile of a long list, with identifiers preserved in full."""
    if all(isinstance(v, dict) for v in items):
        id_key = _identity_key(items)
        fields: dict[str, list] = {}
        for item in items:
            for key in item:
                fields.setdefault(key, [])
        for key in fields:
            fields[key] = [item.get(key) for item in items]

        profile: dict = {"count": len(items)}

        # identity_limit = 0 is the last rung of the ladder: even the list of
        # identifiers is given up (still recoverable via result_id), and the
        # field is profiled like all the others.
        list_identifiers = bool(id_key) and cfg.identity_limit > 0
        if list_identifiers:
            identifiers = [str(item.get(id_key, "")) for item in items]
            if len(identifiers) <= cfg.identity_limit:
                profile[id_key] = identifiers
            else:
                profile[id_key] = identifiers[: cfg.identity_limit]
                profile[f"{id_key}_omitted"] = len(identifiers) - cfg.identity_limit

        profile["fields"] = {
            key: profile_field(values, cfg.distinct_limit)
            for key, values in fields.items()
            if not (list_identifiers and key == id_key)
        }

        result: dict = {"_profile": profile}
        if cfg.sample_items:
            result["sample"] = [
                _transform(item, cfg, protected=False) for item in items[: cfg.sample_items]
            ]
        return result

    profile = {"count": len(items), **profile_field(items, cfg.distinct_limit)}
    result = {"_profile": profile}
    if cfg.sample_items:
        result["sample"] = [
            v if isinstance(v, (int, float, bool)) or v is None else _cap(str(v), 200)
            for v in items[: cfg.sample_items]
        ]
    return result


def _transform(node, cfg: ShapeConfig, protected: bool = False):
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            key_lower = key.lower()
            if not protected and cfg.drop_noise and key_lower in cfg.noise_keys:
                continue
            child_protected = protected or key_lower in cfg.preserve_keys
            transformed = _transform(value, cfg, child_protected)
            if not child_protected and cfg.drop_empty and _is_empty(transformed):
                continue
            result[key] = transformed
        return result

    if isinstance(node, list):
        if protected:
            return node
        elements = [_transform(v, cfg, protected) for v in node]

        # Order matters: profiling takes precedence over factoring.
        # (bug fixed: factoring first turned the list into a dict, so later
        # rungs of the ladder could no longer profile it, flattening the
        # reduction and sending everything to the final stub.)
        if cfg.profile_lists and len(elements) > cfg.list_threshold:
            return _summarize_list(elements, cfg)

        if (cfg.factor_shared and len(elements) >= 3
                and all(isinstance(v, dict) for v in elements)):
            shared = _shared_fields(elements)
            if shared and _worth_factoring(shared, len(elements)):
                residuals = [
                    {k: v for k, v in item.items() if k not in shared} for item in elements
                ]
                # Factoring is lossless: reconstructible as {**_shared, **item}.
                return {"_shared": shared, "_count": len(elements), "items": residuals}

        return elements

    if (isinstance(node, str) and not protected
            and cfg.str_limit is not None and len(node) > cfg.str_limit):
        return _cap(node, cfg.str_limit)
    return node


# ---------------------------------------------------------------------------
# Reduction ladder
# ---------------------------------------------------------------------------
# Ordered by increasing information cost: the first four levels lose nothing
# useful, the profile only kicks in if they aren't enough.

REDUCTION_LADDER: list[tuple[str, ShapeConfig]] = [
    # The first four rungs never cut strings: none of them declares a loss,
    # so none of them can afford one.
    ("full", ShapeConfig(str_limit=None)),
    ("without internal metadata", ShapeConfig(drop_noise=True, str_limit=None)),
    ("without internal metadata and empty fields",
     ShapeConfig(drop_noise=True, drop_empty=True, str_limit=None)),
    ("with shared fields factored out",
     ShapeConfig(drop_noise=True, drop_empty=True, factor_shared=True, str_limit=None)),
    ("with long text values shortened",
     ShapeConfig(drop_noise=True, drop_empty=True, factor_shared=True, str_limit=4000)),
    ("with long lists profiled (identifiers kept in full)",
     ShapeConfig(drop_noise=True, drop_empty=True, factor_shared=True, profile_lists=True,
                 sample_items=3, str_limit=600)),
    ("with profiled lists and reduced sample",
     ShapeConfig(drop_noise=True, drop_empty=True, factor_shared=True, profile_lists=True,
                 sample_items=1, distinct_limit=5, str_limit=300, identity_limit=150)),
    ("with profiles and identifiers only",
     ShapeConfig(drop_noise=True, drop_empty=True, factor_shared=True, profile_lists=True,
                 sample_items=0, distinct_limit=3, str_limit=160, identity_limit=80)),
    ("with only the shape of the data (identifiers recoverable separately)",
     ShapeConfig(drop_noise=True, drop_empty=True, factor_shared=True, profile_lists=True,
                 sample_items=0, distinct_limit=2, str_limit=80, identity_limit=0)),
]


def _dump(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Cache of full results (deferred retrieval)
# ---------------------------------------------------------------------------

_RESULT_CACHE: "OrderedDict[str, dict]" = OrderedDict()
MAX_CACHED_RESULTS = 8

# The cache used to keep 8 full results with no size cap and no expiry, each
# stored twice (deserialized `data` and textual `raw`). With the official
# server's payloads that meant hundreds of MB resident for the whole life of
# the process, for data nobody would ever retrieve again.
MAX_CACHED_CHARS = _int_from_env("MCP_MAX_CACHED_CHARS", 4_000_000)
CACHE_TTL_SECONDS = 1800.0


def _drop_expired() -> None:
    limit = time.monotonic() - CACHE_TTL_SECONDS
    for result_id in [k for k, v in _RESULT_CACHE.items() if v.get("ts", 0.0) < limit]:
        _RESULT_CACHE.pop(result_id, None)


def _cache_size() -> int:
    return sum(v.get("chars", 0) for v in _RESULT_CACHE.values())


def store_result(tool_name: str, data, raw_text: str) -> str:
    _drop_expired()
    raw_text = raw_text or ""
    result_id = uuid.uuid4().hex[:8]
    _RESULT_CACHE[result_id] = {
        "tool": tool_name, "data": data, "raw": raw_text,
        "ts": time.monotonic(), "chars": len(raw_text),
    }
    while len(_RESULT_CACHE) > MAX_CACHED_RESULTS:
        _RESULT_CACHE.popitem(last=False)
    # The size cap applies after the count cap and never evicts the newest
    # entry: it is the one the response just produced points to.
    while len(_RESULT_CACHE) > 1 and _cache_size() > MAX_CACHED_CHARS:
        _RESULT_CACHE.popitem(last=False)
    return result_id


def get_result(result_id: str) -> dict:
    _drop_expired()
    if result_id not in _RESULT_CACHE:
        available = ", ".join(_RESULT_CACHE) or "none"
        raise KeyError(
            f"Result '{result_id}' is no longer available in cache (at most "
            f"{MAX_CACHED_RESULTS} are kept, for up to {MAX_CACHED_CHARS} characters and "
            f"{int(CACHE_TTL_SECONDS / 60)} minutes). Available: {available}. "
            f"Re-run the original call."
        )
    _RESULT_CACHE.move_to_end(result_id)
    return _RESULT_CACHE[result_id]


def resolve_path(data, path: str):
    """
    Navigates a path such as 'tables[0].columns' or 'model.tables'.
    An empty path means the root.
    """
    if not path or not path.strip():
        return data

    current = data
    for piece in path.replace("]", "").replace("[", ".").split("."):
        token = piece.strip()
        if not token:
            continue
        if isinstance(current, list):
            if not token.lstrip("-").isdigit():
                raise KeyError(f"'{token}' is not a valid index for a list in '{path}'.")
            index = int(token)
            if not -len(current) <= index < len(current):
                raise KeyError(f"Index {index} out of range (list has {len(current)} elements).")
            current = current[index]
        elif isinstance(current, dict):
            if token not in current:
                keys = ", ".join(list(current)[:15]) or "none"
                raise KeyError(f"Key '{token}' not found. Available keys: {keys}.")
            current = current[token]
        else:
            raise KeyError(f"Cannot navigate into a scalar value at segment '{token}'.")
    return current


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def extract_diagnostics(node, _depth: int = 0) -> dict:
    """
    Collects diagnostic fields wherever they appear in the structure. This
    ensures an error survives even when the payload is discarded entirely: a
    lost error message is worth far more than the tokens it costs to keep.
    """
    found: dict = {}
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in PRESERVE_KEYS:
                _merge_diagnostic(found, key, value)
            elif _depth < 4:
                for child_key, child_value in extract_diagnostics(value, _depth + 1).items():
                    _merge_diagnostic(found, child_key, child_value)
    elif isinstance(node, list) and _depth < 4:
        for item in node[:20]:
            for child_key, child_value in extract_diagnostics(item, _depth + 1).items():
                _merge_diagnostic(found, child_key, child_value)
    return found


class _Collected(list):
    """Distinct values of one diagnostic key found in several places."""


def _merge_diagnostic(found: dict, key: str, value) -> None:
    """
    Adds a diagnostic without overwriting one already found under the same
    key: with `dict.update` only the LAST of several errors survived (two
    failed items of a batch reported a single error).
    """
    if key not in found:
        found[key] = value
        return
    current = found[key]
    values = current if isinstance(current, _Collected) else _Collected([current])
    for item in (value if isinstance(value, _Collected) else [value]):
        if item not in values:
            values.append(item)
    found[key] = values if len(values) > 1 else values[0]


def shape_payload(data, max_chars: int, result_id: str | None = None) -> str:
    """
    Returns the richest representation that fits the budget, walking down the
    reduction ladder. Adds a note about what was reduced and how to retrieve
    the full data.
    """
    full = _dump(data)
    if len(full) <= max_chars:
        return full

    for label, cfg in REDUCTION_LADDER:
        text = _dump(_transform(data, cfg))
        if len(text) <= max_chars:
            if label == "full":
                return text
            note = (
                f"[response reduced ({label}): original was {len(full)} characters."
            )
            if result_id:
                note += (
                    f" The full result can be retrieved with "
                    f"get_official_result_detail(result_id='{result_id}', path='...')."
                )
            return text + "\n" + note + "]"

    stub = {
        "_truncated": True,
        "_original_chars": len(full),
        "_result_id": result_id,
        "_hint": (
            "Output too large even after maximum reduction. Use "
            "get_official_result_detail with a specific 'path', or apply more "
            "selective filters in the official tool."
        ),
    }
    # Diagnostic fields always accompany the stub, even if that exceeds the
    # budget: without them the model wouldn't even know an error occurred,
    # let alone which one.
    diagnostics = extract_diagnostics(data)
    if diagnostics:
        stub["diagnostics"] = diagnostics
    return _dump(stub)


def shape_lines(raw_text: str, max_chars: int, note: str = "") -> str:
    """
    Last resort for a text output already formatted one line per element: the
    cut happens at a line boundary, so the last line stays readable and the
    structure doesn't break.

    WARNING, this is a TRUNCATION, not a lossless reduction: the tail lines
    disappear. On a sorted list this means everything after a certain letter
    is missing, and a partial answer that looks complete is the most effective
    way to give a wrong answer to an impact-analysis question ("nobody uses
    this measure" when the line was simply past the cut).

    That is why the warning is REPEATED AT THE TOP as well as at the bottom:
    whoever reads it, human or model, meets the limit before the content, not
    after having already formed an opinion.

    Where there is a structure to reason about (DAX lineage, report usages)
    this function is NOT used: a progressive reduction keeps the names of all
    elements and sacrifices only the detail, see `format_dax_lineage`.
    `shape_lines` remains for outputs that are already minimal (one line per
    element) where there is nothing else to compress.
    """
    text = raw_text or ""
    if len(text) <= max_chars:
        return text

    lines = text.splitlines()

    def _warnings(shown: int) -> tuple[str, str]:
        omitted = len(lines) - shown
        head = (
            f"[!] INCOMPLETE RESPONSE: showing {shown} of {len(lines)} lines, "
            f"the other {omitted} were CUT and do not appear below. "
            f"Do NOT read this list as exhaustive"
        )
        head += f": {note}.]" if note else ".]"
        foot = f"... [cut: {omitted} of {len(lines)} lines omitted, {len(text)} characters total"
        foot += f". {note}]" if note else "]"
        return head, foot

    # Header and footer must be SUBTRACTED from the budget before filling, not
    # added afterwards: adding them at the end made the response exceed the
    # limit exactly when the limit mattered. The omitted-line count is part of
    # the warning text, so the length is estimated on the worst case (zero
    # lines shown) and changes by a few digits at most.
    head_estimate, foot_estimate = _warnings(0)
    available = max_chars - len(head_estimate) - len(foot_estimate) - 2
    if available <= 0:
        # Budget so small that not even the warning fits: better the warning
        # alone than a fragment of a list passed off as an answer.
        return head_estimate[:max_chars]

    kept: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > available:
            break
        kept.append(line)
        total += len(line) + 1

    # The estimate was made with "0 lines shown": the real count has more
    # digits, and the response could exceed the budget by a character or two.
    # Lines are given back until the real text fits.
    while True:
        head, foot = _warnings(len(kept))
        result = "\n".join([head, ""] + kept + [foot])
        if len(result) <= max_chars or not kept:
            return result
        kept.pop()


def shape_output(raw_text: str, max_chars: int, tool_name: str = "", cache: bool = True) -> str:
    """
    Entry point for the raw output of an external tool.

    JSON: reduced along the ladder; the full payload is cached ONLY when the
    response was actually reduced. Caching every response, small ones
    included, evicted the entries that a reduced response points to (the
    cache keeps a handful) for results nobody could ask for: an unreduced
    response carries no result_id.

    Non-JSON text keeps its line structure (it used to be flattened into a
    single line, tables and stack traces included) and, if too long, is cut
    at a line boundary, or at a sentence boundary when it is a single line.
    """
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        text = "\n".join(line.rstrip() for line in (raw_text or "").splitlines()).strip()
        if len(text) <= max_chars:
            return text
        if "\n" in text:
            return shape_lines(text, max_chars)
        compact = " ".join(text.split())
        if len(compact) <= max_chars:
            return compact
        cut = compact[:max_chars]
        boundary = max(cut.rfind(". "), cut.rfind("; "), cut.rfind(" | "))
        if boundary > max_chars * 0.6:
            cut = cut[: boundary + 1]
        return cut + f"... [truncated, {len(compact)} characters total]"

    full = _dump(data)
    if len(full) <= max_chars:
        return full
    result_id = store_result(tool_name, data, raw_text) if cache else None
    return shape_payload(data, max_chars, result_id)
