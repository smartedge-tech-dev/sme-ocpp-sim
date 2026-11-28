# 設定

シミュレーターは 1 つの YAML ファイル（`-c FILE`）を読み込みます。どのブロックも省略可能で、未知のフィールドは
エラーになります。完全な例は [`configs/demo-site.yaml`](https://github.com/smartedge-tech-dev/sme-ocpp-sim/blob/main/configs/demo-site.yaml) です。

```yaml
simulation:
  tick_seconds: 1
  time_scale: 1
  running: true

frame_log:
  size: 20000

csms_defaults:
  url: "ws://localhost:8180/steve/websocket/CentralSystemService"
  ocpp_version: "1.6"
  security_profile: 0
  reconnect: { min_s: 1, max_s: 60, jitter: 0.2 }

ev_models:
  - id: "demo-60kwh"
    capacity_kwh: 60
    max_ac_kw: 11
    max_current_a: 16
    phases: 3

chargers:
  - id: "CP-0001"
    max_power_w: 6000
    id_tags: ["04A1B2C3", "TAG-0002"]
    config_keys:
      HeartbeatInterval: 300
```

!!! warning "ID・タグ・パスワードは引用符で囲んでください"
    YAML 1.1 では、引用符なしの `0123`、`1e5`、`ON`、`OFF`、`yes`、`no` は文字列になりません。識別子やテキストの
    フィールド（`id`、`id_tags`、`password`、URL、`vendor` など）は引用符付きの文字列だけを受け付け、`0123` が
    黙って `83` になる代わりに、引用符を付けるよう案内します。

## Web UI からの保存 { #saving-from-the-web-ui }

UI での変更はすぐに反映され（再接続するのは変更した充電器だけです）、YAML ファイルへ保存できます。保存時は、
変更のあったトップレベルブロック（`simulation`、`frame_log`、`csms_defaults`、`ev_models`、`chargers`）を丸ごと置き換えます。

- 変更したブロックの **外** にあるコメントは保持されます。改行コードと UTF-8 BOM も保持されます。
- 変更したブロックの **中** にあるコメントは失われます。どれか 1 台の充電器を変更すると `chargers:` ブロック全体が
  書き直されるため、**個々の充電器エントリの中にはコメントを書かないでください**。コメントは `chargers:` キーの上に書いてください。
- 既定値と同じ値は書き出されません。
- ブロックを差し替えられない場合（特殊なレイアウト）はドキュメント全体を書き直し、すべてのコメントが失われます。その際は警告をログに出力します。

パスワードは HTTP API では返さず、`********` と表示します。マスクのまま設定を送り返すと、保存済みのパスワードが維持されます。

## simulation

| フィールド | 既定値 | 意味 |
|---|---|---|
| `tick_seconds` | `1.0` | 電力モデルの刻み（秒、> 0） |
| `time_scale` | `1.0` | 電力モデル（ランプ、電力量レジスタ、SoC）だけを加速します。充電スケジュールと OCPP のタイミングは常に実時間です |
| `running` | `true` | `false` で電力モデルを停止します。OCPP セッションは動作し続けます |

## frame_log

| フィールド | 既定値 | 意味 |
|---|---|---|
| `size` | `20000` | メモリに保持する OCPP フレーム数（全充電器の合計、≥ 100） |

## csms_defaults

| フィールド | 既定値 | 意味 |
|---|---|---|
| `url` | なし | CSMS の WebSocket URL（`ws://` または `wss://`）。充電器 ID を最後のパスセグメントとして付加します。すべての充電器に `csms_url` がある場合を除き必須です |
| `ocpp_version` | `"1.6"` | `"1.6"` または `"2.0.1"`。`"2.1"` は v0.3 で対応予定としてエラーになります |
| `security_profile` | `0` | `0`、`1`、`2`、`3`。各充電器の設定をこれに照らして検査します。[セキュリティプロファイル](security-profiles.md) を参照 |
| `reconnect.min_s` | `1.0` | 最初の再接続待ち時間（秒） |
| `reconnect.max_s` | `60.0` | 再接続待ち時間の上限（≥ `min_s`）。待ち時間は指数的に増えます |
| `reconnect.jitter` | `0.2` | 待ち時間のランダムなばらつき（0〜1） |
| `reconnect.max_retries` | `null` | 連続した接続失敗（ハンドシェイク失敗、または Boot が Accepted になる前に切れた接続）の上限。超えると充電器は再試行を止め、UI に **再試行停止** と表示され、接続 を押すまで再開しません。`null` = 無制限、`0` = 最初の失敗で停止 |

## ev_models

`session.ev_model` から参照するカスタム EV モデルです。同じ ID の組み込みプリセットより優先されます。
組み込みプリセット: `kei-20kwh`（20 kWh、3 kW、15 A、単相）、`compact-40kwh`（40 kWh、6 kW、30 A、単相）、
`midsize-60kwh`（60 kWh、11 kW、16 A、三相）。

| フィールド | 既定値 | 意味 |
|---|---|---|
| `id` | 必須 | 一意な ID（引用符付き文字列） |
| `capacity_kwh` | 必須 | 電池容量 |
| `max_ac_kw` | 必須 | AC 充電の最大電力 |
| `max_current_a` | `32.0` | 1 相あたりの最大電流 |
| `max_dc_kw` | なし | 将来の互換性のために受け付けます。v0.1 では DC 充電をモデル化していません |
| `phases` | `1` | 車載充電器が使う相数（1、2、3） |
| `min_current_a` | `6.0` | 1 相あたりこの電流を下回ると EV は充電できません（≤ `max_current_a`） |
| `efficiency` | `0.9` | 電池に届くエネルギーの割合（0〜1] |
| `curve` | 組み込み | `[SoC %, 最大値に対する割合]` の組のリスト（充電のテーパー） |

## chargers

| フィールド | 既定値 | 意味 |
|---|---|---|
| `id` | 必須 | Charge Point ID（引用符付き文字列）。URL の最後のパスセグメントと Basic 認証のユーザー ID に使うため、`:` は使えません。`bulk` は予約済みです |
| `csms_url` | `csms_defaults.url` | 充電器ごとの CSMS URL |
| `ocpp_version` | `csms_defaults.ocpp_version` | 充電器ごとの OCPP バージョン（`"1.6"` または `"2.0.1"`）。1 つのサイトで混在できます |
| `password` | なし | Basic 認証のパスワード（セキュリティプロファイル 1 / 2） |
| `ca_file` | システムの信頼ストア | `wss://` で CSMS の証明書を検証する追加の CA バンドル（PEM） |
| `client_cert_file` | なし | セキュリティプロファイル 3：充電器の TLS クライアント証明書（PEM、チェーン可） |
| `client_key_file` | なし | セキュリティプロファイル 3：その秘密鍵（PEM、暗号化なし） |
| `verify_tls` | `true` | `false` で証明書の検証を省略します（試験専用。警告をログに出力します） |
| `require_subprotocol` | `true` | `false` にすると、CSMS が `ocpp1.6` / `ocpp2.0.1` サブプロトコルを返さなくても接続を続けます |
| `vendor` | `"SmartEdge"` | BootNotification の `chargePointVendor`（最大 20 文字） |
| `model` | `"SIM-AC6"` | `chargePointModel`（最大 20 文字） |
| `serial` | なし | `chargePointSerialNumber`（最大 25 文字） |
| `firmware` | `"0.1.0"` | `firmwareVersion`（最大 50 文字） |
| `connectors` | `1` | コネクター数（1〜32）。すべてのコネクターが以下の電源設定と `max_power_w` を共有します。OCPP 2.0.1 では EVSE の数（各 1 コネクター） |
| `config_keys` | `{}` | OCPP 1.6: 設定キーの初期値（例: `HeartbeatInterval: 300`）。数値と真偽値は引用符なしで書けます。2.0.1 の充電器では使いません（`variables` を参照）。`AuthorizationKey` はここでは設定できません（`password` を使います）。Security Whitepaper のキー `SecurityProfile`（読取専用、使用中のプロファイル）、`CpoName`（既定は `vendor`）、`CertificateStoreMaxLength`、`AdditionalRootCertificateCheck`（`false`）もあります |
| `auth_mode` | `local_then_csms` | `local_only`（`id_tags` のみ）、`csms_only`（常に Authorize）、`local_then_csms`（まず `id_tags`、次に Authorize）、`accept_all` |
| `id_tags` | `[]` | ローカルで受け付ける ID タグ（各最大 20 文字、重複不可） |
| `boot_retry_s` | `60.0` | CSMS が interval 0 で Pending / Rejected を返したときの Boot 再送間隔 |
| `boot_max_retries` | `null` | Accepted 以外の BootNotification 応答（Pending・Rejected・無応答。Accepted まで再接続をまたいで数える）の上限。超えると接続を閉じ、`reconnect.max_retries` と同様に停止します。`null` = 無制限（OCPP どおり）。TriggerMessage による BootNotification は数えません |
| `max_power_w` | `6000.0` | 各コネクターのハードウェア上の最大電力 |
| `voltage_v` | `200.0` | 電源の公称電圧 |
| `phases` | `1` | 電源の相数（1、2、3） |
| `line_to_line` | `false` | `true` のとき、三相電源の `voltage_v` を線間電圧とみなします（日本の三相 200 V デルタなど）。相電圧 = `voltage_v / √3` |
| `ramp_up_w_per_s` | なし | 模擬時間 1 秒あたりの電力の上昇幅（W）。なし = ステップ変化 |
| `ramp_down_w_per_s` | なし | 電力の下降幅。なし = ステップ変化（制限値を下げると 1 tick 以内に反映） |
| `session.ev_model` | `compact-40kwh` | コネクターに接続する EV（プリセットまたは `ev_models` の ID） |
| `session.initial_soc` | `30.0` | 各トランザクション開始時の SoC（%） |
| `session.target_soc` | `90.0` | この SoC に達すると EV は電力を受け付けなくなります（SuspendedEV） |
| `template` | なし | 充電器テンプレート（組み込みまたはユーザー。[Excel とテンプレート](excel-templates.md) 参照）。この充電器が設定していないテンプレート項目はテンプレートの値 |
| `params` | `{}` | `{param}` プレースホルダを持つユーザーテンプレートのパラメータ |
| `meter_sample_interval_s` | キーの既定値 | `config_keys` で指定していない場合の `MeterValueSampleInterval` の既定値。2.0.1 では `SampledDataCtrlr.TxUpdatedInterval` の初期値 |
| `variables` | `[]` | OCPP 2.0.1 の充電器のみ: Device Model の値の上書きとユーザー定義変数（下記） |

### variables（OCPP 2.0.1） { #variables }

2.0.1 の充電器は `config_keys` の代わりに Device Model を持ちます。シミュレーターが動作に使う最小限の標準変数を
備えています。例: `OCPPCommCtrlr.HeartbeatInterval`、`SampledDataCtrlr.TxUpdatedInterval`（初期値は
`meter_sample_interval_s`、未指定なら 60）と `TxUpdatedMeasurands`、`AuthCtrlr.AuthorizeRemoteStart`、
`TxCtrlr.StopTxOnInvalidId`、`OCPPCommCtrlr.MessageAttempts[TransactionEvent]`、
`DeviceDataCtrlr.ItemsPerMessage[GetReport|GetVariables|SetVariables]`、認証制御の
`AuthCtrlr.Enabled` / `LocalPreAuthorize` / `LocalAuthorizeOffline` / `OfflineTxForUnknownIdEnabled` /
`DisableRemoteAuthorization`（ReadWrite。初期値は `auth_mode` から決まり、SetVariables による変更は即時に反映）、
`SmartChargingCtrlr.Enabled`（ReadWrite、動作には反映しない）と `NotifyChargingLimitWithSchedules`（既定値
`false`）、`SecurityCtrlr.SecurityProfile` / `Identity` / `BasicAuthPassword`（新しいパスワードは受け付けられ、
充電器はそのパスワードで再接続する。メモリ上のみ）、必須の ChargingStation / EVSE / Connector の変数
（`Available`、`AvailabilityState`、EVSE の `Power` と `SupplyPhases`）。OCPP 2.0.1 が必須とするその他の変数
（例: `OCPPCommCtrlr.OfflineThreshold`、`TxCtrlr.EVConnectionTimeOut`、
`SmartChargingCtrlr.Entries[ChargingProfiles]`）は、シミュレーターの動作を表す値で読み取り専用として報告されます。CSMS は GetVariables / SetVariables で読み書きし、GetBaseReport で一覧を取得します。
`GET /api/chargers/{id}/configuration` では `variables` に表示されます。SetVariables による変更は YAML
ファイルには書き戻されません。

`variables` の各エントリは 1 つの変数を指します: `component`、任意の `component_instance`・`evse`・`connector`、
`variable`、任意の `variable_instance`。

- 標準変数を指すエントリは、その `value`（`Actual` 属性）を設定します。他の設定から導出される変数
  （`auth_mode`、充電器 ID、CSMS URL、EVSE の状態、トランザクションの開始 / 終了点）は設定できません。
- それ以外のエントリはユーザー定義変数で、`data_type`（`string`、`decimal`、`integer`、`dateTime`、`boolean`、
  `OptionList`、`SequenceList`、`MemberList`）と `value` が必要です。任意: `mutability`（既定 `ReadWrite`、
  `ReadOnly`、`WriteOnly`）、`unit`、`min_limit` / `max_limit`（数値の範囲。`string` では長さ）、`values_list`
  （リスト型では必須）、`target` / `min_set` / `max_set`（追加の属性）、`reboot_required`（SetVariables が
  `RebootRequired` を返す）、`persistent`、`constant`、`summary`（SummaryInventory レポートに含める）。

```yaml
chargers:
  - id: "CP201"
    ocpp_version: "2.0.1"
    variables:
      - component: OCPPCommCtrlr
        variable: HeartbeatInterval
        value: 120
      - component: DeviceDataCtrlr
        variable: ItemsPerMessage
        variable_instance: GetReport
        value: 10
      - component: VendorCtrlr
        variable: ChargeMode
        data_type: OptionList
        values_list: [Eco, Fast]
        value: Eco
```

値は設定の読み込み時に検証されます（型、範囲、`values_list`）。1.6 の充電器に `variables` を書くとエラーです。

Web UI では、2.0.1 の充電器の **変数（Device Model）** 表にこれらの値を参照のみで表示します。

![OCPP 2.0.1 充電器の変数](../assets/screenshots/ja/variables.png)

1.6 / 2.0.1 混在の例は [`configs/mixed-site.yaml`](https://github.com/smartedge-tech-dev/sme-ocpp-sim/blob/main/configs/mixed-site.yaml) です。

未対応: 2.0.1 の `evses` ツリー（EVSE あたり複数コネクター）、`behaviors`。充電器テンプレートは [Excel とテンプレート](excel-templates.md) を参照。


## mini_csms { #mini_csms }

組み込みの簡易 CSMS です。[Mini CSMS](mini-csms.md) を参照。既定では無効です。

| 項目 | 既定値 | 意味 |
|---|---|---|
| `enabled` | `false` | Mini CSMS の WebSocket サーバーを起動する |
| `host` | `"127.0.0.1"` | 待受アドレス（他ホストや Docker からは `"0.0.0.0"`） |
| `port` | `9000` | 待受ポート |
| `versions` | `["1.6", "2.0.1"]` | 受け付ける OCPP バージョン（サブプロトコル交渉） |
| `boot.status` | `Accepted` | BootNotification の応答: `Accepted`、`Pending`、`Rejected` |
| `boot.interval` | `300` | Heartbeat 間隔（Accepted）または再試行待ち（Pending / Rejected）、秒 |
| `auth.basic` | `false` | `true` = Security Profile 1: HTTP Basic 認証を必須にする |
| `auth.credentials` | `[]` | ステーションごとの `{id, password}`。`basic: true` ではそれ以外の ID・パスワードを拒否（HTTP 401）。Web UI ではマスク表示 |
| `authorize.default` | `Accepted` | ID タグ / ID トークンに返す認可状態 |
| `authorize.tags` | `{}` | タグごとの上書き（例 `{"BAD": "Blocked"}`。2.0.1 専用の状態は 1.6 には `Invalid` で返す） |
| `strict` | `false` | スキーマに合わないペイロードに CALLERROR を返す（既定: 応答して警告を記録） |
| `call_timeout_s` | `30` | ステーションへ送る CALL のタイムアウト |
| `frame_log_size` | `5000` | Mini CSMS 専用フレームログの保持数 |
| `plan` | `[]` | 予定 CALL: `at_s`、`after`（`boot` / `connect`）、`action`、`payload`、任意で `charger`（パターン）と `ocpp_version` |

`enabled`、`host`、`port` を変えると Mini CSMS は再起動します（ステーションは再接続）。その他の変更は即時反映されます
（計画の変更は次の接続から）。ポートを確保できない場合はイベントログとパネルに表示されます。
