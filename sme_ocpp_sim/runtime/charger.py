"""Single charger runtime: the version-neutral shell (plan §2.2, §11.2, M3 / M4b).

One :class:`ChargerRuntime` per charger owns the reconnect loop, the RPC connection, the BootNotification /
Heartbeat cycle and the power-model clock, and is the interface the engine and the Web API use. The protocol
semantics of the charger's OCPP version (connector state machine, transactions, configuration keys, smart
charging) live in a :class:`~sme_ocpp_sim.protocol.station.VersionStation` (1.6:
``protocol.v16.station.Station16``), which the shell hosts through the ``StationHost`` interface. The shell never
sees version messages, and it has no version-specific code.

Task model: everything that needs the connection (boot / heartbeat loop, the station's session tasks such as the
outbox worker and meter sampling, follow-ups of CSMS commands) runs as a *session task*. ``_spawn`` refuses new
tasks once the session is no longer connected, so cancelling the session tasks at the end of a session is
race-free.

Timing: ``_after_reply`` runs a follow-up right after the CALLRESULT of the current CSMS CALL went out.
``time_scale`` scales only the power model (ramp, energy register, SoC); charging profiles stay on the wall clock
(``clock``), because schedules are in CSMS time.

Pause (:meth:`set_paused`, driven by the engine's run / pause): the power model is frozen - no energy, SoC or
ramp advance, the power is reported as it was. Pausing integrates up to "now" first, resuming restarts the
intervals at "now" (the paused interval is not caught up). The OCPP session itself (heartbeats, status, CSMS
calls, meter readings with the frozen values) keeps running.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Coroutine, Dict, List, Optional, Set

from ..charging import power
from ..charging.ev import PRESETS, EvModel
from ..charging.power import AcSupply, PowerState
from ..config import ChargerConfig, ImpairmentConfig, ReconnectConfig
from ..protocol import base as d
from ..protocol.registry import get_station_class
from ..protocol.station import VersionStation
from ..rpc import (Backoff, HandshakeError, RpcConnection, TransportConfig, after_this_reply, open_connection,
                   wait_event, wait_or_stop)
from ..rpc.impair import ImpairedSocket, impairment_seed, make_rng

log = logging.getLogger(__name__)


async def _wait_any(*events: asyncio.Event, timeout: Optional[float] = None) -> bool:
    """Wait until one of ``events`` is set or ``timeout`` passes; True if one was set."""
    if any(e.is_set() for e in events):
        return True
    tasks = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return bool(done)

BACKOFF_FACTOR = 2.0
#: RpcConnection writes the CALLRESULT right after the handler returns; follow-up CALLs (StatusNotification,
#: StopTransaction, ...) wait this long so they go out behind it on the wire.
REPLY_DELAY_S = 0.01
#: How long a closed session waits for the receive loop to finish.
RX_DRAIN_TIMEOUT_S = 5.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChargerRuntime:
    def __init__(
        self,
        cfg: ChargerConfig,
        csms_url: str,
        ocpp_version: str = "1.6",
        *,
        reconnect: Optional[ReconnectConfig] = None,
        frame_log: Any = None,
        strict: bool = False,
        call_timeout: float = 30.0,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        connect: Callable[[TransportConfig], Awaitable[Any]] = open_connection,
        backoff: Optional[Backoff] = None,
        ev: Optional[EvModel] = None,
        tick_s: float = 1.0,
        time_scale: float = 1.0,
        station: Optional[Callable[..., VersionStation]] = None,
    ) -> None:
        """``ev`` defaults to the preset ``cfg.session.ev_model`` (``SimConfig.resolve_ev`` also knows
        ``ev_models``); ``tick_s`` is the power model step, ``time_scale`` its simulated-time factor. time_scale
        speeds up only the power model (ramp, energy, SoC); charging profiles stay on the wall clock (see the
        module docstring). ``station`` overrides the station class registered for ``ocpp_version`` (tests)."""
        if tick_s <= 0 or time_scale <= 0:
            raise ValueError("tick_s and time_scale must be > 0")
        if ev is None and cfg.session.ev_model not in PRESETS:
            raise ValueError(f"EV model {cfg.session.ev_model!r} is not a preset; pass ev=")
        self.cfg = cfg
        self.ev: EvModel = ev or PRESETS[cfg.session.ev_model]
        self.supply = AcSupply(cfg.voltage_v, cfg.phases, cfg.line_to_line)
        self._tick_s, self._time_scale = tick_s, time_scale
        self._paused = False
        self.call_timeout = call_timeout
        self._clock, self._mono, self._connect = clock, monotonic, connect

        self.connected = False
        self.registration: Optional[str] = None
        self.last_error: Optional[Dict[str, str]] = None
        self.connect_attempts = 0
        self.boots = 0
        #: Set when a retry limit stopped the run (``reason`` ``max_retries`` / ``boot_max_retries``, ``attempts``,
        #: ``last_kind``); cleared when :meth:`run` starts again (the operator's Connect).
        self.gave_up: Optional[Dict[str, Any]] = None
        self._failures = 0       # consecutive failed connections since the last accepted boot
        self._boot_failures = 0  # non-Accepted BootNotifications (incl. no answer) of the current registration
        self.clock_offset_s: Optional[float] = None
        self.last_heartbeat: Optional[datetime] = None
        self._accepted_this_session = False
        self._tasks: Set[asyncio.Task] = set()
        self._boot_kick = asyncio.Event()
        self._reset_done = asyncio.Event()
        self._abort = asyncio.Event()  # a session task failed unexpectedly: drop the connection

        #: The CSMS url this charger connects to (before the station: a station may read it).
        self.csms_url = csms_url
        station_cls = station or get_station_class(ocpp_version)
        self.station: VersionStation = station_cls(self, strict=strict)
        self.adapter: d.VersionAdapter = self.station.adapter
        self.transport = TransportConfig(
            csms_url, cfg.id, subprotocol=self.adapter.subprotocol, password=cfg.password, ca_file=cfg.ca_file,
            verify_tls=cfg.verify_tls, require_subprotocol=cfg.require_subprotocol,
            client_cert_file=cfg.client_cert_file, client_key_file=cfg.client_key_file)
        rc = reconnect or ReconnectConfig()
        self._max_retries: Optional[int] = rc.max_retries
        self._backoff = backoff or Backoff(rc.min_s, BACKOFF_FACTOR, rc.max_s, rc.jitter)
        self._frame_cb = frame_log.callback(cfg.id) if frame_log is not None else None
        #: The RPC connection of the current session (frame injection); None between sessions.
        self._rpc: Optional[RpcConnection] = None
        #: CSMS -> CP actions this charger's version handles: the names ``impairments.block`` may use.
        self.csms_actions: tuple = tuple(sorted(self.adapter.handlers(self.station)))
        #: Simulated network drops so far (forced or periodic); they never count as failed connections.
        self.drops = 0
        self._net_drop = asyncio.Event()             # set by drop(): end the current session like a network failure
        self._drop_request: Optional[tuple] = None   # (mode, reconnect_after_s) of the pending drop
        self._reconnect_after: Optional[float] = None  # set when the last session ended by a simulated drop
        self._imp_changed = asyncio.Event()          # set_impairments ran: the periodic drop timer starts over
        self._session_up = asyncio.Event()           # set while a session runs ...
        self._session_down = asyncio.Event()         # ... and this one while none does
        self._session_down.set()
        self._session_no = 0
        self.impairments = ImpairmentConfig()
        self.set_impairments(cfg.impairments)

    # ================================================================== impairments (v0.2.2 A)

    def validate_impairments(self, imp: ImpairmentConfig) -> None:
        """``ValueError`` when ``imp`` names an action this charger's OCPP version does not receive."""
        unknown = sorted(set(imp.block) - set(self.csms_actions))
        if unknown:
            raise ValueError(f"charger {self.cfg.id}: impairments.block: {unknown} are not CSMS -> CP actions of "
                             f"OCPP {self.adapter.version} (known: {', '.join(self.csms_actions)})")

    def set_impairments(self, imp: ImpairmentConfig) -> None:
        """Apply ``imp`` to the running charger (no reconnect); the next inbound CALL already sees it."""
        self.validate_impairments(imp)
        self.impairments = imp.model_copy(deep=True)
        # Latency / jitter are read per frame by the session's ImpairedSocket; the drop timer re-reads its
        # settings (a change restarts the current interval).
        self._imp_changed.set()

    def _block_mode(self, action: str) -> Optional[str]:
        return self.impairments.block.get(action)

    @property
    def impaired(self) -> bool:
        return self.impairments.active

    def drop(self, mode: Optional[str] = None, reconnect_after_s: Optional[float] = None) -> None:
        """Drop the current connection like a network failure (``mode`` ``abort`` / ``close``, default
        ``impairments.drop_mode``) and reconnect after ``reconnect_after_s`` (default ``impairments.down_s``)
        instead of the back-off. Transactions and queued messages are kept and go out on the next session; the
        drop is not a failed connection (``reconnect.max_retries`` / ``boot_max_retries`` do not count it).
        ``ConnectionError`` when not connected."""
        if not self.connected:
            raise ConnectionError(f"charger {self.cfg.id} is not connected")
        if mode not in (None, "abort", "close"):
            raise ValueError(f"drop mode {mode!r}: expected abort or close")
        if reconnect_after_s is not None and reconnect_after_s < 0:
            raise ValueError("reconnect_after_s must be >= 0")
        imp = self.impairments
        self._drop_request = (mode or imp.drop_mode, imp.down_s if reconnect_after_s is None else reconnect_after_s)
        self._net_drop.set()

    async def _drop_timer(self, stop: asyncio.Event) -> None:
        """Periodic drops (``impairments.drop_every_s`` +- ``drop_jitter_s``), counted from each session start."""
        rng, rng_seed = None, None
        while not stop.is_set():
            self._imp_changed.clear()
            imp = self.impairments
            if imp.drop_every_s is None:
                await _wait_any(stop, self._imp_changed)
                continue
            if not self._session_up.is_set():
                await _wait_any(stop, self._imp_changed, self._session_up)
                continue
            seed = impairment_seed(imp.seed, self.cfg.id)
            if rng is None or seed != rng_seed:
                rng, rng_seed = make_rng(seed, "drop"), seed
            j = imp.drop_jitter_s
            interval = imp.drop_every_s + (rng.uniform(-j, j) if j else 0.0)
            sid = self._session_no
            if await _wait_any(stop, self._imp_changed, timeout=interval):
                continue  # stopped, or the settings changed: start over
            if self._session_no == sid and self.connected:
                try:
                    self.drop(imp.drop_mode, imp.down_s)
                except ConnectionError:
                    pass
            await _wait_any(stop, self._session_down)

    # ================================================================== frame injection (expert use)

    def _live_rpc(self) -> RpcConnection:
        rpc = self._rpc
        if rpc is None or not self.connected:
            raise ConnectionError(f"charger {self.cfg.id} is not connected")
        return rpc

    async def inject_call(self, action: str, payload: Any, timeout: Optional[float] = None) -> Any:
        """Send a CALL as it is, past the station: no station state changes and the answer is only returned.
        Raises like :meth:`RpcConnection.call` (``CallError``, ``TimeoutError``, ``ConnectionError``)."""
        return await self._live_rpc().call(action, payload, timeout=timeout, note="injected")

    async def inject_raw(self, raw: str) -> None:
        """Write ``raw`` to the socket verbatim (no answer is awaited); ``ConnectionError`` when not connected."""
        await self._live_rpc().send_raw(raw)

    # ================================================================== connection

    @property
    def connector_count(self) -> int:
        return self.station.connector_count

    @property
    def fault_codes(self) -> tuple:
        """Fault codes an operator may set on this charger (wire names of its OCPP version)."""
        return tuple(self.station.fault_codes)

    @property
    def online(self) -> bool:
        return self.connected and self.registration == d.BootStatus.ACCEPTED.value

    async def run(self, stop: asyncio.Event) -> None:
        """Connect, boot, serve; reconnect with back-off until ``stop`` is set.

        Setting ``stop`` while a connection attempt is in progress may take up to the transport's
        ``open_timeout`` to return. It also returns when a retry limit is hit (``reconnect.max_retries``,
        ``boot_max_retries``): :attr:`gave_up` tells why; a new run starts counting from zero.
        """
        self.gave_up = None
        self._failures = self._boot_failures = 0
        timer = asyncio.ensure_future(self._drop_timer(stop))
        try:
            await self._run(stop)
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
            self.station.close()  # station timers (reservation expiry, ...) do not outlive the run

    async def _run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.connect_attempts += 1
            try:
                ws = await self._connect(self.transport)
            except HandshakeError as e:
                self.last_error = {"kind": e.kind, "message": str(e)}
                log.warning("%s: connect failed (%s)", self.cfg.id, e.kind)
                try:
                    self.station.on_connect_failed(e.kind, str(e))
                except Exception:  # noqa: BLE001 - a station bug must not stop the reconnect loop
                    log.exception("%s: on_connect_failed failed", self.cfg.id)
                if self._count_failure(e.kind):
                    break
                if await wait_or_stop(stop, self._backoff.next()):
                    break
                continue
            # The back-off is reset only after an accepted boot (see _boot): a CSMS that accepts the socket and
            # then drops it must still be backed off.
            was_reset = await self._session(ws, stop)
            if stop.is_set() or self.gave_up is not None:
                break
            if self._reconnect_after is not None:
                # A simulated drop: not a failed connection (no retry count, no back-off), just the pause.
                if await wait_or_stop(stop, self._reconnect_after):
                    break
                continue
            # A session that ended before an accepted boot is a failed connection (a Reset is not).
            if not was_reset and not self._accepted_this_session and self._count_failure(
                    (self.last_error or {}).get("kind", "disconnected")):
                break
            if not was_reset and await wait_or_stop(stop, self._backoff.next()):
                break

    async def _session(self, ws: Any, stop: asyncio.Event) -> bool:
        """Serve one connection; returns True when it ended because of a Reset."""
        self._reset_done.clear()
        self._abort.clear()
        self._net_drop.clear()
        self._drop_request = self._reconnect_after = None
        before = self.last_error  # an error left from an earlier attempt is not this session's
        self.registration = None
        self._accepted_this_session = False
        # Latency / jitter (impairments): read live per frame, so a change needs no reconnect.
        sock = ImpairedSocket(ws, lambda: self.impairments, impairment_seed(self.impairments.seed, self.cfg.id))
        rpc = RpcConnection(sock, self.adapter.handlers(self.station), error_codes=self.adapter.error_codes,
                            call_timeout=self.call_timeout, on_frame=self._frame_cb, name=self.cfg.id,
                            call_guard=self.station.check_call, block=self._block_mode)
        self._rpc = rpc
        self.adapter.bind(rpc.call)
        self.connected = True
        self._session_no += 1
        self._session_down.clear()
        self._session_up.set()
        rx = asyncio.create_task(rpc.run())
        waits = [asyncio.create_task(stop.wait()), asyncio.create_task(self._reset_done.wait()),
                 asyncio.create_task(self._abort.wait()), asyncio.create_task(self._net_drop.wait())]
        self._spawn(self._online_guarded())
        drop: Optional[tuple] = None
        try:
            await asyncio.wait([rx, *waits], return_when=asyncio.FIRST_COMPLETED)
            if self._net_drop.is_set() and not stop.is_set() and not self._reset_done.is_set():
                drop = self._drop_request
                self.drops += 1
                log.info("%s: simulated drop (%s), reconnect in %.1f s", self.cfg.id, drop[0], drop[1])
                await sock.drop(drop[0])
        finally:
            rx_error = await self._close_session(rpc, rx, waits)
            await sock.aclose_pump()
            self._session_up.clear()
            self._session_down.set()
        if drop is not None:
            self._reconnect_after = drop[1]
            self.last_error = {"kind": "simulated_drop",
                               "message": f"simulated drop ({drop[0]}); reconnecting in {drop[1]:g} s"}
            return False
        # An abort that recorded no error of its own (the outbox worker lost the connection) is a disconnect.
        aborted = self._abort.is_set() and self.last_error is not None and self.last_error is not before
        if not self._reset_done.is_set() and not stop.is_set() and not aborted:
            self.last_error = {"kind": "disconnected", "message": self._close_text(ws, rx_error)}
        return self._reset_done.is_set()

    async def _close_session(self, rpc: RpcConnection, rx: asyncio.Task, waits: List[asyncio.Task]) -> Optional[str]:
        """Tear one session down; returns why the receive loop failed, if it did."""
        self.connected = False  # from here on _spawn refuses: the task set below is final
        self.adapter.bind(None)
        self._rpc = None
        for w in waits:
            w.cancel()
        await self._cancel_tasks()
        self.station.on_session_ended()
        await rpc.close()
        try:
            async with asyncio.timeout(RX_DRAIN_TIMEOUT_S):
                await rx
        except TimeoutError:
            rx.cancel()
            await asyncio.gather(rx, return_exceptions=True)
            log.warning("%s: receive loop did not finish", self.cfg.id)
            return "receive loop did not finish"
        except Exception as e:  # noqa: BLE001 - record why the connection died
            log.warning("%s: receive loop failed: %r", self.cfg.id, e)
            return repr(e)
        return None

    def _count_failure(self, kind: str) -> bool:
        """Count one failed connection; True (and :attr:`gave_up` set) when ``reconnect.max_retries`` is exceeded."""
        self._failures += 1
        if self._max_retries is None or self._failures <= self._max_retries:
            return False
        self._give_up("max_retries", self._failures, kind)
        return True

    def _give_up(self, reason: str, attempts: int, last_kind: Optional[str]) -> None:
        self.gave_up = {"reason": reason, "attempts": attempts, "last_kind": last_kind}
        limit, unit = (("reconnect.max_retries", "attempts") if reason == "max_retries"
                       else ("boot_max_retries", "boots"))
        self.last_error = {"kind": "retries_exhausted",
                           "message": f"gave up after {attempts} {unit} ({limit}); last: {last_kind}"}
        log.warning("%s: gave up after %d %s (%s, last %s)", self.cfg.id, attempts, unit, reason, last_kind)

    @staticmethod
    def _close_text(ws: Any, rx_error: Optional[str]) -> str:
        code, reason = getattr(ws, "close_code", None), getattr(ws, "close_reason", None)
        text = "connection closed"
        if code is not None:
            text += f" (code {code}{': ' + reason if reason else ''})"
        return f"{text}; {rx_error}" if rx_error else text

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> Optional[asyncio.Task]:
        """Start a session task; refused (``coro`` closed, None returned) when the session is not connected."""
        if not self.connected:
            log.debug("%s: session ended, task not started", self.cfg.id)
            coro.close()
            return None
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._task_done)
        return t

    def _task_done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("%s: task failed", self.cfg.id, exc_info=t.exception())

    async def _cancel_tasks(self) -> None:
        tasks = [t for t in self._tasks if t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _after_reply(self, fn: Callable[[], Any]) -> None:
        """Call ``fn`` (awaiting its result when it is awaitable) after the CALLRESULT of the current CSMS CALL
        went out. Bind arguments now (``functools.partial``): state read at call time may have changed.

        Inside a CALL handler the follow-up is started only once the answer was handed to the socket
        (``after_this_reply``), so it can never overtake it - a slow send must not let a session close drop the
        answer. Outside one (no answer pending) it just starts after ``REPLY_DELAY_S``."""
        async def later() -> None:
            await asyncio.sleep(REPLY_DELAY_S)
            res = fn()
            if inspect.isawaitable(res):
                await res
        if not after_this_reply(lambda: self._spawn(later())):
            self._spawn(later())

    async def _safe(self, aw: Awaitable[Any], what: str) -> Any:
        try:
            return await aw
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a failed CP->CSMS message must not kill the runtime
            log.warning("%s: %s failed: %r", self.cfg.id, what, e)
            return None

    # ================================================================== StationHost (used by the station)

    def clock(self) -> datetime:
        return self._clock()

    def monotonic(self) -> float:
        return self._mono()

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> Optional[asyncio.Task]:
        return self._spawn(coro)

    def after_reply(self, fn: Callable[[], Any]) -> None:
        self._after_reply(fn)

    async def safe(self, aw: Awaitable[Any], what: str) -> Any:
        return await self._safe(aw, what)

    def step_power(self, pw: PowerState, dt_s: float, evse_limit_w: Optional[float]) -> PowerState:
        return power.step(pw, dt_s=dt_s, ev=self.ev, target_soc_pct=self.cfg.session.target_soc,
                          evse_limit_w=evse_limit_w, evse_max_w=self.cfg.max_power_w, supply=self.supply,
                          ramp_up_w_per_s=self.cfg.ramp_up_w_per_s,
                          ramp_down_w_per_s=self.cfg.ramp_down_w_per_s, time_scale=self._time_scale)

    def start_ticks(self) -> None:
        self._spawn(self._tick_loop())

    def abort_session(self, error: Optional[str] = None) -> None:
        if error is not None:
            self.last_error = {"kind": "internal", "message": error}
        self._abort.set()

    def end_session_for_reset(self) -> None:
        self._reset_done.set()

    def reconnect_with_password(self, password: str) -> None:
        # The transport changes at once (a connection lost meanwhile reconnects with the new password too); the
        # session closes after the SetVariables answer: a deliberate close, no back-off, no "disconnected" error.
        self.transport = dataclasses.replace(self.transport, password=password)
        self._after_reply(self._reset_done.set)

    def use_client_certificate(self, cert_file: str, key_file: str, *, reconnect: bool) -> None:
        self.transport = dataclasses.replace(self.transport, client_cert_file=cert_file, client_key_file=key_file)
        if reconnect:
            self._after_reply(self._reset_done.set)

    def set_trusted_roots(self, pem: Optional[str]) -> None:
        self.transport = dataclasses.replace(self.transport, ca_data=pem)

    def kick_boot(self) -> None:
        self._boot_kick.set()

    async def send_boot(self, *, triggered: bool = False) -> Optional[d.BootResult]:
        return await self._send_boot(triggered=triggered)

    async def heartbeat(self) -> None:
        await self._heartbeat()

    # ================================================================== boot / heartbeat

    async def _online_guarded(self) -> None:
        try:
            await self._online()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - no zombie session: let the reconnect loop take over
            log.exception("%s: session failed", self.cfg.id)
            self.abort_session(repr(e))

    async def _online(self) -> None:
        if not await self._boot():
            return  # boot_max_retries hit: the session is closing
        await self.station.on_online()
        while True:
            if not await self.station.wait_heartbeat():
                await self._heartbeat()

    async def _boot(self) -> bool:
        """BootNotification until Accepted (True); CSMS CALLs are served meanwhile by the RPC layer. False when
        ``boot_max_retries`` was exceeded."""
        while True:
            self._boot_kick.clear()
            delay: float = self.cfg.boot_retry_s
            res = await self._send_boot()
            if res is not None:
                if res.status is d.BootStatus.ACCEPTED:
                    self._backoff.reset()
                    self._failures = self._boot_failures = 0
                    self._accepted_this_session = True
                    self.last_error = None
                    return True
                if res.interval > 0:
                    delay = res.interval
            if self.connected:  # an answer lost with the connection is the reconnect loop's business
                self._boot_failures += 1
                limit = self.cfg.boot_max_retries
                if limit is not None and self._boot_failures > limit:
                    self._give_up("boot_max_retries", self._boot_failures,
                                  res.status.value if res is not None else "no_reply")
                    self._abort.set()  # close the session; _run sees gave_up and returns
                    return False
            await wait_event(self._boot_kick, delay)

    async def _send_boot(self, *, triggered: bool = False) -> Optional[d.BootResult]:
        ev = d.Booting(self.cfg.vendor, self.cfg.model, self.cfg.serial, self.cfg.firmware)
        res: Optional[d.BootResult] = await self._safe(self.adapter.send_booting(ev), "BootNotification")
        if res is None:
            return None
        self.boots += 1
        self._note_time(res.current_time)
        if triggered:
            # A triggered boot never changes the registration (a Pending / Rejected answer would otherwise take
            # an accepted charger offline with no boot loop running); it is only logged. 1.6: see Station16._trigger.
            log.info("%s: triggered BootNotification answered %s (registration kept)", self.cfg.id,
                     res.status.value)
        else:
            self.registration = res.status.value
        if res.status is d.BootStatus.ACCEPTED and res.interval > 0:
            self.station.set_heartbeat_interval(res.interval)
        return res

    async def _heartbeat(self) -> None:
        t = await self._safe(self.adapter.send_heartbeat(d.Heartbeat()), "Heartbeat")
        if t is not None:
            self._note_time(t)
            self.last_heartbeat = self._clock()

    def _note_time(self, t: Optional[datetime]) -> None:
        if t is None:
            return
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        self.clock_offset_s = (t - self._clock()).total_seconds()

    # ================================================================== power model clock

    def set_time_scale(self, time_scale: float) -> None:
        """Change the power model's simulated-time factor; charging connectors are integrated up to now first."""
        if time_scale <= 0:
            raise ValueError("time_scale must be > 0")
        self.station.settle()
        self._time_scale = time_scale

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        """Freeze (``True``) or resume the power model (see the module docstring); the OCPP session keeps running.

        Charging connectors are integrated up to now before freezing; on resume their intervals restart at now,
        so the paused time is neither lost before the pause nor caught up after it."""
        paused = bool(paused)
        if paused != self._paused:
            self.station.settle()  # pause: integrate up to now; resume: still paused here, so only restart intervals
            self._paused = paused

    async def _tick_loop(self) -> None:
        """Session task: step the power model of every charging connector each ``tick_s``."""
        while True:
            await asyncio.sleep(self._tick_s)
            try:
                self.station.tick()
            except Exception:  # a local bug must not stop the power model for the rest of the session
                log.exception("%s: power model tick failed", self.cfg.id)

    # ================================================================== public API (engine / Web API, M6)

    async def plug(self, connector: int) -> None:
        await self.station.plug(connector)

    async def unplug(self, connector: int) -> None:
        """Never waits for the CSMS."""
        await self.station.unplug(connector)

    async def swipe(self, connector: int, id_tag: str) -> d.AuthResult:
        """Present an id tag (see the station's ``swipe`` for the result values)."""
        return await self.station.swipe(connector, id_tag)

    async def stop_tx(self, connector: int) -> bool:
        """Never waits for the CSMS."""
        return await self.station.stop_tx(connector)

    async def fault(self, connector: int, error_code: str) -> None:
        """``error_code`` is a wire name from :attr:`fault_codes`."""
        await self.station.fault(connector, error_code)

    async def clear_fault(self, connector: int) -> bool:
        return await self.station.clear_fault(connector)

    def has_transaction(self, connector: int) -> bool:
        return self.station.has_transaction(connector)

    def faulted(self, connector: int) -> Optional[str]:
        """The operator fault's error code, or ``None``."""
        return self.station.faulted(connector)

    def installed_profiles(self, connector: Optional[int] = None) -> Any:
        """Installed charging profiles (all, or those of ``connector``; 0 = the charge point)."""
        return self.station.installed_profiles(connector)

    def composite(self, connector: int, duration: int, unit: Optional[str] = None) -> d.GetCompositeResult:
        """The composite schedule for ``connector`` (0 = the charge point) right now, without a CSMS; ``unit``
        ``"W"`` / ``"A"`` (``None`` = W)."""
        return self.station.composite(connector, duration, unit)

    async def set_external_limit(self, connector: int, limit_w: Optional[float]) -> None:
        """Set (W) or remove (``None``) an external charging limit (0 = the whole charger); see the station's
        ``set_external_limit``. ``NotImplementedError`` when the OCPP version has none (1.6)."""
        await self.station.set_external_limit(connector, limit_w)

    def configuration(self) -> List[d.ConfigEntry]:
        """Run-time configuration keys as the CSMS would read them."""
        return self.station.configuration()

    def variables(self) -> Optional[List[Dict[str, Any]]]:
        """Run-time Device Model variables of a version that has one (2.0.1), else ``None``."""
        return self.station.variables()

    async def send_data_transfer(self, vendor_id: str, message_id: Optional[str] = None,
                                 data: Optional[str] = None) -> d.DataTransferResult:
        if not self.connected:
            raise ConnectionError("charger is not connected")
        return await self.adapter.send_data_transfer(d.DataTransfer(vendor_id, message_id, data))

    def snapshot(self) -> Dict[str, Any]:
        return {
            "id": self.cfg.id,
            "ocpp_version": self.adapter.version,
            "connected": self.connected,
            "registration": self.registration,
            "last_error": dict(self.last_error) if self.last_error else None,
            "connect_attempts": self.connect_attempts,
            "gave_up": dict(self.gave_up) if self.gave_up else None,
            "boots": self.boots,
            "clock_offset_s": self.clock_offset_s,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "security_profile": self.transport.security_profile,
            "impaired": self.impaired,
            "drops": self.drops,
            **self.station.snapshot(),
        }
