# Changelog

All notable changes to SME OCPPSim are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

<!-- release.yml copies the "## [x.y.z]" section matching the pushed tag vx.y.z into the GitHub Release notes. -->

## [Unreleased]

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
