# OCPP 1.6J 消息

以下是 v0.1 支持的消息。从 CSMS 收到的其它 action 一律应答 CALLERROR `NotImplemented`，并记录到帧日志。

## 充电桩 → CSMS

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

## CSMS → 充电桩

| Action | 说明 |
|---|---|
| RemoteStartTransaction | 可携带 `chargingProfile`（不带 `transactionId` 的 TxProfile） |
| RemoteStopTransaction | |
| GetConfiguration | `key` 为空时返回全部键；未知键放入 `unknownKey`。Boot 被接受之前也会应答 |
| ChangeConfiguration | Accepted / Rejected / RebootRequired / NotSupported |
| Reset | Soft / Hard：结束交易、断开、重连并重新 Boot |
| TriggerMessage | BootNotification / Heartbeat / MeterValues / StatusNotification；DiagnosticsStatusNotification 和 FirmwareStatusNotification 应答 `NotImplemented` |
| UnlockConnector | Unlocked / UnlockFailed / NotSupported |
| ChangeAvailability | Accepted / Rejected / Scheduled（交易进行中为 Scheduled） |
| DataTransfer | 默认应答 `UnknownVendorId` |
| SetChargingProfile | ChargePointMaxProfile（仅 connector 0）/ TxDefaultProfile / TxProfile（需要进行中的交易） |
| ClearChargingProfile | 按 `id` / `connectorId` / `chargingProfilePurpose` / `stackLevel` 过滤 |
| GetCompositeSchedule | `chargingRateUnit` 可选；与实际功率限值使用同一个计算函数 |

## v0.1 不包含

| 功能 | 计划 |
|---|---|
| OCPP 2.0.1 / 2.1 | v0.2 / v0.3（UI 中灰色显示） |
| Security Profile 3（TLS 客户端证书）、证书管理、Security Whitepaper 消息（SecurityEventNotification 等） | v0.2 |
| SendLocalList / GetLocalListVersion、授权缓存 | v0.2（v0.1 使用 `id_tags` 列表加 CSMS 查询） |
| ReserveNow / CancelReservation | v0.2 |
| UpdateFirmware / GetDiagnostics 及其状态通知 | v0.2（v0.1 应答 CALLERROR `NotImplemented`） |
| ISO 15118 / Plug & Charge | 不支持（付费标准） |
