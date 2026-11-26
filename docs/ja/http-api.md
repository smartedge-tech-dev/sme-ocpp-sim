# HTTP API

Web UI はテストスクリプトからも呼び出せる同じ HTTP API を使っています。ベース URL: `http://127.0.0.1:8082`。
エラー: 存在しない充電器は `404`、不正な入力や現在の状態では実行できない操作は `400`（リクエストの検証エラーも `400`）です。
CSMS が時間内に応答しない DataTransfer は `504`、CALLERROR で応答された場合は `502` になります。

成功したリクエストは `{"ok": true, ...}` を返します（GET ルートはデータそのものを返します）。

## 共通ルート

| メソッド | パス | 意味 |
|---|---|---|
| GET | `/api/version` | バージョン |
| GET | `/api/state` | 状態のスナップショット（充電器、コネクター、値） |
| GET / PUT | `/api/config` | 現在の設定（PUT で新しい設定を適用。再接続するのは変更した充電器のみ） |
| POST | `/api/config/save` | 設定を YAML ファイルへ書き戻す（[設定](configuration.md#saving-from-the-web-ui) を参照）。書き込めない場合は `500` |
| POST | `/api/simulation` | 実行 / 一時停止と時間倍率: `{"running": true, "time_scale": 10}`（どちらも省略可。その他のフィールドは拒否） |
| GET / PUT | `/api/csms-defaults` | `csms_defaults` ブロック（PUT で置き換えて再読み込み） |
| GET / PATCH | `/api/chargers/{id}/settings` | 1 台の充電器の接続 / 認証設定（下記参照） |
| GET | `/api/chargers/{id}/configuration` | GetConfiguration から見える実行時の設定キー |
| POST | `/api/devices` | 充電器を追加 |
| GET / PUT | `/api/devices/{id}/config` | 1 台の充電器の設定（PUT は項目全体を置き換え） |
| POST | `/api/devices/{id}/duplicate` | 充電器を複製（ID は連番で増加） |
| DELETE | `/api/devices/{id}` | 充電器を削除 |

パスワードは常に `********` で返します。マスクのまま送り返すと保存済みのパスワードが維持されます。PUT は項目全体を
置き換えるため、`password` を省略するとパスワードなしになります。

`/api/chargers/{id}/settings` は `csms_url`、`ocpp_version`、`password`、`ca_file`、`verify_tls`、`id_tags`、
`config_keys` と、実際に使われる CSMS URL / OCPP バージョン（`effective`）を扱います。PATCH は含まれる
フィールドだけを変更し、`null` はその設定を解除します（`id_tags` / `config_keys` は空になります。`verify_tls`
は true か false のみ）。パスワードのマスクは上記と同じく送り返せます。変更は設定の再読み込みで反映され、
ファイルへは `POST /api/config/save` のときだけ書き込まれます。

## 充電器の操作

| メソッド | パス | ボディ |
|---|---|---|
| POST | `/api/chargers/{id}/connect` | なし（接続ループを開始） |
| POST | `/api/chargers/{id}/disconnect` | なし（`connect` まで切断状態を維持。トランザクションはネットワーク断と同様に継続） |
| POST | `/api/chargers/{id}/plug` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/unplug` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/swipe` | `{"connector": 1, "id_tag": "04A1B2C3"}` — `id_tag` は必須 |
| POST | `/api/chargers/{id}/start` | `{"connector": 1, "id_tag": "04A1B2C3"}` — プラグを挿してスワイプ。`id_tag` の既定は最初に設定されたタグ（ない場合は `400`）。トランザクション中または故障中のコネクタは `400` |
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
| GET | `/api/frames?since=&until=&charger=&action=&dir=&limit=` | `since < seq <= until` の OCPP フレーム。充電器・アクション・方向で絞り込み |
| GET | `/api/frames.jsonl?...` | 同じ内容を JSON Lines でストリーミングダウンロード（`limit` なしならバッファ内の該当フレームすべて） |

`/api/frames` は `{"frames": [...], "last_seq": n, "truncated": bool}` を返します。該当フレームのうち新しい
`limit` 件まで（既定 500、最大 5000）を古い順に並べます。`truncated: true` は古い該当フレームが省かれたことを
示します。`last_seq` はログ全体の最新シーケンス番号です（絞り込みは無関係）。

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
