"""Version-neutral stores for local authorization and reservations (v0.2 M6).

The Local Authorization List (1.6 §3.5.2 / 2.0.1 C13, C14, D01, D02), the Authorization Cache (1.6 §3.5.1 /
2.0.1 C10 - C12) and the reservation record (1.6 §3.11, §5.13 / 2.0.1 H01 - H04) hold the same data in both
versions. Only the data and its bookkeeping live here; which configuration key / Device Model variable enables
them, how an entry is used to authorize, the wire messages and every status answer belong to the version
station (``protocol/v16`` / ``protocol/v201``). This module imports nothing of the shell (``runtime.charger``) or
of a version package, only the domain types of ``protocol.base``.

Interpretations (not defined by the spec)
-----------------------------------------
- The stores live in memory, per station object: they survive a reconnect and a CSMS Reset (1.6 errata 3.4,
  2.0.1 C10.FR.02 / D01.FR.10: persisted across reboots), but not a restart of the simulator.
- A list update is checked as a whole before anything changes: duplicate id tokens (1.6 §6.41 / D01.FR.06),
  a Full update entry without authorization info (1.6 §7.1 "Required when UpdateType is Full"; 2.0.1 does not
  say, the same rule is used) or a resulting list longer than ``max_entries`` answer :attr:`ListUpdate.FAILED`
  and leave the list and its version unchanged.
- The cache keeps at most ``max_entries`` tokens; a new token in a full cache first removes the oldest entry that
  is not valid, then the oldest valid one (1.6 §3.5.1, C10.FR.07). "Oldest" is the least recently updated.
- A cache entry whose expiry has passed (1.6 ``expiryDate``, 2.0.1 ``cacheExpiryDateTime``, C10.FR.13), or that
  was last updated more than ``lifetime_s`` ago (2.0.1 AuthCacheLifeTime, C10.FR.10), is changed to ``Expired``
  (both versions allow that instead of removing it). Part 2 §2.4.3 gives AuthCacheLifeTime no special value;
  ``lifetime_s`` ``None`` or <= 0 means "no lifetime limit" (the 2.0.1 variable has minLimit 1, so 0 only comes
  from a direct caller). A cache with ``max_entries`` <= 0 stores nothing.
- Reservation matching (1.6 §5.13 "the incoming idTag or the parent idTag match the idTag or parent idTag of the
  reservation", read pairwise; H03.FR.01 - FR.06 "matches the specific idToken ... or when the associated
  groupIdToken matches"): the presented token equals the reserved token, or the presented token's parent / group
  equals the reservation's parent / group when the reservation has one. Cross matches (a presented parent equal
  to the reserved token, or a presented token equal to the reserved parent) do not count.
- A reservation is *claimed* by a connector while a token that matched it waits there (for the cable, or a
  RequestStart / RemoteStart being authorized). A claimed reservation stays stored (its expiry still runs) but no
  longer holds a connector of its own; it is consumed when the transaction starts and becomes unclaimed again
  when the waiting token is dropped.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Dict, Iterable, Optional, Sequence, Tuple

from ..protocol.base import AuthResult, IdTokenInfo

#: Default capacity of the Authorization Cache (neither version has a key for it in 1.6; 2.0.1 reports it as the
#: maxLimit of AuthCacheCtrlr.Entries).
DEFAULT_CACHE_MAX = 1000
#: Default capacity of the Local Authorization List (1.6 LocalAuthListMaxLength / 2.0.1 LocalAuthListCtrlr.Entries
#: maxLimit).
DEFAULT_LIST_MAX = 1000
#: Default number of entries one SendLocalList may carry (1.6 SendLocalListMaxLength / 2.0.1 ItemsPerMessage).
DEFAULT_LIST_ITEMS_PER_MESSAGE = 100


class ListUpdate(str, Enum):
    """Outcome of :meth:`LocalAuthList.update` (the version maps it to its ``UpdateStatus``)."""

    ACCEPTED = "Accepted"
    FAILED = "Failed"
    VERSION_MISMATCH = "VersionMismatch"


class LocalAuthList:
    """The Local Authorization List: changed only by :meth:`update` (1.6 §3.5.2, D01.FR.09)."""

    def __init__(self, max_entries: int = DEFAULT_LIST_MAX) -> None:
        self.max_entries = max_entries
        #: 0 = no list received yet (1.6 §5.10, D02.FR.02).
        self.version = 0
        self._entries: Dict[str, IdTokenInfo] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, token: str) -> Optional[IdTokenInfo]:
        return self._entries.get(token)

    def entries(self) -> Dict[str, IdTokenInfo]:
        return dict(self._entries)

    def update(self, version: int, full: bool, items: Sequence[Tuple[str, Optional[IdTokenInfo]]]) -> ListUpdate:
        """Apply a SendLocalList. ``items`` are ``(token, entry)``; ``entry`` ``None`` removes the token in a
        Differential update. The version is set on success, also for an empty list (D01.FR.04 / FR.05)."""
        tokens = [t for t, _ in items]
        if len(set(tokens)) != len(tokens):
            return ListUpdate.FAILED
        if full:
            if any(e is None for _, e in items):
                return ListUpdate.FAILED
            new: Dict[str, IdTokenInfo] = {t: e for t, e in items if e is not None}
        else:
            if version <= self.version:  # 1.6 §7.47, D01.FR.19
                return ListUpdate.VERSION_MISMATCH
            new = dict(self._entries)
            for t, e in items:
                if e is None:
                    new.pop(t, None)
                else:
                    new[t] = e
        if len(new) > self.max_entries:
            return ListUpdate.FAILED
        self._entries, self.version = new, version
        return ListUpdate.ACCEPTED


class AuthCache:
    """The Authorization Cache (see the module docstring for eviction and expiry)."""

    def __init__(self, max_entries: int = DEFAULT_CACHE_MAX) -> None:
        self.max_entries = max_entries
        self._entries: "OrderedDict[str, Tuple[IdTokenInfo, datetime]]" = OrderedDict()  # oldest update first

    def __len__(self) -> int:
        return len(self._entries)

    def tokens(self) -> Iterable[str]:
        return list(self._entries)

    def update(self, token: str, entry: IdTokenInfo, now: datetime) -> None:
        if self.max_entries <= 0:
            return
        if token in self._entries:
            del self._entries[token]
        elif len(self._entries) >= self.max_entries:
            stale = next((t for t, (e, _) in self._entries.items() if not e.valid(now)), None)
            del self._entries[stale if stale is not None else next(iter(self._entries))]
        self._entries[token] = (entry, now)

    def get(self, token: str, now: datetime, lifetime_s: Optional[int] = None) -> Optional[IdTokenInfo]:
        """The entry of ``token``, changed to ``Expired`` first when its expiry or ``lifetime_s`` has passed."""
        item = self._entries.get(token)
        if item is None:
            return None
        entry, updated = item
        too_old = lifetime_s is not None and lifetime_s > 0 and (now - updated).total_seconds() > lifetime_s
        if entry.status is AuthResult.ACCEPTED and (entry.expired(now) or too_old):
            entry = replace(entry, status=AuthResult.EXPIRED)
            self._entries[token] = (entry, updated)
        return entry

    def clear(self) -> None:
        self._entries.clear()


@dataclass(eq=False)
class Reservation:
    """One accepted ReserveNow. ``connector`` 0 = no specific connector / EVSE (1.6 connectorId 0, 2.0.1 without
    ``evseId``)."""

    id: int
    connector: int
    id_token: str
    expiry: datetime
    #: 1.6 ``parentIdTag`` / 2.0.1 ``groupIdToken.idToken`` (named as in ``IdTokenInfo``).
    parent_id_token: Optional[str] = None
    #: Expiry timer (``loop.call_later``), owned by the station.
    timer: Optional[asyncio.TimerHandle] = None
    #: Connector / EVSE whose waiting token claimed this reservation (module docstring), ``None`` = unclaimed.
    claimed_by: Optional[int] = None

    def matches(self, token: str, parent_id_token: Optional[str] = None) -> bool:
        """The presented token is the reserved one, or its parent / group is the reservation's (module docstring)."""
        if token == self.id_token:
            return True
        return self.parent_id_token is not None and parent_id_token == self.parent_id_token

    def cancel_timer(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
