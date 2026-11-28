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
| StopTransaction | `reason`、`transactionData`：从交易开始起每隔 `MeterValueSampleInterval` 采样的 `StopTxnSampledData` measurand（`Sample.Periodic`）及结束值（`Transaction.End`），每个 `ClockAlignedDataInterval` 时钟边界的 `StopTxnAlignedData` measurand（`Sample.Clock`）；两个 key 都为空时不带。电表不报告的 measurand（Energy.Active.Import.Register、Power.Active.Import、Current.Import、Voltage、SoC 以外）或超过其 `*MaxLength` key 的列表，ChangeConfiguration 回 `Rejected` |
| MeterValues | 交易中每隔 `MeterValueSampleInterval` 发送采样值；measurand 取自 `MeterValuesSampledData`，带 `location` / `context`。时钟对齐的 MeterValues 消息（`ClockAlignedDataInterval` / `MeterValuesAlignedData`）尚未发送（`transactionData` 中的 `StopTxnAlignedData` 读数会发送） |
| DataTransfer | 从 UI 或 HTTP API 手动发送 |
| FirmwareStatusNotification | Downloading → Downloaded → Installing →（重启、BootNotification）→ Installed；失败注入时为 DownloadFailed / InstallationFailed |
| DiagnosticsStatusNotification | Uploading → Uploaded / UploadFailed |
| SecurityEventNotification | Security Whitepaper；与 2.0.1 相同的事件，使用 1.6 名称 InvalidCentralSystemCertificate / FailedToAuthenticateAtCentralSystem / InvalidChargePointCertificate（critical 事件离线时也排队） |
| SignCertificate | Security Whitepaper；收到 ExtendedTriggerMessage `SignChargePointCertificate` 后生成新密钥和 CSR（CN = `serial` 或充电桩 ID，O = `CpoName`，默认 `vendor`） |

交易相关消息（StartTransaction、StopTransaction、周期 MeterValues）和状态变化引起的 StatusNotification 通过同一个有序队列发送。
因断线而没收到应答的消息会在下次 Boot 被接受后重发，因此 CSMS 可能收到两次（1.6 没有幂等键）。
失败的交易消息按 `TransactionMessageRetryInterval` 间隔最多重试 `TransactionMessageAttempts` 次。

### CSMS → 充电桩

| Action | 说明 |
|---|---|
| RemoteStartTransaction | 可携带 `chargingProfile`（不带 `transactionId` 的 TxProfile）。Boot 被接受之前应答 Rejected |
| RemoteStopTransaction | |
| GetConfiguration | `key` 为空时返回全部键；未知键放入 `unknownKey`。Boot 被接受之前也会应答。只写的 `AuthorizationKey` 不返回值 |
| ChangeConfiguration | Accepted / Rejected / RebootRequired / NotSupported。`AuthorizationKey`（Security Whitepaper A01）：16〜40 个字符，之后用新的 Basic 认证密码重连；Profile 3 时为 `Rejected` |
| Reset | Soft / Hard：结束交易、断开、重连并重新 Boot |
| TriggerMessage | BootNotification / Heartbeat / MeterValues / StatusNotification / FirmwareStatusNotification / DiagnosticsStatusNotification（更新 / 上传进行中时为当前步骤，否则 `Idle`） |
| UnlockConnector | 始终应答 `Unlocked`（其他应答暂不可配置）；该 connector 上进行中的 transaction 以 reason `UnlockCommand` 结束 |
| ChangeAvailability | Accepted / Rejected / Scheduled（交易进行中为 Scheduled） |
| DataTransfer | 默认应答 `UnknownVendorId` |
| SetChargingProfile | ChargePointMaxProfile（仅 connector 0）/ TxDefaultProfile / TxProfile（需要进行中的交易） |
| ClearChargingProfile | 按 `id` / `connectorId` / `chargingProfilePurpose` / `stackLevel` 过滤 |
| GetCompositeSchedule | `chargingRateUnit` 可选；与实际功率限值使用同一个计算函数 |
| SendLocalList | Full / Differential。`listVersion` 必须大于 0（否则 CALLERROR `PropertyConstraintViolation`）；Differential 必须提高版本号（`VersionMismatch`）。条目数超过 `SendLocalListMaxLength`、标签重复、Full 的条目没有 `idTagInfo`、或标签数超过 `LocalAuthListMaxLength` → `Failed`（列表不变）；`LocalAuthListEnabled` 为 false 时应答 `NotSupported` |
| GetLocalListVersion | 保存的版本号；收到第一份列表之前为 0 |
| ClearCache | 清空授权缓存；`AuthorizationCacheEnabled` 为 false 时应答 `Rejected` |
| ReserveNow | 按 connector 状态应答 Faulted / Unavailable / Occupied，否则 Accepted；相同 `reservationId` 替换原预约；connector 0 的预约会保留一个空闲 connector（`ReserveConnectorZeroSupported` 为 false 时 `Rejected`）。被预约的 connector 报告 `Reserved`，只允许预约的 `idTag` 或以其为父标签的标签开始；`reservationId` 随 StartTransaction 发送。到期、取消、或 connector 变为 Faulted / Unavailable 时结束 |
| CancelReservation | 已知预约应答 `Accepted`，否则 `Rejected` |
| UpdateFirmware | 仅模拟（不实际下载）：等到 `retrieveDate`，下载失败时按 `retries` / `retryInterval` 重试；没有交易的连接器在交易全部结束前为 `Unavailable`，之后安装并重启（同 Hard Reset）。各步骤时长与失败注入：每台充电桩的 `maintenance`（`download_s`、`install_s`、`upload_s`、`retry_interval_s`、`fail_download`、`fail_install`、`fail_upload`）。尚未安装的更新被新请求替换；安装中收到的请求被忽略 |
| GetDiagnostics | 应答 `fileName` `<id>-diagnostics-<时间>.log` 并模拟上传（不实际上传）；进行中的上传被新请求替换 |
| ExtendedTriggerMessage | Security Whitepaper：BootNotification / Heartbeat / MeterValues / StatusNotification 与 TriggerMessage 相同，另加 `SignChargePointCertificate`；`FirmwareStatusNotification`（指 SignedFirmwareStatusNotification）和 `LogStatusNotification` 为 `NotImplemented`；`connectorId` 0 为 CALLERROR `PropertyConstraintViolation`（Whitepaper 要求 `connectorId` > 0） |
| CertificateSigned | Security Whitepaper；与 2.0.1 相同：与未完成的 CSR 一致时 `Accepted`，然后（Profile 3）用新证书重连 |
| InstallCertificate / DeleteCertificate / GetInstalledCertificateIds | Security Whitepaper；CentralSystemRootCertificate / ManufacturerRootCertificate，与 2.0.1 相同 |

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
| FirmwareStatusNotification | 支持 | UpdateFirmware 的进度（带 `requestId`）；TriggerMessage 时发送最后的 status，Installed 之后或从未发送时为 `Idle` |
| Get15118EVCertificate | Not supported | ISO 15118 |
| GetCertificateStatus | Not supported | ISO 15118 |
| Heartbeat | 支持 | 每隔 `OCPPCommCtrlr.HeartbeatInterval`（被接受的 BootNotification 也会设置该值） |
| LogStatusNotification | 支持 | GetLog 的进度（Uploading → Uploaded / UploadFailure；被新 GetLog 取消的上传发送 AcceptedCanceled）；TriggerMessage 时上传中为 `Uploading`，否则 `Idle` |
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
| ReservationStatusUpdate | 支持 | 到达 `expiryDateTime` 时 `Expired`；预约的 EVSE（无 `evseId` 时为整个充电站）变为 Faulted / Unavailable 时 `Removed` |
| SecurityEventNotification | 支持 | StartupOfTheDevice、ResetOrReboot、FirmwareUpdated、InvalidCsmsCertificate、InvalidTLSVersion、FailedToAuthenticateAtCsms、InvalidChargingStationCertificate、DiscardedRenewedClientCertificate、ReconfigurationOfSecurityParameters。critical 事件离线时也排队，在下一次 Boot 被接受后发送；其它事件只在连接期间发生时发送 |
| SignCertificate | 支持 | 收到 TriggerMessage `SignChargingStationCertificate` 后生成新的 EC P-256 密钥和 CSR（CN = `serial`，未设置时为充电桩 ID；O = `SecurityCtrlr.OrganizationName`），`certificateType` 为 `ChargingStationCertificate`。没有应答的 CSR 不重发（未模拟 CertSigningWaitMinimum） |
| StatusNotification | 支持 | 每个 EVSE（connector 1）一条，EVSE 0 不发送。`connectorStatus` 为 Available / Occupied / Reserved / Unavailable / Faulted；更细的状态（充电、暂停）由 TransactionEvent 的 `chargingState` 表示 |
| TransactionEvent | 支持 | `Started` → `Updated` → `Ended`。`transactionId`（UUID）由充电站生成；`seqNo` 每笔交易从 0 计数；每隔 `SampledDataCtrlr.TxUpdatedInterval` 发送周期性 `Updated`；断线期间产生的事件带 `offline: true`。`Started` 带 `SampledDataCtrlr.TxStartedMeasurands`；`Ended` 带 `SampledDataCtrlr` 与 `AlignedDataCtrlr` `TxEndedMeasurands` 并集的开始值（`Transaction.Begin`）和结束值（`Transaction.End`）、从开始起每隔 `SampledDataCtrlr.TxEndedInterval` 的采样值（`Sample.Periodic`；0 = 只有开始和结束）以及每个 `AlignedDataCtrlr.TxEndedInterval` 时钟边界的值（`Sample.Clock`；0 = 无）。与 1.6 相同，电表不报告的 measurand 不被接受：`*Measurands` 变量的 `valuesList` 只有所报告的 5 个 measurand，SetVariables 回 `Rejected` |

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
| CancelReservation | 支持 | 已知预约应答 `Accepted`（不发送 ReservationStatusUpdate），否则 `Rejected` |
| CertificateSigned | 支持 | 证书与未完成的 CSR 密钥一致且当前有效时 `Accepted`（不检查 CA 层级，由 CSMS 在下一次 TLS 握手中检查），否则 `Rejected` 并产生 InvalidChargingStationCertificate。Profile 3 下用新证书重连；该连接失败时回退到旧证书（DiscardedRenewedClientCertificate）。`V2GCertificate` 为 `Rejected` |
| ChangeAvailability | 支持 | 无 `evse` 或 EVSE 0 设置充电站自身的可用性，与 EVSE 的可用性分开保存（EVSE `Operative` 不会解除充电站 `Inoperative`）。交易进行中（该 EVSE，或对充电站而言任一 EVSE）改为 Inoperative 应答 `Scheduled`，交易结束后生效；充电站 Inoperative 时空闲 EVSE 立即变为 Unavailable |
| ClearCache | 支持 | 清空授权缓存；`AuthCacheCtrlr.Enabled` 为 false 时应答 `Rejected` |
| ClearChargingProfile | 支持 | `chargingProfileId` 优先于其他条件，条件之间为 AND；外部约束不会被 CSMS 清除 |
| ClearDisplayMessage | Not supported |  |
| ClearVariableMonitoring | Not supported |  |
| CostUpdated | Not supported |  |
| CustomerInformation | Not supported |  |
| DataTransfer | 支持 | 默认应答 `UnknownVendorId` |
| DeleteCertificate | 支持 | 按哈希数据（SHA256 / SHA384 / SHA512）`Accepted` / `NotFound`；客户端证书不能删除（`Failed`） |
| GetBaseReport | 支持 | ConfigurationInventory（所有非 ReadOnly 变量）、FullInventory、SummaryInventory；应答之后发送 NotifyReport 各段 |
| GetChargingProfiles | 支持 | 有匹配的 profile 时 `Accepted`，否则 `NoProfiles`；应答之后发送 ReportChargingProfiles 各段 |
| GetCompositeSchedule | 支持 | 与实际功率限值使用同一计算函数；各时段的值以硬件最大值封顶，该最大值也表示“无限制”。未知 EVSE 或 duration < 1 → `Rejected`；`chargingRateUnit` 不在 `SmartChargingCtrlr.RateUnit` 中 → `Rejected`（`UnsupportedRateUnit`） |
| GetDisplayMessages | Not supported |  |
| GetInstalledCertificateIds | 支持 | 已安装 CA 证书的 SHA256 哈希数据；无匹配时 `NotFound`（V2GCertificateChain 永远无匹配） |
| GetLocalListVersion | 支持 | 保存的版本号；收到第一份列表之前、或 `LocalAuthListCtrlr.Enabled` 为 false 时为 0 |
| GetLog | 支持 | `Accepted` 并返回 `filename` `<id>_<logType>_<时间>.log`；取消进行中的上传时为 `AcceptedCanceled`；上传为模拟（`maintenance` 设置） |
| GetMonitoringReport | Not supported |  |
| GetReport | Not supported | 请使用 GetBaseReport |
| GetTransactionStatus | Not supported |  |
| GetVariables | 支持 | 属性类型 Actual / Target / MinSet / MaxSet；`UnknownComponent` / `UnknownVariable` / `NotSupportedAttributeType`；WriteOnly 变量 `Rejected`。条目数超过 `DeviceDataCtrlr.ItemsPerMessage[GetVariables]` → CALLERROR `OccurrenceConstraintViolation` |
| InstallCertificate | 支持 | 仅接受一张当前有效的 PEM 证书；其它内容或已装满 20 张 CA 证书时 `Rejected`。安装的 CSMS 根证书在下一次 TLS 连接中被信任。只保存在内存中 |
| PublishFirmware | Not supported |  |
| RequestStartTransaction | 支持 | `remoteStartId` 与可选的 `chargingProfile`（不带 `transactionId` 的 TxProfile，安装后绑定到新交易）。无 `evseId` 时使用第一个已插枪且可启动的 EVSE。`AuthCtrlr.AuthorizeRemoteStart` 为 true 时，令牌与现场出示一样进行授权。type 为 `Central` / `NoAuthorization` 的令牌不授权。由它启动的交易 `triggerReason` 为 `RemoteStart`（之后才插枪也一样）。超过 `TxCtrlr.EVConnectionTimeOut` 仍在等待插枪的令牌被丢弃 |
| RequestStopTransaction | 支持 | 本充电站进行中的交易应答 `Accepted`（以 `RemoteStop` 结束），否则 `Rejected` |
| ReserveNow | 支持 | 按 EVSE 状态应答 Faulted / Unavailable / Occupied，否则 Accepted；相同 `id` 替换原预约；无 `evseId` 的预约会保留一个空闲 EVSE（`ReservationCtrlr.NonEvseSpecific` 为 false 时 `Rejected`）；不模拟 `connectorType`；`ReservationCtrlr.Enabled` 为 false 时 `Rejected`。被预约的 EVSE 报告 `Reserved`，只允许预约的 `idToken` 或其 `groupIdToken` 的令牌开始；`reservationId` 随 `Started` 事件发送 |
| Reset | 支持 | `Immediate` / `OnIdle`（交易进行中为 `Scheduled`）；之后的 BootNotification 的 reason 为 `RemoteReset`。EVSE 级复位 `Rejected` |
| SendLocalList | 支持 | Full / Differential。`versionNumber` 必须大于 0（否则 CALLERROR `PropertyConstraintViolation`）；Differential 必须提高版本号（`VersionMismatch`）。条目数超过 `LocalAuthListCtrlr.ItemsPerMessage`、令牌重复、Full 的条目没有 `idTokenInfo` → `Failed`；`LocalAuthListCtrlr.Enabled` 为 false 时也应答 `Failed` |
| SetChargingProfile | 支持 | ChargingStationMaxProfile（仅 EVSE 0）、TxDefaultProfile、TxProfile；来自 CSMS 的 ChargingStationExternalConstraints `Rejected`（`UnsupportedPurpose`）。只能有一个 `chargingSchedule`。见 [Smart Charging](smart-charging.md#ocpp-201) |
| SetDisplayMessage | Not supported |  |
| SetMonitoringBase | Not supported |  |
| SetMonitoringLevel | Not supported |  |
| SetNetworkProfile | Not supported |  |
| SetVariableMonitoring | Not supported |  |
| SetVariables | 支持 | 检查取值（数据类型、上下限、取值列表）；ReadOnly 变量 `Rejected`；标记为需重启的变量应答 `RebootRequired`。修改立即生效，不会写回 YAML 文件 |
| TriggerMessage | 支持 | BootNotification（仅在 Boot 未被接受时）、Heartbeat、StatusNotification、MeterValues、TransactionEvent（测量项取自 `TxUpdatedMeasurands`）、LogStatusNotification / FirmwareStatusNotification（当前 status）；其他请求的消息应答 `NotImplemented`。除 BootNotification 外，收到 BootNotification 应答前 `Rejected`，Accepted 与 Pending 期间发送；未知 EVSE `Rejected` |
| UnlockConnector | 支持 | `Unlocked`；EVSE 交易进行中时 `OngoingAuthorizedTransaction`（不会停止交易）；未知 EVSE 或 connector 不为 1 时 `UnknownConnector` |
| UnpublishFirmware | Not supported |  |
| UpdateFirmware | 支持 | 仅模拟（不下载也不校验）：DownloadScheduled（`retrieveDateTime` 在未来）→ Downloading → Downloaded → SignatureVerified（带 `signature` 时）→ InstallScheduled（`installDateTime` 在未来）→ 没有交易的 EVSE 在交易全部结束前为 `Unavailable` → Installing → InstallRebooting → 重启（BootNotification reason `FirmwareUpdate`）→ Installed。下载失败时按 `retries` / `retryInterval` 重试；取消尚未安装的更新时为 `AcceptedCanceled`，安装中为 `Rejected`。时长与失败注入：每台充电桩的 `maintenance` |

## OCPP 2.1 { #ocpp-21 }

OCPP 2.1 通过 `ocpp_version: "2.1"`（子协议 `ocpp2.1`）使用。2.1 充电站的行为与 2.0.1 相同（见上方消息表），
并增加以下内容。

- **帧格式。** OCPP-J 2.1 新增 SEND（`[6, ...]`，单向，不应答）和 CALLRESULTERROR（`[5, ...]`，对端认为
  CALLRESULT 无效），两者均可收发。2.1 下 MessageTypeId 未知的帧会被忽略（不回复 CALLERROR）。
- **放宽的字段。** 生成的 2.1 模型增加了可选字段并放宽了部分 2.0.1 字段，2.0.1 的负载仍然有效。模型会强制校验结构、必填字段、枚举以及字符串/数组的长度限制，但不校验数值范围和正则（与 2.0.1 同样宽松）；出站帧在测试中会按官方 2.1 schema 校验。
- **Reset。** `ImmediateAndResume` 会被接受并按 `Immediate` 处理，重启后恢复交易的过程不做模拟。
- **TriggerMessage。** `CustomTrigger` 返回 `NotImplemented`。

CSMS 到充电站的新消息：

| 消息 | 状态 | 行为 |
|---|---|---|
| AFRRSignal | 存储并应答 | 存储（保留最后一次信号），`Accepted` |
| NotifyAllowedEnergyTransfer | 存储并应答 | 按交易存储，`Accepted`；交易未知则 `Rejected` |
| SetDefaultTariff | 存储并应答 | 按 EVSE 存储（0 表示全部 EVSE）：`Accepted`、`DuplicateTariffId`，EVSE 未知则 `Rejected` |
| GetTariffs | 存储并应答 | 读回资费分配，没有则 `NoTariff` |
| ClearTariffs | 存储并应答 | 每个资费一个结果：`Accepted` / `NoTariff` |
| ChangeTransactionTariff | 存储并应答 | 进行中的交易为 `Accepted`，否则 `TxNotFound` |
| SetDERControl | 存储并应答 | 按 `controlId` 存储，`Accepted` |
| GetDERControl | 存储并应答 | `NotFound`，或 `Accepted` 后跟一条 ReportDERControl |
| ClearDERControl | 存储并应答 | `Accepted` / `NotFound` |
| UpdateDynamicSchedule、UsePriorityCharging | 不支持 | CALLERROR `NotImplemented`（计划在后续 v0.3.x 支持） |
| GetPeriodicEventStream、AdjustPeriodicEventStream | 不支持 | CALLERROR `NotImplemented`（计划在 v0.4 支持） |
| RequestBatterySwap、NotifyWebPaymentStarted | 不支持 | CALLERROR `NotImplemented` |

“存储并应答”指请求保存在内存中并返回符合 schema 的应答，不会据此执行任何动作（不做本地费用计算，也没有 DER 行为）。

12 条新的充电站到 CSMS 消息（BatterySwap、ClosePeriodicEventStream、GetCertificateChainStatus、NotifyDERAlarm、
NotifyDERStartStop、NotifyPeriodicEventStream（SEND）、NotifyPriorityCharging、NotifySettlement、
OpenPeriodicEventStream、PullDynamicScheduleUpdate、ReportDERControl、VatNumberValidation）充电站不会主动发送
（ReportDERControl 仅作为 GetDERControl 的应答发送）。Mini CSMS 对它们做最简应答。

## 尚不支持

| 功能 | 状态 |
|---|---|
| OCPP 2.1：UpdateDynamicSchedule、UsePriorityCharging、周期事件流、换电、Web 支付 | 不支持（CALLERROR `NotImplemented`），见 [OCPP 2.1](#ocpp-21) |
| 1.6 Security Whitepaper 的 SignedUpdateFirmware、GetLog、LogStatusNotification、SignedFirmwareStatusNotification | 不支持（CALLERROR `NotImplemented`） |
| ISO 15118 / Plug & Charge | 不支持（付费标准） |
