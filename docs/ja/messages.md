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
| StopTransaction | `reason`、`transactionData`：トランザクション開始から `MeterValueSampleInterval` ごとの `StopTxnSampledData` の測定項目（`Sample.Periodic`）と終了時の値（`Transaction.End`）、`ClockAlignedDataInterval` の時刻境界ごとの `StopTxnAlignedData` の測定項目（`Sample.Clock`）。両キーが空なら含めません。メーターが報告しない測定項目（Energy.Active.Import.Register、Power.Active.Import、Current.Import、Voltage、SoC 以外）や `*MaxLength` キーより長いリストは ChangeConfiguration で `Rejected` |
| MeterValues | トランザクション中 `MeterValueSampleInterval` ごとにサンプル値を送信。測定項目は `MeterValuesSampledData`、`location` / `context` 付き。時刻同期の MeterValues メッセージ（`ClockAlignedDataInterval` / `MeterValuesAlignedData`）はまだ送信しません（`transactionData` の `StopTxnAlignedData` の読み値は送ります） |
| DataTransfer | UI または HTTP API から手動で送信します |
| FirmwareStatusNotification | Downloading → Downloaded → Installing →（再起動、BootNotification）→ Installed。失敗注入で DownloadFailed / InstallationFailed |
| DiagnosticsStatusNotification | Uploading → Uploaded / UploadFailed |
| SecurityEventNotification | Security Whitepaper。2.0.1 と同じイベントを 1.6 の名前（InvalidCentralSystemCertificate / FailedToAuthenticateAtCentralSystem / InvalidChargePointCertificate）で送信（critical なものはオフライン中もキューに保持） |
| SignCertificate | Security Whitepaper。ExtendedTriggerMessage `SignChargePointCertificate` の後に新しい鍵と CSR（CN = `serial` または充電器 ID、O = `CpoName`、既定は `vendor`） |

トランザクション関連のメッセージ（StartTransaction、StopTransaction、定期 MeterValues）と状態変化による
StatusNotification は、1 本の順序付きキューで送信されます。切断で応答を受け取れなかったメッセージは次回の
Boot 受理後に再送されるため、CSMS が同じメッセージを 2 回受け取ることがあります（1.6 には冪等キーがありません）。
失敗したトランザクションメッセージは `TransactionMessageRetryInterval` 間隔で `TransactionMessageAttempts` 回まで再送します。

### CSMS → Charge Point

| アクション | 備考 |
|---|---|
| RemoteStartTransaction | `chargingProfile`（`transactionId` なしの TxProfile）を含められます。Boot 受理前は Rejected |
| RemoteStopTransaction | |
| GetConfiguration | `key` が空ならすべてのキーを返し、未知のキーは `unknownKey` に入れます。Boot 受理前でも応答します。書込専用の `AuthorizationKey` は値なしで返します |
| ChangeConfiguration | Accepted / Rejected / RebootRequired / NotSupported。`AuthorizationKey`（Security Whitepaper A01）は 16〜40 文字で、新しい Basic 認証パスワードで再接続します。プロファイル 3 では `Rejected` |
| Reset | Soft / Hard: トランザクションを終了し、切断・再接続して再度 Boot します |
| TriggerMessage | BootNotification / Heartbeat / MeterValues / StatusNotification / FirmwareStatusNotification / DiagnosticsStatusNotification（更新 / 送信の実行中は現在の段階、それ以外は `Idle`） |
| UnlockConnector | 常に `Unlocked` を返します（他の応答はまだ設定できません）。コネクタで実行中のトランザクションは理由 `UnlockCommand` で終了します |
| ChangeAvailability | Accepted / Rejected / Scheduled（トランザクション中は Scheduled） |
| DataTransfer | 既定の応答は `UnknownVendorId` |
| SetChargingProfile | ChargePointMaxProfile（コネクター 0 のみ）/ TxDefaultProfile / TxProfile（実行中のトランザクションが必要） |
| ClearChargingProfile | `id` / `connectorId` / `chargingProfilePurpose` / `stackLevel` で絞り込み |
| GetCompositeSchedule | `chargingRateUnit` は任意。実際の電力制限と同じ評価関数を使います |
| SendLocalList | Full / Differential。`listVersion` は 1 以上（0 以下は CALLERROR `PropertyConstraintViolation`）。Differential はバージョンを上げる必要があります（`VersionMismatch`）。`SendLocalListMaxLength` を超える件数、重複タグ、`idTagInfo` のない Full のエントリ、`LocalAuthListMaxLength` を超えるタグ数は `Failed`（リストは変わりません）。`LocalAuthListEnabled` が false の間は `NotSupported` |
| GetLocalListVersion | 保持しているバージョン。最初のリストを受け取るまでは 0 |
| ClearCache | 認証キャッシュを消去します。`AuthorizationCacheEnabled` が false の間は `Rejected` |
| ReserveNow | コネクターの状態に応じて Faulted / Unavailable / Occupied、それ以外は Accepted。同じ `reservationId` は予約を置き換えます。コネクター 0 の予約ではコネクターを 1 つ空けておきます（`ReserveConnectorZeroSupported` が false の間は `Rejected`）。予約されたコネクターは `Reserved` を報告し、予約した `idTag` かその親タグを持つタグでのみ開始します。`reservationId` は StartTransaction で送ります。期限切れ、取り消し、コネクターの Faulted / Unavailable で終了します |
| CancelReservation | 既知の予約なら `Accepted`、それ以外は `Rejected` |
| UpdateFirmware | 模擬のみ（実際のダウンロードはしません）。`retrieveDate` まで待ち、ダウンロード失敗時は `retries` / `retryInterval` に従います。トランザクションのないコネクタはトランザクションがなくなるまで `Unavailable` にし、その後インストールして再起動します（Hard Reset と同じ）。各段階の時間と失敗注入は充電器ごとの `maintenance`（`download_s`、`install_s`、`upload_s`、`retry_interval_s`、`fail_download`、`fail_install`、`fail_upload`）。インストール前の更新は新しい要求で置き換え、インストール中に届いた要求は無視します |
| GetDiagnostics | `fileName` `<id>-diagnostics-<時刻>.log` を返し、アップロードを模擬します（実際の送信はしません）。実行中の送信は新しい要求で置き換えます |
| ExtendedTriggerMessage | Security Whitepaper。BootNotification / Heartbeat / MeterValues / StatusNotification は TriggerMessage と同じ、加えて `SignChargePointCertificate`。`FirmwareStatusNotification`（SignedFirmwareStatusNotification を指す）と `LogStatusNotification` は `NotImplemented`。`connectorId` 0 は CALLERROR `PropertyConstraintViolation`（Whitepaper は `connectorId` > 0） |
| CertificateSigned | Security Whitepaper。2.0.1 と同様に未処理の CSR と一致すれば `Accepted`、その後（プロファイル 3）新しい証明書で再接続 |
| InstallCertificate / DeleteCertificate / GetInstalledCertificateIds | Security Whitepaper。CentralSystemRootCertificate / ManufacturerRootCertificate、2.0.1 と同様 |

## OCPP 2.0.1 { #ocpp-201 }

サブプロトコル `ocpp2.0.1`。`connectors: N` は 1 コネクターの EVSE を N 個持つステーションです。2.0.1 の充電器で
Web UI や HTTP API が「コネクター」と言う場合は EVSE id を指します。ステーションが動作に使う設定は 1.6 の
設定キーではなく Device Model の変数です（[設定](configuration.md#variables) を参照）。仕様で定まっていない点は、
以下の備考にシミュレーターとしての解釈を記載しています。

### Charging Station → CSMS

| メッセージ | 対応 | 備考 |
|---|---|---|
| Authorize | 対応 | `auth_mode` に従います。充電器で提示したトークンは `type` `ISO14443` で送信します。シミュレーターが扱わないステータス（NoCredit、NotAtThisTime など）はオペレーターには Invalid として表示します |
| BootNotification | 対応 | Accepted / Pending / Rejected。`reason` は `PowerUp`、CSMS の Reset 後は `RemoteReset`、TriggerMessage 後は `Triggered`。Boot が受理されるまでは BootNotification だけを送信します（Pending 中に TriggerMessage で要求されたものを除く、B02.FR.02）。Pending 中の RequestStart/StopTransaction は `Rejected`（B02.FR.05）、Rejected 中は CSMS からのすべての要求に CALLERROR `SecurityError` で応答します（B03） |
| ClearedChargingLimit | 対応 | 外部制限値を解除したとき（`chargingLimitSource` `EMS`）。Boot 受理中のみ。[Smart Charging](smart-charging.md#ocpp-201) を参照 |
| DataTransfer | 対応 | UI または HTTP API から手動で送信します |
| FirmwareStatusNotification | 対応 | UpdateFirmware の進捗（`requestId` 付き）。TriggerMessage では最後の status、Installed のあとや未送信なら `Idle` |
| Get15118EVCertificate | Not supported | ISO 15118 |
| GetCertificateStatus | Not supported | ISO 15118 |
| Heartbeat | 対応 | `OCPPCommCtrlr.HeartbeatInterval` ごと（受理された BootNotification でも設定されます） |
| LogStatusNotification | 対応 | GetLog の進捗（Uploading → Uploaded / UploadFailure。新しい GetLog で取り消した送信には AcceptedCanceled）。TriggerMessage では送信中は `Uploading`、それ以外は `Idle` |
| MeterValues | 対応 | TriggerMessage で送信します（context `Trigger`、計測項目は `AlignedDataCtrlr.Measurands`）。トランザクション中の計測値は TransactionEvent で送ります |
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
| ReservationStatusUpdate | 対応 | `expiryDateTime` で `Expired`、予約した EVSE（`evseId` なしの場合はステーション）が Faulted / Unavailable になると `Removed` |
| SecurityEventNotification | 対応 | StartupOfTheDevice、ResetOrReboot、FirmwareUpdated、InvalidCsmsCertificate、InvalidTLSVersion、FailedToAuthenticateAtCsms、InvalidChargingStationCertificate、DiscardedRenewedClientCertificate、ReconfigurationOfSecurityParameters。critical なイベントはオフライン中もキューに保持し、次に Boot が受理された後に送信。それ以外は接続中に発生したものだけ送信 |
| SignCertificate | 対応 | TriggerMessage `SignChargingStationCertificate` の後に新しい EC P-256 鍵と CSR（CN = `serial`、未設定なら充電器 ID。O = `SecurityCtrlr.OrganizationName`）を送信、`certificateType` は `ChargingStationCertificate`。応答のない CSR は再送しない（CertSigningWaitMinimum は未モデル化） |
| StatusNotification | 対応 | EVSE ごと（コネクター 1）に送信し、EVSE 0 の分はありません。`connectorStatus` は Available / Occupied / Reserved / Unavailable / Faulted。より細かい状態（充電中、一時停止）は TransactionEvent の `chargingState` で表します |
| TransactionEvent | 対応 | `Started` → `Updated` → `Ended`。`transactionId`（UUID）はステーションが生成します。`seqNo` はトランザクションごとに 0 から。定期 `Updated` は `SampledDataCtrlr.TxUpdatedInterval` ごと。切断中に発生したイベントは `offline: true`。`Started` には `SampledDataCtrlr.TxStartedMeasurands`。`Ended` には `SampledDataCtrlr` と `AlignedDataCtrlr` の `TxEndedMeasurands` の和集合の開始時（`Transaction.Begin`）・終了時（`Transaction.End`）の値、開始から `SampledDataCtrlr.TxEndedInterval` ごとのサンプル値（`Sample.Periodic`、0 = 開始と終了のみ）、`AlignedDataCtrlr.TxEndedInterval` の時刻境界ごとの値（`Sample.Clock`、0 = なし）。1.6 と同じく、メーターが報告しない測定項目は受け付けません：`*Measurands` 変数の `valuesList` は報告する 5 項目のため、SetVariables は `Rejected` |

TransactionEvent と状態変化による StatusNotification は、1.6 と同様に 1 本の順序付きキューで送信されます。
失敗した TransactionEvent は `OCPPCommCtrlr.MessageAttempts[TransactionEvent]` 回まで、
`MessageAttemptInterval[TransactionEvent]` × 失敗回数の間隔で再送します。切断で応答を受け取れなかったものは次回の
Boot 受理後に再送します（CSMS は `transactionId` + `seqNo` で重複を判別できます）。

シミュレーターの解釈では、トランザクションはケーブル接続**かつ** ID トークンの認証で開始し（`triggerReason` は
後に起きた方の `CablePluggedIn` または `Authorized`）、同じトークンの再提示またはオペレーターの停止
（`StopAuthorized` / `Local`）、ケーブル抜去（`EVCommunicationLost`）、オペレーターによる故障（`AbnormalCondition`。
故障コードが `stoppedReason`）、または TransactionEvent の応答で CSMS がトークンを拒否したとき
（`TxCtrlr.StopTxOnInvalidId` が true の間は `Deauthorized`）に終了します。`StopTxOnInvalidId` が false ならトランザクションは続きますが、送電はすぐに止まります（`Updated` `ChargingStateChanged`、`SuspendedEVSE`）。

### CSMS → Charging Station

| メッセージ | 対応 | 備考 |
|---|---|---|
| CancelReservation | 対応 | 既知の予約なら `Accepted`（ReservationStatusUpdate は送りません）、それ以外は `Rejected` |
| CertificateSigned | 対応 | 未処理の CSR と鍵が一致し現在有効な証明書なら `Accepted`（CA 階層は検査しない。CSMS が次の TLS ハンドシェイクで検査する）、それ以外は `Rejected` と InvalidChargingStationCertificate。プロファイル 3 では新しい証明書で再接続し、その接続が失敗したら旧証明書に戻す（DiscardedRenewedClientCertificate）。`V2GCertificate` は `Rejected` |
| ChangeAvailability | 対応 | `evse` なし、または EVSE 0 はステーション自体の可用性で、EVSE の可用性とは別に保持します（EVSE の `Operative` はステーションの `Inoperative` を解除しません）。トランザクション中（その EVSE、ステーションならいずれかの EVSE）に Inoperative にする場合は `Scheduled` で、終了時に反映します。ステーションの Inoperative では空いている EVSE はすぐに Unavailable になります |
| ClearCache | 対応 | 認証キャッシュを消去します。`AuthCacheCtrlr.Enabled` が false の間は `Rejected` |
| ClearChargingProfile | 対応 | `chargingProfileId` が条件より優先し、条件は AND。外部制約は CSMS からは削除されません |
| ClearDisplayMessage | Not supported |  |
| ClearVariableMonitoring | Not supported |  |
| CostUpdated | Not supported |  |
| CustomerInformation | Not supported |  |
| DataTransfer | 対応 | 既定の応答は `UnknownVendorId` |
| DeleteCertificate | 対応 | ハッシュデータ（SHA256 / SHA384 / SHA512）で `Accepted` / `NotFound`。クライアント証明書は削除できない（`Failed`） |
| GetBaseReport | 対応 | ConfigurationInventory（ReadOnly 以外の全変数）、FullInventory、SummaryInventory。応答の後に NotifyReport を送信します |
| GetChargingProfiles | 対応 | 一致するプロファイルがあれば `Accepted`、なければ `NoProfiles`。応答の後に ReportChargingProfiles を送信します |
| GetCompositeSchedule | 対応 | 実際の電力制限と同じ評価関数。期間の値はハードウェア最大値で頭打ちになり、最大値は「制限なし」も表します。未知の EVSE または duration < 1 は `Rejected`。`SmartChargingCtrlr.RateUnit` にない `chargingRateUnit` は `Rejected`（`UnsupportedRateUnit`） |
| GetDisplayMessages | Not supported |  |
| GetInstalledCertificateIds | 対応 | インストール済み CA 証明書の SHA256 ハッシュデータ。該当なしは `NotFound`（V2GCertificateChain は常に該当なし） |
| GetLocalListVersion | 対応 | 保持しているバージョン。最初のリストを受け取るまで、または `LocalAuthListCtrlr.Enabled` が false の間は 0 |
| GetLog | 対応 | `Accepted` と `filename` `<id>_<logType>_<時刻>.log`。実行中の送信を取り消したときは `AcceptedCanceled`。送信は模擬（`maintenance` の設定） |
| GetMonitoringReport | Not supported |  |
| GetReport | Not supported | GetBaseReport を使用してください |
| GetTransactionStatus | Not supported |  |
| GetVariables | 対応 | 属性タイプ Actual / Target / MinSet / MaxSet。`UnknownComponent` / `UnknownVariable` / `NotSupportedAttributeType`。WriteOnly の変数は `Rejected`。`DeviceDataCtrlr.ItemsPerMessage[GetVariables]` を超える件数は CALLERROR `OccurrenceConstraintViolation` |
| InstallCertificate | 対応 | 現在有効な PEM 証明書 1 枚のみ。それ以外や CA 証明書が 20 枚に達した場合は `Rejected`。インストールした CSMS ルート証明書は次の TLS 接続で信頼される。メモリ上のみ保持 |
| PublishFirmware | Not supported |  |
| RequestStartTransaction | 対応 | `remoteStartId` と任意の `chargingProfile`（`transactionId` なしの TxProfile。新しいトランザクションに紐付けて設定）。`evseId` がなければケーブル接続済みで開始可能な最初の EVSE を使います。`AuthCtrlr.AuthorizeRemoteStart` が true ならトークンを提示時と同様に認証します。type が `Central` / `NoAuthorization` のトークンは認証しません。これで開始したトランザクションの `triggerReason` は、ケーブルが後から接続された場合も `RemoteStart` です。`TxCtrlr.EVConnectionTimeOut` を過ぎてもケーブルを待っているトークンは破棄します |
| RequestStopTransaction | 対応 | このステーションで実行中のトランザクションなら `Accepted`（`RemoteStop` で終了）、それ以外は `Rejected` |
| ReserveNow | 対応 | EVSE の状態に応じて Faulted / Unavailable / Occupied、それ以外は Accepted。同じ `id` は予約を置き換えます。`evseId` なしの予約では EVSE を 1 つ空けておきます（`ReservationCtrlr.NonEvseSpecific` が false の間は `Rejected`）。`connectorType` は模擬しません。`ReservationCtrlr.Enabled` が false の間は `Rejected`。予約された EVSE は `Reserved` を報告し、予約した `idToken` かその `groupIdToken` のトークンでのみ開始します。`reservationId` は `Started` イベントで送ります |
| Reset | 対応 | `Immediate` / `OnIdle`（トランザクション中は `Scheduled`）。次の BootNotification の reason は `RemoteReset`。EVSE 単位のリセットは `Rejected` |
| SendLocalList | 対応 | Full / Differential。`versionNumber` は 1 以上（0 以下は CALLERROR `PropertyConstraintViolation`）。Differential はバージョンを上げる必要があります（`VersionMismatch`）。`LocalAuthListCtrlr.ItemsPerMessage` を超える件数、重複トークン、`idTokenInfo` のない Full のエントリは `Failed`。`LocalAuthListCtrlr.Enabled` が false の間も `Failed` |
| SetChargingProfile | 対応 | ChargingStationMaxProfile（EVSE 0 のみ）、TxDefaultProfile、TxProfile。CSMS からの ChargingStationExternalConstraints は `Rejected`（`UnsupportedPurpose`）。`chargingSchedule` は 1 つのみ。[Smart Charging](smart-charging.md#ocpp-201) を参照 |
| SetDisplayMessage | Not supported |  |
| SetMonitoringBase | Not supported |  |
| SetMonitoringLevel | Not supported |  |
| SetNetworkProfile | Not supported |  |
| SetVariableMonitoring | Not supported |  |
| SetVariables | 対応 | 値を検査します（データ型、上下限、値リスト）。ReadOnly の変数は `Rejected`。該当する変数は `RebootRequired`。変更は即時に反映され、YAML ファイルには書き戻されません |
| TriggerMessage | 対応 | BootNotification（Boot 未受理の間のみ）、Heartbeat、StatusNotification、MeterValues、TransactionEvent（計測項目は `TxUpdatedMeasurands`）、LogStatusNotification / FirmwareStatusNotification（現在の status）。それ以外の要求メッセージは `NotImplemented`。BootNotification 以外は BootNotification の応答前は `Rejected`、Accepted と Pending の間は送信します。未知の EVSE は `Rejected` |
| UnlockConnector | 対応 | `Unlocked`。EVSE がトランザクション中なら `OngoingAuthorizedTransaction`（停止はしません）。未知の EVSE やコネクター 1 以外は `UnknownConnector` |
| UnpublishFirmware | Not supported |  |
| UpdateFirmware | 対応 | 模擬のみ（ダウンロードも検証もしません）。DownloadScheduled（`retrieveDateTime` が未来）→ Downloading → Downloaded → SignatureVerified（`signature` があるとき）→ InstallScheduled（`installDateTime` が未来）→ トランザクションのない EVSE はトランザクションがなくなるまで `Unavailable` → Installing → InstallRebooting → 再起動（BootNotification reason `FirmwareUpdate`）→ Installed。ダウンロード失敗時は `retries` / `retryInterval`。インストール前の更新を取り消したときは `AcceptedCanceled`、インストール中は `Rejected`。時間と失敗注入は充電器ごとの `maintenance` |

## 未対応の機能

| 機能 | 状況 |
|---|---|
| OCPP 2.1 | v0.3 で対応予定（UI ではグレー表示） |
| 1.6 Security Whitepaper の SignedUpdateFirmware、GetLog、LogStatusNotification、SignedFirmwareStatusNotification | 未対応（CALLERROR `NotImplemented`） |
| ISO 15118 / Plug & Charge | 非対応（有償規格のため） |
