# OCPP 1.6J メッセージ

v0.1 で対応しているメッセージです。CSMS から受信したそれ以外のアクションには CALLERROR `NotImplemented` を返し、
フレームログに記録します。

## Charge Point → CSMS

| アクション | 備考 |
|---|---|
| BootNotification | Accepted / Pending / Rejected。Pending / Rejected のときは `interval`（0 の場合は `boot_retry_s`）後に再送し、その間は CSMS からの要求にのみ応答します |
| Heartbeat | `currentTime` から時刻のずれを記録します |
| StatusNotification | コネクター 0 と各コネクター。`errorCode`、`info`、`vendorErrorCode` |
| Authorize | `auth_mode` に従います（[設定](configuration.md) を参照） |
| StartTransaction | `transactionId` を取得します。`idTagInfo` が Accepted 以外ならトランザクションを停止します |
| StopTransaction | `reason`、`transactionData` |
| MeterValues | 定期サンプル（`MeterValueSampleInterval`）と時刻同期（`ClockAlignedDataInterval`）。measurand は `MeterValuesSampledData`。`location` / `context` 付き |
| DataTransfer | UI または HTTP API から手動で送信します |

トランザクション関連のメッセージ（StartTransaction、StopTransaction、定期 MeterValues）と状態変化による
StatusNotification は、1 本の順序付きキューで送信されます。切断で応答を受け取れなかったメッセージは次回の
Boot 受理後に再送されるため、CSMS が同じメッセージを 2 回受け取ることがあります（1.6 には冪等キーがありません）。
失敗したトランザクションメッセージは `TransactionMessageRetryInterval` 間隔で `TransactionMessageAttempts` 回まで再送します。

## CSMS → Charge Point

| アクション | 備考 |
|---|---|
| RemoteStartTransaction | `chargingProfile`（`transactionId` なしの TxProfile）を含められます |
| RemoteStopTransaction | |
| GetConfiguration | `key` が空ならすべてのキーを返し、未知のキーは `unknownKey` に入れます。Boot 受理前でも応答します |
| ChangeConfiguration | Accepted / Rejected / RebootRequired / NotSupported |
| Reset | Soft / Hard: トランザクションを終了し、切断・再接続して再度 Boot します |
| TriggerMessage | BootNotification / Heartbeat / MeterValues / StatusNotification。DiagnosticsStatusNotification と FirmwareStatusNotification は `NotImplemented` |
| UnlockConnector | Unlocked / UnlockFailed / NotSupported |
| ChangeAvailability | Accepted / Rejected / Scheduled（トランザクション中は Scheduled） |
| DataTransfer | 既定の応答は `UnknownVendorId` |
| SetChargingProfile | ChargePointMaxProfile（コネクター 0 のみ）/ TxDefaultProfile / TxProfile（実行中のトランザクションが必要） |
| ClearChargingProfile | `id` / `connectorId` / `chargingProfilePurpose` / `stackLevel` で絞り込み |
| GetCompositeSchedule | `chargingRateUnit` は任意。実際の電力制限と同じ評価関数を使います |

## v0.1 の対象外

| 機能 | 予定 |
|---|---|
| OCPP 2.0.1 / 2.1 | v0.2 / v0.3（UI ではグレー表示） |
| セキュリティプロファイル 3（TLS クライアント証明書）、証明書管理、Security Whitepaper のメッセージ（SecurityEventNotification など） | v0.2 |
| SendLocalList / GetLocalListVersion、認証キャッシュ | v0.2（v0.1 は `id_tags` リストと CSMS への照会） |
| ReserveNow / CancelReservation | v0.2 |
| UpdateFirmware / GetDiagnostics とその StatusNotification | v0.2（v0.1 では CALLERROR `NotImplemented`） |
| ISO 15118 / Plug & Charge | 非対応（有償規格のため） |
