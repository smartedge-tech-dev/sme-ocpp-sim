"""OCPP 2.0.1 / 2.1 replay rewriting (v0.3 plan §3.7).

The Charging Station generates the ``transactionId`` (TransactionEvent ``transactionInfo.transactionId``), so a
replay would re-use the ids of the original run and a CSMS that keeps its transactions would see duplicates. With
``tx_suffix`` (default on) every ``transactionId`` string the charger sends - in CALLs and in the recorded answers
to CSMS CALLs (e.g. RequestStartTransactionResponse) - gets the run suffix ``-<run_id>``; the id is cut from the
left side so the result stays within the 36 characters of ``identifierString``. 2.1 uses the same rules.
"""

from __future__ import annotations

from typing import Any, Dict

from .rewrite import Rewriter, walk

#: Maximum length of a transactionId (identifierString[36]).
MAX_TX_ID = 36


class Rewriter201(Rewriter):
    version = "2.0.1"

    def __init__(self, *, tx_suffix: bool = True, **kw) -> None:
        super().__init__(**kw)
        self.tx_suffix = tx_suffix and bool(self.run_id)
        self.tx_map: Dict[str, str] = {}

    def tx_id(self, old: str) -> str:
        if not self.tx_suffix:
            return old
        new = self.tx_map.get(old)
        if new is None:
            suffix = f"-{self.run_id}"
            new = self.tx_map[old] = old[:MAX_TX_ID - len(suffix)] + suffix
        return new

    def _tx(self, payload: Any) -> Any:
        return walk(payload, lambda k, v: self.tx_id(v) if k == "transactionId" and isinstance(v, str) else v)

    def call(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._tx(super().call(action, payload))

    def reply(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._tx(super().reply(action, payload))


class Rewriter21(Rewriter201):
    version = "2.1"
