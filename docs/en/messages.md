# OCPP 1.6J messages

Supported in v0.1. Any other action received from the CSMS is answered with CALLERROR `NotImplemented` and
recorded in the frame log.

## Charge point to CSMS

| Action | Notes |
|---|---|
| BootNotification | Accepted / Pending / Rejected. On Pending / Rejected the charger retries after `interval` (or `boot_retry_s` when the interval is 0) and in the meantime only answers CSMS requests |
| Heartbeat | `currentTime` is used to record the clock offset |
| StatusNotification | Connector 0 and every connector; `errorCode`, `info`, `vendorErrorCode` |
| Authorize | Depends on `auth_mode` (see [Configuration](configuration.md)) |
| StartTransaction | Receives the `transactionId`; a non-Accepted `idTagInfo` stops the transaction |
| StopTransaction | `reason`, `transactionData` |
| MeterValues | Sampled (`MeterValueSampleInterval`) and clock-aligned (`ClockAlignedDataInterval`); measurands from `MeterValuesSampledData`; with `location` / `context` |
| DataTransfer | Sent manually from the UI or the HTTP API |

Transaction-related messages (StartTransaction, StopTransaction, periodic MeterValues) and state-change
StatusNotifications go through one ordered queue. A message whose answer was lost in a disconnect is resent
after the next accepted boot, so the CSMS may receive it twice (1.6 has no idempotency key). Failed transaction
messages are retried `TransactionMessageAttempts` times at `TransactionMessageRetryInterval`.

## CSMS to charge point

| Action | Notes |
|---|---|
| RemoteStartTransaction | May carry a `chargingProfile` (TxProfile without `transactionId`) |
| RemoteStopTransaction | |
| GetConfiguration | An empty `key` list returns all keys; unknown keys go to `unknownKey`. Answered even before the boot is accepted |
| ChangeConfiguration | Accepted / Rejected / RebootRequired / NotSupported |
| Reset | Soft / Hard: ends transactions, disconnects, reconnects and boots again |
| TriggerMessage | BootNotification / Heartbeat / MeterValues / StatusNotification; DiagnosticsStatusNotification and FirmwareStatusNotification answer `NotImplemented` |
| UnlockConnector | Unlocked / UnlockFailed / NotSupported |
| ChangeAvailability | Accepted / Rejected / Scheduled (Scheduled while a transaction is running) |
| DataTransfer | Default answer `UnknownVendorId` |
| SetChargingProfile | ChargePointMaxProfile (connector 0 only) / TxDefaultProfile / TxProfile (needs a running transaction) |
| ClearChargingProfile | Filters `id` / `connectorId` / `chargingProfilePurpose` / `stackLevel` |
| GetCompositeSchedule | Optional `chargingRateUnit`; same evaluator as the live power limit |

## Not in v0.1

| Feature | Plan |
|---|---|
| OCPP 2.0.1 / 2.1 | v0.2 / v0.3 (greyed out in the UI) |
| Security profile 3 (TLS client certificates), certificate management, Security Whitepaper messages (SecurityEventNotification, ...) | v0.2 |
| SendLocalList / GetLocalListVersion, authorization cache | v0.2 (v0.1 has the `id_tags` list plus CSMS lookups) |
| ReserveNow / CancelReservation | v0.2 |
| UpdateFirmware / GetDiagnostics and their status notifications | v0.2 (CALLERROR `NotImplemented` in v0.1) |
| ISO 15118 / Plug & Charge | Not supported (paid standard) |
