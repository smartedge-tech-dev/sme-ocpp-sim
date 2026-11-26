"""Parse inbound 1.6J payloads into models (with CALLERROR mapping) and dump outbound ones."""

from __future__ import annotations

import copy
import json
from typing import Any, Literal

from pydantic import ValidationError

from ...rpc.errors import OCPP16_ERRORS, CallError
from .models import REQUESTS, RESPONSES, OcppModel

# pydantic error type -> attribute name on OCPP16_ERRORS. Meanings from OCPP-J 1.6
# §4.2.3 Table 7 "Valid Error Codes" (p.14); the OCPP-J errata (2025-04) §5.1/§5.2 (p.11-12)
# keeps the misspelled wire names and changes no meaning:
#   FormationViolation: "syntactically incorrect or not conform the PDU structure".
#   PropertyConstraintViolation: "syntactically correct but at least one field contains an
#     invalid value" (enum, maxLength).
#   OccurenceConstraintViolation: "at least one of the fields violates occurence constraints",
#     i.e. the CARD. column of the core spec message tables (required field missing, an array
#     below its 1..* minimum).
#   TypeConstraintViolation: "violates data type constraints (e.g. "somestring": 12)".
# Validation runs in pydantic strict JSON mode, so "1" never becomes 1 and 1.0 is not an int.
_ERROR_TYPE_TO_CODE: dict[str, str] = {
    # TODO(spec): Table 7 (p.14) also defines ProtocolError as "Payload for Action is
    # incomplete", which could be read as covering a missing required field too; the text
    # does not say which one applies. OccurenceConstraintViolation is the narrower match
    # (CARD. 1..1 violated) and what SteVe / the ocpp library send.
    "missing": "occurrence_constraint_violation",
    # Array cardinality (core spec CARD. 1..*, e.g. MeterValues.meterValue): occurrence.
    "too_short": "occurrence_constraint_violation",
    "too_long": "occurrence_constraint_violation",
    "int_type": "type_constraint_violation",
    "int_from_float": "type_constraint_violation",
    "float_type": "type_constraint_violation",
    "finite_number": "type_constraint_violation",
    "string_type": "type_constraint_violation",
    "bool_type": "type_constraint_violation",
    "list_type": "type_constraint_violation",
    "dict_type": "type_constraint_violation",
    "model_type": "type_constraint_violation",
    "model_attributes_type": "type_constraint_violation",
    "datetime_type": "type_constraint_violation",
    # TODO(spec): a string that is not a valid dateTime -> TypeConstraintViolation, since
    # dateTime is a core spec data type (§2.1 / message tables "FIELD TYPE"). Table 7 (p.14)
    # does not decide between this and PropertyConstraintViolation ("invalid value"): on the
    # JSON level the value has the right type (string), on the OCPP level it does not.
    "datetime_parsing": "type_constraint_violation",
    "datetime_from_date_parsing": "type_constraint_violation",
    "datetime_object_invalid": "type_constraint_violation",
    # Explicit JSON null for a field: no official schema allows type null, so null is a
    # wrong JSON data type -> TypeConstraintViolation (Table 7, p.14). Detected by
    # codec.parse after validation, since the models accept None for optional fields.
    "null_value": "type_constraint_violation",
    # A non-string input to an enum field is re-mapped to TypeConstraintViolation below.
    "enum": "property_constraint_violation",
    "string_too_long": "property_constraint_violation",
    # Only reached in strict mode; lenient mode strips unknown fields first.
    "extra_forbidden": "format_violation",
}
# Anything not in the table above is still a schema/PDU mismatch, which is the Table 7
# (p.14) definition of FormationViolation; GenericError ("any other error not covered")
# is left for non-validation failures.
_FALLBACK_CODE = "format_violation"

Kind = Literal["request", "response"]


def _path(loc: tuple) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def _strip(payload: Any, loc: tuple) -> None:
    node = payload
    for part in loc[:-1]:
        node = node[part]
    if isinstance(node, dict):
        node.pop(loc[-1], None)


def _code_for(err: dict) -> str:
    etype = err["type"]
    attr = _ERROR_TYPE_TO_CODE.get(etype, _FALLBACK_CODE)
    if etype == "enum" and not isinstance(err.get("input"), str):
        attr = "type_constraint_violation"
    return getattr(OCPP16_ERRORS, attr)


def _find_null(node: Any, loc: tuple = ()) -> tuple | None:
    if isinstance(node, dict):
        items = node.items()
    elif isinstance(node, list):
        items = enumerate(node)
    else:
        return None
    for key, value in items:
        if value is None:
            return loc + (key,)
        found = _find_null(value, loc + (key,))
        if found:
            return found
    return None


def _to_call_error(err: dict) -> CallError:
    loc = tuple(err["loc"])
    field = _path(loc) or "<payload>"
    return CallError(_code_for(err), f"{field}: {err['msg']}", {"field": field})


def model_for(action: str, kind: Kind = "request") -> type[OcppModel]:
    registry = REQUESTS if kind == "request" else RESPONSES
    try:
        return registry[action]
    except KeyError:
        raise CallError(OCPP16_ERRORS.not_implemented, f"unknown action {action!r}") from None


def parse(
    action: str, payload: Any, kind: Kind = "request", strict: bool = False
) -> tuple[OcppModel, list[str]]:
    """Validate ``payload`` for ``action`` and return ``(model, ignored_fields)``.

    Lenient (default): unknown fields, also nested ones, are dropped and returned as paths
    such as ``meterValue[0].foo``. Strict: an unknown field raises FormationViolation.
    Raises :class:`CallError` with an OCPP-J 1.6 code on any other violation.
    """
    model_cls = model_for(action, kind)
    if not isinstance(payload, dict):
        # Not a JSON object: "not conform the PDU structure" -> FormationViolation
        # (OCPP-J 1.6 §4.2.3 Table 7, p.14).
        raise CallError(OCPP16_ERRORS.format_violation, "payload must be a JSON object")
    ignored: list[str] = []
    work = payload
    while True:
        try:
            model = model_cls.model_validate_json(json.dumps(work), strict=True)
            null_loc = _find_null(work)
            if null_loc is not None:
                raise _to_call_error(
                    {"type": "null_value", "loc": null_loc, "msg": "must not be null"}
                )
            return model, ignored
        except ValidationError as exc:
            errors = exc.errors()
        extras = [e for e in errors if e["type"] == "extra_forbidden"]
        if not extras or strict:
            # Report the first error in pydantic's (field declaration) order.
            # TODO(spec): OCPP-J 1.6 §4.2.3 (p.14) and its errata only allow one errorCode
            # per CALLERROR and do not say which one wins when several fields fail.
            raise _to_call_error(extras[0] if strict and extras else errors[0])
        if work is payload:
            work = copy.deepcopy(payload)
        for e in extras:
            loc = tuple(e["loc"])
            if _path(loc) in ignored:  # no progress: never loop forever
                raise _to_call_error(e)
            _strip(work, loc)
            ignored.append(_path(loc))


def dump(model: OcppModel) -> dict:
    """Outbound payload: wire names, no ``None`` fields, datetimes as UTC ``...sss Z``."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)
