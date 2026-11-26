"""Parse inbound 1.6J payloads into models (with CALLERROR mapping) and dump outbound ones."""

from __future__ import annotations

import copy
import json
from typing import Any, Literal

from pydantic import ValidationError

from ...rpc.errors import OCPP16_ERRORS, CallError
from .models import REQUESTS, RESPONSES, OcppModel

# pydantic error type -> attribute name on OCPP16_ERRORS (OCPP-J 1.6 §4.2.3 meanings):
#   OccurenceConstraintViolation: a required field is missing (occurrence constraint).
#   TypeConstraintViolation: a field has the wrong data type (e.g. "somestring": 12).
#   PropertyConstraintViolation: right type, but the value is not allowed (enum, length).
#   FormationViolation: payload does not conform to the PDU structure of the action.
# Validation runs in pydantic strict JSON mode, so "1" never becomes 1 and 1.0 is not an int.
_ERROR_TYPE_TO_CODE: dict[str, str] = {
    "missing": "occurrence_constraint_violation",
    # TODO(spec): an empty array where the schema says minItems=1 (MeterValues.meterValue,
    # .sampledValue) is treated as a cardinality (occurrence) violation.
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
    # TODO(spec): a string that is not a valid dateTime is mapped to TypeConstraintViolation
    # (dateTime is a spec data type); PropertyConstraintViolation would also be defensible.
    "datetime_parsing": "type_constraint_violation",
    "datetime_from_date_parsing": "type_constraint_violation",
    "datetime_object_invalid": "type_constraint_violation",
    # TODO(spec): explicit JSON null for a field (the schemas allow no null type); detected
    # by codec.parse after validation, since the models accept None for optional fields.
    "null_value": "type_constraint_violation",
    # A non-string input to an enum field is re-mapped to TypeConstraintViolation below.
    "enum": "property_constraint_violation",
    "string_too_long": "property_constraint_violation",
    # Only reached in strict mode; lenient mode strips unknown fields first.
    "extra_forbidden": "format_violation",
}
# TODO(spec): anything not in the table above falls back to FormationViolation.
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
        # TODO(spec): a payload that is not a JSON object is treated as a formation error.
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
            # TODO(spec): the spec does not say which code wins when several fields fail.
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
