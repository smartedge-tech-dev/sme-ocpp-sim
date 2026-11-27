"""Charger model templates (plan M10): built-in ones plus user templates, on ``sme_sim_common.templates``.

A template is a set of charger defaults (hardware, identity and behaviour fields: :data:`TEMPLATE_FIELDS`). A
charger refers to one with ``template: <name>`` (and may pass ``params:``); every template field the charger does
not set itself comes from the template (``config.ChargerConfig`` expands it while validating, a dict field such as
``config_keys`` / ``session`` is merged with the charger's keys winning). Dumps of a templated charger leave out
the fields whose value equals the template's, so the YAML write-back, ``GET /api/devices/{id}/config`` and the
Excel export keep only the charger's own overrides. The comparison is with the template's value, not the model
default (``phases: 1`` on a 3-phase template is written even under ``exclude_defaults``), and dict fields are
compared per key: a charger that sets one ``config_keys`` / ``session`` key writes only that key, the template's
other keys keep following the template. Consequence: a field explicitly set to the template's value follows the
template when the template changes.

A user template that is gone (file deleted, then reloaded) while chargers still refer to it: the running chargers
keep their values, and a dump writes them in full (nothing to compare with), but loading such a config (start,
config reload, Excel import) fails with ``charger 'CP1': unknown template 'name' ...``.

Connection and security fields (``id``, ``csms_url``, ``ocpp_version``, ``password``, certificate files ...) are
never part of a template, so every template fits both OCPP 1.6 and 2.0.1 chargers (``config_keys`` in a user
template only take effect on 1.6 chargers; 2.0.1 Device Model ``variables`` are not template fields).

``TemplateRegistry`` is item based (a device = a list of named items, as in the Modbus / BACnet simulators). A
charger has no such items, so the registry is used through its ``extra_keys`` mechanism: the template fields are
the extra keys, the item list (``items``) is always empty. That gives the shared user-template directory
(``$SME_OCPP_SIM_TEMPLATES`` or ``~/.sme-ocpp-sim/templates/*.yaml``), ``{param}`` substitution, the catalog and
the ``/api/templates*`` routes of ``make_base_app`` (incl. "save a charger as user template"). User template file::

    name: my-wallbox
    title: My 7 kW wallbox
    params: {kw: 7}
    model: WB-{kw}
    max_power_w: "{kw}000"      # exactly one placeholder: the parameter's value with its type ("7000" here)
    config_keys: {MeterValueSampleInterval: "60"}

Only built-in models the physics supports (AC, 1 or 3 phases); DC charging is not modelled, so there is no DC
template. :func:`next_ids` is the id rule of the bulk create (``CP001`` -> ``CP002`` ...).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ValidationError
from sme_sim_common.templates import TemplateRegistry

APP_NAME = "sme-ocpp-sim"

#: Charger fields a template may set (``TemplateRegistry`` extra keys).
TEMPLATE_FIELDS = ("vendor", "model", "firmware", "connectors", "max_power_w", "voltage_v", "phases",
                   "line_to_line", "ramp_up_w_per_s", "ramp_down_w_per_s", "auth_mode", "boot_retry_s",
                   "meter_sample_interval_s", "require_subprotocol", "config_keys", "session", "maintenance")


class _NoItem(BaseModel):
    """Item model of the registry; charger templates have no items (see the module docstring)."""

    name: str


class ChargerTemplates(TemplateRegistry):
    """``TemplateRegistry`` for chargers: devices are keyed by ``id`` (not ``name``), user templates are checked
    as a whole charger, and the cache of :func:`template_values` is dropped on every (re)load."""

    def __init__(self) -> None:
        super().__init__(_NoItem, app_name=APP_NAME, items_attr="items", extra_keys=TEMPLATE_FIELDS)

    def register_charger(self, name: str, title: str, fields: Dict[str, Any], *, description: str,
                         params: Optional[Dict[str, Any]] = None) -> None:
        unknown = set(fields) - set(TEMPLATE_FIELDS)
        if unknown:
            raise ValueError(f"template {name}: not template fields {sorted(unknown)}")
        self.register(name, title, dict(params or {}), lambda _p: [], description)
        self.builtins[name]["extras"] = dict(fields)

    def _user_template(self, data: Any, path) -> Dict[str, Any]:
        t = super()._user_template(data, path)
        if t["build"]({**t["params"]}):
            raise ValueError("charger templates have no items; set the charger fields at the top level")
        from .config import ChargerConfig  # lazy: config imports this module lazily too

        try:  # the fields with the default params must make a valid charger
            ChargerConfig.model_validate({"id": "template-check", **_substituted(t)})
        except ValidationError as e:
            raise ValueError("; ".join(f"{'.'.join(map(str, err['loc'])) or 'template'}: "
                                       f"{str(err['msg']).removeprefix('Value error, ')}" for err in e.errors())) from None
        return t

    def load_user_templates(self, directory=None):
        _VALUES.clear()
        return super().load_user_templates(directory)

    def clear_user_templates(self) -> None:
        _VALUES.clear()
        super().clear_user_templates()

    # ``make_base_app``'s "save as template" route passes a ChargerConfig (key ``id``, no ``name``).
    def expand_device(self, dev: Any) -> List[Any]:
        return []

    def expand_device_extras(self, dev: Any) -> Dict[str, Any]:
        return {k: getattr(dev, k) for k in self.extra_keys if k in dev.model_fields_set}

    def device_as_template(self, dev: Any, name: str, title: Optional[str] = None,
                           description: Optional[str] = None) -> str:
        return super().device_as_template(dev, name, title, description or f"Saved from charger {dev.id}")


def _substituted(t: Dict[str, Any]) -> Dict[str, Any]:
    from sme_sim_common.templates import substitute

    return {k: substitute(v, dict(t["params"])) for k, v in (t.get("extras") or {}).items()}


REGISTRY = ChargerTemplates()
_loaded = False
#: ``(template, params) -> {field: value}`` of :func:`template_values` (cleared when user templates reload).
_VALUES: Dict[str, Dict[str, Any]] = {}


def registry() -> ChargerTemplates:
    """The registry, with the user templates loaded on first use (a missing directory is fine)."""
    global _loaded
    if not _loaded:
        _loaded = True
        REGISTRY.load_user_templates()
    return REGISTRY


def expand_charger(data: Dict[str, Any]) -> Dict[str, Any]:
    """A raw charger mapping with the template fields it does not set filled in from ``data["template"]``.
    Raises ``ValueError`` for an unknown template or a bad ``{param}``."""
    name = data.get("template")
    if not isinstance(name, str):
        return data  # None, or not a string: the field validator reports it
    params = data.get("params") or {}
    if not isinstance(params, dict):
        return data
    own = {k: data[k] for k in TEMPLATE_FIELDS if k in data}
    if registry().get(name) is None:  # e.g. a user template file deleted while chargers still refer to it
        raise ValueError(f"charger {str(data.get('id', '?'))!r}: unknown template {name!r} (known: "
                         f"{', '.join(registry().names())}); restore the user template file, or remove "
                         f"'template' from the charger and set its fields")
    return {**data, **registry().expand_extras(f"charger {str(data.get('id', '?'))!r}", name, params, own)}


def template_values(name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """The validated field values (model attributes) a template gives a charger; ``{}`` when it cannot be
    expanded (e.g. a user template that is gone)."""
    try:
        key = json.dumps([name, params], sort_keys=True, default=str)
    except (TypeError, ValueError):
        key = None
    if key is not None and key in _VALUES:
        return _VALUES[key]
    from .config import ChargerConfig

    out: Dict[str, Any] = {}
    try:
        fields = registry().expand_extras(name, name, params)
        ref = ChargerConfig.model_validate({"id": "template-ref", "template": name, "params": params})
        out = {k: getattr(ref, k) for k in fields if k in TEMPLATE_FIELDS}
    except (ValueError, TypeError):
        out = {}
    if key is not None:
        _VALUES[key] = out
    return out


_TRAILING = re.compile(r"^(.*?)(\d+)$")


def next_ids(first: str, count: int) -> List[str]:
    """``count`` ids counting up from the trailing digits of ``first`` with the same width (``CP-0009`` ->
    ``CP-0010``; wider when it overflows). Without trailing digits ``first`` is a prefix: ``CP-`` -> ``CP-1``,
    ``CP-2`` ... (a single id is ``first`` itself)."""
    if count == 1:
        return [first]
    m = _TRAILING.match(first)
    prefix, start, width = (m.group(1), int(m.group(2)), len(m.group(2))) if m else (first, 1, 1)
    return [f"{prefix}{start + i:0{width}d}" for i in range(count)]


# ------------------------------------------------------------------ built-in templates

_JP = "Japanese AC normal charging, single-phase 200 V"
REGISTRY.register_charger("jp-ac-3kw", "AC 3 kW, 1 phase 200 V 15 A (Japan)",
                          {"model": "SIM-AC3", "connectors": 1, "max_power_w": 3000.0, "voltage_v": 200.0,
                           "phases": 1, "line_to_line": False},
                          description=f"{_JP}, 15 A (e.g. a home or hotel outlet-type charger)")
REGISTRY.register_charger("jp-ac-6kw", "AC 6 kW, 1 phase 200 V 30 A (Japan)",
                          {"model": "SIM-AC6", "connectors": 1, "max_power_w": 6000.0, "voltage_v": 200.0,
                           "phases": 1, "line_to_line": False},
                          description=f"{_JP}, 30 A (the typical public / workplace wallbox)")
REGISTRY.register_charger("jp-ac-6kw-dual", "AC 6 kW x 2 connectors, 1 phase 200 V (Japan)",
                          {"model": "SIM-AC6D", "connectors": 2, "max_power_w": 6000.0, "voltage_v": 200.0,
                           "phases": 1, "line_to_line": False},
                          description=f"{_JP}, two connectors (EVSEs) of 6 kW each")
REGISTRY.register_charger("eu-ac-11kw", "AC 11 kW, 3 phase 230/400 V 16 A",
                          {"model": "SIM-AC11", "connectors": 1, "max_power_w": 11000.0, "voltage_v": 230.0,
                           "phases": 3, "line_to_line": False},
                          description="European three-phase wallbox, 16 A per phase (voltage given per phase)")
REGISTRY.register_charger("eu-ac-22kw", "AC 22 kW, 3 phase 230/400 V 32 A",
                          {"model": "SIM-AC22", "connectors": 1, "max_power_w": 22000.0, "voltage_v": 230.0,
                           "phases": 3, "line_to_line": False},
                          description="European three-phase charger, 32 A per phase (voltage given per phase)")
