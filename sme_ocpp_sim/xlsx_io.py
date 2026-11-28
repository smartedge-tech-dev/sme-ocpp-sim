"""Excel (.xlsx) import / export of the charger list (plan M10, ``sme_sim_common.xlsx.XlsxSchema``).

Workbook sheets (row 1 = column headers, row 2 = ``#`` help texts, ``#`` rows are ignored on import):

* ``chargers`` - one row per charger: the common fields as columns (``session`` flattened into ``ev_model`` /
  ``initial_soc`` / ``target_soc``), every other field in ``extra`` as a YAML flow mapping;
* ``config_keys`` - ``charger`` / ``key`` / ``value``: the OCPP 1.6 ``config_keys`` of each charger;
* ``variables`` - one row per 2.0.1 Device Model ``variables`` entry (``charger`` + the entry's fields; the
  definition of a user-defined variable in ``extra``);
* ``ev_models`` - the ``ev_models`` list (``curve`` / ``max_dc_kw`` in ``extra``).

An import replaces ``chargers`` and ``ev_models`` as a whole (a missing optional sheet means "none"); every other
block (``simulation``, ``csms_defaults``, ``mini_csms`` ...) is kept from the running config. The engine reload
then restarts only the chargers whose settings changed.

Export: a charger without a template has every column filled with its value; a templated charger leaves the
columns its template provides empty (blank = from the template, see :mod:`.templates_builtin`). Other blank cells
mean the field's default. Passwords are exported as :data:`.config.PASSWORD_MASK`; importing the mask keeps the
stored password of the charger with that id (an unknown id with the mask is an error).

Text columns stay text: ``0123`` or ``1e5`` written as text cells are read back unchanged (a cell Excel stores as a
number is converted with ``sme_sim_common.xlsx.convert``: ``123.0`` -> ``"123"``). Errors name the sheet, the row
and the column: ``chargers row 4, column 'ocpp_version': ...``.

Formula injection: every exported text cell is a plain string cell, never a formula. ``sme_sim_common``'s
``XlsxSchema.write`` (1.2.1+) writes a text starting with ``= + - @ ＠``, a tab or a CR (``FORMULA_PREFIXES``) as a
literal string with Excel's quote prefix, so editing the cell in Excel keeps it text. The value itself is unchanged
(no ``'`` is added), so the import reads it back as the same text. Limits (also upstream): a ``chargers`` sheet with
more than :data:`MAX_IMPORT_ROWS` rows, or another sheet with ten times as many, is rejected (``Sheet.max_rows`` /
``XlsxSchema.max_rows``); the upload size is capped by ``make_base_app(max_import_bytes=)`` in :mod:`.web`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError
from sme_sim_common.xlsx import Sheet, SheetRow, XlsxSchema

from .config import PASSWORD_MASK, ChargerConfig, EvModelConfig, SimConfig
from .templates_builtin import registry

SESSION_COLUMNS = ("ev_model", "initial_soc", "target_soc")
#: Data rows read per sheet on import (``chargers``; the other sheets get ten times as many); more is an error
#: (``sme_sim_common.xlsx`` row cap: ``Sheet.max_rows`` / ``XlsxSchema.max_rows``).
MAX_IMPORT_ROWS = 5000


def _text(v: Any) -> str:
    """A cell as OCPP text: TRUE / FALSE -> ``true`` / ``false``, an integral number without ``.0``."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip() if isinstance(v, str) else str(v)


CHARGERS = Sheet("chargers", [
    ("id", "Charger id = chargePointId / station id (required, unique, no ':')"),
    ("template", "Charger template (blank = none); its fields fill the blank cells"),
    ("ocpp_version", "1.6 or 2.0.1 (blank = csms_defaults)"),
    ("csms_url", "ws:// or wss:// CSMS URL (blank = csms_defaults); the id is appended"),
    ("password", "Basic Auth password (profile 1 / 2); ******** keeps the stored one"),
    ("vendor", "chargePointVendor / vendorName (max 20)"),
    ("model", "chargePointModel / model (max 20)"),
    ("serial", "Serial number (max 25)"),
    ("firmware", "Firmware version (max 50)"),
    ("connectors", "Connectors (2.0.1: EVSEs with one connector each)"),
    ("max_power_w", "Max power per connector (W)"),
    ("voltage_v", "Nominal supply voltage (V)"),
    ("phases", "1, 2 or 3"),
    ("line_to_line", "TRUE = voltage_v is line-to-line (3-phase 200 V delta)"),
    ("auth_mode", "local_only / csms_only / local_then_csms / accept_all"),
    ("id_tags", "Local id tags, comma separated"),
    ("ev_model", "EV plugged in (preset or ev_models id)"),
    ("initial_soc", "SoC at transaction start (%)"),
    ("target_soc", "SoC where the EV stops (%)"),
    ("extra", "Other fields as YAML flow mapping, e.g. {verify_tls: false}"),
], kinds={"connectors": "int", "max_power_w": "number", "voltage_v": "number", "phases": "int",
          "line_to_line": "bool", "id_tags": "list", "initial_soc": "number", "target_soc": "number"},
    required=("id",), max_rows=MAX_IMPORT_ROWS)

CONFIG_KEYS = Sheet("config_keys", [
    ("charger", "Charger id (chargers sheet)"),
    ("key", "OCPP 1.6 configuration key, e.g. HeartbeatInterval"),
    ("value", "Value as text (blank = empty string)"),
], kinds={"value": _text}, required=("charger", "key"), required_headers=("value",), optional=True)

VARIABLES = Sheet("variables", [
    ("charger", "Charger id (chargers sheet, OCPP 2.0.1)"),
    ("component", "Component name, e.g. OCPPCommCtrlr"),
    ("component_instance", "Component instance (blank = none)"),
    ("evse", "EVSE id (blank = station level)"),
    ("connector", "Connector id (needs evse)"),
    ("variable", "Variable name, e.g. HeartbeatInterval"),
    ("variable_instance", "Variable instance (blank = none)"),
    ("value", "Actual value"),
    ("target", "Target value"),
    ("min_set", "MinSet value"),
    ("max_set", "MaxSet value"),
    ("extra", "User-defined variable: {data_type: integer, mutability: ReadWrite}"),
], kinds={"evse": "int", "connector": "int", "value": _text, "target": _text, "min_set": _text,
          "max_set": _text}, required=("charger", "component", "variable"), optional=True)

EV_MODELS = Sheet("ev_models", [
    ("id", "EV model id (session.ev_model refers to it)"),
    ("capacity_kwh", "Battery capacity (kWh)"),
    ("max_ac_kw", "Max AC charging power (kW)"),
    ("max_current_a", "Max current per phase (A)"),
    ("phases", "1, 2 or 3"),
    ("min_current_a", "Min current (A)"),
    ("efficiency", "Charging efficiency (0-1]"),
    ("extra", "Other fields, e.g. {curve: [[0, 1.0], [80, 0.5], [100, 0.1]]}"),
], kinds={"capacity_kwh": "number", "max_ac_kw": "number", "max_current_a": "number", "phases": "int",
          "min_current_a": "number", "efficiency": "number"}, required=("id", "capacity_kwh", "max_ac_kw"),
    optional=True)

SHEETS = (CHARGERS, CONFIG_KEYS, VARIABLES, EV_MODELS)


# ------------------------------------------------------------------ export

def export_rows(cfg: SimConfig) -> Dict[str, List[Dict[str, Any]]]:
    chargers, keys, variables = [], [], []
    for c in cfg.chargers:
        d = c.model_dump(mode="json", exclude_defaults=True)  # a templated charger: only its overrides
        if c.template is None:  # fill every column with the effective value
            full = c.model_dump(mode="json")
            full.update(full.pop("session"))
            for col in CHARGERS.headers:
                if col not in d and full.get(col) is not None and col not in SESSION_COLUMNS:
                    d[col] = full[col]
            d["session"] = {**{k: full[k] for k in SESSION_COLUMNS}, **d.get("session", {})}
        if d.get("password") is not None:
            d["password"] = PASSWORD_MASK
        for k, v in d.pop("config_keys", {}).items():
            keys.append({"charger": c.id, "key": k, "value": v})
        for v in d.pop("variables", []):
            variables.append({"charger": c.id, **v})
        d.update(d.pop("session", {}))
        chargers.append({"id": c.id, **d})
    evs = [e.model_dump(mode="json", exclude_defaults=True) | {"id": e.id} for e in cfg.ev_models]
    return {"chargers": chargers, "config_keys": keys, "variables": variables, "ev_models": evs}


# ------------------------------------------------------------------ import

def _msg(err: dict) -> str:
    return str(err["msg"]).removeprefix("Value error, ")


def _column(sheet: Sheet, loc: Tuple[Any, ...]) -> str:
    return f", column {loc[0]!r}" if loc and loc[0] in sheet.headers else (", column 'extra'" if loc else "")


def _charger_errors(e: ValidationError, row: int, var_rows: List[int], cid: str) -> List[str]:
    out = []
    for err in e.errors():
        loc = tuple(err["loc"])
        tail = f"{'.'.join(map(str, loc))}: " if loc else ""
        if loc[:1] == ("session",) and len(loc) > 1:
            out.append(f"chargers row {row}, column {loc[1]!r}: {_msg(err)}")
        elif loc[:1] == ("variables",) and len(loc) > 1 and isinstance(loc[1], int) and loc[1] < len(var_rows):
            out.append(f"variables row {var_rows[loc[1]]}{_column(VARIABLES, loc[2:])}: "
                       f"{'.'.join(map(str, loc[2:])) + ': ' if loc[2:] else ''}{_msg(err)}")
        elif loc[:1] == ("config_keys",):
            out.append(f"config_keys (charger {cid!r}): {tail}{_msg(err)}")
        else:
            out.append(f"chargers row {row}{_column(CHARGERS, loc)}: {tail}{_msg(err)}")
    return out


def _rows_errors(e: ValidationError, sheet: Sheet, row: int) -> List[str]:
    return [f"{sheet.title} row {row}{_column(sheet, tuple(err['loc']))}: "
            f"{'.'.join(map(str, err['loc'])) + ': ' if err['loc'] else ''}{_msg(err)}" for err in e.errors()]


def build(tables: Dict[str, List[SheetRow]], base: Optional[SimConfig], errors: List[str],
          warnings: List[str]) -> Optional[SimConfig]:
    base = base or SimConfig()
    others = {k: v for k, v in base.model_dump().items() if k not in ("chargers", "ev_models")}

    evs: List[EvModelConfig] = []
    ev_rows: Dict[str, int] = {}
    for r in tables["ev_models"]:
        if not r.ok:
            continue
        try:
            ev = EvModelConfig.model_validate(r.merged())
        except ValidationError as e:
            errors.extend(_rows_errors(e, EV_MODELS, r.row_no))
            continue
        if ev.id in ev_rows:
            errors.append(f"ev_models row {r.row_no}, column 'id': duplicate ev_models id {ev.id!r} "
                          f"(also row {ev_rows[ev.id]})")
            continue
        ev_rows[ev.id] = r.row_no
        evs.append(ev)

    # id -> (row, charger mapping or None when the row has errors already, rows of its variables)
    rows: Dict[str, Tuple[int, Optional[Dict[str, Any]], List[int]]] = {}
    for r in tables["chargers"]:
        d = r.merged()
        cid = d.get("id")
        if cid in rows:
            errors.append(f"chargers row {r.row_no}, column 'id': duplicate charger id {cid!r} "
                          f"(also row {rows[cid][0]})")
            continue
        if not r.ok:
            if cid is not None:
                rows[cid] = (r.row_no, None, [])  # known id for the other sheets, row already reported
            continue
        session = {k: d.pop(k) for k in SESSION_COLUMNS if k in d}
        if session:
            d["session"] = {**(d.get("session") or {}), **session}
        if d.get("template") is not None and registry().get(str(d["template"])) is None:
            errors.append(f"chargers row {r.row_no}, column 'template': unknown template {d['template']!r} "
                          f"(known: {', '.join(registry().names())})")
            rows[cid] = (r.row_no, None, [])
            continue
        rows[cid] = (r.row_no, d, [])

    for r in tables["config_keys"]:
        if not r.ok:
            continue
        cid, key = r.values["charger"], r.values["key"]
        if cid not in rows:
            errors.append(f"config_keys row {r.row_no}, column 'charger': unknown charger {cid!r}")
            continue
        if rows[cid][1] is None:
            continue
        keys = rows[cid][1].setdefault("config_keys", {})
        if key in keys:
            errors.append(f"config_keys row {r.row_no}, column 'key': duplicate key {key!r} for charger {cid!r}")
        keys[key] = r.values.get("value", "")
    for r in tables["variables"]:
        if not r.ok:
            continue
        v = r.merged()
        cid = v.pop("charger")
        if cid not in rows:
            errors.append(f"variables row {r.row_no}, column 'charger': unknown charger {cid!r}")
            continue
        if rows[cid][1] is None:
            continue
        rows[cid][1].setdefault("variables", []).append(v)
        rows[cid][2].append(r.row_no)

    stored = {c.id: c.password for c in base.chargers}
    chargers: List[ChargerConfig] = []
    for cid, (row, d, var_rows) in rows.items():
        if d is None:
            continue
        if d.get("password") == PASSWORD_MASK and stored.get(cid) is None:
            errors.append(f"chargers row {row}, column 'password': {PASSWORD_MASK} keeps a stored password, but "
                          f"charger {cid!r} has none; enter the password")
            continue
        try:
            c = ChargerConfig.model_validate(d)
        except ValidationError as e:
            errors.extend(_charger_errors(e, row, var_rows, cid))
            continue
        # The rules across blocks (security profile, ev_model, variables), located at this row. This validates
        # ``others`` and ``ev_models`` once per charger: their cross-block errors carry no charger index, so one
        # validation of the whole list could not name the row. Bounded by MAX_IMPORT_ROWS; fast enough there.
        try:
            SimConfig.model_validate({**others, "ev_models": evs, "chargers": [c]})
        except ValidationError as e:
            errors.extend(f"chargers row {row}: {_msg(err)}" for err in e.errors())
            continue
        chargers.append(c)
    if errors:
        return None
    return SimConfig.model_validate({**others, "ev_models": evs, "chargers": chargers})


SCHEMA = XlsxSchema(SHEETS, export_rows=export_rows, build=build, template_file="sme-ocpp-sim-template.xlsx",
                     export_file="sme-ocpp-sim-chargers.xlsx", max_rows=MAX_IMPORT_ROWS * 10)


def export_xlsx(cfg: SimConfig) -> Tuple[bytes, List[str]]:
    """``(bytes, warnings)``; raises ``sme_sim_common.xlsx.XlsxUnavailable`` without openpyxl."""
    return SCHEMA.export_xlsx(cfg)


def import_xlsx(data: bytes, base: Optional[SimConfig] = None) -> Tuple[Optional[SimConfig], List[str]]:
    """``(config, warnings)`` or ``(None, errors)``; never raises."""
    return SCHEMA.import_xlsx(data, base)
