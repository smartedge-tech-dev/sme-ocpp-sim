"""Payload matchers of scenario assertions (v0.3 plan §3.5, Q6: a small built-in matcher, no JSONPath, no Python).

A **path** is dotted: ``idTagInfo.status``, ``meterValue.0.sampledValue.0.value`` (an integer segment indexes a
list). A **matcher** is

- a literal (string / number / bool / null / list): equality (``1 == 1.0``; ``True`` is not ``1``);
- a mapping of operators, all of which must hold: ``in: [..]``, ``gt`` / ``ge`` / ``lt`` / ``le: n`` (numbers;
  a numeric string such as ``"12.5"`` compares as its number), ``re: "..."`` (``re.search`` on a string value)
  and ``exists: bool`` (whether the path is there at all).

A path that is not there matches only ``exists: false``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Mapping, Optional

OPERATORS = frozenset({"in", "gt", "ge", "lt", "le", "re", "exists"})
_MISSING: Any = object()


class MatcherError(ValueError):
    """A matcher that cannot be used (unknown operator, wrong operand type)."""


def check_matcher(m: Any) -> None:
    """Raise :class:`MatcherError` for a matcher :func:`matches` cannot evaluate (run at load time)."""
    if not isinstance(m, Mapping):
        return
    if not m:
        raise MatcherError("empty matcher (give a literal or at least one operator)")
    unknown = sorted(set(m) - OPERATORS)
    if unknown:
        raise MatcherError(f"unknown matcher operator(s) {unknown} (known: {sorted(OPERATORS)})")
    for op in ("gt", "ge", "lt", "le"):
        if op in m and (isinstance(m[op], bool) or not isinstance(m[op], (int, float))):
            raise MatcherError(f"{op}: needs a number")
    if "in" in m and not isinstance(m["in"], list):
        raise MatcherError("in: needs a list")
    if "exists" in m and not isinstance(m["exists"], bool):
        raise MatcherError("exists: needs true or false")
    if "re" in m:
        if not isinstance(m["re"], str):
            raise MatcherError("re: needs a string")
        try:
            re.compile(m["re"])
        except re.error as e:
            raise MatcherError(f"re: {e}") from None


def get_path(obj: Any, path: str) -> Any:
    """The value at dotted ``path`` in ``obj``, or the module's missing sentinel (see :func:`is_missing`)."""
    cur = obj
    for part in path.split(".") if path else ():
        if isinstance(cur, Mapping):
            cur = cur.get(part, _MISSING)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            i = int(part)
            cur = cur[i] if -len(cur) <= i < len(cur) else _MISSING
        else:
            return _MISSING
        if cur is _MISSING:
            return _MISSING
    return cur


def is_missing(v: Any) -> bool:
    return v is _MISSING


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def matches(value: Any, m: Any) -> bool:
    """Whether ``value`` (possibly missing) satisfies matcher ``m``."""
    if not isinstance(m, Mapping):
        return not is_missing(value) and _eq(value, m)
    if "exists" in m and m["exists"] != (not is_missing(value)):
        return False
    rest = set(m) - {"exists"}
    if not rest:
        return True
    if is_missing(value):
        return False
    if "in" in m and not any(_eq(value, x) for x in m["in"]):
        return False
    if "re" in m and not (isinstance(value, str) and re.search(m["re"], value)):
        return False
    n = _num(value)
    for op, ok in (("gt", lambda a, b: a > b), ("ge", lambda a, b: a >= b),
                   ("lt", lambda a, b: a < b), ("le", lambda a, b: a <= b)):
        if op in m and (n is None or not ok(n, float(m[op]))):
            return False
    return True


def match_all(obj: Any, checks: Mapping[str, Any]) -> Optional[str]:
    """``None`` when every ``path: matcher`` of ``checks`` holds for ``obj``, else the first mismatch, described."""
    for path, m in checks.items():
        v = get_path(obj, path)
        if not matches(v, m):
            shown = "missing" if is_missing(v) else json.dumps(v, ensure_ascii=False, default=str)
            return f"{path}: {shown} does not match {json.dumps(m, ensure_ascii=False, default=str)}"
    return None


def frame_payload(raw: str) -> Dict[str, Any]:
    """The payload of an OCPP-J frame text: a CALL's / CALLRESULT's payload, a CALLERROR as ``{errorCode,
    errorDescription, errorDetails}``; ``{}`` for anything else."""
    try:
        f = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(f, list) or not f:
        return {}
    if f[0] == 2 and len(f) >= 4:
        return f[3] if isinstance(f[3], dict) else {"_": f[3]}
    if f[0] == 3 and len(f) >= 3:
        return f[2] if isinstance(f[2], dict) else {"_": f[2]}
    if f[0] == 4 and len(f) >= 4:
        return {"errorCode": f[2], "errorDescription": f[3], "errorDetails": f[4] if len(f) > 4 else {}}
    return {}
