# Changelog

All notable changes to SME OCPPSim are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

<!-- release.yml copies the "## [x.y.z]" section matching the pushed tag vx.y.z into the GitHub Release notes. -->

## [Unreleased]

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
