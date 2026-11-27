"""Version-neutral firmware update and diagnostics / log upload simulation (v0.2 M7).

1.6 Firmware Management (§4.4, §4.5, §5.9, §5.19, errata 3.12 / 3.31 / 3.47) and 2.0.1 L01 / L02 / N01 move
through the same steps: [scheduled] -> Downloading -> Downloaded -> [wait for the transactions to end] ->
Installing -> reboot -> BootNotification -> Installed, and Uploading -> Uploaded for a log. Only the step
sequence and its timing live here; which steps a version reports (``FirmwareState`` subset), the wire messages,
the request checks and every response status belong to the version station. Nothing is downloaded or uploaded:
a step only waits its configured duration (``config.MaintenanceConfig``), and a ``fail_*`` switch makes every
attempt of that step fail. This module imports nothing of the shell or of a version package.

Interpretations (not defined by the spec)
-----------------------------------------
- ``retries`` is the number of attempts after the first one (absent or < 0: 0, "left to the Charge Point");
  ``retryInterval`` absent or < 0 uses ``MaintenanceConfig.retry_interval_s``. Each attempt reports Downloading /
  Uploading again (L02.FR.19 / N01.FR.10 allow it); the failure status is sent once, after the last attempt.
- A retrieve / install time in the past starts the step at once. The durations and the scheduled times run on
  the wall clock (not scaled by the time scale, not paused), like the reservation expiry.
- The install waits until no transaction is left (1.6 §5.19 RECOMMENDED, L02.FR.03: the simulator cannot install
  while charging); meanwhile :attr:`Maintenance.hold` is set and the station reports the connectors / EVSEs
  without a transaction as Unavailable (1.6 RECOMMENDED, L02.FR.04: AllowNewSessionsPendingFirmwareUpdate does not
  exist). The hold ends with the install (failed or rebooting).
- A successful install always reboots (the 1.6 "automatic reboot" variant, 2.0.1 L02.FR.21 option (a)): the
  station closes the session as for a Reset, and Installed is sent after the next accepted boot. The reported
  firmware version (BootNotification ``firmwareVersion``) does not change: the simulator has no image to read
  it from. Only a boot accepted after the reboot has closed the session counts (a reconnect while the Reset
  drains does not); when the station is offline the reboot is the next session. A notification while not online
  is not sent (the step still counts as the last status).
- Stopping the charger abandons a running update / upload (no status is sent for it) and forgets its last status:
  a status asked for afterwards is Idle.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, FrozenSet, Optional

from ..protocol.base import FirmwareState as F
from ..protocol.base import FirmwareStatus, LogState, LogStatus

if TYPE_CHECKING:
    from ..config import MaintenanceConfig

log = logging.getLogger(__name__)

#: How often the install re-checks for running transactions (besides :meth:`Maintenance.wake`).
TX_POLL_S = 0.5


@dataclass(frozen=True)
class FirmwareJob:
    location: str
    retrieve_at: datetime
    install_at: Optional[datetime] = None
    retries: Optional[int] = None
    retry_interval_s: Optional[float] = None
    #: 2.0.1 requestId (``None`` for 1.6).
    request_id: Optional[int] = None
    #: 2.0.1 secure update (L01): the request carried a signature; SignatureVerified is reported after the download.
    signed: bool = False


@dataclass(frozen=True)
class LogJob:
    location: str
    file_name: str
    retries: Optional[int] = None
    retry_interval_s: Optional[float] = None
    request_id: Optional[int] = None


class Maintenance:
    """One charger's firmware update and log upload (at most one of each at a time).

    ``firmware_states``: the steps the version reports (others are skipped). ``send_firmware`` / ``send_log``
    send one notification (and return when it is answered or dropped). ``busy``: a transaction is running.
    ``hold_changed``: :attr:`hold` changed, the station refreshes its connector states. ``reboot``: close the
    session as for a Reset (the next accepted boot calls :meth:`on_online`).
    """

    def __init__(self, settings: MaintenanceConfig, *, clock: Callable[[], datetime], firmware_states: FrozenSet[F],
                 send_firmware: Callable[[FirmwareStatus], Awaitable[Any]],
                 send_log: Callable[[LogStatus], Awaitable[Any]], busy: Callable[[], bool],
                 hold_changed: Callable[[], None], reboot: Callable[[], Awaitable[Any]]) -> None:
        self.settings = settings
        self._clock = clock
        self._states = firmware_states
        self._send_firmware, self._send_log = send_firmware, send_log
        self._busy, self._hold_changed, self._reboot = busy, hold_changed, reboot
        #: Last firmware status entered (``None``: no update since the start) and the request it belongs to.
        self.firmware_status: Optional[F] = None
        self.firmware_request_id: Optional[int] = None
        self.log_status: Optional[LogState] = None
        self.log_request_id: Optional[int] = None
        #: Connectors without a transaction are held Unavailable (waiting to install).
        self.hold = False
        #: Installing has begun; the update can no longer be cancelled.
        self.installing = False
        self._fw_task: Optional[asyncio.Task] = None
        self._log_task: Optional[asyncio.Task] = None
        self._idle = asyncio.Event()
        #: Accepted boots so far (:meth:`on_online`); the reboot waits for one counted after it.
        self._boots = 0
        self._booted = asyncio.Event()

    # ------------------------------------------------------------------ state

    @property
    def firmware_busy(self) -> bool:
        return self._fw_task is not None and not self._fw_task.done()

    @property
    def log_busy(self) -> bool:
        return self._log_task is not None and not self._log_task.done()

    def snapshot(self) -> dict:
        return {"firmware": {"status": self.firmware_status.value if self.firmware_status else None,
                             "request_id": self.firmware_request_id, "busy": self.firmware_busy},
                "log": {"status": self.log_status.value if self.log_status else None,
                        "request_id": self.log_request_id, "busy": self.log_busy}}

    # ------------------------------------------------------------------ control

    def start_firmware(self, job: FirmwareJob) -> None:
        """Start ``job`` (cancelling a running update first); call it after the UpdateFirmware answer went out."""
        self.cancel_firmware()
        self.firmware_status, self.firmware_request_id = None, job.request_id
        self._fw_task = asyncio.get_running_loop().create_task(self._run_firmware(job))

    def cancel_firmware(self) -> bool:
        """Cancel a running update and forget its status (a trigger answers Idle); ``True`` when one was running."""
        task, self._fw_task = self._fw_task, None
        if task is None or task.done():
            return False
        task.cancel()
        self.firmware_status, self.firmware_request_id = None, None
        self.installing = False
        self._set_hold(False)
        return True

    def start_log(self, job: LogJob) -> None:
        self.cancel_log()
        self.log_status, self.log_request_id = None, job.request_id
        self._log_task = asyncio.get_running_loop().create_task(self._run_log(job))

    def cancel_log(self) -> bool:
        task, self._log_task = self._log_task, None
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def wake(self) -> None:
        """A transaction ended: re-check a waiting install now."""
        self._idle.set()

    def on_online(self) -> None:
        """A boot was accepted (after the firmware reboot: send Installed)."""
        self._boots += 1
        self._booted.set()

    def close(self) -> None:
        """The charger stops: abandon a running update / upload."""
        self.cancel_firmware()
        self.cancel_log()

    # ------------------------------------------------------------------ steps

    def _set_hold(self, hold: bool) -> None:
        if self.hold != hold:
            self.hold = hold
            self._hold_changed()

    async def _firmware(self, state: F, request_id: Optional[int]) -> None:
        if state not in self._states:
            return
        self.firmware_status = state
        await self._send_firmware(FirmwareStatus(state, request_id))

    async def _log(self, state: LogState, request_id: Optional[int]) -> None:
        self.log_status = state
        await self._send_log(LogStatus(state, request_id))

    async def _wait_until(self, at: Optional[datetime]) -> bool:
        """Sleep until ``at``; ``False`` (no wait) when it is absent or not in the future."""
        if at is None:
            return False
        delay = (at - self._clock()).total_seconds()
        if delay <= 0:
            return False
        await asyncio.sleep(delay)
        return True

    def _attempts(self, retries: Optional[int]) -> int:
        return 1 + max(0, retries or 0)

    def _interval(self, interval: Optional[float]) -> float:
        return interval if interval is not None and interval >= 0 else self.settings.retry_interval_s

    async def _run_firmware(self, job: FirmwareJob) -> None:
        rid = job.request_id
        try:
            if not await self._download(job):
                return
            await self._wait_install_window(job)
            if not await self._install(rid):
                return
            await self._reboot_and_wait(job)
            await self._firmware(F.INSTALLED, rid)
        finally:
            self.installing = False
            if self._fw_task is asyncio.current_task():
                self._set_hold(False)

    async def _download(self, job: FirmwareJob) -> bool:
        """[DownloadScheduled] -> Downloading (each attempt) -> Downloaded [-> SignatureVerified]; ``False``
        after DownloadFailed."""
        s, rid = self.settings, job.request_id
        if job.retrieve_at > self._clock():
            await self._firmware(F.DOWNLOAD_SCHEDULED, rid)  # L02.FR.07
            await self._wait_until(job.retrieve_at)
        attempts = self._attempts(job.retries)
        for attempt in range(attempts):
            await self._firmware(F.DOWNLOADING, rid)
            await asyncio.sleep(s.download_s)
            if not s.fail_download:
                break
            if attempt + 1 < attempts:
                await asyncio.sleep(self._interval(job.retry_interval_s))
        else:  # no ``break``: every attempt failed
            await self._firmware(F.DOWNLOAD_FAILED, rid)  # L02.FR.19
            return False
        await self._firmware(F.DOWNLOADED, rid)
        if job.signed:
            await self._firmware(F.SIGNATURE_VERIFIED, rid)  # L01.FR.04: the signature is taken as valid
        return True

    async def _wait_install_window(self, job: FirmwareJob) -> None:
        """[InstallScheduled], then hold the free connectors until no transaction is left."""
        if job.install_at is not None and job.install_at > self._clock():
            await self._firmware(F.INSTALL_SCHEDULED, job.request_id)  # L01.FR.16 / L02.FR.10
            await self._wait_until(job.install_at)
        self._set_hold(True)
        while self._busy():  # 1.6 §5.19, L02.FR.03
            self._idle.clear()
            try:
                await asyncio.wait_for(self._idle.wait(), TX_POLL_S)
            except asyncio.TimeoutError:
                pass

    async def _install(self, rid: Optional[int]) -> bool:
        """Installing -> InstallRebooting; ``False`` after InstallationFailed (the hold ends)."""
        self.installing = True
        await self._firmware(F.INSTALLING, rid)
        await asyncio.sleep(self.settings.install_s)
        if self.settings.fail_install:
            self._set_hold(False)
            await self._firmware(F.INSTALLATION_FAILED, rid)
            return False
        await self._firmware(F.INSTALL_REBOOTING, rid)  # L02.FR.21 (a)
        return True

    async def _reboot_and_wait(self, job: FirmwareJob) -> None:
        """Reboot and wait for a boot accepted after it."""
        # The hold ends quietly: the station derives every connector state afresh when the next session comes
        # online, so a refresh now would only report Available just before the session closes.
        self.hold = False
        log.info("firmware update %s: rebooting to activate", job.location)
        await self._reboot()
        # Counted after the reboot returned: it has closed the session (or a Reset already running will), so a
        # boot accepted while it drained the outbox (an unrelated reconnect) does not count.
        boots = self._boots
        while self._boots == boots:
            self._booted.clear()
            await self._booted.wait()

    async def _run_log(self, job: LogJob) -> None:
        s, rid = self.settings, job.request_id
        attempts = self._attempts(job.retries)
        for attempt in range(attempts):
            await self._log(LogState.UPLOADING, rid)
            await asyncio.sleep(s.upload_s)
            if not s.fail_upload:
                await self._log(LogState.UPLOADED, rid)
                return
            if attempt + 1 < attempts:
                await asyncio.sleep(self._interval(job.retry_interval_s))
        await self._log(LogState.UPLOAD_FAILED, rid)
