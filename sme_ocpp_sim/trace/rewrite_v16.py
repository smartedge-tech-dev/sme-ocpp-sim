"""OCPP 1.6 replay rewriting (v0.3 plan §3.7).

The CSMS assigns the ``transactionId`` in StartTransaction.conf, so a replay cannot re-use the recorded one: the
id the CSMS answers now is mapped from the recorded id, and every later ``transactionId`` the charger sends
(StopTransaction, MeterValues, DataTransfer payloads that carry the key) is replaced through that map. A recorded
id whose StartTransaction got no answer now (CALLERROR, timeout, no ``transactionId``) stays as it is and is listed
in :attr:`.rewrite.Rewriter.notes`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .rewrite import Rewriter, walk


class Rewriter16(Rewriter):
    version = "1.6"

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.tx_map: Dict[int, int] = {}

    def _tx(self, payload: Any) -> Any:
        def fn(key, value):
            if key == "transactionId" and isinstance(value, int) and not isinstance(value, bool):
                return self.tx_map.get(value, value)
            return value
        return walk(payload, fn)

    def call(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._tx(super().call(action, payload))

    def learn(self, action: str, recorded: Optional[Dict[str, Any]], replayed: Optional[Dict[str, Any]]) -> None:
        if action != "StartTransaction":
            return
        old = (recorded or {}).get("transactionId")
        new = (replayed or {}).get("transactionId")
        if isinstance(old, int) and isinstance(new, int):
            self.tx_map[old] = new
        elif isinstance(old, int):
            self.notes.append(f"transactionId {old}: StartTransaction got no transactionId now; later frames keep {old}")
