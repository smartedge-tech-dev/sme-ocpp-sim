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

    ``bad_call_frame`` is the code for a CALL whose frame structure is wrong (element count, action not a
    string); ``None`` = ``format_violation``. A CALL whose payload is not a JSON object is always answered with
    ``format_violation``.

    ``max_message_id_length``: a received CALL with a longer MessageId is answered with ``rpc_framework_error``
    (``None`` = not checked). ``max_error_description_length``: an outgoing errorDescription is cut to this
    length (``None`` = not cut).
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
    bad_call_frame: Optional[str] = None
    max_message_id_length: Optional[int] = None
    max_error_description_length: Optional[int] = None


# OCPP-J 1.6 §4.2.3 misspells two codes; the 1.6 errata keeps the misspellings on the
# wire ("known issues that will not be fixed").
# 1.6J defines no answer to a frame without a usable uniqueId: such a frame is logged and dropped.
OCPP16_ERRORS = ErrorCodes(
    format_violation="FormationViolation",
    occurrence_constraint_violation="OccurenceConstraintViolation",
)

# OCPP-J 2.0.1 (the protocol semantics live in protocol/v201/; only the wire table is here so the RPC layer
# stays version neutral). The codes are those of Part 4 §4.3 Table 8 (the normative RPC table):
# "FormatViolation" and "OccurrenceConstraintViolation" are spelled correctly there. Part 2 B06 / B08 spell the
# latter "OccurenceConstraintViolation" (the 1.6 misspelling); we follow Part 4 Table 8.
# Spec-based rules (Part 4):
#   - §4.2.3: a frame whose MessageId cannot be read is answered with CALLERROR MessageId "-1" +
#     RpcFrameworkError ("Content of the call is not a valid RPC Request", Table 8).
#   - §4.4: an unknown MessageTypeId (with a readable MessageId) is answered with MessageTypeNotSupported.
#   - Table 8: a CALL whose frame structure is wrong (element count, action not a string) is not a valid RPC
#     request -> RpcFrameworkError; a payload that is not a JSON object -> FormatViolation ("Payload for Action
#     is syntactically incorrect").
#   - §4.1.4 / Table 7: MessageId is string[36] (a longer received MessageId -> RpcFrameworkError) and
#     errorDescription is string[255] (we cut what we send).
# OCPP-J 1.6 (Table 3) also limits the message ID to 36 characters, but it has no RpcFrameworkError and no
# errorDescription length, so the 1.6 table keeps its earlier wire behaviour (no check, no cut).
OCPP201_ERRORS = ErrorCodes(
    rpc_framework_error="RpcFrameworkError",
    message_type_not_supported="MessageTypeNotSupported",
    bad_frame_reply="-1",
    bad_call_frame="RpcFrameworkError",
    max_message_id_length=36,
    max_error_description_length=255,
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
