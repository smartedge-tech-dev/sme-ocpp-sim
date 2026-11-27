# Changelog

All notable changes to SME OCPPSim are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

<!-- release.yml copies the "## [x.y.z]" section matching the pushed tag vx.y.z into the GitHub Release notes. -->

## [Unreleased]

### Added

- OCPP 2.0.1 station core (`ocpp_version: "2.0.1"`, subprotocol `ocpp2.0.1`): BootNotification (Accepted /
  Pending / Rejected), Heartbeat, StatusNotification per EVSE (`connectors: N` = N EVSEs with one connector
  each), Authorize with every `auth_mode`, the TransactionEvent flow Started -> Updated -> Ended with
  station-generated transaction ids, continuous `seqNo` and offline queueing (`offline: true`), MeterValues and
  DataTransfer. Every other 2.0.1 action, including the ISO 15118 messages, is answered `NotImplemented`.
- OCPP 2.0.1 remote control: RequestStartTransaction (with `remoteStartId` and an optional `TxProfile`, not
  applied before smart charging), RequestStopTransaction, UnlockConnector, Reset (`Immediate` / `OnIdle`, with
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

### Changed

- Internal split for OCPP 2.0.1: the charger runtime is a version-neutral shell (connection, boot / heartbeat,
  power-model clock) hosting a per-version station; the OCPP 1.6 state machine, transactions, configuration keys
  and smart-charging rules moved to `sme_ocpp_sim.protocol.v16`. No change on the 1.6 wire.
- The fault dialog takes the fault codes of the charger's OCPP version from the new `GET /api/chargers/{id}`;
  `POST /api/chargers/{id}/fault` rejects a code that is not one of them with `400`.

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
