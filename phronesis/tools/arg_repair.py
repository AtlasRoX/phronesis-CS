"""
Conservative, schema-driven repair of tool arguments that fail validation for a trivially fixable reason.

Agents (supervisor / swarm specialists, plan nodes) sometimes send arguments that are right in substance but
wrong in shape: activity_limit=100 where the schema allows at most 50, six drug names where the tool takes a
handful, {"name": "NSCLC"} where a disease name string is expected, or a bare string where a list is expected.
Rejecting such a call costs an agent turn and shows up as an error step, so before giving up a caller may try
repair_arguments() once. Only these fixes are made, each driven by a pydantic error and its schema bound:

- a result-size number (limit, max_*, top_n) outside ge/le/gt/lt is clamped to the bound; scientific values are not;
- a list longer than max_length keeps its first max_length items;
- an object where a string is expected becomes its "name"/"label"/"symbol"/"id"/"mention" string;
- a single string where a list is expected becomes a one-item list;
- an argument the schema does not take is dropped, when the model forbids extras (or the caller asks).

Nothing else is guessed: a value that is missing, malformed (wrong id pattern) or of another type is left alone,
and when the repaired arguments still do not validate the caller keeps its original error.
"""

from __future__ import annotations

import copy
import math
import re
from typing import AbstractSet, Any, Dict, List, Optional, Sequence, Tuple, Type

from pydantic import BaseModel, ValidationError

# Keys tried, in order, when an object stands where a string is expected.
STRING_KEYS: Tuple[str, ...] = ("name", "label", "symbol", "id", "mention")
MAX_PASSES = 4  # errors surface in stages (list length is checked after the items), so repair re-validates
_BOUND_TYPES = {"less_than_equal": "le", "greater_than_equal": "ge", "less_than": "lt", "greater_than": "gt"}


def _show(value: Any, limit: int = 40) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _loc_text(loc: Sequence[Any]) -> str:
    return ".".join(str(x) for x in loc) or "arguments"


def _navigate(root: Any, loc: Sequence[Any]) -> Optional[Tuple[Any, Any, List[Any]]]:
    """(container, key, path walked) of the value at a pydantic error loc; union-member tags are skipped."""
    cur = root
    walked: List[Any] = []
    parent: Any = None
    last: Any = None
    for seg in loc:
        if isinstance(cur, dict) and seg in cur:
            parent, last, cur = cur, seg, cur[seg]
            walked.append(seg)
        elif isinstance(cur, list) and isinstance(seg, int) and -len(cur) <= seg < len(cur):
            parent, last, cur = cur, seg, cur[seg]
            walked.append(seg)
        elif isinstance(seg, str):
            continue  # a union tag such as 'constrained-str' or 'list[str]'
        else:
            return None
    if parent is None:
        return None
    return parent, last, walked


def _string_from_object(value: Dict[str, Any]) -> Optional[str]:
    for key in STRING_KEYS:
        v = value.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _clamp(value: Any, kind: str, bound: Any) -> Optional[Any]:
    """The value moved to its violated bound, or None when that cannot be done exactly."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isinstance(bound, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if kind in ("le", "ge"):
        return type(value)(bound) if isinstance(value, int) and float(bound).is_integer() else bound
    if isinstance(value, int) and float(bound).is_integer():
        return int(bound) - 1 if kind == "lt" else int(bound) + 1
    return math.nextafter(float(bound), -math.inf if kind == "lt" else math.inf)


_SIZE_FIELD = re.compile(r"(?:^|_)(?:limit|max|min|top|count|size|page|n|k|num|number)(?:_|$)|^(?:max|top|num|n)_", re.I)


def _is_size_field(walked: Any) -> bool:
    """Result-size knobs (activity_limit, max_trials, top_n, page_size) may be clamped; other numbers may not."""
    name = str(walked[-1] if isinstance(walked, (list, tuple)) and walked else walked)
    return bool(_SIZE_FIELD.search(name))


def _fix_one(args: Dict[str, Any], err: Any, notes: List[str]) -> bool:
    etype = str(err.get("type") or "")
    loc = tuple(err.get("loc") or ())
    ctx = err.get("ctx") or {}
    if etype == "extra_forbidden":
        if len(loc) == 1 and loc[0] in args:
            args.pop(loc[0])
            notes.append(f"dropped unknown argument {loc[0]}")
            return True
        return False
    spot = _navigate(args, loc)
    if spot is None:
        return False
    container, key, walked = spot
    value = container[key]
    where = _loc_text(walked)
    if etype in _BOUND_TYPES:
        if not _is_size_field(walked):
            return False  # a scientific value (a duration, a dose, a threshold) is never silently changed
        kind = _BOUND_TYPES[etype]
        new = _clamp(value, kind, ctx.get(kind))
        if new is None or new == value:
            return False
        container[key] = new
        notes.append(f"{where} {_show(value)}→{_show(new)}")
        return True
    if etype == "too_long" and isinstance(value, list):
        cap = ctx.get("max_length")
        if not isinstance(cap, int) or cap < 0 or len(value) <= cap:
            return False
        container[key] = value[:cap]
        notes.append(f"{where} cut to the first {cap} of {len(value)} items")
        return True
    if etype == "string_type" and isinstance(value, dict):
        text = _string_from_object(value)
        if text is None:
            return False
        container[key] = text
        notes.append(f"{where} object→{_show(text)}")
        return True
    if etype == "list_type" and isinstance(value, str):
        container[key] = [value]
        notes.append(f"{where} {_show(value)}→[{_show(value)}]")
        return True
    return False


def _forbids_extra(schema: Type[BaseModel]) -> bool:
    return str((getattr(schema, "model_config", {}) or {}).get("extra", "")) == "forbid"


def _errors(schema: Type[BaseModel], args: Dict[str, Any], ignore: AbstractSet[str]) -> Optional[List[Any]]:
    """The validation errors outside the ignored fields ([] when valid); None when validation itself broke."""
    try:
        schema.model_validate(args)
        return []
    except ValidationError as exc:
        return [e for e in exc.errors() if not (e.get("loc") and str(e["loc"][0]) in ignore)]
    except Exception:  # noqa: BLE001 - a schema that raises something else is not ours to repair
        return None


def repair_arguments(
    schema: Optional[Type[BaseModel]],
    args: Dict[str, Any],
    *,
    drop_unknown: bool = False,
    ignore_fields: Optional[AbstractSet[str]] = None,
) -> Optional[Tuple[Dict[str, Any], List[str]]]:
    """(repaired arguments, notes) when args fail the schema only for a repairable reason and the repaired
    arguments validate; None otherwise (including when args already validate). args is never modified.

    drop_unknown: also drop arguments the schema does not declare (for callers that forbid them themselves).
    ignore_fields: fields left alone and whose errors are not counted (plan arguments that hold references)."""
    if schema is None or not isinstance(args, dict):
        return None
    ignore = frozenset(ignore_fields or ())
    work = copy.deepcopy(args)
    notes: List[str] = []
    fields = getattr(schema, "model_fields", {}) or {}
    extra_ok = str((getattr(schema, "model_config", {}) or {}).get("extra", "")) == "allow"
    if (drop_unknown or _forbids_extra(schema)) and not extra_ok:
        for k in [k for k in work if k not in fields and k not in ignore]:
            work.pop(k)
            notes.append(f"dropped unknown argument {k}")
    for _ in range(MAX_PASSES):
        errors = _errors(schema, work, ignore)
        if errors is None:
            return None
        if not errors:
            return (work, notes) if notes else None
        changed = False
        for err in errors:
            changed = _fix_one(work, err, notes) or changed
        if not changed:
            return None
    return (work, notes) if notes and _errors(schema, work, ignore) == [] else None


def adjustment_text(notes: Sequence[str], limit: int = 200) -> str:
    """'arguments adjusted: activity_limit 100→50; names cut to the first 8 of 9 items'."""
    text = "arguments adjusted: " + "; ".join(notes)
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = ["STRING_KEYS", "adjustment_text", "repair_arguments"]
