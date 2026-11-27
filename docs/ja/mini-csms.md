# Mini CSMS

SME OCPPSim には簡易 CSMS が組み込まれています。同じプロセス内の WebSocket サーバーで、Web UI に専用パネルがあります。
バックエンドなしでシミュレーターを試す（本ツールの充電器が本ツールの CSMS に接続する）、デモ、あるいは試験対象の実機充電器の
CSMS として使うためのものです。

意図的に小さく作っています。ステーションが送るすべてのメッセージに有効な最小限の応答を返し、得た情報を記録し、JSON エディターで
入力した CALL や計画（plan）に登録した CALL を送信します。データベース、ユーザー管理、エネルギー管理ロジックはありません。

## クイックスタート

```bash
sme-ocpp-sim run -c configs/mini-csms-demo.yaml
```

[デモ設定](https://github.com/smartedge-tech-dev/sme-ocpp-sim/blob/main/configs/mini-csms-demo.yaml) は Mini CSMS を `ws://127.0.0.1:9000/ocpp` で起動し、そこへ接続する充電器を 2 台（OCPP 1.6 と 2.0.1）
起動します。各充電器の BootNotification が受理されてから 30 秒後に、計画が SetChargingProfile（TxDefaultProfile、3 kW）を
送信し、充電器の Smart Charging パネルに表示されます。**シミュレーション → Mini CSMS** でステーションを確認できます。

`--mini-csms`、`--csms-host`、`--csms-port` で、設定を編集せずにその実行だけ有効化・待受先の変更ができます。

## 機能

| 項目 | 動作 |
|---|---|
| バージョン | OCPP 1.6J と 2.0.1（サブプロトコル `ocpp1.6` / `ocpp2.0.1`）。`mini_csms.versions` で限定可能 |
| サブプロトコル | ステーションが提示した中で有効な最初のもの。該当がなければ `Sec-WebSocket-Protocol` なしでハンドシェイクを完了し、直ちに切断（コード 1002）。拒否はパネルに一覧表示 |
| ステーション ID | URL パスの最後のセグメント: `ws://host:9000/<任意>/<ステーション ID>` |
| セキュリティ | Profile 0（認証なし）または Profile 1（HTTP Basic 認証、`auth.basic: true`）。TLS サーバーはないため Profile 2 / 3 は非対応 |
| BootNotification | `boot.status`（`Accepted` / `Pending` / `Rejected`）と `boot.interval` |
| 認可 | `authorize.default` と、タグごとの上書き `authorize.tags`（Authorize、StartTransaction、StopTransaction、TransactionEvent） |
| トランザクション | 1.6 の StartTransaction には 1 つのカウンターから transactionId を払い出し、2.0.1 の TransactionEvent Started / Updated / Ended は transactionId で追跡 |
| その他のアクション | そのバージョンの CP → CSMS アクションすべてに有効な最小応答（下記）。未知のアクションは CALLERROR `NotImplemented` |
| 許容性 | スキーマに合わないペイロードにも応答し、警告として記録。`strict: true` では CALLERROR を返す |
| 再接続 | 同じ ID で再接続したステーションは古い接続を置き換える |

判断を伴う固定応答: DataTransfer → `UnknownVendorId`、SignCertificate → `Rejected`（CA なし）、2.0.1 の
Get15118EVCertificate / GetCertificateStatus → `Failed`、NotifyEVChargingNeeds → `Rejected`、
NotifyEVChargingSchedule → `Accepted`。その他の応答は空オブジェクトです。すべての応答はテストで `ocpp` ライブラリの
レスポンススキーマに対して検証しています。

## パネル

**シミュレーション → Mini CSMS** に表示されるもの:

- 待受 URL（または停止している理由）と拒否した接続（パスワード誤り、未対応サブプロトコル）
- ステーション一覧: バージョン、Boot 状態、最新 Heartbeat、実行中トランザクション（各 ID の最新接続は切断後も残る）
- 選択したステーションの BootNotification、コネクタ / EVSE ごとの最新状態、トランザクション、最近のメーター値、計画の状態、
  ペイロード警告、CSMS 側から見たフレーム
- **JSON エディター**: CSMS → CP アクションを選び、ペイロードを入力して送信。先にそのバージョンのスキーマで検査し、不一致なら
  該当フィールドを表示して送信しません。応答（CALLRESULT または CALLERROR）はエディターの下に表示されます。

## 計画（plan）

`mini_csms.plan` には、接続ごとに自動送信する CALL を並べます:

```yaml
mini_csms:
  enabled: true
  plan:
    - at_s: 30              # トリガーからの秒数
      after: boot           # boot = 接続後最初に受理した BootNotification、connect = WebSocket 接続時
      ocpp_version: "1.6"   # このバージョンのステーションのみ（省略: 有効な全バージョン）
      charger: "CP-*"       # 一致するステーション ID のみ（省略: 全ステーション）
      action: SetChargingProfile
      payload:
        connectorId: 1
        csChargingProfiles:
          chargingProfileId: 1
          stackLevel: 0
          chargingProfilePurpose: TxDefaultProfile
          chargingProfileKind: Relative
          chargingSchedule:
            chargingRateUnit: W
            chargingSchedulePeriod: [{ startPeriod: 0, limit: 3000 }]
```

ペイロードは電文の形（camelCase）で書き、設定の読込時にスキーマで検査されます。`ocpp_version` がない場合は有効な全バージョンに
合う必要があるため、バージョン固有のアクションには指定してください。各項目は接続ごとに 1 回送信され、再接続すると再度実行されます。

## 実機ステーションの試験

`host: "0.0.0.0"`（または `--csms-host 0.0.0.0`）にして、ステーションの接続先を `ws://<この PC>:9000/ocpp` にします。
Docker ではポートも公開します（`-p 9000:9000`）。Profile 1 ではステーションを `auth.credentials` に登録します。

!!! warning "公開範囲"
    `host: "0.0.0.0"` かつ `auth.basic: false` では、ポートに届く誰もが任意のステーション ID で接続でき、応答を受け取れます
    （起動時に警告をログ出力します）。Mini CSMS には TLS（プロファイル 2 / 3）もレート制限もありません。信頼できるネットワーク
    でのみ使い、`127.0.0.1` 以外で待ち受けるときは Basic Auth を有効にしてください。固定の上限: 同時接続 500（超えるハンドシェイク
    は HTTP 503）、記憶するステーション 200（切断済みの古いものから忘れます）。Basic Auth の失敗は原因によらず同じ `401` と
    `WWW-Authenticate` です（原因はパネルの拒否一覧にだけ表示）。CSMS が送る秘密値（1.6 `AuthorizationKey`、2.0.1
    `BasicAuthPassword`、SetNetworkProfile の VPN / APN パスワード）はフレームログと送信記録では `********` と表示され、
    ステーションには実際の値が届きます。

Mini CSMS は適合性試験用の本物の CSMS の代わりにはなりません。本プロジェクトの CI も、独立実装である `ocpp` ライブラリの
対向を試験相手として残しています。

設定項目: [mini_csms](configuration.md#mini_csms)。HTTP ルート: [Mini CSMS API](http-api.md#mini-csms)。
