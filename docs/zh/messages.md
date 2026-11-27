# OCPP 消息

SME OCPPSim 模拟 OCPP 1.6J 充电桩（Charge Point）和 OCPP 2.0.1 充电站（Charging Station）；用 `ocpp_version`
按充电桩或为全部充电桩选择版本（见 [配置](configuration.md)）。收到 CSMS 发来的不支持的动作时，应答 CALLERROR
`NotImplemented` 并记录到帧日志。

## OCPP 1.6J

子协议 `ocpp1.6`。

### 充电桩 → CSMS

| Action | 说明 |
|---|---|
| BootNotification | Accepted / Pending / Rejected。Pending / Rejected 时在 `interval`（为 0 时用 `boot_retry_s`）后重试，期间只应答 CSMS 的请求 |
| Heartbeat | 用 `currentTime` 记录时钟偏差 |
| StatusNotification | connector 0 及各 connector；`errorCode`、`info`、`vendorErrorCode` |
| Authorize | 取决于 `auth_mode`（见 [配置](configuration.md)） |
| StartTransaction | 取得 `transactionId`；`idTagInfo` 不是 Accepted 时停止交易 |
| StopTransaction | `reason`、`transactionData` |
| MeterValues | 周期采样（`MeterValueSampleInterval`）与时钟对齐（`ClockAlignedDataInterval`）；measurand 取自 `MeterValuesSampledData`；带 `location` / `context` |
| DataTransfer | 从 UI 或 HTTP API 手动发送 |

交易相关消息（StartTransaction、StopTransaction、周期 MeterValues）和状态变化引起的 StatusNotification 通过同一个有序队列发送。
因断线而没收到应答的消息会在下次 Boot 被接受后重发，因此 CSMS 可能收到两次（1.6 没有幂等键）。
失败的交易消息按 `TransactionMessageRetryInterval` 间隔最多重试 `TransactionMessageAttempts` 次。

### CSMS → 充电桩

| Action | 说明 |
|---|---|
| RemoteStartTransaction | 可携带 `chargingProfile`（不带 `transactionId` 的 TxProfile）。Boot 被接受之前应答 Rejected |
| RemoteStopTransaction | |
| GetConfiguration | `key` 为空时返回全部键；未知键放入 `unknownKey`。Boot 被接受之前也会应答 |
| ChangeConfiguration | Accepted / Rejected / RebootRequired / NotSupported |
| Reset | Soft / Hard：结束交易、断开、重连并重新 Boot |
| TriggerMessage | BootNotification / Heartbeat / MeterValues / StatusNotification；DiagnosticsStatusNotification 和 FirmwareStatusNotification 应答 `NotImplemented` |
| UnlockConnector | 始终应答 `Unlocked`（其他应答暂不可配置）；该 connector 上进行中的 transaction 以 reason `UnlockCommand` 结束 |
| ChangeAvailability | Accepted / Rejected / Scheduled（交易进行中为 Scheduled） |
| DataTransfer | 默认应答 `UnknownVendorId` |
| SetChargingProfile | ChargePointMaxProfile（仅 connector 0）/ TxDefaultProfile / TxProfile（需要进行中的交易） |
| ClearChargingProfile | 按 `id` / `connectorId` / `chargingProfilePurpose` / `stackLevel` 过滤 |
| GetCompositeSchedule | `chargingRateUnit` 可选；与实际功率限值使用同一个计算函数 |

## OCPP 2.0.1 { #ocpp-201 }

子协议 `ocpp2.0.1`。`connectors: N` 表示有 N 个 EVSE、每个 EVSE 一个 connector 的充电站；对 2.0.1 充电桩，Web UI
和 HTTP API 中的“connector”指 EVSE id。充电站使用的设置是 Device Model 变量（见 [配置](configuration.md#variables)），
而不是 1.6 的配置键。规范未明确的地方，下表备注给出模拟器的解释。

### 充电站 → CSMS

| 消息 | 支持 | 备注 |
|---|---|---|
| Authorize | 支持 | 取决于 `auth_mode`；在充电桩上出示的令牌以 `type` `ISO14443` 发送。模拟器不建模的状态（NoCredit、NotAtThisTime 等）对操作员显示为 Invalid |
| BootNotification | 支持 | Accepted / Pending / Rejected。`reason` 为 `PowerUp`，CSMS Reset 之后为 `RemoteReset`，TriggerMessage 之后为 `Triggered`。Boot 被接受前只发送 BootNotification（Pending 期间 TriggerMessage 要求的消息除外，B02.FR.02）。Pending 期间 RequestStart/StopTransaction `Rejected`（B02.FR.05）；Rejected 期间所有 CSMS 请求应答 CALLERROR `SecurityError`（B03） |
| ClearedChargingLimit | 支持 | 解除外部限值时发送（`chargingLimitSource` `EMS`）；仅在 Boot 被接受时，见 [Smart Charging](smart-charging.md#ocpp-201) |
| DataTransfer | 支持 | 从 UI 或 HTTP API 手动发送 |
| FirmwareStatusNotification | 支持 | 仅在 TriggerMessage 时发送，status `Idle`（不模拟固件更新） |
| Get15118EVCertificate | Not supported | ISO 15118 |
| GetCertificateStatus | Not supported | ISO 15118 |
| Heartbeat | 支持 | 每隔 `OCPPCommCtrlr.HeartbeatInterval`（被接受的 BootNotification 也会设置该值） |
| LogStatusNotification | 支持 | 仅在 TriggerMessage 时发送，status `Idle`（不模拟日志上传） |
| MeterValues | 支持 | 收到 TriggerMessage 时发送（context `Trigger`，测量项取自 `AlignedDataCtrlr.Measurands`）；交易中的计量值随 TransactionEvent 发送 |
| NotifyChargingLimit | 支持 | 设置外部限值时发送（`chargingLimitSource` `EMS`；`SmartChargingCtrlr.NotifyChargingLimitWithSchedules` 为 true 时附带计划）；仅在 Boot 被接受时，见 [Smart Charging](smart-charging.md#ocpp-201) |
| NotifyCustomerInformation | Not supported |  |
| NotifyDisplayMessages | Not supported |  |
| NotifyEVChargingNeeds | Not supported | ISO 15118 |
| NotifyEVChargingSchedule | Not supported | ISO 15118 |
| NotifyEvent | Not supported | 故障只作为被中断交易的 `stoppedReason` 上报 |
| NotifyMonitoringReport | Not supported |  |
| NotifyReport | 支持 | 分段应答 GetBaseReport（每段 `DeviceDataCtrlr.ItemsPerMessage[GetReport]` 条，`seqNo` 从 0 开始，除最后一段外 `tbc`） |
| PublishFirmwareStatusNotification | Not supported |  |
| ReportChargingProfiles | 支持 | 应答 GetChargingProfiles：每个 EVSE 与 `chargingLimitSource` 一段，除最后一段外 `tbc` |
| ReservationStatusUpdate | Not supported |  |
| SecurityEventNotification | Not supported |  |
| SignCertificate | Not supported |  |
| StatusNotification | 支持 | 每个 EVSE（connector 1）一条，EVSE 0 不发送。`connectorStatus` 为 Available / Occupied / Unavailable / Faulted；更细的状态（充电、暂停）由 TransactionEvent 的 `chargingState` 表示 |
| TransactionEvent | 支持 | `Started` → `Updated` → `Ended`。`transactionId`（UUID）由充电站生成；`seqNo` 每笔交易从 0 计数；每隔 `SampledDataCtrlr.TxUpdatedInterval` 发送周期性 `Updated`；断线期间产生的事件带 `offline: true` |

TransactionEvent 和状态变化引起的 StatusNotification 与 1.6 一样经同一个有序队列发送。失败的 TransactionEvent
最多重试 `OCPPCommCtrlr.MessageAttempts[TransactionEvent]` 次，间隔为 `MessageAttemptInterval[TransactionEvent]` × 失败次数；
因断线未收到应答的消息会在下次 Boot 被接受后重发（CSMS 可按 `transactionId` + `seqNo` 去重）。

按模拟器的解释，交易在插枪**且** ID 令牌授权通过时开始（`triggerReason` 为后发生的 `CablePluggedIn` 或
`Authorized`），在以下情况结束：再次出示同一令牌或操作员停止（`StopAuthorized` / `Local`）、拔枪
（`EVCommunicationLost`）、操作员触发故障（`AbnormalCondition`，故障码即 `stoppedReason`），或 CSMS 在
TransactionEvent 应答中拒绝令牌（`TxCtrlr.StopTxOnInvalidId` 为 true 时为 `Deauthorized`）。`StopTxOnInvalidId` 为 false 时交易继续，但立即停止供电（`Updated` `ChargingStateChanged`、`SuspendedEVSE`）。

### CSMS → 充电站

| 消息 | 支持 | 备注 |
|---|---|---|
| CancelReservation | Not supported |  |
| CertificateSigned | Not supported |  |
| ChangeAvailability | 支持 | 无 `evse` 或 EVSE 0 设置充电站自身的可用性，与 EVSE 的可用性分开保存（EVSE `Operative` 不会解除充电站 `Inoperative`）。交易进行中（该 EVSE，或对充电站而言任一 EVSE）改为 Inoperative 应答 `Scheduled`，交易结束后生效；充电站 Inoperative 时空闲 EVSE 立即变为 Unavailable |
| ClearCache | Not supported |  |
| ClearChargingProfile | 支持 | `chargingProfileId` 优先于其他条件，条件之间为 AND；外部约束不会被 CSMS 清除 |
| ClearDisplayMessage | Not supported |  |
| ClearVariableMonitoring | Not supported |  |
| CostUpdated | Not supported |  |
| CustomerInformation | Not supported |  |
| DataTransfer | 支持 | 默认应答 `UnknownVendorId` |
| DeleteCertificate | Not supported |  |
| GetBaseReport | 支持 | ConfigurationInventory（所有非 ReadOnly 变量）、FullInventory、SummaryInventory；应答之后发送 NotifyReport 各段 |
| GetChargingProfiles | 支持 | 有匹配的 profile 时 `Accepted`，否则 `NoProfiles`；应答之后发送 ReportChargingProfiles 各段 |
| GetCompositeSchedule | 支持 | 与实际功率限值使用同一计算函数；各时段的值以硬件最大值封顶，该最大值也表示“无限制”。未知 EVSE 或 duration < 1 → `Rejected`；`chargingRateUnit` 不在 `SmartChargingCtrlr.RateUnit` 中 → `Rejected`（`UnsupportedRateUnit`） |
| GetDisplayMessages | Not supported |  |
| GetInstalledCertificateIds | Not supported |  |
| GetLocalListVersion | Not supported |  |
| GetLog | Not supported |  |
| GetMonitoringReport | Not supported |  |
| GetReport | Not supported | 请使用 GetBaseReport |
| GetTransactionStatus | Not supported |  |
| GetVariables | 支持 | 属性类型 Actual / Target / MinSet / MaxSet；`UnknownComponent` / `UnknownVariable` / `NotSupportedAttributeType`；WriteOnly 变量 `Rejected`。条目数超过 `DeviceDataCtrlr.ItemsPerMessage[GetVariables]` → CALLERROR `OccurrenceConstraintViolation` |
| InstallCertificate | Not supported |  |
| PublishFirmware | Not supported |  |
| RequestStartTransaction | 支持 | `remoteStartId` 与可选的 `chargingProfile`（不带 `transactionId` 的 TxProfile，安装后绑定到新交易）。无 `evseId` 时使用第一个已插枪且可启动的 EVSE。`AuthCtrlr.AuthorizeRemoteStart` 为 true 时，令牌与现场出示一样进行授权。type 为 `Central` / `NoAuthorization` 的令牌不授权。由它启动的交易 `triggerReason` 为 `RemoteStart`（之后才插枪也一样）。超过 `TxCtrlr.EVConnectionTimeOut` 仍在等待插枪的令牌被丢弃 |
| RequestStopTransaction | 支持 | 本充电站进行中的交易应答 `Accepted`（以 `RemoteStop` 结束），否则 `Rejected` |
| ReserveNow | Not supported |  |
| Reset | 支持 | `Immediate` / `OnIdle`（交易进行中为 `Scheduled`）；之后的 BootNotification 的 reason 为 `RemoteReset`。EVSE 级复位 `Rejected` |
| SendLocalList | Not supported |  |
| SetChargingProfile | 支持 | ChargingStationMaxProfile（仅 EVSE 0）、TxDefaultProfile、TxProfile；来自 CSMS 的 ChargingStationExternalConstraints `Rejected`（`UnsupportedPurpose`）。只能有一个 `chargingSchedule`。见 [Smart Charging](smart-charging.md#ocpp-201) |
| SetDisplayMessage | Not supported |  |
| SetMonitoringBase | Not supported |  |
| SetMonitoringLevel | Not supported |  |
| SetNetworkProfile | Not supported |  |
| SetVariableMonitoring | Not supported |  |
| SetVariables | 支持 | 检查取值（数据类型、上下限、取值列表）；ReadOnly 变量 `Rejected`；标记为需重启的变量应答 `RebootRequired`。修改立即生效，不会写回 YAML 文件 |
| TriggerMessage | 支持 | BootNotification（仅在 Boot 未被接受时）、Heartbeat、StatusNotification、MeterValues、TransactionEvent（测量项取自 `TxUpdatedMeasurands`）、LogStatusNotification / FirmwareStatusNotification（status `Idle`）；其他请求的消息应答 `NotImplemented`。除 BootNotification 外，收到 BootNotification 应答前 `Rejected`，Accepted 与 Pending 期间发送；未知 EVSE `Rejected` |
| UnlockConnector | 支持 | `Unlocked`；EVSE 交易进行中时 `OngoingAuthorizedTransaction`（不会停止交易）；未知 EVSE 或 connector 不为 1 时 `UnknownConnector` |
| UnpublishFirmware | Not supported |  |
| UpdateFirmware | Not supported |  |

## 尚不支持

| 功能 | 状态 |
|---|---|
| OCPP 2.1 | 计划在 v0.3 支持（UI 中灰色显示） |
| Security Profile 3（TLS 客户端证书）、证书管理、Security Whitepaper 消息（SecurityEventNotification 等） | 计划中 |
| 本地授权列表（SendLocalList / GetLocalListVersion）、授权缓存 | 计划中（目前为 `id_tags` 列表加 CSMS 查询） |
| 预约（ReserveNow / CancelReservation） | 计划中 |
| 固件更新、诊断 / 日志上传及其状态通知 | 计划中（目前应答 CALLERROR `NotImplemented`） |
| ISO 15118 / Plug & Charge | 不支持（付费标准） |
