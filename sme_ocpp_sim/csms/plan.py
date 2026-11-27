"""Mini CSMS plan: scheduled CALLs per connection (``mini_csms.plan``).

Each connection gets its own :class:`PlanRunner` with the items that match the station (``charger`` pattern,
``ocpp_version``). An item fires ``at_s`` seconds after its trigger: ``connect`` (the WebSocket connection) or
``boot`` (the first BootNotification of the connection that was answered Accepted). A reconnecting station runs
the plan again. Every item is sent once per connection; its state (``waiting`` -> ``sent`` -> ``ok`` / ``error``,
``cancelled`` when the connection ends first) is visible in the session detail. The plan of a connection is
fixed when it connects: a changed ``mini_csms.plan`` applies to the next connection.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List

log = logging.getLogger(__name__)

Send = Callable[[str, dict], Awaitable[Dict[str, Any]]]


def matching_items(items: List[Any], cp_id: str, version: str) -> List[Any]:
    return [i for i in items
            if (i.charger is None or fnmatch.fnmatchcase(cp_id, i.charger))
            and (i.ocpp_version is None or i.ocpp_version == version)]


class PlanRunner:
    def __init__(self, items: List[Any], send: Send, states: List[dict],
                 clock: Callable[[], float] = time.time) -> None:
        """``items`` are ``MiniCsmsPlanItem``-like (at_s, after, action, payload); ``states`` is the list the
        session shows (filled here, one dict per item)."""
        self._items = list(items)
        self._send = send
        self._clock = clock
        self._tasks: set = set()
        self._fired: set = set()
        self.states = states
        states.clear()
        for n, i in enumerate(items):
            states.append({"index": n, "action": i.action, "after": i.after, "at_s": i.at_s,
                           "status": "waiting", "due_at": None, "sent_at": None, "result": None, "error": None})

    def trigger(self, event: str) -> None:
        """Start the items waiting for ``event`` (``connect`` / ``boot``); a repeated event does nothing."""
        if event in self._fired:
            return
        self._fired.add(event)
        for n, item in enumerate(self._items):
            if item.after == event:
                self.states[n]["due_at"] = self._clock() + item.at_s
                t = asyncio.create_task(self._run(n, item), name=f"csms-plan:{n}")
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)

    async def _run(self, n: int, item: Any) -> None:
        st = self.states[n]
        try:
            if item.at_s > 0:
                await asyncio.sleep(item.at_s)
            st.update(status="sent", sent_at=self._clock())
            res = await self._send(item.action, item.payload)
        except asyncio.CancelledError:
            if st["status"] in ("waiting", "sent"):
                st["status"] = "cancelled"
            raise
        except Exception as e:  # noqa: BLE001 - shown in the plan state
            st.update(status="error", error=f"{type(e).__name__}: {e}")
            return
        if res.get("ok"):
            st.update(status="ok", result=res.get("result"))
        else:
            st.update(status="error", error=res.get("error"))

    def cancel(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for st in self.states:
            if st["status"] in ("waiting", "sent"):
                st["status"] = "cancelled"
