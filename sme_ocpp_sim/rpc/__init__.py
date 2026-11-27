"""Version-neutral OCPP-J RPC layer. ``websockets`` is imported only inside this package."""

from .connection import FrameCallback, FrameInfo, Handler, RpcConnection, after_this_reply
from .errors import OCPP16_ERRORS, OCPP21_ERRORS, OCPP201_ERRORS, CallError, ErrorCodes
from .transport import Backoff, HandshakeError, TransportConfig, open_connection, wait_event, wait_or_stop

__all__ = [
    "Backoff", "CallError", "ErrorCodes", "FrameCallback", "FrameInfo", "Handler", "HandshakeError",
    "OCPP16_ERRORS", "OCPP201_ERRORS", "OCPP21_ERRORS", "RpcConnection", "TransportConfig",
    "after_this_reply", "open_connection", "wait_event", "wait_or_stop",
]
