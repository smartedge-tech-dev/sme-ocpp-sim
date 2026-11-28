# Changelog

All notable changes to SME OCPPSim are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

<!-- release.yml copies the "## [x.y.z]" section matching the pushed tag vx.y.z into the GitHub Release notes. -->

## [Unreleased]

### Added

- Vehicle profile pool (v0.3 M1): `session.ev_model: pool` draws the EV of every transaction (OCPP 1.6 and
  2.0.1) from the built-in presets plus `ev_models`, by the new `ev_models` field `weight`, with the new
  `initial_soc` / `target_soc` fields (a number or a `fixed` / `uniform` / truncated `normal` distribution).
  Draws are deterministic per connector (seeded stream `vehicle:{charger}:{connector}`).
- Three more abstract presets: `suv-90kwh`, `phev-12kwh`, `van-75kwh` (vehicle classes, not real models).
- Excel `ev_models` sheet: columns `weight`, `initial_soc_dist` / `_min` / `_max` / `_mean` / `_sd`,
  `target_soc`.

## [0.2.2] - 2026-11-28

### Added

- Retry limits: `csms_defaults.reconnect.max_retries` (consecutive failed connections) and the charger field
  `boot_max_retries` (non-Accepted BootNotification answers, Pending included). Both default to `null`
  (unlimited). At the limit the charger stops, the UI shows a **Gave up** badge (snapshot `gave_up`,
  `last_error` kind `retries_exhausted`), and only Connect starts it again.
- New handshake error kind `client_cert_required`: a TLS connection closed right after the handshake, or an
  nginx HTTP 400 "SSL certificate" page, while no client certificate is configured. It raises no
  FailedToAuthenticateAtCsms security event and triggers no certificate fallback.
- Charger block `meter:` (simulator options): `begin_end_messages` (1.6 MeterValues `Transaction.Begin` /
  `Transaction.End`), `phase_values` (`off` / `L-N` / `L-L`: Voltage and Current.Import per phase with
  `phase`), `post_tx_delay_s` (delay before StopTransaction / `Ended`) and `tx_data_max_samples` (cap of the
  sampled and of the clock-aligned readings kept for one transaction, default 1000, one warning on overflow).
- OCPP 2.0.1: `SampledDataCtrlr.TxEndedInterval` is ReadWrite; new `AlignedDataCtrlr.TxEndedInterval` and
  `AlignedDataCtrlr.TxEndedMeasurands`. `Ended.meterValue` holds the start and end readings plus the sampled
  and clock-aligned readings of these intervals.
- Command blocking: charger block `impairments.block` maps CSMS → charger actions to `NotImplemented` /
  `NotSupported` (answered with that CALLERROR) or `NoReply` (no answer). Applied live without a reconnect,
  switchable through `GET` / `PUT /api/chargers/{id}/impairments` and the **Impairments** dialog; the frame log
  marks blocked CALLs and the snapshot has `impaired`. `GET /api/chargers/{id}` adds `csms_actions`.
- Network impairments in the same `impairments` block, all applied live: latency / jitter per direction
  (`latency_ms`, `jitter_ms`, `direction`, seeded by `seed` for reproducible runs; frame order is kept),
  periodic disconnects (`drop_every_s`, `drop_jitter_s`, `drop_mode` `abort` / `close`) and the pause before
  reconnecting (`down_s`). `POST /api/chargers/{id}/drop` drops the connection now. Simulated drops do not count
  against `max_retries` / `boot_max_retries` and keep transactions; snapshot `drops`, `last_error` kind
  `simulated_drop`. UI: the **Impairments** dialog (with **Drop now**); the badge shows `!` while any is on.
- Frame injection (expert use): `POST /api/chargers/{id}/send` sends an arbitrary CALL (schema check optional,
  `validate: false`) and returns the CSMS answer, or writes a `raw` frame verbatim. The station state does not
  change; injected frames are marked in the frame log.
- MCP endpoint `POST /mcp` on the web port (MCP streamable HTTP; no authentication, like the web UI;
  DNS-rebinding protection on a loopback `--host`): 27 tools wrapping the HTTP API (charger state, connect /
  disconnect, plug, authorize, start / stop transaction, faults, external limit, impairments, drop, frame
  injection, DataTransfer, frame log, Mini CSMS). `claude mcp add --transport http ocpp http://127.0.0.1:8082/mcp`; see the new
  docs page "MCP, health check and logs".
- `GET /healthz` (`{"status": "ok", ...}`) for Docker / Kubernetes probes.
- `--log-format json`: one JSON object per log line, uvicorn's logs included.

### Changed

- Dependency `sme-sim-common[xlsx,mcp]>=1.3,<2` (adds the official `mcp` SDK). The Excel export's formula
  quoting, the 10 MB import size limit and the import row limits (5000 `chargers` data rows, 50 000 in each
  other sheet; the cap reported counts the 2 header / help rows and blank or `#` rows too) now come from
  sme-sim-common; a sheet over its limit is reported as `<sheet>: more than N rows (row limit), import refused`, an oversized upload as 413 `... (import size limit)`.
- OCPP 2.0.1: `Ended.meterValue` now starts with a `Transaction.Begin` reading (TxEndedInterval 0 = the start
  and end values).
- An nginx HTTP 400 "SSL certificate" page with a client certificate configured is reported as
  `client_cert_rejected` (with `status` 400) instead of `bad_request`; other HTTP 400 answers over wss:// get a
  hint that also names a TLS proxy refusing the client certificate.

### Fixed

- OCPP 1.6: StopTransaction `transactionData` holds the StopTxnSampledData readings taken every
  MeterValueSampleInterval from the start of the transaction (not only one reading at the end), and the
  StopTxnAlignedData readings at every ClockAlignedDataInterval boundary.
- OCPP 1.6: ChangeConfiguration of `MeterValuesSampledData`, `MeterValuesAlignedData`, `StopTxnSampledData` and
  `StopTxnAlignedData` rejects measurands the simulated meter does not report and lists longer than the
  matching `*MaxLength` key. In YAML / template / Excel `config_keys` such measurands are dropped with a
  warning instead.
- Docs: the 1.6 MeterValues row no longer claims clock-aligned MeterValues messages (not sent yet).
- Windows: the asyncio proactor's "Exception in callback ..._call_connection_lost" `ConnectionResetError`
  traceback after a peer reset is no longer printed (logged at debug level).

## [0.2.1] - 2026-11-28

### Added

- Excel import / export of the charger list (#21): File → Export Excel workbook / Import Excel workbook / Download
  blank Excel template (sheets `chargers`, `config_keys`, `variables`, `ev_models`; passwords exported as
  `********`). An import is all or nothing, reports errors by sheet, row and column, keeps every other block and
  restarts only changed chargers. New routes `GET /api/config/xlsx`, `GET /api/config/xlsx-template`,
  `POST /api/config/import`; new dependency `sme-sim-common[xlsx]` (openpyxl).
- Charger templates (#21): `template:` / `params:` on a charger take the model defaults (power, voltage, phases,
  connectors, model ...) from a built-in template (`jp-ac-3kw`, `jp-ac-6kw`, `jp-ac-6kw-dual`, `eu-ac-11kw`,
  `eu-ac-22kw`) or a user template in `~/.sme-ocpp-sim/templates/` (`SME_OCPP_SIM_TEMPLATES`); the saved config
  keeps only a charger's own overrides. The Add charger dialog adds N chargers from a template at once (IDs and
  passwords count up), new route `POST /api/chargers/bulk/create`; `GET /api/templates`,
  `POST /api/templates/reload` and `POST /api/templates/from-device/{id}` (save a charger as user template).
  A templated charger's dump is compared with the template, not the model default (`phases: 1` on a 3-phase
  template is kept on save and Excel round trip), and `config_keys` / `session` / `maintenance` keep only the
  keys the charger sets. A charger whose (deleted) user template is unknown fails to load with an error naming
  the charger and the template; the bulk create rejects the password mask `********`.
- Mini CSMS: a simple built-in OCPP 1.6J / 2.0.1 CSMS (WebSocket server, default port 9000) in the same process,
  with a new web UI panel (Simulation → Mini CSMS). Subprotocol negotiation, Security Profile 0 / 1 (optional
  Basic Auth), configurable BootNotification answer (Accepted / Pending / Rejected + interval) and authorization
  statuses, valid minimal answers to every CP → CSMS action, records boot info, statuses, transactions and meter
  values per station, a JSON editor that checks any CALL against the schema before sending it, and a YAML plan of
  scheduled CALLs (`mini_csms:` block; e.g. SetChargingProfile 30 s after boot). New `/api/csms/*` routes,
  `--mini-csms` / `--csms-host` / `--csms-port` options, demo config `configs/mini-csms-demo.yaml` and the
  Docker image exposes 9000.
- Local Authorization List and Authorization Cache for OCPP 1.6J and 2.0.1: SendLocalList (Full / Differential,
  version numbers, `VersionMismatch` / `Failed`), GetLocalListVersion and ClearCache. A presented id token is
  authorized from the list first, then the cache (LocalPreAuthorize / LocalAuthorizeOffline); Authorize,
  StartTransaction / StopTransaction (1.6) and TransactionEvent (2.0.1) answers update the cache. New 1.6 keys
  LocalAuthListEnabled, LocalAuthListMaxLength, SendLocalListMaxLength, ReserveConnectorZeroSupported
  (SupportedFeatureProfiles now lists LocalAuthListManagement and Reservation); new 2.0.1 variables
  AuthCacheCtrlr.*, LocalAuthListCtrlr.* and ReservationCtrlr.*.
- Reservations for OCPP 1.6J and 2.0.1: ReserveNow (Accepted / Faulted / Occupied / Rejected / Unavailable,
  replacing by id, connector 0 / no `evseId`) and CancelReservation. A reserved connector reports `Reserved`,
  refuses other id tokens (operator swipe result `Reserved`) and passes the `reservationId` with the start;
  reservations end on expiry, cancel, fault or unavailability (2.0.1 ReservationStatusUpdate `Expired` /
  `Removed`).
- Web UI: the charger detail shows the local list version, a connector card its reservation.
- Firmware update and diagnostics / log upload simulation for OCPP 1.6J and 2.0.1 (nothing is downloaded or
  uploaded): UpdateFirmware (1.6 Downloading → Downloaded → Installing → reboot → Installed; 2.0.1 adds
  DownloadScheduled, SignatureVerified, InstallScheduled, InstallRebooting, `requestId`, AcceptedCanceled /
  Rejected), the install waits for running transactions with the free connectors Unavailable, then reboots and
  boots again (2.0.1 reason `FirmwareUpdate`); GetDiagnostics (1.6) / GetLog (2.0.1, AcceptedCanceled) with a
  file name and Uploading → Uploaded / failure. TriggerMessage FirmwareStatusNotification /
  DiagnosticsStatusNotification (1.6, was NotImplemented) and LogStatusNotification (2.0.1) report the current
  status. New per-charger `maintenance` settings: step durations, retry interval and failure injection
  (`fail_download`, `fail_install`, `fail_upload`). The Web UI charger detail shows the firmware / log status.
- Security profile 3 for OCPP 1.6J and 2.0.1: `wss://` with a TLS client certificate (`client_cert_file` /
  `client_key_file` per charger, `csms_defaults.security_profile: 3`, no Basic Auth); TLS 1.2 or above; a
  refused client certificate is reported as `client_cert_rejected`.
- Certificate management for OCPP 2.0.1 and the 1.6 Security Whitepaper: SignCertificate with a generated CSR
  (TriggerMessage `SignChargingStationCertificate` / ExtendedTriggerMessage `SignChargePointCertificate`),
  CertificateSigned (the new certificate is used after a reconnect, with a fallback to the old one when the CSMS
  refuses it), InstallCertificate (installed CSMS roots are trusted for the next TLS connection),
  DeleteCertificate and GetInstalledCertificateIds; ExtendedTriggerMessage (1.6).
- SecurityEventNotification for both versions: StartupOfTheDevice, ResetOrReboot, FirmwareUpdated, invalid
  CSMS / charging station certificate, TLS version, failed authentication, discarded renewed certificate and
  reconfiguration events; critical events are queued while offline. New 1.6 keys SecurityProfile, CpoName,
  CertificateStoreMaxLength, AdditionalRootCertificateCheck and the write-only AuthorizationKey (ChangeConfiguration
  changes the Basic Auth password and reconnects with it, Security Whitepaper A01); the 2.0.1 SecurityCtrlr.SecurityProfile reports 3,
  CertificateEntries counts the installed certificates, AdditionalRootCertificateCheck is `false`.
- Web UI: the charger detail shows the security profile and the client certificate subject; the settings
  dialog has profile 3 and the client certificate / key files.
- Documentation (#22): the READMEs (en / ja / zh) and the documentation home pages list the local
  authorization list, reservations, firmware / diagnostics simulation, security profile 3 and certificate
  management, the Mini CSMS and Excel / charger templates; retaken screenshots, new `mini-csms.png` and
  `add-charger.png` (`scripts/screenshots.py` also runs `configs/mini-csms-demo.yaml`).

### Changed

- The 1.6 security support was checked against the OCPP 1.6 Security Whitepaper edition 4: ExtendedTriggerMessage
  `FirmwareStatusNotification` (which means SignedFirmwareStatusNotification) is now `NotImplemented`, and
  `connectorId` 0 is CALLERROR `PropertyConstraintViolation` (the whitepaper requires `connectorId` > 0).
- `cryptography` is now a runtime dependency (CSR generation, certificate parsing); it is bundled in the exe
  and the Docker image.

### Fixed

- Windows executable: saving the config from the web UI was written to the temporary PyInstaller extraction
  directory and lost when the exe exited (#27). Started without `-c`, the exe now uses the per-user
  `~/.sme-ocpp-sim/config.yaml` (created from the bundled demo config on the first run); `-c FILE` is used and
  saved in place. pip, source and Docker runs are unchanged. The config file in use is logged at startup.

### Security

- Secret values are masked as `********` in the frame log (charger simulator and Mini CSMS) and in the Mini
  CSMS's recorded calls: 1.6 ChangeConfiguration `AuthorizationKey`, 2.0.1 SetVariables `BasicAuthPassword` and
  2.0.1 SetNetworkProfile VPN / APN credentials (#54). The frame sent on the wire is unchanged.
- Excel export writes no formula cells: a text such as `=1+1` (vendor, id tag, config key value ...) is a text
  cell with Excel's quote prefix and imports back unchanged (#21). `POST /api/config/import` rejects a body over
  10 MB with 413 and a workbook with more than 5000 charger rows (50000 on the other sheets) with 400.

## [0.2.0] - 2026-11-27

### Added

- OCPP 2.0.1 station core (`ocpp_version: "2.0.1"`, subprotocol `ocpp2.0.1`): BootNotification (Accepted /
  Pending / Rejected), Heartbeat, StatusNotification per EVSE (`connectors: N` = N EVSEs with one connector
  each), Authorize with every `auth_mode`, the TransactionEvent flow Started -> Updated -> Ended with
  station-generated transaction ids, continuous `seqNo` and offline queueing (`offline: true`), MeterValues and
  DataTransfer. Every other 2.0.1 action, including the ISO 15118 messages, is answered `NotImplemented`.
- OCPP 2.0.1 remote control: RequestStartTransaction (with `remoteStartId` and an optional `TxProfile`), RequestStopTransaction, UnlockConnector, Reset (`Immediate` / `OnIdle`, with
  `Scheduled` while a transaction runs and BootNotification reason `RemoteReset`), ChangeAvailability (station
  or EVSE level, `Scheduled` while a transaction runs) and TriggerMessage (BootNotification, Heartbeat,
  StatusNotification, MeterValues, TransactionEvent; the other messages answer `NotImplemented`).
- OCPP 2.0.1 Device Model: GetVariables / SetVariables (attribute types Actual / Target / MinSet / MaxSet,
  ReadOnly / WriteOnly / ReadWrite, `UnknownComponent` / `UnknownVariable` / `NotSupportedAttributeType`,
  `RebootRequired`, value checks for dataType, limits and valuesList) and GetBaseReport (ConfigurationInventory,
  FullInventory, SummaryInventory) answered by NotifyReport parts (`seqNo` / `tbc`). A minimal set of standard
  variables drives the station (HeartbeatInterval, TxUpdatedInterval / Tx*Measurands, AuthorizeRemoteStart,
  StopTxOnInvalidId, MessageAttempts, ItemsPerMessage, ...); the new charger `variables:` YAML list overrides
  them and adds user-defined variables. `GET /api/chargers/{id}/configuration` returns a 2.0.1 charger's
  variables (`ocpp_version`, `variables`). SetVariables changes are not written back to the YAML file.
- OCPP 2.0.1 Smart Charging: SetChargingProfile, ClearChargingProfile, GetCompositeSchedule, GetChargingProfiles
  (answered by ReportChargingProfiles parts with `tbc`), NotifyChargingLimit and ClearedChargingLimit, with the
  purposes ChargingStationMaxProfile, TxDefaultProfile, TxProfile and ChargingStationExternalConstraints. The
  2.0.1 rules are a separate copy of the 1.6 ones; the power model is shared, so the composite schedule and the
  TransactionEvent power stay equal. The `chargingProfile` of RequestStartTransaction is now applied. New
  SmartChargingCtrlr variables (Enabled, Available, ExternalControlSignalsEnabled,
  NotifyChargingLimitWithSchedules, ProfileStackLevel, PeriodsPerSchedule, RateUnit).
- `POST /api/chargers/{id}/external-limit`: an external (energy management) limit per EVSE or for the whole
  charger. OCPP 2.0.1 applies it as ChargingStationExternalConstraints and notifies the CSMS; OCPP 1.6 answers
  400 (not supported).

- Web UI for OCPP 2.0.1: the version can be chosen in the settings dialog (2.1 stays greyed out); a 2.0.1
  charger shows its Device Model in a read-only Variables table (from `GET /api/chargers/{id}/configuration`;
  there is no Web API route to change a variable), labels its connectors as EVSEs and has an External limit (EMS)
  control in the Smart Charging panel (`POST /api/chargers/{id}/external-limit`). The fault dialog preselects
  `Other` for 2.0.1. New UI strings in English, Japanese and Chinese.
- Documentation for OCPP 2.0.1 (en / ja / zh): the supported 2.0.1 messages (every other one marked Not
  supported), a 2.0.1 quick-start example, `ocpp_version` / `variables` configuration, Smart Charging and HTTP API
  notes; README features, the OCPP badge and the screenshots now show a 2.0.1 charger, incl. the Variables table.
- `configs/mixed-site.yaml`: a sample site with OCPP 1.6J chargers and OCPP 2.0.1 stations.
- `scripts/screenshots.py` runs a mixed 1.6 / 2.0.1 site against both dev CSMS peers and also captures
  `variables.png`.

### Changed

- Internal split for OCPP 2.0.1: the charger runtime is a version-neutral shell (connection, boot / heartbeat,
  power-model clock) hosting a per-version station; the OCPP 1.6 state machine, transactions, configuration keys
  and smart-charging rules moved to `sme_ocpp_sim.protocol.v16`. No change on the 1.6 wire.
- The fault dialog takes the fault codes of the charger's OCPP version from the new `GET /api/chargers/{id}`;
  `POST /api/chargers/{id}/fault` rejects a code that is not one of them with `400`.

### Fixed

- OCPP 2.0.1 spec conformance, RPC (Part 4): the error code is `OccurrenceConstraintViolation` (Part 4 Table 8;
  Part 2 B06 / B08 spell it `Occurence`); a CALL with a wrong frame structure is answered `RpcFrameworkError`,
  a non-object payload still `FormatViolation`; a received MessageId longer than 36 characters is answered
  `RpcFrameworkError`; an outgoing errorDescription is cut to 255 characters. OCPP 1.6 is unchanged.
- OCPP 2.0.1 Device Model (Part 2 Referenced Components and Variables): HeartbeatInterval minLimit 1; the
  AuthCtrlr controls (Enabled, LocalPreAuthorize, LocalAuthorizeOffline, OfflineTxForUnknownIdEnabled,
  DisableRemoteAuthorization) and SmartChargingCtrlr.Enabled are ReadWrite, the AuthCtrlr ones start from
  `auth_mode` and change the authorization at once; NotifyChargingLimitWithSchedules defaults to `false`;
  Identity maxLimit 48, BasicAuthPassword maxLimit 64, Tx*Measurands maxLimit; the required ChargingStation /
  EVSE / Connector variables (Available, AvailabilityState, EVSE Power and SupplyPhases) and the missing
  required OCPPCommCtrlr, SampledDataCtrlr, TxCtrlr, SecurityCtrlr and SmartChargingCtrlr variables are
  reported; SummaryInventory follows B07.FR.09 (the station's and each EVSE's AvailabilityState).
- OCPP 2.0.1 B use cases: SetVariables of SecurityCtrlr.BasicAuthPassword is `Accepted` and the station
  reconnects with the new password (A01; in memory only); GetBaseReport never answers `EmptyResultSet` (B07) but
  `Accepted` with one empty NotifyReport.
- OCPP 2.0.1 spec conformance, functional rules (Part 2 B / E / F / G / K, #42): while Pending,
  RequestStart/StopTransaction are `Rejected` (B02.FR.05) and triggered Heartbeat / StatusNotification /
  MeterValues / TransactionEvent are `Accepted` (B02.FR.02); while Rejected every CSMS CALL is answered
  CALLERROR `SecurityError` (B03). `StopTxOnInvalidId` false stops the energy transfer (`SuspendedEVSE`,
  E05.FR.02); `evse` is only in the first TransactionEvent (E01.FR.16). A remote-start token that waits for the
  cable starts with `RemoteStart` (F02.FR.21); `Central` / `NoAuthorization` tokens are never authorized
  (F01.FR.02); a waiting token is dropped after `TxCtrlr.EVConnectionTimeOut` (now ReadWrite). Triggered
  MeterValues use the new `AlignedDataCtrlr.Measurands`, a triggered TransactionEvent `TxUpdatedMeasurands`
  (F06.FR.06 / FR.07); TriggerMessage LogStatusNotification / FirmwareStatusNotification send status `Idle`
  (F06.FR.14 / FR.16). The station availability is kept apart from the EVSEs' (G03 / G04). Smart charging: one
  `chargingSchedule` only (K01.FR.34), ChargingStationMaxProfile never Relative (K01.FR.38), `startSchedule`
  required for Absolute / Recurring and forbidden for Relative (K01.FR.40 / FR.41), TxProfile conflicts ignore
  the validity period (K01.FR.39), a TxDefaultProfile with the stackLevel of one on evseId 0 / an EVSE is
  `DuplicateProfile` (K01.FR.52 / FR.53), Rejected profiles carry `DuplicateProfile` / `UnsupportedPurpose` /
  `UnsupportedRateUnit` / `TxNotFound`, GetCompositeSchedule rejects a unit outside `RateUnit` (K08.FR.07), and
  an external limit change during a transaction queues `ChargingRateChanged` (K11.FR.04 / K13.FR.03).

## [0.1.0] - 2026-11-27

First release.

### Added

- OCPP-J RPC layer and WebSocket transport (subprotocol `ocpp1.6`): CALL / CALLRESULT / CALLERROR with
  correct error codes for malformed frames, at most one outstanding outbound CALL per connection, each inbound CALL handled
  in its own task, call timeouts, reconnect with exponential backoff and jitter, classified handshake errors (HTTP 404 unknown charge point, subprotocol).
- OCPP 1.6J message models with lenient parsing and schema-equivalent validation (official OCA schemas are
  not bundled).
- Charge point runtime: BootNotification (Accepted / Pending / Rejected), Heartbeat, StatusNotification,
  Authorize (`auth_mode`: local_only / csms_only / local_then_csms / accept_all), Start / StopTransaction,
  sampled and clock-aligned MeterValues, DataTransfer. CSMS requests: RemoteStart / RemoteStopTransaction,
  Get / ChangeConfiguration, Reset (Soft / Hard), TriggerMessage, UnlockConnector, ChangeAvailability,
  DataTransfer. Unimplemented actions answer CALLERROR `NotImplemented`.
- Ordered transaction message queue with retries (`TransactionMessageAttempts` /
  `TransactionMessageRetryInterval`) and resend after reconnect.
- Smart Charging: SetChargingProfile, ClearChargingProfile, GetCompositeSchedule and RemoteStartTransaction
  profiles. One evaluator computes both the composite schedule and the live power limit. Interpretations of
  points the specification leaves open are documented.
- EV and power model: battery capacity, SoC, charging curve, phases, minimum current, efficiency, ramps,
  single-phase and 3-phase (line-to-line) supplies; Charging / SuspendedEV / SuspendedEVSE; `time_scale`.
- Security profiles 0, 1 and 2 (`ws://`, HTTP Basic, `wss://` with a configurable CA file).
- Multi-charger engine (200+ chargers per process), YAML configuration with validation, live reload that
  reconnects only changed chargers, and write-back that keeps comments outside the changed blocks.
- Web UI (English / Japanese / Simplified Chinese, light / dark): charger list with bulk connect / disconnect,
  connector cards and actions, Smart Charging panel, OCPP frame log with JSON Lines export, configuration
  keys, settings.
- HTTP API and `/ws` live updates; passwords are masked in every API response.
- Windows single-file executable, multi-arch Docker image on GHCR (linux/amd64, linux/arm64) with offline
  image archives, and the PyPI package.
- Documentation in English, Japanese and Simplified Chinese.
