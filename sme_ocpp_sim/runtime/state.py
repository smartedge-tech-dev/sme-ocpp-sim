"""Connector state machine over :class:`DomainStatus` (design §3.1).

The allowed transitions follow the OCPP 1.6 StatusNotification state table (rows A..I, "from" state per row).
TODO(spec): the table below is from memory; verify cell by cell against OCPP 1.6 §4.9 (StatusNotification).
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Optional

from ..protocol.base import DomainStatus as S


class IllegalTransition(ValueError):
    pass


_ALL = frozenset(S)

TRANSITIONS: Dict[S, FrozenSet[S]] = {
    S.AVAILABLE: _ALL - {S.AVAILABLE, S.FINISHING},
    S.PREPARING: frozenset({S.AVAILABLE, S.CHARGING, S.SUSPENDED_EV, S.SUSPENDED_EVSE, S.FINISHING, S.FAULTED}),
    S.CHARGING: frozenset({S.AVAILABLE, S.SUSPENDED_EV, S.SUSPENDED_EVSE, S.FINISHING, S.UNAVAILABLE, S.FAULTED}),
    S.SUSPENDED_EV: frozenset({S.AVAILABLE, S.CHARGING, S.SUSPENDED_EVSE, S.FINISHING, S.UNAVAILABLE,
                               S.FAULTED}),
    S.SUSPENDED_EVSE: frozenset({S.AVAILABLE, S.CHARGING, S.SUSPENDED_EV, S.FINISHING, S.UNAVAILABLE,
                                 S.FAULTED}),
    S.FINISHING: frozenset({S.AVAILABLE, S.PREPARING, S.UNAVAILABLE, S.FAULTED}),
    S.RESERVED: frozenset({S.AVAILABLE, S.PREPARING, S.UNAVAILABLE, S.FAULTED}),
    S.UNAVAILABLE: frozenset({S.AVAILABLE, S.PREPARING, S.CHARGING, S.SUSPENDED_EV, S.SUSPENDED_EVSE,
                              S.FAULTED}),
    S.FAULTED: _ALL - {S.FAULTED},
}

#: Connector 0 (the whole charger) only reports these.
CHARGER_TRANSITIONS: Dict[S, FrozenSet[S]] = {
    S.AVAILABLE: frozenset({S.UNAVAILABLE, S.FAULTED}),
    S.UNAVAILABLE: frozenset({S.AVAILABLE, S.FAULTED}),
    S.FAULTED: frozenset({S.AVAILABLE, S.UNAVAILABLE}),
}


class ConnectorState:
    """Status of one connector (``connector_id`` 0 = whole charger)."""

    def __init__(self, connector_id: int, initial: S = S.AVAILABLE):
        self.connector_id = connector_id
        self.table = CHARGER_TRANSITIONS if connector_id == 0 else TRANSITIONS
        if initial not in self.table:
            raise IllegalTransition(f"connector {connector_id}: {initial.value} is not a valid state")
        self.status: S = initial
        self.error_code: Optional[str] = None
        self.info: Optional[str] = None
        self.vendor_error_code: Optional[str] = None

    def can_transition(self, new: S) -> bool:
        return new in self.table.get(self.status, frozenset())

    def transition(self, new: S, *, error_code: Optional[str] = None, info: Optional[str] = None,
                   vendor_error_code: Optional[str] = None) -> bool:
        """Move to ``new``. Returns ``True`` when something changed (status or fault details).

        Same-state is a no-op, except Faulted -> Faulted with different fault details. Faulted needs an
        ``error_code`` (version wire name, e.g. 1.6 ``ChargePointErrorCode``); leaving Faulted clears it.
        Raises :class:`IllegalTransition` for transitions outside the table.
        """
        if new is S.FAULTED and not error_code:
            raise ValueError("Faulted needs an error_code")
        if new is self.status:
            if new is not S.FAULTED:
                return False
            details = (error_code, info, vendor_error_code)
            if details == (self.error_code, self.info, self.vendor_error_code):
                return False
            self.error_code, self.info, self.vendor_error_code = details
            return True
        if not self.can_transition(new):
            raise IllegalTransition(f"connector {self.connector_id}: {self.status.value} -> {new.value} not allowed")
        self.status = new
        if new is S.FAULTED:
            self.error_code, self.info, self.vendor_error_code = error_code, info, vendor_error_code
        else:
            self.error_code, self.info, self.vendor_error_code = None, info, vendor_error_code
        return True
