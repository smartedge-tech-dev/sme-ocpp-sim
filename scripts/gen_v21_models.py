"""Generate ``sme_ocpp_sim/protocol/v21/models.py`` from the vendored OCPP 2.1 JSON schemas.

Usage: ``python scripts/gen_v21_models.py`` (writes the module; ``--check`` exits 1 when it is stale).

The output follows the conventions of the hand-written 2.0.1 models (``protocol/v201/models.py``): snake_case
attributes with the wire name as alias, ``None`` defaults for optional fields, ``maxLength`` / array bounds as
constraints, class names = schema definition names / ``<Action>Request`` / ``<Action>Response``. Only the
structure is taken from the schemas: no ``description`` / ``comment`` text is copied (the schemas are
CC BY-ND 4.0, see THIRD-PARTY-NOTICES).

Enums stay compatible with the 2.0.1 station logic: an enum whose value set is unchanged reuses the 2.0.1 class;
an extended enum is typed ``Union[<2.0.1 class>, <2.1 extension class>]`` (known values become the 2.0.1 members,
so ``is`` comparisons keep working; new values become members of the extension class). A field that 2.1 turned
from an enum into a string (``IdTokenType.type``, ``chargingLimitSource``, ``connectorType``) is typed
``Union[<2.0.1 enum>, String<n>]`` for the same reason.
"""

from __future__ import annotations

import json
import keyword
import re
import sys
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "sme_ocpp_sim" / "schemas" / "ocpp2.1"
OUT = ROOT / "sme_ocpp_sim" / "protocol" / "v21" / "models.py"
sys.path.insert(0, str(ROOT))

from pydantic.alias_generators import to_camel  # noqa: E402

from sme_ocpp_sim.protocol.v201 import enums as e201  # noqa: E402

# 2.1 string fields that were 2.0.1 enums: property name -> 2.0.1 enum class (see the module docstring).
WIDENED = {"chargingLimitSource": "ChargingLimitSourceEnumType", "connectorType": "ConnectorEnumType"}
WIDENED_IN = {("IdTokenType", "type"): "IdTokenEnumType"}


def snake(name: str) -> str:
    s = re.sub(r"[.\-/ ]", "_", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"_+", "_", s).strip("_").lower()
    if not s or s[0].isdigit():
        s = "v_" + s
    return s + "_" if keyword.iskeyword(s) else s


def load() -> tuple[dict, dict]:
    defs: dict[str, dict] = {}
    messages: dict[str, dict] = {}
    for path in sorted(SCHEMAS.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        for name, d in schema.get("definitions", {}).items():
            d = {k: v for k, v in d.items() if k not in ("description", "javaType")}
            if name in defs and _strip(defs[name]) != _strip(d):
                raise SystemExit(f"definition {name} differs between schemas ({path.name})")
            defs[name] = d
        messages[path.stem] = schema
    return defs, messages


def _strip(node):
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k not in ("description", "javaType", "comment")}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


class Gen:
    def __init__(self, defs: dict) -> None:
        self.defs = defs
        self.strings: set[int] = set()
        self.enum_src: list[str] = []
        self.enum_type: dict[str, str] = {}
        self.uses_201: set[str] = set()
        #: (enum, value) pairs of a reused 2.0.1 enum that the 2.1 schema no longer has (``--check`` fails on them).
        self.removed: list[tuple[str, str]] = []
        for name, d in sorted(defs.items()):
            if "enum" in d:
                self.enum_type[name] = self._enum(name, d["enum"])

    def _enum(self, name: str, values: list[str]) -> str:
        old = getattr(e201, name, None)
        old_values = [m.value for m in old] if isinstance(old, type) and issubclass(old, Enum) else None
        if old_values is not None and set(old_values) == set(values):
            self.uses_201.add(name)
            return f"e201.{name}"
        new = [v for v in values if old_values is None or v not in old_values]
        self.removed += [(name, v) for v in (old_values or ()) if v not in values]
        cls = name if old_values is None else f"{name}Ext21"
        lines = [f"class {cls}(str, Enum):"]
        lines += [f"    {snake(v)} = {v!r}" for v in new]
        self.enum_src.append("\n".join(lines))
        if old_values is None:
            return cls
        self.uses_201.add(name)
        return f"Union[e201.{name}, {cls}]"

    def string(self, node: dict) -> str:
        if node.get("format") == "date-time":
            return "DateTime"
        n = node.get("maxLength")
        if n is None:
            return "str"
        self.strings.add(n)
        return f"String{n}"

    def type_of(self, owner: str, prop: str, node: dict) -> str:
        if "$ref" in node:
            ref = node["$ref"].rsplit("/", 1)[-1]
            return self.enum_type.get(ref, ref)
        t = node.get("type")
        if t == "string":
            widened = WIDENED_IN.get((owner, prop)) or WIDENED.get(prop)
            base = self.string(node)
            if widened:
                self.uses_201.add(widened)
                return f"Annotated[Union[e201.{widened}, {base}], Field(union_mode='left_to_right')]"
            return base
        if t == "integer":
            return "int"
        if t == "number":
            return "float"
        if t == "boolean":
            return "bool"
        if t == "array":
            item = self.type_of(owner, prop, node["items"])
            bounds = [f"{k}={node[s]}" for s, k in (("minItems", "min_length"), ("maxItems", "max_length"))
                      if s in node]
            return f"Annotated[list[{item}], Field({', '.join(bounds)})]" if bounds else f"list[{item}]"
        if t is None:
            return "Any"
        raise SystemExit(f"{owner}.{prop}: unsupported type {t!r}")

    def model(self, name: str, node: dict) -> str:
        lines = [f"class {name}(OcppModel):"]
        if node.get("additionalProperties", False) is not False:
            lines.append('    model_config = ConfigDict(extra="allow")')
        required = set(node.get("required", ()))
        props = node.get("properties", {})
        for prop, sub in props.items():
            attr = snake(prop) if prop != "customData" else "custom_data"
            typ = self.type_of(name, prop, sub)
            alias = None if to_camel(attr) == prop else prop
            if prop in required:
                default = f" = Field(alias={alias!r})" if alias else ""
                lines.append(f"    {attr}: {typ}{default}")
            else:
                default = f"Field(default=None, alias={alias!r})" if alias else "None"
                lines.append(f"    {attr}: Optional[{typ}] = {default}")
        if not props:
            lines.append("    pass")
        return "\n".join(lines)


REMOVED: list[tuple[str, str]] = []


def render() -> str:
    defs, messages = load()
    g = Gen(defs)
    REMOVED[:] = g.removed
    models = [g.model(n, d) for n, d in sorted(defs.items()) if d.get("type") == "object"]
    msgs = [g.model(n, s) for n, s in sorted(messages.items())]
    head = f'''"""OCPP 2.1 message models: GENERATED by ``scripts/gen_v21_models.py`` from ``schemas/ocpp2.1``; do not edit.

Every 2.1 message and data type is here; :data:`REQUESTS` / :data:`RESPONSES` (``protocol/v21/__init__``
tables) decide which actions the simulator answers. Conventions: see ``protocol/v201/models.py`` and the
generator's docstring.
"""
# ruff: noqa: E501

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Optional, Union

from pydantic import ConfigDict, Field, StringConstraints

from ..v201 import enums as e201
from ..v201.models import DateTime, OcppModel
'''
    strings = "\n".join(f"String{n} = Annotated[str, StringConstraints(max_length={n})]" for n in sorted(g.strings))
    body = "\n\n\n".join(g.enum_src + models + msgs)
    names = sorted(set(d for d, v in defs.items() if v.get("type") == "object") | set(messages))
    tail = "\n".join(f"{n}.model_rebuild()" for n in names)
    return f"{head}\n{strings}\n\n\n{body}\n\n\n{tail}\n"


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        for enum_name, value in REMOVED:
            print(f"removed in 2.1: {enum_name} value {value!r} is still reused through Union[e201.{enum_name}, ...]",
                  file=sys.stderr)
        if REMOVED:
            sys.exit(1)
        sys.exit(0 if OUT.read_text(encoding="utf-8") == text else 1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(text.splitlines())} lines)")
