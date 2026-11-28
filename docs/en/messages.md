# OCPP messages

SME OCPPSim simulates OCPP 1.6J charge points and OCPP 2.0.1 charging stations; `ocpp_version` selects the
version per charger or for all of them (see [Configuration](configuration.md)). Any action received from the
CSMS that is not supported is answered with CALLERROR `NotImplemented` and recorded in the frame log.

## OCPP 1.6J

Subprotocol `ocpp1.6`.

### Charge point to CSMS

| Action | Notes |
|---|---|
| BootNotification | Accepted / Pending / Rejected. On Pending / Rejected the charger retries after `interval` (or `boot_retry_s` when the interval is 0) and in the meantime only answers CSMS requests |
| Heartbeat | `currentTime` is used to record the clock offset |
| StatusNotification | Connector 0 and every connector; `errorCode`, `info`, `vendorErrorCode` |
| Authorize | Depends on `auth_mode` (see [Configuration](configuration.md)) |
| StartTransaction | Receives the `transactionId`; a non-Accepted `idTagInfo` stops the transaction |
| StopTransaction | `reason`, `transactionData`: the `StopTxnSampledData` measurands every `MeterValueSampleInterval` from the start of the transaction (`Sample.Periodic`) and at the end (`Transaction.End`), the `StopTxnAlignedData` measurands at every `ClockAlignedDataInterval` boundary (`Sample.Clock`); none when both keys are empty. A measurand the meter does not report (other than Energy.Active.Import.Register, Power.Active.Import, Current.Import, Voltage, SoC) or a list longer than its `*MaxLength` key is `Rejected` by ChangeConfiguration |
| MeterValues | Sampled every `MeterValueSampleInterval` during a transaction; measurands from `MeterValuesSampledData`; with `location` / `context`. Clock-aligned MeterValues messages (`ClockAlignedDataInterval` / `MeterValuesAlignedData`) are not sent yet (`StopTxnAlignedData` readings in `transactionData` are) |
| DataTransfer | Sent manually from the UI or the HTTP API |
| FirmwareStatusNotification | Downloading → Downloaded → Installing → (reboot, BootNotification) → Installed; DownloadFailed / InstallationFailed by failure injection |
| DiagnosticsStatusNotification | Uploading → Uploaded / UploadFailed |
| SecurityEventNotification | Security Whitepaper; the same events as 2.0.1, with the 1.6 names InvalidCentralSystemCertificate / FailedToAuthenticateAtCentralSystem / InvalidChargePointCertificate (critical ones queued while offline) |
| SignCertificate | Security Whitepaper; after ExtendedTriggerMessage `SignChargePointCertificate`: a new key and CSR (CN = `serial` or the charger id, O = `CpoName`, default `vendor`) |

Transaction-related messages (StartTransaction, StopTransaction, periodic MeterValues) and state-change
StatusNotifications go through one ordered queue. A message whose answer was lost in a disconnect is resent
after the next accepted boot, so the CSMS may receive it twice (1.6 has no idempotency key). Failed transaction
messages are retried `TransactionMessageAttempts` times at `TransactionMessageRetryInterval`.

### CSMS to charge point

| Action | Notes |
|---|---|
| RemoteStartTransaction | May carry a `chargingProfile` (TxProfile without `transactionId`). Rejected before the boot is accepted |
| RemoteStopTransaction | |
| GetConfiguration | An empty `key` list returns all keys; unknown keys go to `unknownKey`. Answered even before the boot is accepted. The write-only `AuthorizationKey` is listed without a value |
| ChangeConfiguration | Accepted / Rejected / RebootRequired / NotSupported. `AuthorizationKey` (Security Whitepaper A01): 16 - 40 characters, then a reconnect with the new Basic Auth password; `Rejected` with profile 3 |
| Reset | Soft / Hard: ends transactions, disconnects, reconnects and boots again |
| TriggerMessage | BootNotification / Heartbeat / MeterValues / StatusNotification / FirmwareStatusNotification / DiagnosticsStatusNotification (the current step while an update / upload runs, else `Idle`) |
| UnlockConnector | Always answers `Unlocked` (other answers are not configurable yet); a running transaction on the connector ends with reason `UnlockCommand` |
| ChangeAvailability | Accepted / Rejected / Scheduled (Scheduled while a transaction is running) |
| DataTransfer | Default answer `UnknownVendorId` |
| SetChargingProfile | ChargePointMaxProfile (connector 0 only) / TxDefaultProfile / TxProfile (needs a running transaction) |
| ClearChargingProfile | Filters `id` / `connectorId` / `chargingProfilePurpose` / `stackLevel` |
| GetCompositeSchedule | Optional `chargingRateUnit`; same evaluator as the live power limit |
| SendLocalList | Full / Differential. `listVersion` must be > 0 (else CALLERROR `PropertyConstraintViolation`); a Differential update must raise it (`VersionMismatch`). More entries than `SendLocalListMaxLength`, duplicate tags, a Full entry without `idTagInfo` or more than `LocalAuthListMaxLength` tags → `Failed` (the list is unchanged); `NotSupported` while `LocalAuthListEnabled` is false |
| GetLocalListVersion | The stored version; 0 before the first list |
| ClearCache | Clears the Authorization Cache; `Rejected` while `AuthorizationCacheEnabled` is false |
| ReserveNow | Faulted / Unavailable / Occupied by the connector state, else Accepted; the same `reservationId` replaces a reservation; connector 0 keeps one connector free (`Rejected` while `ReserveConnectorZeroSupported` is false). A reserved connector reports `Reserved` and starts only for the reserved `idTag` or a tag with that parent; the `reservationId` goes with StartTransaction. It ends on expiry, cancel, or when the connector becomes Faulted / Unavailable |
| CancelReservation | `Accepted` for a known reservation, else `Rejected` |
| UpdateFirmware | Simulated, nothing is downloaded: waits for `retrieveDate`, `retries` / `retryInterval` on a download failure; connectors without a transaction are `Unavailable` until no transaction is left, then it installs and reboots (as a Hard Reset). Step durations and failure injection: `maintenance` per charger (`download_s`, `install_s`, `upload_s`, `retry_interval_s`, `fail_download`, `fail_install`, `fail_upload`). A new request replaces one that is not installing yet; one received while installing is ignored |
| GetDiagnostics | Answers `fileName` `<id>-diagnostics-<time>.log` and simulates the upload (nothing is uploaded); a new request replaces a running upload |
| ExtendedTriggerMessage | Security Whitepaper: BootNotification / Heartbeat / MeterValues / StatusNotification as TriggerMessage, plus `SignChargePointCertificate`; `FirmwareStatusNotification` (it means SignedFirmwareStatusNotification) and `LogStatusNotification` are `NotImplemented`; `connectorId` 0 is CALLERROR `PropertyConstraintViolation` (the whitepaper requires `connectorId` > 0) |
| CertificateSigned | Security Whitepaper; as 2.0.1: `Accepted` for a chain matching the outstanding CSR, then (profile 3) reconnect with it |
| InstallCertificate / DeleteCertificate / GetInstalledCertificateIds | Security Whitepaper; CentralSystemRootCertificate / ManufacturerRootCertificate, as 2.0.1 |

## OCPP 2.0.1 { #ocpp-201 }

Subprotocol `ocpp2.0.1`. `connectors: N` is a station with N EVSEs of one connector each; wherever the web UI or
the HTTP API says "connector" for a 2.0.1 charger, it means the EVSE id. The settings the station acts on are
Device Model variables (see [Configuration](configuration.md#variables)), not 1.6 configuration keys. Where the
specification leaves a point open, the notes below give the simulator's interpretation.

### Charging station to CSMS

| Message | Support | Notes |
|---|---|---|
| Authorize | Supported | Depends on `auth_mode`; a token presented at the charger is sent with `type` `ISO14443`. Status values the simulator does not model (NoCredit, NotAtThisTime, ...) are shown to the operator as Invalid |
| BootNotification | Supported | Accepted / Pending / Rejected. `reason` is `PowerUp`, `RemoteReset` after a CSMS Reset, `Triggered` after a TriggerMessage. Until the boot is accepted only BootNotification is sent, except what a TriggerMessage asks for while Pending (B02.FR.02). While Pending, RequestStart/StopTransaction are `Rejected` (B02.FR.05); while Rejected every CSMS request is answered CALLERROR `SecurityError` (B03) |
| ClearedChargingLimit | Supported | When the external limit is removed (`chargingLimitSource` `EMS`); only while the boot is accepted, see [Smart Charging](smart-charging.md#ocpp-201) |
| DataTransfer | Supported | Sent manually from the UI or the HTTP API |
| FirmwareStatusNotification | Supported | Progress of UpdateFirmware with its `requestId`; on TriggerMessage the last status, `Idle` when it was Installed or none |
| Get15118EVCertificate | Not supported | ISO 15118 |
| GetCertificateStatus | Not supported | ISO 15118 |
| Heartbeat | Supported | Every `OCPPCommCtrlr.HeartbeatInterval` (also set by an accepted BootNotification) |
| LogStatusNotification | Supported | Progress of GetLog (Uploading → Uploaded / UploadFailure; AcceptedCanceled for an upload a new GetLog cancelled); on TriggerMessage `Uploading` while uploading, else `Idle` |
| MeterValues | Supported | Sent on TriggerMessage (context `Trigger`, measurands from `AlignedDataCtrlr.Measurands`); transaction readings travel in TransactionEvent |
| NotifyChargingLimit | Supported | When the external limit is set (`chargingLimitSource` `EMS`; with the schedule when `SmartChargingCtrlr.NotifyChargingLimitWithSchedules` is true); only while the boot is accepted, see [Smart Charging](smart-charging.md#ocpp-201) |
| NotifyCustomerInformation | Not supported |  |
| NotifyDisplayMessages | Not supported |  |
| NotifyEVChargingNeeds | Not supported | ISO 15118 |
| NotifyEVChargingSchedule | Not supported | ISO 15118 |
| NotifyEvent | Not supported | A fault is reported only as the `stoppedReason` of the interrupted transaction |
| NotifyMonitoringReport | Not supported |  |
| NotifyReport | Supported | Answers GetBaseReport in parts (`DeviceDataCtrlr.ItemsPerMessage[GetReport]` entries each, `seqNo` from 0, `tbc` on all but the last) |
| PublishFirmwareStatusNotification | Not supported |  |
| ReportChargingProfiles | Supported | Answers GetChargingProfiles: one part per EVSE and `chargingLimitSource`, `tbc` on all but the last |
| ReservationStatusUpdate | Supported | `Expired` at `expiryDateTime`, `Removed` when the reserved EVSE (no `evseId`: the station) becomes Faulted / Unavailable |
| SecurityEventNotification | Supported | StartupOfTheDevice, ResetOrReboot, FirmwareUpdated, InvalidCsmsCertificate, InvalidTLSVersion, FailedToAuthenticateAtCsms, InvalidChargingStationCertificate, DiscardedRenewedClientCertificate, ReconfigurationOfSecurityParameters. Critical events are queued while offline and sent after the next accepted boot; non-critical ones only when they happen while connected |
| SignCertificate | Supported | After TriggerMessage `SignChargingStationCertificate`: a new EC P-256 key and a CSR (CN = `serial`, or the charger id without one; O = `SecurityCtrlr.OrganizationName`), `certificateType` `ChargingStationCertificate`. An unanswered CSR is not resent (CertSigningWaitMinimum is not modelled) |
| StatusNotification | Supported | One per EVSE (connector 1), none for EVSE 0. `connectorStatus` Available / Occupied / Reserved / Unavailable / Faulted; the finer state (charging, suspended) is the `chargingState` of TransactionEvent |
| TransactionEvent | Supported | `Started` → `Updated` → `Ended`. The station generates the `transactionId` (a UUID); `seqNo` counts from 0 per transaction; periodic `Updated` every `SampledDataCtrlr.TxUpdatedInterval`; `offline: true` for events raised while disconnected. `Started` carries `SampledDataCtrlr.TxStartedMeasurands`; `Ended` carries the union of `SampledDataCtrlr` and `AlignedDataCtrlr` `TxEndedMeasurands` at the start (`Transaction.Begin`) and end (`Transaction.End`), the sampled ones every `SampledDataCtrlr.TxEndedInterval` from the start (`Sample.Periodic`; 0 = start and end only) and the aligned ones at every `AlignedDataCtrlr.TxEndedInterval` clock boundary (`Sample.Clock`; 0 = none). As in 1.6, a measurand the meter does not report is refused: the `*Measurands` variables have the five reported measurands as `valuesList`, so SetVariables answers `Rejected` |

TransactionEvents and state-change StatusNotifications go through one ordered queue, as in 1.6. A failed
TransactionEvent is retried `OCPPCommCtrlr.MessageAttempts[TransactionEvent]` times,
`MessageAttemptInterval[TransactionEvent]` × failures apart; one whose answer was lost in a disconnect is sent
again after the next accepted boot (the CSMS can de-duplicate by `transactionId` + `seqNo`).

In the simulator's interpretation a transaction starts when the cable is plugged in **and** the id token is
authorized (`triggerReason` `CablePluggedIn` or `Authorized`, whichever comes last) and ends on the same token
presented again or an operator stop (`StopAuthorized` / `Local`), an unplugged cable (`EVCommunicationLost`), an
operator fault (`AbnormalCondition`; the fault code is the `stoppedReason`) or a token the CSMS refuses in a
TransactionEvent answer (`Deauthorized`, while `TxCtrlr.StopTxOnInvalidId` is true). With `StopTxOnInvalidId` false the transaction goes on, but the energy transfer stops at once (`Updated` `ChargingStateChanged`, `SuspendedEVSE`).

### CSMS to charging station

| Message | Support | Notes |
|---|---|---|
| CancelReservation | Supported | `Accepted` for a known reservation (no ReservationStatusUpdate), else `Rejected` |
| CertificateSigned | Supported | `Accepted` for a chain whose certificate matches the outstanding CSR and is valid now (the CA hierarchy is not checked; the CSMS checks it in the next TLS handshake), else `Rejected` with InvalidChargingStationCertificate. With profile 3 the station reconnects with the new certificate; if that connection fails it falls back to the old one (DiscardedRenewedClientCertificate). `V2GCertificate` is `Rejected` |
| ChangeAvailability | Supported | No `evse` or EVSE 0 sets the station's own availability, kept apart from the EVSEs' (an EVSE `Operative` does not undo a station `Inoperative`). Going Inoperative while a transaction runs (on the EVSE, or on any EVSE for the station) is `Scheduled` until it ends; idle EVSEs follow a station Inoperative at once |
| ClearCache | Supported | Clears the Authorization Cache; `Rejected` while `AuthCacheCtrlr.Enabled` is false |
| ClearChargingProfile | Supported | `chargingProfileId` wins over the criteria, the criteria are ANDed; external constraints are never cleared by the CSMS |
| ClearDisplayMessage | Not supported |  |
| ClearVariableMonitoring | Not supported |  |
| CostUpdated | Not supported |  |
| CustomerInformation | Not supported |  |
| DataTransfer | Supported | Default answer `UnknownVendorId` |
| DeleteCertificate | Supported | `Accepted` / `NotFound` by the hash data (any of SHA256 / SHA384 / SHA512); the client certificate cannot be deleted (`Failed`) |
| GetBaseReport | Supported | ConfigurationInventory (every variable that is not ReadOnly), FullInventory, SummaryInventory; the NotifyReport parts follow the answer |
| GetChargingProfiles | Supported | `Accepted` when a profile matches, else `NoProfiles`; the ReportChargingProfiles parts follow the answer |
| GetCompositeSchedule | Supported | Same evaluator as the live power limit; periods are capped at the hardware maximum, which also stands for "no limit". Unknown EVSE or duration < 1 → `Rejected`; a `chargingRateUnit` not in `SmartChargingCtrlr.RateUnit` → `Rejected` (`UnsupportedRateUnit`) |
| GetDisplayMessages | Not supported |  |
| GetInstalledCertificateIds | Supported | SHA256 hash data of the installed CA certificates; `NotFound` when none matches (V2GCertificateChain never does) |
| GetLocalListVersion | Supported | The stored version; 0 before the first list or while `LocalAuthListCtrlr.Enabled` is false |
| GetLog | Supported | `Accepted` with `filename` `<id>_<logType>_<time>.log`, or `AcceptedCanceled` when it cancels a running upload; the upload is simulated (`maintenance` settings) |
| GetMonitoringReport | Not supported |  |
| GetReport | Not supported | Use GetBaseReport |
| GetTransactionStatus | Not supported |  |
| GetVariables | Supported | Attribute types Actual / Target / MinSet / MaxSet; `UnknownComponent` / `UnknownVariable` / `NotSupportedAttributeType`; a WriteOnly variable is `Rejected`. More items than `DeviceDataCtrlr.ItemsPerMessage[GetVariables]` → CALLERROR `OccurrenceConstraintViolation` |
| InstallCertificate | Supported | One PEM certificate, valid now; `Rejected` for anything else or when 20 CA certificates are installed. Installed CSMS root certificates are trusted for the next TLS connection. Kept in memory only |
| PublishFirmware | Not supported |  |
| RequestStartTransaction | Supported | `remoteStartId` and an optional `chargingProfile` (a TxProfile without `transactionId`, installed bound to the new transaction). Without `evseId` the first startable EVSE with a plugged cable is used. With `AuthCtrlr.AuthorizeRemoteStart` true the token is authorized like a presented one. A token of type `Central` or `NoAuthorization` is never authorized. A transaction started by it has `triggerReason` `RemoteStart`, also when the cable comes later. A token still waiting for the cable after `TxCtrlr.EVConnectionTimeOut` is dropped |
| RequestStopTransaction | Supported | `Accepted` for a running transaction of this station (it ends with `RemoteStop`), else `Rejected` |
| ReserveNow | Supported | Faulted / Unavailable / Occupied by the EVSE state, else Accepted; the same `id` replaces a reservation; without `evseId` one EVSE is kept free (`Rejected` while `ReservationCtrlr.NonEvseSpecific` is false); `connectorType` is not modelled; `Rejected` while `ReservationCtrlr.Enabled` is false. A reserved EVSE reports `Reserved` and starts only for the reserved `idToken` or a token of its `groupIdToken`; the `reservationId` goes with the `Started` event |
| Reset | Supported | `Immediate` / `OnIdle` (`Scheduled` while a transaction runs); the next BootNotification has reason `RemoteReset`. An EVSE reset is `Rejected` |
| SendLocalList | Supported | Full / Differential. `versionNumber` must be > 0 (else CALLERROR `PropertyConstraintViolation`); a Differential update must raise it (`VersionMismatch`). More entries than `LocalAuthListCtrlr.ItemsPerMessage`, duplicate tokens or a Full entry without `idTokenInfo` → `Failed`; `Failed` while `LocalAuthListCtrlr.Enabled` is false |
| SetChargingProfile | Supported | ChargingStationMaxProfile (EVSE 0 only), TxDefaultProfile, TxProfile; ChargingStationExternalConstraints is `Rejected` from the CSMS (`UnsupportedPurpose`). One `chargingSchedule` only. See [Smart Charging](smart-charging.md#ocpp-201) |
| SetDisplayMessage | Not supported |  |
| SetMonitoringBase | Not supported |  |
| SetMonitoringLevel | Not supported |  |
| SetNetworkProfile | Not supported |  |
| SetVariableMonitoring | Not supported |  |
| SetVariables | Supported | Values are checked (data type, limits, values list); a ReadOnly variable is `Rejected`; `RebootRequired` for variables marked so. Changes apply at once and are not written back to the YAML file |
| TriggerMessage | Supported | BootNotification (only while the boot is not accepted), Heartbeat, StatusNotification, MeterValues, TransactionEvent (readings from `TxUpdatedMeasurands`), LogStatusNotification / FirmwareStatusNotification (current status); other requested messages answer `NotImplemented`. All but BootNotification are `Rejected` before a BootNotification answer and served while Accepted or Pending. Unknown EVSE → `Rejected` |
| UnlockConnector | Supported | `Unlocked`; `OngoingAuthorizedTransaction` while the EVSE has a transaction (it is not stopped); `UnknownConnector` for an unknown EVSE or a connector other than 1 |
| UnpublishFirmware | Not supported |  |
| UpdateFirmware | Supported | Simulated, nothing is downloaded or verified: DownloadScheduled (future `retrieveDateTime`) → Downloading → Downloaded → SignatureVerified (with `signature`) → InstallScheduled (future `installDateTime`) → EVSEs without a transaction `Unavailable` until none is left → Installing → InstallRebooting → reboot (BootNotification reason `FirmwareUpdate`) → Installed. `retries` / `retryInterval` on a download failure; `AcceptedCanceled` when it cancels an update that is not installing yet, `Rejected` while installing. Timings and failure injection: `maintenance` per charger |

## OCPP 2.1 { #ocpp-21 }

OCPP 2.1 is available as `ocpp_version: "2.1"` (subprotocol `ocpp2.1`). A 2.1 station behaves as a 2.0.1 station
(same message tables above) plus the following.

- **Framing.** OCPP-J 2.1 adds SEND (`[6, ...]`, one-way, never answered) and CALLRESULTERROR (`[5, ...]`, the peer
  found a CALLRESULT invalid). Both are sent and understood. A frame with an unknown MessageTypeId is ignored under
  2.1 (not answered with a CALLERROR).
- **Relaxed fields.** The generated 2.1 models add optional fields and relax some 2.0.1 fields; a 2.0.1 payload is
  still valid. They enforce structure, required fields, enums and string or array length limits, but not numeric
  ranges or patterns (the same leniency as 2.0.1). The tests validate every outbound frame against the official 2.1
  schemas.
- **Reset.** `ImmediateAndResume` is accepted and handled as `Immediate`; resuming the transactions after the reboot
  is not simulated.
- **TriggerMessage.** `CustomTrigger` answers `NotImplemented`.

New CSMS to station messages:

| Message | Status | Behaviour |
|---|---|---|
| AFRRSignal | Store-and-ack | Stored (the last signal is kept), `Accepted` |
| NotifyAllowedEnergyTransfer | Store-and-ack | Stored per transaction, `Accepted`; `Rejected` if the transaction is unknown |
| SetDefaultTariff | Store-and-ack | Stored per EVSE (0 = all EVSEs): `Accepted`, `DuplicateTariffId`, or `Rejected` for an unknown EVSE |
| GetTariffs | Store-and-ack | Reads back the tariff assignments, or `NoTariff` |
| ClearTariffs | Store-and-ack | A result per tariff: `Accepted` / `NoTariff` |
| ChangeTransactionTariff | Store-and-ack | `Accepted` for an ongoing transaction, otherwise `TxNotFound` |
| SetDERControl | Store-and-ack | Stored by `controlId`, `Accepted` |
| GetDERControl | Store-and-ack | `NotFound`, or `Accepted` followed by one ReportDERControl |
| ClearDERControl | Store-and-ack | `Accepted` / `NotFound` |
| UpdateDynamicSchedule, UsePriorityCharging | Supported | See [Smart charging and V2X](#ocpp-21-smart-charging) |
| GetPeriodicEventStream, AdjustPeriodicEventStream | Not supported | CALLERROR `NotImplemented` (planned for v0.4) |
| RequestBatterySwap, NotifyWebPaymentStarted | Not supported | CALLERROR `NotImplemented` |

Store-and-ack means the request is kept in memory and answered schema-valid; nothing acts on it (no local cost
calculation, no DER behaviour).

The 12 new station to CSMS messages (BatterySwap, ClosePeriodicEventStream, GetCertificateChainStatus, NotifyDERAlarm,
NotifyDERStartStop, NotifyPeriodicEventStream (SEND), NotifyPriorityCharging, NotifySettlement,
OpenPeriodicEventStream, PullDynamicScheduleUpdate, ReportDERControl, VatNumberValidation) are not sent by the station
on its own, except ReportDERControl (the answer to GetDERControl), PullDynamicScheduleUpdate and
NotifyPriorityCharging (see below). The Mini CSMS answers them minimally.

### Smart charging and V2X (2.1-M2) { #ocpp-21-smart-charging }

The basic subset of OCPP 2.1 smart charging (K) and bidirectional power transfer (Q) is simulated. Rules that the
specification leaves to the implementation, and options that are not modelled, are listed here.

| Feature | Behaviour |
|---|---|
| Purposes PriorityCharging, LocalGeneration | Accepted (SmartChargingCtrlr.SupportedAdditionalPurposes). PriorityCharging replaces TxProfile / TxDefaultProfile while priority charging is active for the transaction; a LocalGeneration limit is added on top of the composite limit |
| Kind Dynamic (K28) | One schedule with one period starting at 0, else `Rejected` `InvalidSchedule`. `dynUpdateTime` is set on receipt; with `duration` the profile is valid for `duration` seconds after the last update, then the next lower profile applies |
| operationMode | Checked per purpose (K01.FR.111 - FR.116, reasonCode `InvalidOperationMode`). ChargingOnly charges within `limit`; CentralSetpoint, ExternalSetpoint and CentralFrequency follow `setpoint` (negative = discharge), capped to [`dischargeLimit`, `limit`]; Idle draws 0 W; ExternalLimits, LocalFrequency and LocalLoadBalancing charge within the limits (no frequency or load measurement is simulated). A ChargingStationExternalConstraints setpoint (ExternalSetpoint) takes precedence |
| GetCompositeSchedule | Periods carry `operationMode` (omitted for ChargingOnly), `dischargeLimit` and `setpoint`; Idle periods have no values |
| `limitAtSoC` | Caps limit and setpoint while the EV's SoC is at or above `soc` (the current SoC also for future periods) |
| `maxOfflineDuration` | A profile is not valid after that many seconds offline; on reconnection the ones with `invalidAfterOfflineDuration` that expired are deleted, the others are valid again |
| Operation mode change | TransactionEvent `Updated` with triggerReason `OperationModeChanged` and `transactionInfo.operationMode` |
| Discharge physics | Power is negative while discharging, down to 10 % SoC at most at the EV's charging maximum. Energy.Active.Import.Register and Energy.Active.Export.Register both only grow; once a transaction has discharged, the Export measurands are reported next to the Import ones. The Web UI shows the signed power, the exported energy and the operation mode |
| V2XChargingCtrlr | `Enabled` (true, read-write; false = no discharge and no bidirectional modes), `SupportedEnergyTransferModes`, `SupportedOperationModes` |

| Message | Direction | Behaviour |
|---|---|---|
| UpdateDynamicSchedule | CSMS to station | `Accepted` for a Dynamic profile (given values replace the period's), `Rejected` `InvalidProfile` for another kind, `Rejected` `NotFound` for an unknown id |
| PullDynamicScheduleUpdate | Station to CSMS | Sent at `dynUpdateTime + dynUpdateInterval` and then every interval while online; an `Accepted` update is applied. The Mini CSMS answers with the current values of the Dynamic profiles it sent, else `Rejected` `InvalidProfile` |
| UsePriorityCharging | CSMS to station | `Accepted`, `NoProfile` (no PriorityCharging profile on the EVSE or EVSE 0) or `Rejected` `TxNotFound`; a change sends NotifyPriorityCharging |
| NotifyPriorityCharging | Station to CSMS | After each switch of priority charging (remote or local); not queued while offline |

Not modelled: frequency response, SmartChargingCtrlr.SetpointPriority, `setpointReactive` and the `_L2` / `_L3`
values (accepted, not applied), `evseSleep`, randomized delay and local time (`Rejected`), ISO 15118-20
renegotiation, the 2.1 fields of a RequestStartTransaction profile (handled with the 2.0.1 rules).

## Not supported yet

| Feature | Status |
|---|---|
| OCPP 2.1: periodic event streams, battery swap, web payment | Not supported (CALLERROR `NotImplemented`), see [OCPP 2.1](#ocpp-21) |
| 1.6 Security Whitepaper SignedUpdateFirmware, GetLog, LogStatusNotification, SignedFirmwareStatusNotification | Not supported (CALLERROR `NotImplemented`) |
| ISO 15118 / Plug & Charge | Not supported (paid standard) |
