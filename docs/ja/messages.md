# OCPP メッセージ

SME OCPPSim は OCPP 1.6J の Charge Point と OCPP 2.0.1 の Charging Station を模擬します。バージョンは
`ocpp_version` で充電器ごと、または全体に指定します（[設定](configuration.md) を参照）。CSMS から受信した
未対応のアクションには CALLERROR `NotImplemented` を返し、フレームログに記録します。

## OCPP 1.6J

サブプロトコル `ocpp1.6`。

### Charge Point → CSMS

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

### CSMS → Charge Point

| アクション | 備考 |
|---|---|
| RemoteStartTransaction | `chargingProfile`（`transactionId` なしの TxProfile）を含められます。Boot 受理前は Rejected |
| RemoteStopTransaction | |
| GetConfiguration | `key` が空ならすべてのキーを返し、未知のキーは `unknownKey` に入れます。Boot 受理前でも応答します |
| ChangeConfiguration | Accepted / Rejected / RebootRequired / NotSupported |
| Reset | Soft / Hard: トランザクションを終了し、切断・再接続して再度 Boot します |
| TriggerMessage | BootNotification / Heartbeat / MeterValues / StatusNotification。DiagnosticsStatusNotification と FirmwareStatusNotification は `NotImplemented` |
| UnlockConnector | 常に `Unlocked` を返します（他の応答はまだ設定できません）。コネクタで実行中のトランザクションは理由 `UnlockCommand` で終了します |
| ChangeAvailability | Accepted / Rejected / Scheduled（トランザクション中は Scheduled） |
| DataTransfer | 既定の応答は `UnknownVendorId` |
| SetChargingProfile | ChargePointMaxProfile（コネクター 0 のみ）/ TxDefaultProfile / TxProfile（実行中のトランザクションが必要） |
| ClearChargingProfile | `id` / `connectorId` / `chargingProfilePurpose` / `stackLevel` で絞り込み |
| GetCompositeSchedule | `chargingRateUnit` は任意。実際の電力制限と同じ評価関数を使います |

## OCPP 2.0.1 { #ocpp-201 }

サブプロトコル `ocpp2.0.1`。`connectors: N` は 1 コネクターの EVSE を N 個持つステーションです。2.0.1 の充電器で
Web UI や HTTP API が「コネクター」と言う場合は EVSE id を指します。ステーションが動作に使う設定は 1.6 の
設定キーではなく Device Model の変数です（[設定](configuration.md#variables) を参照）。仕様で定まっていない点は、
以下の備考にシミュレーターとしての解釈を記載しています。

### Charging Station → CSMS

| メッセージ | 対応 | 備考 |
|---|---|---|
| Authorize | 対応 | `auth_mode` に従います。充電器で提示したトークンは `type` `ISO14443` で送信します。シミュレーターが扱わないステータス（NoCredit、NotAtThisTime など）はオペレーターには Invalid として表示します |
| BootNotification | 対応 | Accepted / Pending / Rejected。`reason` は `PowerUp`、CSMS の Reset 後は `RemoteReset`、TriggerMessage 後は `Triggered`。Boot が受理されるまでは BootNotification だけを送信します（CSMS からの要求には応答します） |
| ClearedChargingLimit | 対応 | 外部制限値を解除したとき（`chargingLimitSource` `EMS`）。Boot 受理中のみ。[Smart Charging](smart-charging.md#ocpp-201) を参照 |
| DataTransfer | 対応 | UI または HTTP API から手動で送信します |
| FirmwareStatusNotification | Not supported |  |
| Get15118EVCertificate | Not supported | ISO 15118 |
| GetCertificateStatus | Not supported | ISO 15118 |
| Heartbeat | 対応 | `OCPPCommCtrlr.HeartbeatInterval` ごと（受理された BootNotification でも設定されます） |
| LogStatusNotification | Not supported |  |
| MeterValues | 対応 | TriggerMessage で送信します（context `Trigger`）。トランザクション中の計測値は TransactionEvent で送ります |
| NotifyChargingLimit | 対応 | 外部制限値を設定したとき（`chargingLimitSource` `EMS`。`SmartChargingCtrlr.NotifyChargingLimitWithSchedules` が true ならスケジュール付き）。Boot 受理中のみ。[Smart Charging](smart-charging.md#ocpp-201) を参照 |
| NotifyCustomerInformation | Not supported |  |
| NotifyDisplayMessages | Not supported |  |
| NotifyEVChargingNeeds | Not supported | ISO 15118 |
| NotifyEVChargingSchedule | Not supported | ISO 15118 |
| NotifyEvent | Not supported | 故障は中断されたトランザクションの `stoppedReason` としてのみ通知します |
| NotifyMonitoringReport | Not supported |  |
| NotifyReport | 対応 | GetBaseReport への応答を分割して送信します（各 `DeviceDataCtrlr.ItemsPerMessage[GetReport]` 件、`seqNo` は 0 から、最後以外は `tbc`） |
| PublishFirmwareStatusNotification | Not supported |  |
| ReportChargingProfiles | 対応 | GetChargingProfiles への応答: EVSE と `chargingLimitSource` ごとに 1 通、最後以外は `tbc` |
| ReservationStatusUpdate | Not supported |  |
| SecurityEventNotification | Not supported |  |
| SignCertificate | Not supported |  |
| StatusNotification | 対応 | EVSE ごと（コネクター 1）に送信し、EVSE 0 の分はありません。`connectorStatus` は Available / Occupied / Unavailable / Faulted。より細かい状態（充電中、一時停止）は TransactionEvent の `chargingState` で表します |
| TransactionEvent | 対応 | `Started` → `Updated` → `Ended`。`transactionId`（UUID）はステーションが生成します。`seqNo` はトランザクションごとに 0 から。定期 `Updated` は `SampledDataCtrlr.TxUpdatedInterval` ごと。切断中に発生したイベントは `offline: true` |

TransactionEvent と状態変化による StatusNotification は、1.6 と同様に 1 本の順序付きキューで送信されます。
失敗した TransactionEvent は `OCPPCommCtrlr.MessageAttempts[TransactionEvent]` 回まで、
`MessageAttemptInterval[TransactionEvent]` × 失敗回数の間隔で再送します。切断で応答を受け取れなかったものは次回の
Boot 受理後に再送します（CSMS は `transactionId` + `seqNo` で重複を判別できます）。

シミュレーターの解釈では、トランザクションはケーブル接続**かつ** ID トークンの認証で開始し（`triggerReason` は
後に起きた方の `CablePluggedIn` または `Authorized`）、同じトークンの再提示またはオペレーターの停止
（`StopAuthorized` / `Local`）、ケーブル抜去（`EVCommunicationLost`）、オペレーターによる故障（`AbnormalCondition`。
故障コードが `stoppedReason`）、または TransactionEvent の応答で CSMS がトークンを拒否したとき
（`TxCtrlr.StopTxOnInvalidId` が true の間は `Deauthorized`）に終了します。

### CSMS → Charging Station

| メッセージ | 対応 | 備考 |
|---|---|---|
| CancelReservation | Not supported |  |
| CertificateSigned | Not supported |  |
| ChangeAvailability | 対応 | `evse` なし、または EVSE 0 はすべての EVSE。対象 EVSE がトランザクション中に Inoperative にする場合は `Scheduled` で、終了時に反映します |
| ClearCache | Not supported |  |
| ClearChargingProfile | 対応 | `chargingProfileId` が条件より優先し、条件は AND。外部制約は CSMS からは削除されません |
| ClearDisplayMessage | Not supported |  |
| ClearVariableMonitoring | Not supported |  |
| CostUpdated | Not supported |  |
| CustomerInformation | Not supported |  |
| DataTransfer | 対応 | 既定の応答は `UnknownVendorId` |
| DeleteCertificate | Not supported |  |
| GetBaseReport | 対応 | ConfigurationInventory（ReadOnly 以外の全変数）、FullInventory、SummaryInventory。応答の後に NotifyReport を送信します |
| GetChargingProfiles | 対応 | 一致するプロファイルがあれば `Accepted`、なければ `NoProfiles`。応答の後に ReportChargingProfiles を送信します |
| GetCompositeSchedule | 対応 | 実際の電力制限と同じ評価関数。期間の値はハードウェア最大値で頭打ちになり、最大値は「制限なし」も表します。未知の EVSE または duration < 1 は `Rejected` |
| GetDisplayMessages | Not supported |  |
| GetInstalledCertificateIds | Not supported |  |
| GetLocalListVersion | Not supported |  |
| GetLog | Not supported |  |
| GetMonitoringReport | Not supported |  |
| GetReport | Not supported | GetBaseReport を使用してください |
| GetTransactionStatus | Not supported |  |
| GetVariables | 対応 | 属性タイプ Actual / Target / MinSet / MaxSet。`UnknownComponent` / `UnknownVariable` / `NotSupportedAttributeType`。WriteOnly の変数は `Rejected`。`DeviceDataCtrlr.ItemsPerMessage[GetVariables]` を超える件数は CALLERROR `OccurrenceConstraintViolation` |
| InstallCertificate | Not supported |  |
| PublishFirmware | Not supported |  |
| RequestStartTransaction | 対応 | `remoteStartId` と任意の `chargingProfile`（`transactionId` なしの TxProfile。新しいトランザクションに紐付けて設定）。`evseId` がなければケーブル接続済みで開始可能な最初の EVSE を使います。`AuthCtrlr.AuthorizeRemoteStart` が true ならトークンを提示時と同様に認証します |
| RequestStopTransaction | 対応 | このステーションで実行中のトランザクションなら `Accepted`（`RemoteStop` で終了）、それ以外は `Rejected` |
| ReserveNow | Not supported |  |
| Reset | 対応 | `Immediate` / `OnIdle`（トランザクション中は `Scheduled`）。次の BootNotification の reason は `RemoteReset`。EVSE 単位のリセットは `Rejected` |
| SendLocalList | Not supported |  |
| SetChargingProfile | 対応 | ChargingStationMaxProfile（EVSE 0 のみ）、TxDefaultProfile、TxProfile。CSMS からの ChargingStationExternalConstraints は `Rejected`。[Smart Charging](smart-charging.md#ocpp-201) を参照 |
| SetDisplayMessage | Not supported |  |
| SetMonitoringBase | Not supported |  |
| SetMonitoringLevel | Not supported |  |
| SetNetworkProfile | Not supported |  |
| SetVariableMonitoring | Not supported |  |
| SetVariables | 対応 | 値を検査します（データ型、上下限、値リスト）。ReadOnly の変数は `Rejected`。該当する変数は `RebootRequired`。変更は即時に反映され、YAML ファイルには書き戻されません |
| TriggerMessage | 対応 | BootNotification（Boot 未受理の間のみ）、Heartbeat、StatusNotification、MeterValues、TransactionEvent。それ以外の要求メッセージは `NotImplemented`。BootNotification 以外は Boot 受理まで `Rejected`。未知の EVSE は `Rejected` |
| UnlockConnector | 対応 | `Unlocked`。EVSE がトランザクション中なら `OngoingAuthorizedTransaction`（停止はしません）。未知の EVSE やコネクター 1 以外は `UnknownConnector` |
| UnpublishFirmware | Not supported |  |
| UpdateFirmware | Not supported |  |

## 未対応の機能

| 機能 | 状況 |
|---|---|
| OCPP 2.1 | v0.3 で対応予定（UI ではグレー表示） |
| セキュリティプロファイル 3（TLS クライアント証明書）、証明書管理、Security Whitepaper のメッセージ（SecurityEventNotification など） | 対応予定 |
| ローカル認証リスト（SendLocalList / GetLocalListVersion）、認証キャッシュ | 対応予定（現在は `id_tags` リストと CSMS への照会） |
| 予約（ReserveNow / CancelReservation） | 対応予定 |
| ファームウェア更新、診断 / ログのアップロードとその通知 | 対応予定（現在は CALLERROR `NotImplemented`） |
| ISO 15118 / Plug & Charge | 非対応（有償規格のため） |
