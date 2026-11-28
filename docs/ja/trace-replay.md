# トレースの記録・解析・リプレイ

**トレース**は、実行中のすべての OCPP フレームと、その周辺のイベント（接続、オペレーター・シナリオの操作、
時計の変更）を記録したファイルです。試験の証跡の保存、実行同士の比較、`analyze` による集計レポートに使います。
`replay` で CSMS に対して再生し、CSMS を確認することもできます。

## フォーマット `ocppsim-trace/1`

JSON Lines（1 行 1 オブジェクト）で、各行に `type` があります。

| `type` | 内容 |
|---|---|
| `header` | 必ず 1 行目。`format: "ocppsim-trace"`、`version: 1`、`tool_version`、`created`（実時刻）、`time_scale`、`sim_start`（記録開始時のシミュレーション時刻）、`masked: true`、`part`、`chargers`（`id`、`ocpp_version`、`connectors`、ユーザー・パスワードを除いた `csms_url`） |
| `frame` | OCPP-J フレーム 1 件。フレームログと同じ項目（`seq`、`ts`、`charger`、`direction`、`message_type`、`uid`、`action`、`raw`、`note`）と `sim_t`（記録開始からのシミュレーション秒）。`direction` は充電器から見た向き（`tx` = CSMS へ送信、`rx` = 受信） |
| `event` | `ts`、`sim_t`、`charger`（全体イベントは `null`）、`event`、`detail`。イベント：`connected`、`disconnected`（`detail.reason`）、`connect_failed`、オペレーター・シナリオの各操作（`plug`、`unplug`、`swipe`、`start`、`stop`、`fault`、`drop`、`send` など。`detail.since_seq` = 操作直前のフレーム seq）、`clock`（倍速・一時停止の変更）、`chargers`（リロードで充電器一覧が変わった） |
| `footer` | 正常に閉じた記録の最終行。`frames`、`events`、`dropped`（ディスクが追いつかず失われた行数）、`next`（次のパート。ローテーション参照） |

秘密情報はフレームログと同じくマスクされます（例：ChangeConfiguration の `AuthorizationKey` の値は
`********`）。トレースにパスワードは含まれません。[リプレイ](#replay)では認証情報を引数で渡します。

ヘッダーのないファイル（`/api/frames.jsonl` のフレームログ出力など）は **バージョン 0** として読み、
全行をフレームとして扱います。`analyze` はどちらも受け付けます。

### ヘッダーの充電器エントリとフッター

`chargers` の各エントリには、`id`、`ocpp_version`、`connectors`、`csms_url` のほか、リプレイが充電器を再構築するための
`vendor`、`model`、`serial`、`security_profile`、`subprotocol`（WebSocket サブプロトコル）、`path`（充電器 ID を含む
WebSocket パス。例 `/ocpp/CP001`）、`ev_model`、`seed`（`traffic.seed`。車両の乱数列も決める）、`impairments`（開始時の
充電器の障害注入設定）を記録します。秘密情報は含みません。`csms_url` からはユーザー・パスワード・クエリ・フラグメントを
除去し（解析できない URL は `<invalid url>`）、`connected` イベントの `detail.path` は接続パスのみです。これらは追加項目で、
フォーマットのバージョンは 1 のまま、リーダーは未知の項目を無視します。

**フッターは累積値です。** フッターの `frames`、`events`、`dropped` はそのパートではなく、そのフッターまでの記録全体の
合計です。リーダーは最後のフッターを採用します（パートごとに足すと二重計上になります）。フッターには `error`
（次のパートを作れなかったなど、記録が途中で終了した理由。このとき `next` は `null`）が付くことがあります。
`analyze` は、最後のフッターがない・`error` がある・`next` のパートファイルがない場合に `complete: false` を報告します。

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

相対パスの `trace.path`（と相対パスの `--trace FILE`）は、設定ファイルの他のファイルパス（`ca_file`、
`client_cert_file`）と同じく、設定ファイルのディレクトリではなくプロセスの作業ディレクトリ基準で解決されます。
`--trace` は `trace.path` を書き換えないので、Web UI が書き戻す設定ファイルの値は変わりません。HTTP API（と MCP の
`start_trace`）はトレースディレクトリにのみ書き込み、既存のファイル名は 409 で拒否します。CLI の `--trace FILE` は
任意のパスを受け付け、上書きします。

ローテーション中に次のパートを開けなかった場合、`next: null` と `error` 付きのフッターで記録を終了します。
書き込みスレッドはキューが空になるたびに、また最低でも 1 秒ごとにフラッシュするため、記録中のファイルも読めます。

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
  まま送られた CALL（60 秒を超えて未応答の CALL、切断時に未応答だった CALL は代わりに「未応答」と数えます）。フレームとイベントは時刻順に処理し、`seq`・`ts`・`direction` のないフレーム行は読み飛ばして不正行に数えます。
- **Smart Charging**：CSMS からの SetChargingProfile / ClearChargingProfile / UpdateDynamicSchedule と
  その応答ステータスの時系列。

解析はフレームとイベントだけを読み、シミュレーターの状態は参照しないため、実機のトレースにも使えます。
グラフは出力しません。JSON から作成してください。

## リプレイ { #replay }

`replay` は、トレースの**1 台の充電器**を CSMS に対して再生します。その充電器が送った CALL（`direction: tx`）を新しい
WebSocket 接続で送り直し、今回の CSMS の応答をトレースに記録された応答と比較します。CSMS の更新後に記録済みの
セッションで CSMS を確認するときや、実機のトレースから不具合を再現するときに使います。

```
python -m sme_ocpp_sim replay run1.jsonl --csms ws://csms.example:9000/ocpp \
    [--charger CP001] [--as CP900] [--timing original|scale:N|asap] [--max-gap SECONDS] [--session N] [--password PW] \
    [--out report.json] [--trace replay1.jsonl] [--json] [--fail-on-diff]
```

`--csms` は充電器 ID を含まない CSMS の URL です（模擬充電器と同じく ID が末尾に付きます）。トレースに複数の充電器が
ある場合は `--charger` が必要です。OCPP バージョン（と WebSocket サブプロトコル）はトレースのヘッダーから取ります。
ヘッダーのない v0 ファイルでは action から判定するか、`--ocpp-version` で指定します。判定は推測です（2.0.1 の action や `transactionInfo`、`evse`、`eventType` などのペイロード形状）。**v0 トレースでは `--ocpp-version` を指定してください**。推測した場合は `notes` に記録されます。

### 再生される内容

リプレイは**充電器側**かつ**フレーム単位**です。CSMS が試験対象で、シミュレーターは記録された充電器を演じます。
シミュレーターのステーション状態機械は使いません。送るのは記録されたフレームで、2 回目の実行に必要な箇所だけを
書き換えます。

| 書き換え | ルール |
|---|---|
| uniqueId | CALL ごとに新しい値 |
| タイムスタンプ | ペイロードの任意の深さ（DataTransfer の `data` も含む）にある、完全な ISO 8601 日時の文字列をすべて、**CALL ごとに送信時に計算した**同じ量だけずらします。その CALL の最も早い時刻が「現在」になり、CALL 内の時刻の間隔は保たれます。そのため `scale:N` や `asap` でも時刻が未来へずれていきません。CSMS の CALL への記録済み応答は、応答する時点のずれ量を使います |
| 1.6 `transactionId` | 今回 CSMS が StartTransaction に返した ID で、以降のフレーム（StopTransaction、MeterValues）の記録上の ID を置き換えます |
| 2.0.1 / 2.1 `transactionId` | ID は充電器が生成するため、実行ごとの接尾辞 `-<replay id>` を付けます。36 文字に収まらない ID は先頭を残し、末尾を切り落とします。切り落とした ID 同士が同じになる場合は、元の ID 全体の短いハッシュを使い、どちらの場合も `notes` に記録します。`--no-tx-suffix` で記録どおりの ID を使います |
| `--as ID` | 別の充電器 ID で接続します（URL パスと Basic 認証のユーザー）。ペイロードは変えません |

CALL は順番に 1 件ずつ送ります。OCPP-J の規定どおり、次の CALL は応答またはタイムアウト（`--call-timeout`、既定 30 秒）
の後に送ります。2.1 の SEND フレームも順番どおり送り直します（応答はありません）。

接続イベントは再現しません。リプレイ全体で 1 本の接続を使い、トレース中の切断は繰り返しません。CSMS が接続を
閉じた場合はリプレイを止め、残りの CALL を `not_sent` として報告します。

### タイミング

| `--timing` | 2 つの CALL の間隔 |
|---|---|
| `original`（既定） | 記録どおりの間隔（前回の送信から数えます）。応答が遅いと次の CALL も遅れます |
| `scale:N` | 記録の間隔を N で割った値（`scale:10` = 10 倍速） |
| `asap` | 間隔なし。応答の直後に次の CALL を送ります |

`--max-gap SECONDS`（既定：なし。記録どおりの間隔）は、`original` と `scale:N` での CALL 間の各待ち時間に上限を設けます。長い休止のあるトレースで何時間も待たないようにするためです。レポートでは `timing` の隣に示されます。

### CSMS からの CALL

リプレイ中に CSMS が送る CALL（TriggerMessage、GetConfiguration など）には、トレースで充電器が返した応答を返します。
action と出現順で対応付けます（2 回目の GetConfiguration には 2 番目に記録された応答）。記録された応答を使い切ったら、
最後の応答を繰り返し使います。記録が CALLERROR ならその CALLERROR を返し、トレースで応答がなかった CALL には応答
しません。トレースに一度も応答のない action は `--unknown-call` に従います。`reject`（既定）は CALLERROR
`NotImplemented` を返し、`noreply` は応答しません。

### 認証情報

トレースはマスクされており、パスワードを含みません。認証情報は引数で渡します。`--password`（セキュリティプロファイル
1 / 2 の Basic 認証）、`wss://` 用の `--ca-file` または `--insecure`、プロファイル 3 用の `--client-cert` /
`--client-key` です。パスワードはレポートに出力されません。

### セッション・停止・制限

トレースには充電器の接続（`connected` イベント）が複数含まれることがあります。既定ではすべてを 1 本の接続にまとめて
再生し、その旨を note に記録します。`--session N`（1 始まり）で N 番目の接続だけを再生します。

`stop`（またはアプリの終了）は、実行中の CALL の応答待ちを打ち切ります。その CALL は `closed`、残りは `not_sent` として
報告されます。アプリ終了時は、実行中のリプレイをすべて停止し、記録にもフッターを書きます。レポートの `inbound` に残す
CSMS の CALL は最大 1000 件で、超過分は `summary.inbound_overflow` に数えます。

HTTP API で同時に実行できるリプレイは最大 4 件です（次の 1 件は 409）。また、このシミュレーターの充電器が使用中の
ID（`as_id`、または記録上の充電器 ID）は 409 で拒否します。`as_id` は充電器 ID と同じ規則に従います。`csms_url` は
`ws://` または `wss://` のみです。**API は呼び出し側が指定した任意のホストへ接続します**。ループバックにバインドするか、
認証の背後に置いてください。HTTP 経由の `call_timeout` は最大 300 秒です。

### レポートの notes

`notes` が空でないときは必ず確認してください。次の内容が入ります：不完全なトレース（パートの欠落）や記録時に捨てられた行
（CALL が足りない可能性）、推測した OCPP バージョン、充電器の複数セッション、切り詰め・ハッシュで解決した 2.0.1 の
`transactionId`、トレースに応答のない 1.6 の StartTransaction とトレース外で開始された取引の Stop / MeterValues、
そのまま送信・応答したマスク値（`********`、CSMS が検証する秘密値とは一致しません）、上限をかけた待ち時間、一覧の上限を
超えた CSMS の CALL。notes によって `result` は変わりません。

### レポート `ocppsim-replay/1`

`--out` は JSON レポートをファイルに書き、`--json` は標準出力に出します。既定の出力はテキストの要約です。`--trace` は
リプレイを新しいトレースファイルに記録します。そのヘッダーのエントリには `replay_of`（元のトレース、充電器、replay id）
が入ります。

| フィールド | 内容 |
|---|---|
| `id`、`charger`、`as`、`ocpp_version`、`csms_url`、`security_profile`、`timing` | 何をどう再生したか |
| `result` | `match`、`diff`（応答が 1 件以上異なる）、`error`（接続の失敗・切断、または停止。理由は `error`） |
| `summary` | `calls`、`sent`、`matched`、`differed`、`not_compared`、`timeouts`、`not_sent`、`inbound`（CSMS の CALL）、`inbound_not_in_trace` |
| `calls[]` | CALL ごとの `action`、トレース上の `seq`、`recorded` と `replayed` の結果、`match` と `diff` |
| `inbound[]` | CSMS の CALL ごとの `action`、`occurrence`、`answer`（`result`、`error <code>`、`not_implemented`、`no_reply`）、`in_trace`、`reused` |
| `notes` | 書き換えられなかったもの（例：1.6 の StartTransaction で今回 `transactionId` が返らなかった） |

結果は `result`（`idTagInfo.status` などの status 系フィールド付き）、`error`（CALLERROR の `code` 付き）、`timeout`、
`closed`、`not_sent` のいずれかです。トレースに応答がない場合、記録側の結果は `none` になり、その CALL は比較しません。
status 系フィールドとは `status` というキーと `Status` で終わるキーです。`currentTime` や `interval` など実行ごとに
変わるフィールドは比較しません。

差異があってもリプレイは失敗になりません。終了コードは 0 = 完了（差異があっても）、1 = `--fail-on-diff` 指定時の差異、
2 = エラー（トレースを読めない、オプションの誤り、接続の失敗・切断）です。

## HTTP API

| メソッド | パス | 意味 |
|---|---|---|
| GET | `/api/trace` | 記録状態（`recording`、`path`、`frames`、`events`、`dropped`）とトレースディレクトリ |
| POST | `/api/trace/start` | ボディ `{"file": "run1.jsonl"}`（省略可。既定 `trace-<時刻>.jsonl`）。記録中、またはファイルが既に存在する場合は 409 |
| POST | `/api/trace/stop` | 停止してフッターを書く。記録していなければ 409 |
| GET | `/api/trace/files` | トレースディレクトリのファイル一覧（新しい順） |
| GET | `/api/trace/files/{file}` | トレースファイルのダウンロード |
| GET | `/api/trace/files/{file}/analyze?charger=&format=json\|text` | トレースファイルの解析 |
| GET | `/api/frames/analyze?<フレームの絞り込み>&format=json\|text` | メモリ上のフレームログの解析（接続イベントなし） |
| GET | `/api/frames.jsonl?format=v1` | v1 ヘッダー付きのフレームログ出力（既定は従来どおり） |
| POST | `/api/replay` | トレースディレクトリのトレースファイルを再生します。ボディ `{"file", "csms_url", "charger", "as_id", "timing", "max_gap", "session", "password", "ocpp_version", "unknown_call", "tx_suffix", "call_timeout", "verify_tls"}`（`file` と `csms_url` は必須）。模擬充電器ではなく専用の接続で動き、トレースディレクトリの `replay-<id>.jsonl` に記録されます。`id` を返します |
| GET | `/api/replay` | このアプリ起動中のリプレイの一覧 |
| GET | `/api/replay/{id}` | 状態（`running` / `done`、`sent` / `total`）と、完了後は `report` |
| POST | `/api/replay/{id}/stop` | 送信中の CALL の後で停止 |

MCP ツール：`get_trace`、`start_trace`、`stop_trace`、`analyze_frames`、`start_replay`、`get_replay`。
