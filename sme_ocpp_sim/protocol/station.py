"""Interface between the shared charger shell (``runtime.charger``) and a per-version station (plan §11.2).

The shell owns what every OCPP version shares: the reconnect loop, the RPC session and its task set, the
BootNotification / Heartbeat cycle, the power-model clock (tick, time scale, pause) and the operator API the
engine and the Web API call. A :class:`VersionStation` owns the protocol semantics of one version: its adapter,
connector state machine, transaction flow, configuration keys and smart-charging rules. Versions share no code
below this interface.

Import direction: ``runtime.charger`` -> ``protocol.registry`` -> ``protocol.vNN.station`` -> ``protocol.vNN``
adapter -> ``rpc``. A station sees the shell only through :class:`StationHost`, never by importing it.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Awaitable, Callable, Coroutine, Dict, List, Optional, Protocol, Sequence, Tuple

from .base import AuthResult, BootResult, Command, ConfigEntry, VersionAdapter


class StationHost(Protocol):
    """What a station may use of the shell that hosts it."""

    #: ``config.ChargerConfig`` of this charger.
    cfg: Any
    #: ``charging.ev.EvModel`` / ``charging.power.AcSupply`` of the power model.
    ev: Any
    supply: Any
    #: Timeout of one outbound CALL (also bounds how long a Reset waits for queued messages).
    call_timeout: float
    #: Registration status of the last (not triggered) BootNotification, ``None`` before the first answer.
    registration: Optional[str]
    #: The CSMS url the shell connects to.
    csms_url: str

    @property
    def connected(self) -> bool: ...

    @property
    def online(self) -> bool:
        """Connected and the boot was accepted."""

    @property
    def paused(self) -> bool:
        """The power model is frozen (engine pause)."""

    def clock(self) -> datetime:
        """Wall clock (UTC), the clock charging schedules and message timestamps use."""

    def monotonic(self) -> float:
        """Monotonic clock of the power model intervals."""

    def step_power(self, pw: Any, dt_s: float, evse_limit_w: Optional[float]) -> Any:
        """Step one connector's power model (``charging.power.step``) by ``dt_s`` wall seconds at the time scale."""

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> Optional[asyncio.Task]:
        """Start a session task; refused (``coro`` closed, ``None`` returned) when the session is not connected."""

    def after_reply(self, fn: Callable[[], Any]) -> None:
        """Call ``fn`` after the CALLRESULT of the current CSMS CALL went out (see ``ChargerRuntime``)."""

    async def safe(self, aw: Awaitable[Any], what: str) -> Any:
        """Await a CP->CSMS message; a failure is logged and ``None`` returned."""

    def start_ticks(self) -> None:
        """Start the session task that calls :meth:`VersionStation.tick` every ``tick_s``."""

    def abort_session(self, error: Optional[str] = None) -> None:
        """Drop the connection (the reconnect loop takes over); ``error`` records an internal error."""

    def end_session_for_reset(self) -> None:
        """Close the session because of a Reset (reconnect without back-off)."""

    def kick_boot(self) -> None:
        """Wake the boot loop (send the next BootNotification now)."""

    async def send_boot(self, *, triggered: bool = False) -> Optional[BootResult]: ...

    async def heartbeat(self) -> None: ...


class VersionStation(ABC):
    """The protocol semantics of one OCPP version for one charger.

    The abstract members are what the shell needs to run a session; the operator methods (``plug`` ...) have
    defaults that raise ``NotImplementedError`` so that a new version can be brought up step by step.
    """

    #: Wire names of the connector fault codes an operator may set (1.6: ``ChargePointErrorCode`` w/o NoError).
    fault_codes: Tuple[str, ...] = ()

    def __init__(self, host: StationHost, *, strict: bool = False) -> None:
        """``strict``: strict inbound parsing; the station builds its own adapter and passes it on."""
        self.host = host

    # ------------------------------------------------------------------ session (called by the shell)

    @property
    @abstractmethod
    def adapter(self) -> VersionAdapter:
        """The version adapter; the shell binds it to each session and serves its handlers with this station."""

    @property
    @abstractmethod
    def connector_count(self) -> int: ...

    @abstractmethod
    async def handle(self, cmd: Command) -> Any:
        """Execute one domain command from the adapter (``protocol.base.CommandSink``)."""

    @abstractmethod
    async def on_online(self) -> None:
        """Session task, right after the boot was accepted: report the state, start the session's own tasks
        (including :meth:`StationHost.start_ticks`)."""

    @abstractmethod
    def on_session_ended(self) -> None:
        """The session tasks are cancelled and the connection is closing: drop per-session state."""

    @abstractmethod
    async def wait_heartbeat(self) -> bool:
        """Wait one heartbeat interval; ``True`` when the interval was changed meanwhile (wait again)."""

    @abstractmethod
    def set_heartbeat_interval(self, seconds: int) -> None:
        """The interval an accepted BootNotification asked for."""

    @abstractmethod
    def settle(self) -> None:
        """Integrate every charging connector up to now (before a time-scale / pause / limit change)."""

    @abstractmethod
    def tick(self) -> None:
        """One power-model step of every charging connector."""

    @abstractmethod
    def snapshot(self) -> Dict[str, Any]:
        """Version part of ``ChargerRuntime.snapshot``, merged into it flat. The keys are a per-version contract
        with the Web UI (1.6: ``tx_queue``, ``station``, ``connectors``)."""

    @abstractmethod
    def configuration(self) -> List[ConfigEntry]:
        """Run-time configuration as the CSMS would read it (1.6: GetConfiguration without keys)."""

    def variables(self) -> Optional[List[Dict[str, Any]]]:
        """Run-time Device Model variables (JSON view) of a version that has one (2.0.1); ``None`` = none."""
        return None

    # ------------------------------------------------------------------ operator (engine / Web API)

    async def plug(self, connector: int) -> None:
        raise NotImplementedError

    async def unplug(self, connector: int) -> None:
        raise NotImplementedError

    async def swipe(self, connector: int, id_tag: str) -> AuthResult:
        raise NotImplementedError

    async def stop_tx(self, connector: int) -> bool:
        raise NotImplementedError

    async def fault(self, connector: int, error_code: str) -> None:
        raise NotImplementedError

    async def clear_fault(self, connector: int) -> bool:
        raise NotImplementedError

    def has_transaction(self, connector: int) -> bool:
        raise NotImplementedError

    def faulted(self, connector: int) -> Optional[str]:
        raise NotImplementedError

    def installed_profiles(self, connector: Optional[int] = None) -> Sequence[Any]:
        raise NotImplementedError

    def composite(self, connector: int, duration: int, unit: Optional[str] = None) -> Any:
        """``unit`` "W" / "A" (``None`` = W); ``ValueError`` for another unit."""
        raise NotImplementedError
