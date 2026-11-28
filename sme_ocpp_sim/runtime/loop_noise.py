"""Event loop noise filter (Windows proactor).

On Windows, ``asyncio``'s proactor transport shuts the socket down in ``_call_connection_lost``; when the peer has
already reset the connection (a CSMS refusing the TLS client certificate, a dropped network) that raises
``ConnectionResetError`` / ``ConnectionAbortedError``, which the loop's default exception handler prints as
"Exception in callback ..._call_connection_lost" with a full traceback. The connection loss itself is already
reported by the charger runtime, so the traceback is only noise: :func:`install_proactor_filter` turns exactly
that case into a debug log line and leaves every other exception to the previous handler.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

_MARK = "_call_connection_lost"
_FILTER_ATTR = "_ocppsim_proactor_filter"


def _is_proactor_noise(context: Dict[str, Any]) -> bool:
    if not isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
        return False
    return _MARK in repr(context.get("handle")) or _MARK in str(context.get("message", ""))


def install_proactor_filter(loop: asyncio.AbstractEventLoop, platform: Optional[str] = None) -> bool:
    """Install the filter on ``loop`` (Windows only; idempotent). True when it was installed by this call.
    ``platform`` overrides ``sys.platform`` (tests)."""
    if (platform or sys.platform) != "win32":
        return False
    prev = loop.get_exception_handler()
    if getattr(prev, _FILTER_ATTR, False):
        return False

    def handler(lp: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
        if _is_proactor_noise(context):
            log.debug("ignored after connection loss: %s: %r", context.get("message"), context.get("exception"))
            return
        if prev is not None:
            prev(lp, context)
        else:
            lp.default_exception_handler(context)

    setattr(handler, _FILTER_ATTR, True)
    loop.set_exception_handler(handler)
    return True
