"""v0.2 M7: firmware update and diagnostics / log upload simulation, OCPP 1.6J and 2.0.1.

The scenarios run against the ``ocpp``-library CSMS peer of each version (the peer schema-validates every message
both ways). A firmware install reboots the charger, so the notifications of one update are spread over the peer's
sessions (``peer.history``). The version-neutral steps of ``runtime.maintenance`` are tested directly at the end.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from ocpp.v16 import call as call16
from ocpp.v201 import call as call201
from test_runtime_charger import CP, PEERS, _scenario, _until

from sme_ocpp_sim.config import MaintenanceConfig
from sme_ocpp_sim.protocol.base import AuthResult as A
from sme_ocpp_sim.protocol.base import FirmwareState as F
from sme_ocpp_sim.protocol.base import LogState
from sme_ocpp_sim.runtime.maintenance import FirmwareJob, LogJob, Maintenance

VERSIONS = pytest.mark.parametrize("version", list(PEERS))
FAST = {"download_s": 0.05, "install_s": 0.05, "upload_s": 0.05, "retry_interval_s": 0.01}


def _v16(version):
    return version == "1.6"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now(delta_s=0.0):
    return _iso(datetime.now(timezone.utc) + timedelta(seconds=delta_s))


def _maint(**kw):
    return {"maintenance": {**FAST, **kw}}


async def _online(peer, rt):
    await _until(lambda: rt.online and CP in peer.sessions and peer.sessions[CP].statuses)
    return peer.sessions[CP]


def _fw(peer):
    return [st for s in peer.history for st in s.firmware_statuses]


def _logs(peer, version):
    return [st for s in peer.history for st in (s.diagnostics_statuses if _v16(version) else s.log_statuses)]


async def _update(peer, version, retrieve=None, request_id=5, **kw):
    retrieve = retrieve or _now()
    if _v16(version):
        return await peer.request(CP, call16.UpdateFirmware(location="ftp://fw.example.com/fw.bin",
                                                            retrieve_date=retrieve, **kw))
    fw = {"location": "https://fw.example.com/fw.bin", "retrieve_date_time": retrieve}
    fw.update(kw.pop("firmware", {}))
    return await peer.request(CP, call201.UpdateFirmware(request_id=request_id, firmware=fw, **kw))


async def _get_log(peer, version, request_id=3, **kw):
    if _v16(version):
        return await peer.request(CP, call16.GetDiagnostics(location="ftp://logs.example.com/", **kw))
    return await peer.request(CP, call201.GetLog(log={"remote_location": "https://logs.example.com/"},
                                                 log_type="DiagnosticsLog", request_id=request_id, **kw))


async def _trigger(peer, version, what):
    if _v16(version):
        return await peer.trigger(CP, what)
    return await peer.request(CP, call201.TriggerMessage(requested_message=what))


def _connector_status(peer, version, cid):
    s = peer.history[-1]
    if _v16(version):
        return [st for c, st, _ in s.statuses if c == cid]
    return [st for e, _, st in s.statuses if e == cid]


# --------------------------------------------------------------------------------------------- firmware


@VERSIONS
def test_firmware_update_installs_reboots_and_reports_installed(version):
    """1.6 §5.19 / 2.0.1 L02: Downloading -> Downloaded -> Installing -> [InstallRebooting] -> reboot ->
    BootNotification -> Installed (sent by the "new firmware", in the session after the reboot)."""
    async def body(peer, rt):
        await _online(peer, rt)
        r = await _update(peer, version)
        assert _v16(version) or r.status == "Accepted"
        await _until(lambda: "Installed" in _fw(peer))
        expected = ["Downloading", "Downloaded", "Installing"] + ([] if _v16(version) else ["InstallRebooting"])
        assert _fw(peer) == expected + ["Installed"]
        assert len(peer.history) == 2 and peer.history[-1].firmware_statuses == ["Installed"]
        assert peer.history[-1].boots == 1  # re-Boot after the install
        if not _v16(version):
            assert set(peer.history[0].firmware_request_ids + peer.history[1].firmware_request_ids) == {5}
            assert any('"FirmwareUpdate"' in raw for d, raw in peer.history[1].frames if d == "rx")
        assert rt.snapshot()["firmware"] == {"status": "Installed", "request_id": None if _v16(version) else 5,
                                             "busy": False}
    _scenario(body, cfg_kw=_maint(), version=version)


@VERSIONS
def test_firmware_download_failed_after_retries(version):
    """1.6 §7.25 DownloadFailed / L02.FR.19: every attempt fails, the failure is sent after the last retry."""
    async def body(peer, rt):
        await _online(peer, rt)
        await _update(peer, version, retries=1, retry_interval=0)
        await _until(lambda: "DownloadFailed" in _fw(peer))
        await asyncio.sleep(0.2)
        assert _fw(peer) == ["Downloading", "Downloading", "DownloadFailed"] and len(peer.history) == 1
    _scenario(body, cfg_kw=_maint(fail_download=True), version=version)


@VERSIONS
def test_firmware_installation_failed_no_reboot(version):
    async def body(peer, rt):
        await _online(peer, rt)
        await _update(peer, version)
        await _until(lambda: "InstallationFailed" in _fw(peer))
        await asyncio.sleep(0.2)
        assert _fw(peer) == ["Downloading", "Downloaded", "Installing", "InstallationFailed"]
        assert len(peer.history) == 1 and _connector_status(peer, version, 1)[-1] == "Available"
    _scenario(body, cfg_kw=_maint(fail_install=True), version=version)


@VERSIONS
def test_firmware_waits_for_the_transaction_and_holds_free_connectors(version):
    """1.6 §5.19 (RECOMMENDED) / L02.FR.03, FR.04: install only after the transaction ended; meanwhile the
    connector without a transaction is Unavailable and cannot start one."""
    async def body(peer, rt):
        await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "T1") is A.ACCEPTED
        await _until(lambda: rt.station.has_transaction(1))
        await _update(peer, version)
        await _until(lambda: "Downloaded" in _fw(peer))
        await _until(lambda: _connector_status(peer, version, 2)[-1:] == ["Unavailable"])
        await asyncio.sleep(0.3)
        assert "Installing" not in _fw(peer)
        await rt.plug(2)
        assert await rt.swipe(2, "T2") is not A.ACCEPTED or not rt.station.has_transaction(2)
        await rt.stop_tx(1)
        await _until(lambda: "Installed" in _fw(peer))
        assert _connector_status(peer, version, 2)[-1] != "Unavailable"  # after the reboot
    _scenario(body, cfg_kw={**_maint(), "connectors": 2, "auth_mode": "accept_all"}, version=version)


def test_201_scheduled_download_signed_and_scheduled_install():
    """L02.FR.07 DownloadScheduled, L01.FR.04 SignatureVerified (taken as valid), L02.FR.10 InstallScheduled."""
    async def body(peer, rt):
        await _online(peer, rt)
        r = await _update(peer, "2.0.1", retrieve=_now(0.3),
                          firmware={"install_date_time": _now(0.8), "signature": "c2ln",
                                    "signing_certificate": "-----BEGIN CERTIFICATE-----"})
        assert r.status == "Accepted"
        await _until(lambda: "Installed" in _fw(peer))
        assert _fw(peer) == ["DownloadScheduled", "Downloading", "Downloaded", "SignatureVerified",
                             "InstallScheduled", "Installing", "InstallRebooting", "Installed"]
    _scenario(body, cfg_kw=_maint(), version="2.0.1")


def test_201_new_update_cancels_or_is_rejected_while_installing():
    """L02.FR.15 AcceptedCanceled for an update not yet installing; L02.FR.18 Rejected while installing."""
    async def body(peer, rt):
        await _online(peer, rt)
        assert (await _update(peer, "2.0.1", retrieve=_now(60), request_id=1)).status == "Accepted"
        await _until(lambda: _fw(peer) == ["DownloadScheduled"])
        assert (await _update(peer, "2.0.1", request_id=2)).status == "AcceptedCanceled"
        await _until(lambda: "Installing" in _fw(peer))
        r = await _update(peer, "2.0.1", request_id=3)
        assert r.status == "Rejected" and r.status_info["reason_code"] == "Installing"
        await _until(lambda: "Installed" in _fw(peer))
        ids = [i for s in peer.history for i in s.firmware_request_ids]
        assert ids[0] == 1 and set(ids[1:]) == {2}
    _scenario(body, cfg_kw=_maint(install_s=0.5), version="2.0.1")


def test_16_update_while_installing_is_ignored():
    async def body(peer, rt):
        await _online(peer, rt)
        await _update(peer, "1.6")
        await _until(lambda: "Installing" in _fw(peer))
        await _update(peer, "1.6")
        await _until(lambda: "Installed" in _fw(peer))
        await asyncio.sleep(0.3)
        assert _fw(peer) == ["Downloading", "Downloaded", "Installing", "Installed"]
    _scenario(body, cfg_kw=_maint(install_s=0.4))


@VERSIONS
def test_trigger_firmware_status(version):
    """1.6 §4.5 / F06.FR.16: Idle when nothing runs; the current step while an update runs. 2.0.1 L02.FR.16 /
    FR.17: Idle after Installed, else the last sent status (also an end state such as DownloadFailed)."""
    async def body(peer, rt):
        s = await _online(peer, rt)
        assert (await _trigger(peer, version, "FirmwareStatusNotification")).status == "Accepted"
        await _until(lambda: s.firmware_statuses == ["Idle"])
        await _update(peer, version)
        await _until(lambda: s.firmware_statuses[-1:] == ["Downloading"])
        await _trigger(peer, version, "FirmwareStatusNotification")
        await _until(lambda: s.firmware_statuses == ["Idle", "Downloading", "Downloading"])
        await _until(lambda: "DownloadFailed" in s.firmware_statuses)
        await _trigger(peer, version, "FirmwareStatusNotification")
        await _until(lambda: len(s.firmware_statuses) == 5)
        assert s.firmware_statuses[-1] == ("Idle" if _v16(version) else "DownloadFailed")
        if not _v16(version):
            assert s.firmware_request_ids == [None, 5, 5, 5, 5]  # L02.FR.14: no requestId with Idle
    _scenario(body, cfg_kw=_maint(fail_download=True, download_s=0.6), version=version)


def test_201_trigger_firmware_idle_after_installed():
    async def body(peer, rt):
        await _online(peer, rt)
        await _update(peer, "2.0.1")
        await _until(lambda: "Installed" in _fw(peer))
        s = peer.history[-1]
        await _trigger(peer, "2.0.1", "FirmwareStatusNotification")
        await _until(lambda: s.firmware_statuses == ["Installed", "Idle"])
    _scenario(body, cfg_kw=_maint(), version="2.0.1")


# --------------------------------------------------------------------------------------------- log / diagnostics


@VERSIONS
def test_log_upload(version):
    """1.6 §5.9 / N01: the answer names the file; Uploading -> Uploaded with the request's requestId."""
    async def body(peer, rt):
        s = await _online(peer, rt)
        r = await _get_log(peer, version)
        name = r.file_name if _v16(version) else r.filename
        assert name.startswith(CP) and name.endswith(".log")
        assert _v16(version) or r.status == "Accepted"
        await _until(lambda: _logs(peer, version) == ["Uploading", "Uploaded"])
        if not _v16(version):
            assert s.log_request_ids == [3, 3]
    _scenario(body, cfg_kw=_maint(), version=version)


@VERSIONS
def test_log_upload_failed(version):
    async def body(peer, rt):
        await _online(peer, rt)
        await _get_log(peer, version, retries=1, retry_interval=0)
        fail = "UploadFailed" if _v16(version) else "UploadFailure"
        await _until(lambda: _logs(peer, version) == ["Uploading", "Uploading", fail])
    _scenario(body, cfg_kw=_maint(fail_upload=True), version=version)


def test_201_get_log_cancels_a_running_upload():
    """N01.FR.12 AcceptedCanceled and N01.FR.20 LogStatusNotification AcceptedCanceled for the old request."""
    async def body(peer, rt):
        s = await _online(peer, rt)
        assert (await _get_log(peer, "2.0.1", request_id=1)).status == "Accepted"
        await _until(lambda: s.log_statuses == ["Uploading"])
        assert (await _get_log(peer, "2.0.1", request_id=2)).status == "AcceptedCanceled"
        await _until(lambda: s.log_statuses[-1:] == ["Uploaded"])
        assert s.log_statuses == ["Uploading", "AcceptedCanceled", "Uploading", "Uploaded"]
        assert s.log_request_ids == [1, 1, 2, 2]
    _scenario(body, cfg_kw=_maint(upload_s=0.5), version="2.0.1")


@VERSIONS
def test_trigger_log_status(version):
    """1.6 §4.4 / F06.FR.13, FR.14: Idle when no upload runs, else Uploading (2.0.1 with its requestId)."""
    what = "DiagnosticsStatusNotification" if _v16(version) else "LogStatusNotification"

    async def body(peer, rt):
        await _online(peer, rt)
        assert (await _trigger(peer, version, what)).status == "Accepted"
        await _until(lambda: _logs(peer, version) == ["Idle"])
        await _get_log(peer, version)
        await _until(lambda: _logs(peer, version) == ["Idle", "Uploading"])
        await _trigger(peer, version, what)
        await _until(lambda: _logs(peer, version)[:3] == ["Idle", "Uploading", "Uploading"])
        if not _v16(version):
            assert peer.history[-1].log_request_ids[:3] == [None, 3, 3]
    _scenario(body, cfg_kw=_maint(upload_s=0.6), version=version)


# --------------------------------------------------------------------------------------------- runtime.maintenance


class _Host:
    def __init__(self, busy=False):
        self.fw, self.logs, self.reboots, self.holds = [], [], 0, []
        self.busy = busy
        self.m = None

    async def send_fw(self, ev):
        self.fw.append((ev.status, ev.request_id))

    async def send_log(self, ev):
        self.logs.append((ev.status, ev.request_id))

    async def reboot(self):
        self.reboots += 1
        asyncio.get_running_loop().call_soon(self.m.on_online)


def _make(host, states=frozenset(F), **kw):
    host.m = Maintenance(MaintenanceConfig(**{**FAST, **kw}), clock=lambda: datetime.now(timezone.utc),
                         firmware_states=states, send_firmware=host.send_fw, send_log=host.send_log,
                         busy=lambda: host.busy, hold_changed=lambda: host.holds.append(host.m.hold),
                         reboot=host.reboot)
    return host.m


def test_maintenance_skips_unsupported_states_and_reboots_once():
    async def main():
        h = _Host()
        m = _make(h, states=frozenset({F.DOWNLOADING, F.DOWNLOADED, F.INSTALLING, F.INSTALLED}))
        m.start_firmware(FirmwareJob("x", datetime.now(timezone.utc), request_id=None))
        await asyncio.wait_for(m._fw_task, 3)
        assert [s for s, _ in h.fw] == [F.DOWNLOADING, F.DOWNLOADED, F.INSTALLING, F.INSTALLED]
        assert h.reboots == 1 and h.holds == [True] and not m.hold and not m.firmware_busy
    asyncio.run(main())


def test_maintenance_cancel_and_close():
    async def main():
        h = _Host(busy=True)
        m = _make(h)
        m.start_firmware(FirmwareJob("x", datetime.now(timezone.utc), request_id=1))
        await _until(lambda: m.hold)
        assert m.firmware_busy and m.cancel_firmware() and not m.hold and not m.cancel_firmware()
        m.start_log(LogJob("x", "f.log", request_id=2))
        m.close()
        await asyncio.sleep(0.1)
        assert not m.log_busy and h.reboots == 0
        assert (LogState.UPLOADED, 2) not in h.logs
    asyncio.run(main())


def test_maintenance_retries_default_and_negative():
    async def main():
        h = _Host()
        m = _make(h, fail_upload=True)
        m.start_log(LogJob("x", "f.log", retries=-3))
        await asyncio.wait_for(m._log_task, 3)
        assert h.logs == [(LogState.UPLOADING, None), (LogState.UPLOAD_FAILED, None)]
    asyncio.run(main())
