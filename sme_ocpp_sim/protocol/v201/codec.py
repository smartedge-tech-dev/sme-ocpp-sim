"""Parse inbound OCPP 2.0.1 payloads into models (with CALLERROR mapping) and dump outbound ones.

Same contract as ``protocol/v16/codec.py`` (lenient by default, strict on request, one CALLERROR per
failure), but written against the 2.0.1 wire table :data:`~sme_ocpp_sim.rpc.errors.OCPP201_ERRORS`.

Interpretations (not defined by the spec's JSON schemas, which only say what is valid, not which
CALLERROR code an invalid payload gets):

- Missing required field / array below ``minItems`` or above ``maxItems`` ->
  OccurrenceConstraintViolation (the cardinality is the "occurrence constraint"; Part 4 Table 8 spelling).
  The ``ocpp`` library (``ocpp/messages.py``) answers a missing field with ProtocolError instead; we keep the
  narrower occurrence code, as the 1.6 codec does.
- Value outside an enum / string longer than ``maxLength`` -> PropertyConstraintViolation ("a field
  contains an invalid value"). AMBIGUOUS: Part 4 Table 8 does not settle the maxLength case. The ``ocpp``
  library uses TypeConstraintViolation for maxLength; the JSON type is right there, only the value is not,
  so we keep the property code.
- Wrong JSON type (``"1"`` for an integer, ``1.5`` for an integer, ``null``, a string that is no dateTime
  string) -> TypeConstraintViolation.
- Unknown field (strict mode only; AMBIGUOUS: Part 4 Table 8 does not settle it), a payload that is not a
  JSON object (Table 8 "syntactically incorrect"), anything else the schema rejects -> FormatViolation (the
  ``ocpp`` library does the same for additionalProperties and its fallback).
- A dateTime without a UTC offset (``2026-01-01T00:00:00``) is accepted and read as UTC, as in the 1.6
  codec: the schema's ``format: date-time`` asks for an offset, but a simulator tolerates the peer here.
  The optional ``schema.SchemaValidator`` rejects it when ``rfc3339-validator`` is installed.
- An action with no model (unknown or not implemented in this build) -> NotImplemented, as in 1.6.
- ``null`` is accepted only inside the untyped ``data`` field and as a vendor property of ``customData``,
  the two places where the schema allows any JSON value.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Literal

from pydantic import ValidationError

from ...rpc.errors import OCPP201_ERRORS, CallError
from .models import REQUESTS, RESPONSES, OcppModel

# pydantic error type -> attribute name on OCPP201_ERRORS (see the module docstring).
# Validation runs in pydantic strict JSON mode, so "1" never becomes 1 and 1.0 is not an int.
_ERROR_TYPE_TO_CODE: dict[str, str] = {
    "missing": "occurrence_constraint_violation",
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
    "datetime_parsing": "type_constraint_violation",
    "datetime_from_date_parsing": "type_constraint_violation",
    "datetime_object_invalid": "type_constraint_violation",
    # Explicit JSON null: detected by parse() after validation (the models accept None for optional fields).
    "null_value": "type_constraint_violation",
    # A non-string input to an enum field is re-mapped to TypeConstraintViolation in _code_for.
    "enum": "property_constraint_violation",
    "string_too_long": "property_constraint_violation",
    # Only reached in strict mode; lenient mode strips unknown fields first.
    "extra_forbidden": "format_violation",
}
_FALLBACK_CODE = "format_violation"

# Fields whose value is any JSON (schema without "type"): not searched for null. Matched by key at any
# depth; today only DataTransfer.data. Revisit when a later message adds a typed field named "data".
_OPAQUE_FIELDS = frozenset({"data"})

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
    return getattr(OCPP201_ERRORS, attr)


def _find_null(node: Any, loc: tuple = ()) -> tuple | None:
    if isinstance(node, dict):
        items = node.items()
    elif isinstance(node, list):
        items = enumerate(node)
    else:
        return None
    in_custom_data = bool(loc) and loc[-1] == "customData"
    for key, value in items:
        if key in _OPAQUE_FIELDS or (in_custom_data and key != "vendorId"):
            continue
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


def model_for(action: str, kind: Kind = "request", requests: dict | None = None,
              responses: dict | None = None) -> type[OcppModel]:
    """Model of ``action``; ``requests`` / ``responses`` replace the 2.0.1 tables (OCPP 2.1 passes its own)."""
    registry = (REQUESTS if requests is None else requests) if kind == "request" else (
        RESPONSES if responses is None else responses)
    try:
        return registry[action]
    except KeyError:
        raise CallError(OCPP201_ERRORS.not_implemented, f"unknown action {action!r}") from None


def parse(
    action: str, payload: Any, kind: Kind = "request", strict: bool = False,
    requests: dict | None = None, responses: dict | None = None,
) -> tuple[OcppModel, list[str]]:
    """Validate ``payload`` for ``action`` and return ``(model, ignored_fields)``.

    Lenient (default): unknown fields, also nested ones, are dropped and returned as paths
    such as ``meterValue[0].foo``. Strict: an unknown field raises FormatViolation.
    Raises :class:`CallError` with an OCPP-J 2.0.1 code on any other violation.
    """
    model_cls = model_for(action, kind, requests, responses)
    if not isinstance(payload, dict):
        raise CallError(OCPP201_ERRORS.format_violation, "payload must be a JSON object")
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
            # One errorCode per CALLERROR: report the first error in pydantic's (field declaration) order.
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


class Codec:
    """``parse`` / ``model_for`` / ``dump`` bound to one version's model tables.

    The adapter holds a ``Codec`` as a class attribute so that OCPP 2.1 (``protocol.v21``) reuses this module
    with its own tables. The CALLERROR codes are the same in 2.0.1 and 2.1 (Part 4 §4.3)."""

    def __init__(self, requests: dict, responses: dict) -> None:
        self.requests, self.responses = requests, responses

    def model_for(self, action: str, kind: Kind = "request") -> type[OcppModel]:
        return model_for(action, kind, self.requests, self.responses)

    def parse(self, action: str, payload: Any, kind: Kind = "request",
              strict: bool = False) -> tuple[OcppModel, list[str]]:
        return parse(action, payload, kind, strict, self.requests, self.responses)

    @staticmethod
    def dump(model: OcppModel) -> dict:
        return dump(model)


#: The 2.0.1 codec.
CODEC = Codec(REQUESTS, RESPONSES)
