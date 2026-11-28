"""Verdict output (v0.3 plan §3.5, Q5): the ``ocppsim-verdict/1`` JSON is always written; JUnit XML on request.
Exit codes of the headless run: 0 pass, 1 fail, 2 error / aborted (and a scenario or config that cannot load).

JUnit: ``testsuites`` with one ``testsuite`` (the scenario), one ``testcase`` per assertion step (``expect`` /
``expect_not`` / ``await_csms``) and per action step that did not pass (an action error is a JUnit error);
``classname`` = track. A setup error or an abort adds a ``scenario`` case with an ``error``. Characters XML 1.0
cannot carry are removed.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Union

EXIT_CODES = {"pass": 0, "fail": 1}
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _x(v: Any) -> str:
    return _XML_ILLEGAL.sub("", str(v))


def exit_code(result: str) -> int:
    return EXIT_CODES.get(result, 2)


def to_json(verdict: Dict[str, Any]) -> str:
    return json.dumps(verdict, ensure_ascii=False, indent=2) + "\n"


def to_junit(verdict: Dict[str, Any]) -> str:
    cases = [s for s in verdict.get("steps", []) if not s["kind"].startswith("do:") or s["status"] != "pass"]
    count = lambda st: sum(1 for s in cases if s["status"] in st)  # noqa: E731
    extra = verdict.get("error") or ("aborted" if verdict.get("result") == "aborted" else None)
    totals = {"tests": str(len(cases) + bool(extra)), "failures": str(count(("fail",))),
              "errors": str(count(("error",)) + bool(extra)), "skipped": str(count(("skipped", "aborted"))),
              "time": f"{verdict.get('wall_s') or 0:.3f}"}
    root = ET.Element("testsuites", name=_x(verdict.get("scenario")), **totals)
    suite = ET.SubElement(root, "testsuite", name=_x(verdict.get("scenario")), **totals)
    if verdict.get("started"):
        suite.set("timestamp", str(verdict["started"]))
    props = ET.SubElement(suite, "properties")
    for k in ("result", "seed", "sim_s", "tool_version", "trace"):
        ET.SubElement(props, "property", name=k, value="" if verdict.get(k) is None else _x(verdict[k]))
    if extra:
        case = ET.SubElement(suite, "testcase", classname="scenario", name="scenario")
        ET.SubElement(case, "error", message=_x(extra))
    for s in cases:
        case = ET.SubElement(suite, "testcase", classname=_x(s["track"]), name=_x(f"{s['id']} {s['kind']}"))
        msg = s.get("message") or ""
        if s.get("evidence"):
            msg += f" [frames {', '.join(map(str, s['evidence']))}]"
        tag = {"fail": "failure", "error": "error", "skipped": "skipped", "aborted": "skipped"}.get(s["status"])
        if tag is not None:
            ET.SubElement(case, tag, message=_x(msg))
        elif msg:
            ET.SubElement(case, "system-out").text = _x(msg)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def write(path: Union[str, Path], text: str) -> None:
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
