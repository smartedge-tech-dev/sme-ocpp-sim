"""Connector state machine over :class:`DomainStatus` (design §3.1).

The allowed transitions are the OCPP 1.6 edition 2 §4.9 Status Notification state table
(p.39, rows A..I = "from" state, columns 1..9 = Available, Preparing, Charging, SuspendedEV,
SuspendedEVSE, Finishing, Reserved, Unavailable, Faulted), checked cell by cell. It already
includes B6 Preparing -> Finishing, added by 1.6 errata sheet 3.20 (p.12); errata 4.9 only fixes
the "Prepairing" header typo. Event meanings per cell: §4.9 p.40-42.

Not enforced here: I1-I8 say a resolved fault "returns to the pre-fault state"; the table
itself allows Faulted -> any state, and so does this module.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Optional

from ..protocol.base import DomainStatus as S


class IllegalTransition(ValueError):
    pass


_ALL = frozenset(S)

# One entry per table row; the comment lists the row's cells (§4.9 p.39).
TRANSITIONS: Dict[S, FrozenSet[S]] = {
    S.AVAILABLE: _ALL - {S.AVAILABLE, S.FINISHING},  # A2-A5, A7-A9
    # B1, B3-B6, B9 (B6 per errata 3.20)
    S.PREPARING: frozenset({S.AVAILABLE, S.CHARGING, S.SUSPENDED_EV, S.SUSPENDED_EVSE, S.FINISHING, S.FAULTED}),
    # C1, C4-C6, C8, C9
    S.CHARGING: frozenset({S.AVAILABLE, S.SUSPENDED_EV, S.SUSPENDED_EVSE, S.FINISHING, S.UNAVAILABLE, S.FAULTED}),
    # D1, D3, D5, D6, D8, D9
    S.SUSPENDED_EV: frozenset({S.AVAILABLE, S.CHARGING, S.SUSPENDED_EVSE, S.FINISHING, S.UNAVAILABLE,
                               S.FAULTED}),
    # E1, E3, E4, E6, E8, E9
    S.SUSPENDED_EVSE: frozenset({S.AVAILABLE, S.CHARGING, S.SUSPENDED_EV, S.FINISHING, S.UNAVAILABLE,
                                 S.FAULTED}),
    S.FINISHING: frozenset({S.AVAILABLE, S.PREPARING, S.UNAVAILABLE, S.FAULTED}),  # F1, F2, F8, F9
    S.RESERVED: frozenset({S.AVAILABLE, S.PREPARING, S.UNAVAILABLE, S.FAULTED}),  # G1, G2, G8, G9
    # H1-H5, H9
    S.UNAVAILABLE: frozenset({S.AVAILABLE, S.PREPARING, S.CHARGING, S.SUSPENDED_EV, S.SUSPENDED_EVSE,
                              S.FAULTED}),
    S.FAULTED: _ALL - {S.FAULTED},  # I1-I8
}

#: Connector 0 (the whole charger) only reports Available / Unavailable / Faulted (§4.9 p.39 note
#: under the table, and p.42). The spec gives no separate table for it; these are the cells
#: A8, A9, H1, H9, I1, I8 of the main table restricted to those three states.
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
