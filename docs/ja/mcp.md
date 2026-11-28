# MCP・ヘルスチェック・ログ

## MCP エンドポイント

SME OCPPSim は Web ポートで [MCP](https://modelcontextprotocol.io/) エンドポイントを提供します。AI アシスタント
（Claude Code、Claude Desktop、その他の MCP クライアント）から、充電器の一覧取得、プラグ接続、トランザクションの開始・停止、
故障やネットワーク障害の注入、フレームログの参照ができます。

- URL: `http://127.0.0.1:8082/mcp`（MCP streamable HTTP、ステートレス、JSON 応答。Web UI と同じポート）
- 認証はありません（Web UI・[HTTP API](http-api.md) と同じ）。既定の `--host 127.0.0.1` では `Host` / `Origin`
  ヘッダーがループバックのリクエストだけを受け付けます（DNS リバインディング対策）。`--host 0.0.0.0`（Docker、LAN）
  ではポートに届く誰からでも利用できます。

DNS リバインディング対策は `/mcp` だけに適用されます。HTTP API の他のルートは Web UI と同じく対策がないため、
ループバックか信頼できるネットワークにバインドしてください。

Claude Code:

```bash
claude mcp add --transport http ocpp http://127.0.0.1:8082/mcp
```

stdio だけに対応したクライアント（Claude Desktop など）は `npx mcp-remote http://127.0.0.1:8082/mcp`
のようなブリッジを使えます。

各ツールは [HTTP API](http-api.md) のルートをプロセス内で 1 つ呼び出します。検証・エラー（HTTP エラーはそのメッセージの
ツールエラーになります）・効果は Web UI のボタンと同じです。充電器のツールは充電器 ID を `cid` で受け取り、
コネクタ番号は 1 から始まります。

| ツール | HTTP ルート | 内容 |
|---|---|---|
| `get_version` | `GET /api/version` | 製品とライブラリのバージョン |
| `get_state` | `GET /api/state` | 全充電器とその状態（Web UI のスナップショット） |
| `export_config` | `GET /api/config` | 実行中の設定（YAML、パスワードはマスク） |
| `set_simulation` | `POST /api/simulation` | 実行 / 一時停止、時間倍率 |
| `get_charger` | `GET /api/chargers/{id}` | 1 台の充電器、故障コードと CSMS アクション |
| `get_charger_settings` | `GET /api/chargers/{id}/settings` | 接続・認可の設定 |
| `get_charging_profiles` | `GET /api/chargers/{id}/profiles` | インストール済みの充電プロファイル |
| `get_composite_schedule` | `GET /api/chargers/{id}/composite` | 合成スケジュール（ローカル計算） |
| `get_frames` | `GET /api/frames` | フレームログ（絞り込み `charger`、`action`、`dir`） |
| `get_impairments` | `GET /api/chargers/{id}/impairments` | 障害注入の設定 |
| `connect_charger` / `disconnect_charger` | `POST /api/chargers/{id}/connect` / `disconnect` | 接続ループの開始 / 停止 |
| `plug_in` / `unplug` | `POST /api/chargers/{id}/plug` / `unplug` | EV ケーブル |
| `authorize` | `POST /api/chargers/{id}/swipe` | ID タグの提示（開始 / 停止） |
| `start_transaction` / `stop_transaction` | `POST /api/chargers/{id}/start` / `stop` | プラグ接続 + ID タグ / ローカル停止 |
| `data_transfer` | `POST /api/chargers/{id}/datatransfer` | CSMS へ DataTransfer を送信 |
| `set_fault` / `clear_fault` | `POST /api/chargers/{id}/fault` / `clear-fault` | コネクタの故障 |
| `set_external_limit` | `POST /api/chargers/{id}/external-limit` | 外部充電制限（2.0.1） |
| `set_impairments` | `PUT /api/chargers/{id}/impairments` | コマンドブロック、遅延、定期切断 |
| `drop_connection` | `POST /api/chargers/{id}/drop` | 接続を今すぐ切断 |
| `send_frame` | `POST /api/chargers/{id}/send` | CALL または生フレームの注入（上級者向け） |
| `mini_csms_status` | `GET /api/csms` | 内蔵 [Mini CSMS](mini-csms.md) の状態 |
| `mini_csms_actions` | `GET /api/csms/actions` | Mini CSMS が送信できる CSMS → 充電器のアクション |
| `mini_csms_call` | `POST /api/csms/chargers/{id}/call` | Mini CSMS から CSMS → 充電器の CALL を送信 |

読み取り専用のツールには `readOnlyHint`、`disconnect_charger`・`drop_connection`・`set_impairments`・`send_frame`・`mini_csms_call` には `destructiveHint`
が付いているので、クライアントは呼び出し前に確認を求められます。

## ヘルスチェック

`GET /healthz` は Web サーバーの稼働中 `200` と `{"status": "ok", "name", "version", "common_version"}` を返します
（Docker / Kubernetes のプローブ用）。

```bash
curl -fsS http://127.0.0.1:8082/healthz
```

## JSON ログ

`--log-format json` はログを 1 行 1 つの JSON オブジェクト（`ts`、`level`、`logger`、`message`、追加フィールド、`exc`）
で出力します（ログ収集基盤向け）。Web サーバー自身のログも同じ形式になります。既定は `--log-format text` です。
`-v` / `--verbose` はどちらの形式でもデバッグレベルにします。

```bash
sme-ocpp-sim --log-format json
```
