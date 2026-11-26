# HTTP API

Web UI はテストスクリプトからも呼び出せる同じ HTTP API を使っています。ベース URL: `http://127.0.0.1:8082`。
エラー: 存在しない充電器は `404`、不正な入力や現在の状態では実行できない操作は `400`（リクエストの検証エラーも `400`）です。
CSMS が時間内に応答しない DataTransfer は `504`、CALLERROR で応答された場合は `502` になります。

## 共通ルート

| メソッド | パス | 意味 |
|---|---|---|
| GET | `/api/version` | バージョン |
| GET | `/api/state` | 状態のスナップショット（充電器、コネクター、値） |
| GET / PUT | `/api/config` | 現在の設定（PUT で新しい設定を適用。再接続するのは変更した充電器のみ） |
| POST | `/api/config/save` | 設定を YAML ファイルへ書き戻す（[設定](configuration.md#web-ui) を参照） |
| POST | `/api/simulation` | シミュレーションの実行 / 一時停止 |
| POST | `/api/devices` | 充電器を追加 |
| GET / PUT | `/api/devices/{id}/config` | 1 台の充電器の設定（PUT は項目全体を置き換え） |
| POST | `/api/devices/{id}/duplicate` | 充電器を複製（ID は連番で増加） |
| DELETE | `/api/devices/{id}` | 充電器を削除 |

パスワードは常に `********` で返します。マスクのまま送り返すと保存済みのパスワードが維持されます。PUT は項目全体を
置き換えるため、`password` を省略するとパスワードなしになります。

## 充電器の操作

| メソッド | パス | ボディ |
|---|---|---|
| POST | `/api/chargers/{id}/connect` | なし（接続ループを開始） |
| POST | `/api/chargers/{id}/disconnect` | なし（`connect` まで切断状態を維持。トランザクションはネットワーク断と同様に継続） |
| POST | `/api/chargers/{id}/plug` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/unplug` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/swipe` | `{"connector": 1, "id_tag": "04A1B2C3"}` |
| POST | `/api/chargers/{id}/start` | `{"connector": 1, "id_tag": "04A1B2C3"}` |
| POST | `/api/chargers/{id}/stop` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/fault` | `{"connector": 1, "error_code": "OtherError"}` — 1.6 の `ChargePointErrorCode`。実行中のトランザクションを理由 `Other` で終了し、`Faulted` を報告します |
| POST | `/api/chargers/{id}/clear-fault` | `{"connector": 1}` — Available / Preparing に戻します |
| POST | `/api/chargers/{id}/datatransfer` | `{"vendor_id": "...", "message_id": "...", "data": "..."}`（充電器 → CSMS） |
| POST | `/api/chargers/bulk/connect` | `{"ids": ["CP-0001", "CP-0002"]}`、`["CP-0001"]` または `{"all": true}` |
| POST | `/api/chargers/bulk/disconnect` | 同上 |

一括操作はアトミックではありません。充電器を 1 台ずつ処理し、エラーが起きた時点で停止します（それまでの充電器は変更済みです）。

## Smart Charging

| メソッド | パス | 意味 |
|---|---|---|
| GET | `/api/chargers/{id}/profiles?connector=` | インストール済みの充電プロファイル |
| GET | `/api/chargers/{id}/composite?connector=&duration=&unit=` | GetCompositeSchedule の応答をローカルで計算した結果（`connector` 0 = 充電器全体。別名 `evse` も可） |

## フレームログ

| メソッド | パス | 意味 |
|---|---|---|
| GET | `/api/frames?since=&charger=&action=&dir=&limit=` | シーケンス番号 `since` 以降の OCPP フレーム。充電器・アクション・方向で絞り込み |
| GET | `/api/frames.jsonl?...` | 同じ内容を JSON Lines でストリーミングダウンロード（`limit` なしならバッファ内の該当フレームすべて） |

フレームログに含まれるのは OCPP フレームだけで、HTTP ヘッダーやパスワードは含みません。

## WebSocket `/ws`

ライブ更新を受け取るクライアントは `ws://127.0.0.1:8082/ws` に接続します。

1. 最初のメッセージは `{"type": "snapshot", "data": ...}` で、内容は `GET /api/state` と同じです。
2. 続いて `charger_state`、`values`、`frame` がプッシュされます。それぞれ接続ごとに 1, 2, 3 ... と数える `push_seq` を持ちます。
3. エンジンのイベントは `{"type": "log", "event": {...}}` で届き、独自の `event.seq` で番号付けされます（`push_seq` なし）。
4. 設定を再読み込みすると、新しい `{"type": "snapshot"}` が送られます。

`push_seq` の欠番は、遅いクライアント向けのメッセージが破棄されたことを意味します。その中に状態のプッシュや再読み込みの
通知があった場合、次の状態プッシュは完全版（`"full": true`）になるため、状態は自動的に回復します（`/api/state` を
取り直しても構いません）。失われたフレームは `GET /api/frames?since=<最後に受け取った frame の seq>` で取得できます。
完全版の `charger_state` には *すべて* の充電器が含まれるので、充電器リストをそれで置き換えてください（含まれない
充電器は再読み込みで削除されたものです）。完全版の `values` も同様にすべての値を置き換えます。2 つの完全版プッシュの順序には依存しないでください。
