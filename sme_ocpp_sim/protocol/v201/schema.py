"""Optional runtime check of 2.0.1 payloads against the official JSON schemas (design §11.1 Q10).

The schemas are not shipped with this project. The user points :class:`SchemaValidator` at a directory
holding ``<Action>Request.json`` / ``<Action>Response.json`` (for example the OCA 2.0.1 package, or the
copy inside the ``ocpp`` package, see :func:`ocpp_package_schema_dir`). ``jsonschema`` comes with the
``schema`` extra: ``pip install "sme-ocpp-sim[schema]"``.

The check runs in addition to the pydantic models (``codec.parse``), never instead of them; the station
logic decides when to call it (M2). A violation raises :class:`CallError` with the same code choice as
the codec (see the Interpretations in ``codec.py``), so both paths answer alike. A dateTime ``format`` is
only checked when ``rfc3339-validator`` is installed as well (a ``jsonschema`` format-checker rule).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from ...rpc.errors import OCPP201_ERRORS, CallError

Kind = Literal["request", "response"]

# jsonschema validator keyword -> attribute name on OCPP201_ERRORS (mirrors codec._ERROR_TYPE_TO_CODE).
_KEYWORD_TO_CODE: dict[str, str] = {
    "required": "occurrence_constraint_violation",
    "minItems": "occurrence_constraint_violation",
    "maxItems": "occurrence_constraint_violation",
    "type": "type_constraint_violation",
    "format": "type_constraint_violation",
    "enum": "property_constraint_violation",
    "maxLength": "property_constraint_violation",
    "additionalProperties": "format_violation",
}


def _field(error: Any) -> str:
    """Field path in the codec's form (``a.b[0].c``); for required / additionalProperties the offending key."""
    path = error.json_path.removeprefix("$").removeprefix(".")
    key = None
    if isinstance(error.instance, dict):
        if error.validator == "required":
            key = next((k for k in error.validator_value if k not in error.instance), None)
        elif error.validator == "additionalProperties":
            known = error.schema.get("properties", {})
            key = next((k for k in error.instance if k not in known), None)
    if key is not None:
        path = f"{path}.{key}" if path else key
    return path or "<payload>"


def bundled_schema_dir(subprotocol: str = "ocpp2.1") -> Optional[Path]:
    """The schemas shipped in this package (``sme_ocpp_sim/schemas/<subprotocol>``; only ``ocpp2.1`` today, see
    THIRD-PARTY-NOTICES), or ``None`` when there are none for ``subprotocol``."""
    path = Path(__file__).resolve().parents[2] / "schemas" / subprotocol
    return path if path.is_dir() else None


def ocpp_package_schema_dir(version_dir: str = "v201") -> Optional[Path]:
    """The schema directory ``version_dir`` (``v201`` / ``v21``) of the installed ``ocpp`` package, or ``None``
    when it is not installed."""
    try:
        import ocpp  # noqa: PLC0415 - optional, dev-only package
    except ImportError:
        return None
    path = Path(ocpp.__file__).resolve().parent / version_dir / "schemas"
    return path if path.is_dir() else None


class SchemaValidator:
    """Validates payloads against the JSON schemas in ``schema_dir``; schemas are loaded on first use."""

    def __init__(self, schema_dir: Path | str) -> None:
        try:
            import jsonschema  # noqa: PLC0415 - optional extra
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise ImportError(
                'strict schema validation needs jsonschema: pip install "sme-ocpp-sim[schema]"'
            ) from e
        self._jsonschema = jsonschema
        self.schema_dir = Path(schema_dir)
        if not self.schema_dir.is_dir():
            raise FileNotFoundError(f"schema directory not found: {self.schema_dir}")
        self._validators: dict[str, Any] = {}

    def _validator(self, action: str, kind: Kind) -> Any:
        name = f"{action}{'Request' if kind == 'request' else 'Response'}"
        validator = self._validators.get(name)
        if validator is None:
            path = self.schema_dir / f"{name}.json"
            try:
                schema = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise CallError(OCPP201_ERRORS.not_implemented, f"no schema for action {action!r}") from None
            cls = self._jsonschema.validators.validator_for(schema)
            validator = cls(schema, format_checker=cls.FORMAT_CHECKER)
            self._validators[name] = validator
        return validator

    def validate(self, action: str, payload: Any, kind: Kind = "request") -> None:
        """Raise :class:`CallError` (2.0.1 code) if ``payload`` violates the schema of ``action``."""
        error = self._jsonschema.exceptions.best_match(self._validator(action, kind).iter_errors(payload))
        if error is None:
            return
        attr = _KEYWORD_TO_CODE.get(str(error.validator), "format_violation")
        if error.validator == "enum" and not isinstance(error.instance, str):
            attr = "type_constraint_violation"
        field = _field(error)
        raise CallError(getattr(OCPP201_ERRORS, attr), f"{field}: {error.message}", {"field": field})
