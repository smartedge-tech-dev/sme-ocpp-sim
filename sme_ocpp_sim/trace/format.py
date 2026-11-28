"""Trace file format ``ocppsim-trace`` (v0.3 plan §3.6).

A trace is a JSON Lines file. Version 1 (written by :class:`.recorder.TraceRecorder`) has four line types:

* ``{"type": "header", "format": "ocppsim-trace", "version": 1, ...}`` - always the first line: ``tool_version``,
  ``created`` (ISO wall time), ``time_scale``, ``sim_start`` (ISO sim time when the recording started), ``masked``
  (always true: secrets are masked as in the frame log), ``part`` (0, 1, ... for a rotated recording; ``previous``
  names the part before) and ``chargers`` (``id``, ``ocpp_version``, ``connectors``, ``csms_url`` without
  credentials).
* ``{"type": "frame", <FrameEntry fields>, "sim_t": s}`` - one OCPP-J frame; ``seq`` / ``ts`` / ``direction`` mean
  what they mean in the frame log (``direction`` seen from the charger). ``sim_t`` = sim seconds since the
  recording started (``null`` when unknown, e.g. a frame log export).
* ``{"type": "event", "ts", "sim_t", "charger", "event", "detail"}`` - something that is not a frame: connection
  lifecycle (``connected`` / ``disconnected``), operator / scenario actions (``plug``, ``swipe``, ``start`` ...:
  the :mod:`.scenario.ops` function names), ``clock`` (time_scale / running changed) and ``chargers`` (the charger
  list changed by a reload).
* ``{"type": "footer", "ended", "frames", "events", "dropped", "next"}`` - the last line of a part that was closed
  properly; ``dropped`` = lines lost because the write queue was full, ``next`` = the file name of the following
  part (rotation) or ``null``.

A file without a header (the frame log's ``frames.jsonl``: one FrameEntry object per line) is version 0; it is read
the same way (every line a frame). Unknown line types and unknown fields are ignored, so v1 readers accept
additions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union
from urllib.parse import urlsplit, urlunsplit

FORMAT = "ocppsim-trace"
VERSION = 1
LINE_TYPES = ("header", "frame", "event", "footer")
#: FrameEntry fields every frame line carries.
FRAME_FIELDS = ("seq", "ts", "charger", "direction", "message_type", "uid", "action", "raw", "note")


class TraceError(ValueError):
    """A file that is not a readable trace."""


def strip_credentials(url: Optional[str]) -> Optional[str]:
    """``url`` without ``user:password@`` (a trace never carries the Basic Auth secret)."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def dumps(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


def header(*, tool_version: str, created: str, time_scale: float, sim_start: Optional[str],
           chargers: List[Dict[str, Any]], part: int = 0, previous: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "header", "format": FORMAT, "version": VERSION, "tool_version": tool_version,
                           "created": created, "time_scale": time_scale, "sim_start": sim_start, "masked": True,
                           "part": part, "chargers": chargers}
    if previous is not None:
        out["previous"] = previous
    return out


def frame_line(entry: Any, sim_t: Optional[float]) -> Dict[str, Any]:
    """A frame line from a :class:`.framelog.FrameEntry` (or a dict with its fields)."""
    d = entry if isinstance(entry, dict) else entry.to_dict()
    return {"type": "frame", **{k: d.get(k) for k in FRAME_FIELDS}, "sim_t": sim_t}


def event_line(ts: float, sim_t: Optional[float], charger: Optional[str], event: str,
               detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"type": "event", "ts": ts, "sim_t": sim_t, "charger": charger, "event": event, "detail": detail or {}}


def footer(*, ended: str, frames: int, events: int, dropped: int, next_part: Optional[str] = None) -> Dict[str, Any]:
    return {"type": "footer", "ended": ended, "frames": frames, "events": events, "dropped": dropped,
            "next": next_part}


@dataclass
class Trace:
    """A trace read into memory (all parts of a rotated recording joined)."""

    version: int                                   # 0 (no header) or 1
    header: Optional[Dict[str, Any]] = None        # the first part's header
    frames: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    footer: Optional[Dict[str, Any]] = None        # the last part's footer (None = not closed properly)
    dropped: int = 0                               # sum over every part's footer
    bad_lines: List[int] = field(default_factory=list)   # 1-based line numbers that were not JSON objects
    parts: List[str] = field(default_factory=list)

    @property
    def chargers(self) -> Dict[str, Dict[str, Any]]:
        """Charger id -> its header entry (empty for v0)."""
        return {c["id"]: c for c in (self.header or {}).get("chargers", []) if isinstance(c, dict) and "id" in c}

    def ocpp_version(self, charger: str, default: Optional[str] = None) -> Optional[str]:
        return self.chargers.get(charger, {}).get("ocpp_version") or default


def parse_lines(lines: Iterable[str], trace: Optional[Trace] = None, *, first_part: bool = True) -> Trace:
    """Parse the lines of one file (part) into ``trace`` (a new one by default)."""
    out = trace
    for no, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            if out is None:
                out = Trace(version=0)
            out.bad_lines.append(no)
            continue
        kind = obj.get("type")
        if out is None:
            if kind == "header":
                if obj.get("format") != FORMAT:
                    raise TraceError(f"line 1: format {obj.get('format')!r} is not {FORMAT!r}")
                if not isinstance(obj.get("version"), int) or obj["version"] < 1:
                    raise TraceError(f"line 1: bad version {obj.get('version')!r}")
                out = Trace(version=obj["version"], header=obj)
                continue
            out = Trace(version=0)
        if kind == "header":
            if first_part and out.header is None and no == 1:
                out.header = obj
            continue
        if kind is None and out.version == 0 and "raw" in obj:
            out.frames.append({"type": "frame", **{k: obj.get(k) for k in FRAME_FIELDS}, "sim_t": obj.get("sim_t")})
        elif kind == "frame":
            out.frames.append(obj)
        elif kind == "event":
            out.events.append(obj)
        elif kind == "footer":
            out.footer = obj
            out.dropped += int(obj.get("dropped") or 0)
    return out if out is not None else Trace(version=0)


def load(path: Union[str, Path], *, follow: bool = True) -> Trace:
    """Read a trace file; with ``follow`` the parts named by the footers' ``next`` are read too (when present)."""
    p = Path(path)
    trace: Optional[Trace] = None
    seen = set()
    while True:
        with p.open(encoding="utf-8") as fh:
            trace = parse_lines(fh, trace, first_part=trace is None)
        trace.parts.append(str(p))
        seen.add(p.resolve())
        nxt = (trace.footer or {}).get("next") if follow and trace.version >= 1 else None
        if not nxt:
            break
        q = p.with_name(Path(str(nxt)).name)  # a bare file name beside this part, never another directory
        if q.resolve() in seen or not q.exists():
            break
        trace.footer = None
        p = q
    return trace


def from_entries(entries: Iterable[Any]) -> Trace:
    """A v0 trace of frame log entries (the in-memory ring buffer: no events, no sim time)."""
    return Trace(version=0, frames=[frame_line(e, None) for e in entries])
