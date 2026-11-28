# トレースの記録と解析

**トレース**は、実行中のすべての OCPP フレームと、その周辺のイベント（接続、オペレーター・シナリオの操作、
時計の変更）を記録したファイルです。試験の証跡の保存、実行同士の比較、`analyze` による集計レポートに使います。
CSMS へのトレース再生（replay）は今後のリリースで追加予定で、フォーマットは再生に必要な情報をすでに含んでいます。

## フォーマット `ocppsim-trace/1`

JSON Lines（1 行 1 オブジェクト）で、各行に `type` があります。

| `type` | 内容 |
|---|---|
| `header` | 必ず 1 行目。`format: "ocppsim-trace"`、`version: 1`、`tool_version`、`created`（実時刻）、`time_scale`、`sim_start`（記録開始時のシミュレーション時刻）、`masked: true`、`part`、`chargers`（`id`、`ocpp_version`、`connectors`、ユーザー・パスワードを除いた `csms_url`） |
| `frame` | OCPP-J フレーム 1 件。フレームログと同じ項目（`seq`、`ts`、`charger`、`direction`、`message_type`、`uid`、`action`、`raw`、`note`）と `sim_t`（記録開始からのシミュレーション秒）。`direction` は充電器から見た向き（`tx` = CSMS へ送信、`rx` = 受信） |
| `event` | `ts`、`sim_t`、`charger`（全体イベントは `null`）、`event`、`detail`。イベント：`connected`、`disconnected`（`detail.reason`）、`connect_failed`、オペレーター・シナリオの各操作（`plug`、`unplug`、`swipe`、`start`、`stop`、`fault`、`drop`、`send` など。`detail.since_seq` = 操作直前のフレーム seq）、`clock`（倍速・一時停止の変更）、`chargers`（リロードで充電器一覧が変わった） |
| `footer` | 正常に閉じた記録の最終行。`frames`、`events`、`dropped`（ディスクが追いつかず失われた行数）、`next`（次のパート。ローテーション参照） |

秘密情報はフレームログと同じくマスクされます（例：ChangeConfiguration の `AuthorizationKey` の値は
`********`）。トレースにパスワードは含まれません。将来の再生では認証情報を引数で渡します。

ヘッダーのないファイル（`/api/frames.jsonl` のフレームログ出力など）は **バージョン 0** として読み、
全行をフレームとして扱います。`analyze` はどちらも受け付けます。

## 記録

| 方法 | 記録対象 | ファイル |
|---|---|---|
| 設定 `trace.path` | 全充電器、シミュレーター起動から停止まで | `trace.path`（上書き） |
| `sme-ocpp-sim --trace FILE` | 同上（`trace.path` を上書き） | `FILE` |
| Web UI のフレームログ欄 **トレース記録** ボタン、`POST /api/trace/start` | 全充電器、**記録停止** / `POST /api/trace/stop` まで | トレースディレクトリ |
| Web UI / API から開始したシナリオ | シナリオの充電器 | トレースディレクトリの `scenario-<名前>-<時刻>.jsonl`。判定結果の `trace` に記載 |
| `sme-ocpp-sim scenario run ... --trace FILE` | シナリオの充電器 | `FILE` |

```yaml
trace:
  path: traces/run.jsonl   # null（既定）= 起動時からは記録しない
  rotate_mb: 100           # このサイズで次のパートへ。null = ローテーションしない
```

**トレースディレクトリ**は、`trace.path` があればそのディレクトリ、なければ設定ファイルと同じ場所の `traces/`
（Windows exe は `~/.sme-ocpp-sim/traces`）です。HTTP API はこのディレクトリの中だけを読み書きします。

**ローテーション**：パートが `rotate_mb` に達すると、`next` に次のパート名を書いたフッターで閉じます
（`run.jsonl` → `run.1.jsonl` → `run.2.jsonl` …）。各パートは自身のヘッダーで始まります。`analyze` に
最初のパートを渡すと、連なったパートをすべて読みます。

記録がシミュレーターを遅くすることはありません。行は上限付きキューを経てバックグラウンドで書き込まれ、
ディスクが追いつかない場合は破棄してフッターの `dropped` に数えます。

## analyze

```
sme-ocpp-sim analyze traces/run.jsonl                 # テキストの集計
sme-ocpp-sim analyze traces/run.jsonl --json          # ocppsim-analysis/1 JSON
sme-ocpp-sim analyze frames.jsonl --charger CP001 --ocpp-version 2.0.1
```

`--ocpp-version` は v0 ファイルにない充電器のバージョンを指定します（既定：アクション名から判定）。
充電器ごとのレポート内容：

- **アクション**（方向別）：CALL 数、CALLRESULT、エラーコード別 CALLERROR、応答なし CALL、応答遅延
  p50 / p95 / max（ms。CALL と応答はメッセージ ID で対応付け）。
- **接続**：接続・切断回数と接続時間（v1 トレース）、BootNotification の結果の推移、Heartbeat 間隔の分布。
- **トランザクション**：開始・終了、時間、Δ電力量（Wh）、終了理由。OCPP 2.0.1 / 2.1 は取引ごとの `seqNo`
  の欠番・重複。
- **プロトコル異常**：解析できないフレーム、CALL のない応答、重複したメッセージ ID、同じ側の CALL が未応答の
  まま送られた CALL。
- **Smart Charging**：CSMS からの SetChargingProfile / ClearChargingProfile / UpdateDynamicSchedule と
  その応答ステータスの時系列。

解析はフレームとイベントだけを読み、シミュレーターの状態は参照しないため、実機のトレースにも使えます。
グラフは出力しません。JSON から作成してください。

## HTTP API

| メソッド | パス | 意味 |
|---|---|---|
| GET | `/api/trace` | 記録状態（`recording`、`path`、`frames`、`events`、`dropped`）とトレースディレクトリ |
| POST | `/api/trace/start` | ボディ `{"file": "run1.jsonl"}`（省略可。既定 `trace-<時刻>.jsonl`）。記録中は 409 |
| POST | `/api/trace/stop` | 停止してフッターを書く。記録していなければ 409 |
| GET | `/api/trace/files` | トレースディレクトリのファイル一覧（新しい順） |
| GET | `/api/trace/files/{file}` | トレースファイルのダウンロード |
| GET | `/api/trace/files/{file}/analyze?charger=&format=json\|text` | トレースファイルの解析 |
| GET | `/api/frames/analyze?<フレームの絞り込み>&format=json\|text` | メモリ上のフレームログの解析（接続イベントなし） |
| GET | `/api/frames.jsonl?format=v1` | v1 ヘッダー付きのフレームログ出力（既定は従来どおり） |

MCP ツール：`get_trace`、`start_trace`、`stop_trace`、`analyze_frames`。
