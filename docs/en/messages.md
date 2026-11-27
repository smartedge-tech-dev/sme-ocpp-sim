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
| StopTransaction | `reason`, `transactionData` |
| MeterValues | Sampled (`MeterValueSampleInterval`) and clock-aligned (`ClockAlignedDataInterval`); measurands from `MeterValuesSampledData`; with `location` / `context` |
| DataTransfer | Sent manually from the UI or the HTTP API |

Transaction-related messages (StartTransaction, StopTransaction, periodic MeterValues) and state-change
StatusNotifications go through one ordered queue. A message whose answer was lost in a disconnect is resent
after the next accepted boot, so the CSMS may receive it twice (1.6 has no idempotency key). Failed transaction
messages are retried `TransactionMessageAttempts` times at `TransactionMessageRetryInterval`.

### CSMS to charge point

| Action | Notes |
|---|---|
| RemoteStartTransaction | May carry a `chargingProfile` (TxProfile without `transactionId`). Rejected before the boot is accepted |
| RemoteStopTransaction | |
| GetConfiguration | An empty `key` list returns all keys; unknown keys go to `unknownKey`. Answered even before the boot is accepted |
| ChangeConfiguration | Accepted / Rejected / RebootRequired / NotSupported |
| Reset | Soft / Hard: ends transactions, disconnects, reconnects and boots again |
| TriggerMessage | BootNotification / Heartbeat / MeterValues / StatusNotification; DiagnosticsStatusNotification and FirmwareStatusNotification answer `NotImplemented` |
| UnlockConnector | Always answers `Unlocked` (other answers are not configurable yet); a running transaction on the connector ends with reason `UnlockCommand` |
| ChangeAvailability | Accepted / Rejected / Scheduled (Scheduled while a transaction is running) |
| DataTransfer | Default answer `UnknownVendorId` |
| SetChargingProfile | ChargePointMaxProfile (connector 0 only) / TxDefaultProfile / TxProfile (needs a running transaction) |
| ClearChargingProfile | Filters `id` / `connectorId` / `chargingProfilePurpose` / `stackLevel` |
| GetCompositeSchedule | Optional `chargingRateUnit`; same evaluator as the live power limit |

## OCPP 2.0.1 { #ocpp-201 }

Subprotocol `ocpp2.0.1`. `connectors: N` is a station with N EVSEs of one connector each; wherever the web UI or
the HTTP API says "connector" for a 2.0.1 charger, it means the EVSE id. The settings the station acts on are
Device Model variables (see [Configuration](configuration.md#variables)), not 1.6 configuration keys. Where the
specification leaves a point open, the notes below give the simulator's interpretation.

### Charging station to CSMS

| Message | Support | Notes |
|---|---|---|
| Authorize | Supported | Depends on `auth_mode`; a token presented at the charger is sent with `type` `ISO14443`. Status values the simulator does not model (NoCredit, NotAtThisTime, ...) are shown to the operator as Invalid |
| BootNotification | Supported | Accepted / Pending / Rejected. `reason` is `PowerUp`, `RemoteReset` after a CSMS Reset, `Triggered` after a TriggerMessage. Until the boot is accepted only BootNotification is sent (CSMS requests are still answered) |
| ClearedChargingLimit | Supported | When the external limit is removed (`chargingLimitSource` `EMS`); only while the boot is accepted, see [Smart Charging](smart-charging.md#ocpp-201) |
| DataTransfer | Supported | Sent manually from the UI or the HTTP API |
| FirmwareStatusNotification | Not supported |  |
| Get15118EVCertificate | Not supported | ISO 15118 |
| GetCertificateStatus | Not supported | ISO 15118 |
| Heartbeat | Supported | Every `OCPPCommCtrlr.HeartbeatInterval` (also set by an accepted BootNotification) |
| LogStatusNotification | Not supported |  |
| MeterValues | Supported | Sent on TriggerMessage (context `Trigger`); transaction readings travel in TransactionEvent |
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
| ReservationStatusUpdate | Not supported |  |
| SecurityEventNotification | Not supported |  |
| SignCertificate | Not supported |  |
| StatusNotification | Supported | One per EVSE (connector 1), none for EVSE 0. `connectorStatus` Available / Occupied / Unavailable / Faulted; the finer state (charging, suspended) is the `chargingState` of TransactionEvent |
| TransactionEvent | Supported | `Started` → `Updated` → `Ended`. The station generates the `transactionId` (a UUID); `seqNo` counts from 0 per transaction; periodic `Updated` every `SampledDataCtrlr.TxUpdatedInterval`; `offline: true` for events raised while disconnected |

TransactionEvents and state-change StatusNotifications go through one ordered queue, as in 1.6. A failed
TransactionEvent is retried `OCPPCommCtrlr.MessageAttempts[TransactionEvent]` times,
`MessageAttemptInterval[TransactionEvent]` × failures apart; one whose answer was lost in a disconnect is sent
again after the next accepted boot (the CSMS can de-duplicate by `transactionId` + `seqNo`).

In the simulator's interpretation a transaction starts when the cable is plugged in **and** the id token is
authorized (`triggerReason` `CablePluggedIn` or `Authorized`, whichever comes last) and ends on the same token
presented again or an operator stop (`StopAuthorized` / `Local`), an unplugged cable (`EVCommunicationLost`), an
operator fault (`AbnormalCondition`; the fault code is the `stoppedReason`) or a token the CSMS refuses in a
TransactionEvent answer (`Deauthorized`, while `TxCtrlr.StopTxOnInvalidId` is true).

### CSMS to charging station

| Message | Support | Notes |
|---|---|---|
| CancelReservation | Not supported |  |
| CertificateSigned | Not supported |  |
| ChangeAvailability | Supported | No `evse` or EVSE 0 = every EVSE. Going Inoperative while an addressed EVSE has a transaction is `Scheduled` until it ends |
| ClearCache | Not supported |  |
| ClearChargingProfile | Supported | `chargingProfileId` wins over the criteria, the criteria are ANDed; external constraints are never cleared by the CSMS |
| ClearDisplayMessage | Not supported |  |
| ClearVariableMonitoring | Not supported |  |
| CostUpdated | Not supported |  |
| CustomerInformation | Not supported |  |
| DataTransfer | Supported | Default answer `UnknownVendorId` |
| DeleteCertificate | Not supported |  |
| GetBaseReport | Supported | ConfigurationInventory (every variable that is not ReadOnly), FullInventory, SummaryInventory; the NotifyReport parts follow the answer |
| GetChargingProfiles | Supported | `Accepted` when a profile matches, else `NoProfiles`; the ReportChargingProfiles parts follow the answer |
| GetCompositeSchedule | Supported | Same evaluator as the live power limit; periods are capped at the hardware maximum, which also stands for "no limit". Unknown EVSE or duration < 1 → `Rejected` |
| GetDisplayMessages | Not supported |  |
| GetInstalledCertificateIds | Not supported |  |
| GetLocalListVersion | Not supported |  |
| GetLog | Not supported |  |
| GetMonitoringReport | Not supported |  |
| GetReport | Not supported | Use GetBaseReport |
| GetTransactionStatus | Not supported |  |
| GetVariables | Supported | Attribute types Actual / Target / MinSet / MaxSet; `UnknownComponent` / `UnknownVariable` / `NotSupportedAttributeType`; a WriteOnly variable is `Rejected`. More items than `DeviceDataCtrlr.ItemsPerMessage[GetVariables]` → CALLERROR `OccurrenceConstraintViolation` |
| InstallCertificate | Not supported |  |
| PublishFirmware | Not supported |  |
| RequestStartTransaction | Supported | `remoteStartId` and an optional `chargingProfile` (a TxProfile without `transactionId`, installed bound to the new transaction). Without `evseId` the first startable EVSE with a plugged cable is used. With `AuthCtrlr.AuthorizeRemoteStart` true the token is authorized like a presented one |
| RequestStopTransaction | Supported | `Accepted` for a running transaction of this station (it ends with `RemoteStop`), else `Rejected` |
| ReserveNow | Not supported |  |
| Reset | Supported | `Immediate` / `OnIdle` (`Scheduled` while a transaction runs); the next BootNotification has reason `RemoteReset`. An EVSE reset is `Rejected` |
| SendLocalList | Not supported |  |
| SetChargingProfile | Supported | ChargingStationMaxProfile (EVSE 0 only), TxDefaultProfile, TxProfile; ChargingStationExternalConstraints is `Rejected` from the CSMS. See [Smart Charging](smart-charging.md#ocpp-201) |
| SetDisplayMessage | Not supported |  |
| SetMonitoringBase | Not supported |  |
| SetMonitoringLevel | Not supported |  |
| SetNetworkProfile | Not supported |  |
| SetVariableMonitoring | Not supported |  |
| SetVariables | Supported | Values are checked (data type, limits, values list); a ReadOnly variable is `Rejected`; `RebootRequired` for variables marked so. Changes apply at once and are not written back to the YAML file |
| TriggerMessage | Supported | BootNotification (only while the boot is not accepted), Heartbeat, StatusNotification, MeterValues, TransactionEvent; other requested messages answer `NotImplemented`. All but BootNotification are `Rejected` until the boot is accepted; an unknown EVSE is `Rejected` |
| UnlockConnector | Supported | `Unlocked`; `OngoingAuthorizedTransaction` while the EVSE has a transaction (it is not stopped); `UnknownConnector` for an unknown EVSE or a connector other than 1 |
| UnpublishFirmware | Not supported |  |
| UpdateFirmware | Not supported |  |

## Not supported yet

| Feature | Status |
|---|---|
| OCPP 2.1 | Planned for v0.3 (greyed out in the UI) |
| Security profile 3 (TLS client certificates), certificate management, Security Whitepaper messages (SecurityEventNotification, ...) | Planned |
| Local authorization list (SendLocalList / GetLocalListVersion), authorization cache | Planned (today: the `id_tags` list plus CSMS lookups) |
| Reservations (ReserveNow / CancelReservation) | Planned |
| Firmware update, diagnostics / log upload and their status notifications | Planned (CALLERROR `NotImplemented` today) |
| ISO 15118 / Plug & Charge | Not supported (paid standard) |
