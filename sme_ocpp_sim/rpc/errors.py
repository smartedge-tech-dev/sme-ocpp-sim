"""CALLERROR codes per OCPP-J version and the CallError exception."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ErrorCodes:
    """Wire names of the CALLERROR codes for one OCPP-J version.

    The attribute names are version neutral; the values are what goes on the wire.
    ``rpc_framework_error`` / ``message_type_not_supported`` are ``None`` where the version has no such code
    (an unknown MessageTypeId is then answered with ``protocol_error``).

    ``bad_frame_reply`` is the per-version policy for a frame whose uniqueId cannot be read: ``None`` = log and
    drop it, otherwise the uniqueId of the CALLERROR that answers it (code ``rpc_framework_error``, or
    ``protocol_error`` where the version has none).
    """

    not_implemented: str = "NotImplemented"
    not_supported: str = "NotSupported"
    internal_error: str = "InternalError"
    protocol_error: str = "ProtocolError"
    security_error: str = "SecurityError"
    format_violation: str = "FormatViolation"
    property_constraint_violation: str = "PropertyConstraintViolation"
    occurrence_constraint_violation: str = "OccurrenceConstraintViolation"
    type_constraint_violation: str = "TypeConstraintViolation"
    generic_error: str = "GenericError"
    rpc_framework_error: Optional[str] = None
    message_type_not_supported: Optional[str] = None
    bad_frame_reply: Optional[str] = None


# OCPP-J 1.6 §4.2.3 misspells two codes; the 1.6 errata keeps the misspellings on the
# wire ("known issues that will not be fixed").
# 1.6J defines no answer to a frame without a usable uniqueId: such a frame is logged and dropped.
OCPP16_ERRORS = ErrorCodes(
    format_violation="FormationViolation",
    occurrence_constraint_violation="OccurenceConstraintViolation",
)

# OCPP-J 2.0.1 (the protocol semantics live in protocol/v201/; only the wire table is here so the RPC layer
# stays version neutral). "FormatViolation" is spelled correctly, "OccurenceConstraintViolation" keeps the
# 1.6 misspelling (the ocpp library, ocpp/exceptions.py, marks exactly these two as the 2.0.1 names), and
# RpcFrameworkError / MessageTypeNotSupported are new.
# Interpretations (not settled by the JSON schemas; the ocpp library defines neither code and logs and drops
# a frame it cannot read, so it does not decide them either):
#   - A frame whose uniqueId cannot be read is answered with CALLERROR uniqueId "-1" + RpcFrameworkError
#     (design §5 spike, issue #34), not dropped as in 1.6.
#   - An unknown MessageTypeId with a readable uniqueId is answered with MessageTypeNotSupported.
OCPP201_ERRORS = ErrorCodes(
    occurrence_constraint_violation="OccurenceConstraintViolation",
    rpc_framework_error="RpcFrameworkError",
    message_type_not_supported="MessageTypeNotSupported",
    bad_frame_reply="-1",
)
OCPP21_ERRORS = ErrorCodes(rpc_framework_error="RpcFrameworkError")


class CallError(Exception):
    """A CALLERROR.

    Raised by :meth:`RpcConnection.call` when the peer answers with CALLERROR, and
    raised by a handler to make the RPC layer answer with that CALLERROR.
    """

    def __init__(self, code: str, description: str = "", details: Optional[dict] = None):
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description
        self.details = details or {}
