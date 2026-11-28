# シナリオ

**シナリオ**は、シミュレーターの充電器を時刻付きのステップで動かし、CSMS の応答を検証する YAML ファイルです。
「プラグを挿す → 認証 → 2 時間待つ → プラグを抜く」を実行し、CSMS がトランザクションを受け付けたことを判定します。
CI 向けにヘッドレス実行（判定 JSON・JUnit XML（任意）・終了コード）でき、起動中のアプリでも実行できます（HTTP API、MCP）。

- シナリオは独立したファイルです（`format: ocppsim-scenario/1`）。実行中の設定の充電器を ID で参照し、
  充電器や CSMS の設定は含みません。同時に実行できるシナリオは 1 本です。
- 時間は**シミュレーション時刻**に従います。`after`・`wait`・`at` は `time_scale` で加速し、一時停止中は止まります。
  `clock: {time_scale: 3600}` なら 2 時間のセッションが実時間 2 秒で終わります。OCPP の通信上のタイムスタンプは
  実時刻のままです（CSMS からはセッションが短く見え、電力量はシミュレーション時間で積算されます）。
- 判定の待ち時間は**実時間**の秒数です（`within`、既定 30 秒）。CSMS の応答速度は加速と無関係だからです。
  シミュレーション時間で待つ場合は `within_sim` を使います。

リポジトリの `scenarios/` フォルダーに例があり、`configs/mini-csms-demo.yaml` で実行できます。

```bash
sme-ocpp-sim scenario run scenarios/plug-charge-unplug.yaml -c configs/mini-csms-demo.yaml --out verdict.json
```

## ファイル形式

```yaml
format: ocppsim-scenario/1
name: plug-charge-unplug
seed: 7                                   # 判定結果に記録
clock: {start_at: "2026-12-01T07:55:00+09:00", time_scale: 3600}   # どちらも任意
params: {tag: "TAG001"}                   # 以下の文字列で ${tag}
tracks:
  - name: session
    charger: DEMO-16
    steps:
      - expect: {state: {online: true}}
        within: 30s
      - {at: "08:00", do: plug, connector: 1}
      - {after: 1m, do: swipe, connector: 1, id_tag: "${tag}"}
      - expect:
          frame: {dir: tx, action: StartTransaction}
          then: {message_type: CALLRESULT, payload: {idTagInfo.status: Accepted}}
        within: 10s
      - {do: wait, duration: 2h}
      - {do: unplug, connector: 1}
      - expect: {frame: {dir: tx, action: StopTransaction}}
until: "+4h"                              # 任意の終了時刻（シミュレーション時間）
```

| キー | 意味 |
|---|---|
| `tracks` | 並行に実行。トラック内のステップはその `charger` で順に実行 |
| `for_each: {chargers: [A, B]}` | `charger` の代わりに充電器ごとに 1 トラック（`"*"` = 全充電器）。`${charger}` / `${i}` は ID / 0 始まりの番号 |
| `repeat: n` | ステップを n 回繰り返す（`${round}` = 0 始まりの回数。ステップ ID に `#1`, `#2` … が付く） |
| `clock.start_at` | 実行時にシミュレーション時刻をこの日時から再開（`at: "HH:MM"` の基準） |
| `clock.time_scale` | 実行中の速度（終了後に戻る）。CLI の `--time-scale` が優先 |
| `until` | 実行の終わり。その時点で実行中のステップは fail、未開始のステップは skipped（件数はイベントログに記録） |

すべてのキーが検査されます（綴り誤りは位置付きのエラー、例 `tracks.0.steps.2.do`）。充電器 ID と `${...}` の名前は
実行前に実行中の設定と照合されます。文字列全体が `"${n}"` の場合はパラメーターの型（数値・真偽値）が保たれます。
時刻は引用符で囲んでください（`"08:00"`）。引用符なしは YAML が数値として読むためエラーになります。

### タイミング

- `after: <時間>`：前のステップからこの時間（シミュレーション）後に実行。
- `at: "HH:MM[:SS]"`：シナリオ開始以降で最初のその時刻。`at: "+90m"`：開始からのシミュレーション時間。ISO 日時も可。
- 時間は秒数または `500ms`、`30s`、`5m`、`2h`、`1h30m`、`1d` の形式。

### アクション（`do:`）

| アクション | パラメーター | 動作（Web UI のボタン / HTTP ルートと同じ） |
|---|---|---|
| `plug` / `unplug` | `connector`（既定 1） | EV ケーブルを挿す / 抜く（抜くとトランザクション終了） |
| `swipe` | `connector`、`id_tag` | ID タグを提示（開始または停止） |
| `start` | `connector`、`id_tag`（既定：充電器の最初のタグ） | `plug` + `swipe` |
| `stop` | `connector` | トランザクションをローカルで停止 |
| `fault` / `clear_fault` | `connector`、`error_code` | コネクター故障の発生 / 解除 |
| `connect` / `disconnect` | | 接続ループの開始 / 切断保持 |
| `drop` | `mode`（`abort` / `close`）、`reconnect_after_s` | 接続を即時切断 |
| `impair` | `impairments`（ブロック全体） | 充電器の [impairments](http-api.md#transaction-and-response-anomalies) を即時置換 |
| `send` | `action` + `payload` または `raw`、`validate`、`timeout` | CALL / raw フレームの注入 |
| `set_ev` | `connector`、`ev_model`、`initial_soc`、`target_soc` | そのコネクターの次のトランザクションの EV（1 回のみ） |
| `wait` | `duration` | 待機（シミュレーション時間） |
| `log` | `message` | イベントログに記録 |

拒否されたアクション（存在しないコネクター、停止するトランザクションがない等）はステップの **error** です。

### 判定

```yaml
- expect: {frame: {dir: tx, action: MeterValues, payload: {meterValue.0.sampledValue.0.value: {gt: 0}}}}
- expect: {state: {connector: 1, status: Charging, power_w: {ge: 3000}, has_transaction: true}}
  within_sim: 10m
- expect_not: {dir: tx, action: StartTransaction}    # 期間内に該当フレームがなければ pass
  within: 20s
- await_csms: {action: RemoteStartTransaction, prompt: "CSMS から CP001 の充電を開始してください"}
  within: 5m
```

- `expect.frame`：`dir`（`tx` = 充電器が送信、`rx` = 受信）、`action`、`message_type`（`CALL` / `CALLRESULT` /
  `CALLERROR`）、`payload` の matcher。`then:` は一致した CALL への応答（同じメッセージ ID）を検査します：
  `message_type` と `payload`（CALLERROR の payload は `errorCode` / `errorDescription` / `errorDetails`）。
- `expect.state`：`connector`、`status`、`has_transaction`、`power_w`、`soc_pct`、`meter_wh`、`online`、`connected`。
- `expect_not`：フレーム条件。期間内に一致が現れた時点で fail。
- `await_csms`：CSMS からの CALL（CSMS 側でのオペレーター操作）を待ちます。待機中は `prompt` がステータスと
  イベントログに表示されます。
- 各トラックには**フレームカーソル**があります。シナリオ開始時点から始まり、pass した `expect.frame` /
  `await_csms` は一致したフレーム（`then` があれば応答）まで進め、アクションステップはアクション開始時点の最新
  フレームまで進めます（判定開始前に届いた応答も見つかります）。`expect_not` と `expect.state` は動かしません。
  フレーム判定は既定でカーソルより後を探すため、同じアクションの `expect` を 2 回書くとフレームが 2 つ必要です。
  `from: action` は直前のアクションの開始時点から、`from: scenario_start` はシナリオ開始時点から探します。
- `await_csms` も `within` を指定しなければ実時間 30 秒で終わります。オペレーター操作には長めに指定してください。
- `on_fail: stop`（既定）は失敗・拒否されたステップでシナリオ全体を終了し、`continue` は続行します。

**matcher**（Q6：組み込み、JSONPath なし、コードなし）：パスはドット区切りで、整数部分はリストの添字です
（`meterValue.0.sampledValue.0.value`）。値はリテラル（等価）か、すべて満たすべき演算子：`in: [...]`、
`gt` / `ge` / `lt` / `le: n`（数値文字列は数値として比較）、`re: "正規表現"`（search）、`exists: true|false`。

## 判定結果（verdict）

```json
{"format": "ocppsim-verdict/1", "scenario": "plug-charge-unplug", "result": "pass", "error": null, "seed": 7,
 "started": "2026-11-28T17:12:16+09:00", "wall_s": 2.1, "sim_s": 7423.0, "tool_version": "0.3.0",
 "trace": null, "steps": [{"id": "4", "track": "session", "kind": "expect", "status": "pass",
 "t_wall": 0.02, "t_sim": 60.0, "message": "StartTransaction: seq 7, answer seq 8", "evidence": [7, 8]}]}
```

`result`：`pass`、`fail`（判定が失敗）、`error`（アクションが拒否された、またはシナリオを開始できない）、
`aborted`（外部から停止）。ステップの `status`：`pass` / `fail` / `error` / `skipped` / `aborted`。
`evidence` はフレームの通し番号です（フレームログ / `--trace` ファイル）。

## コマンドライン

```bash
sme-ocpp-sim scenario run FILE [-c CONFIG] [--out verdict.json] [--junit junit.xml] [--trace frames.jsonl]
                              [--time-scale N] [-v] [--log-format json]
sme-ocpp-sim scenario validate FILE [-c CONFIG]
```

`run` は Web UI なしでエンジン（設定で有効なら Mini CSMS も）を起動してシナリオを実行し、判定結果を書き出して
（`--out` がなければ標準出力）、**0** pass、**1** fail、**2** error / aborted / シナリオまたは設定が不正、で終了します。
`--junit` は判定ごと（と失敗したアクションごと）に 1 テストケース、`--trace` はシナリオの充電器のフレームを
JSON Lines（フレームログのエクスポート形式）で書き出します。

## 組み込みシナリオ { #builtin }

リポジトリの `scenarios/` フォルダの例は全ビルド（wheel、Windows exe、Docker イメージ）に同梱され、
`builtin:<名前>` で参照できます。

```bash
sme-ocpp-sim scenario run builtin:plug-charge-unplug -c configs/mini-csms-demo.yaml --out verdict.json
```

| 名前 | 内容 |
|---|---|
| `plug-charge-unplug` | `configs/mini-csms-demo.yaml` の `DEMO-16` で OCPP 1.6 のセッションを 1 回（シミュレーション 2 時間を約 2 秒） |
| `remote-start-no-tx` | `DEMO-16` を `remote_start: accept_no_tx` に：待機中に CSMS から RemoteStartTransaction を送ると受理されるがトランザクションは始まらない |
| `both-versions` | 設定の全充電器（`for_each`）で同じセッションを OCPP 1.6 と 2.0.1 並行で実行 |

HTTP では `GET /api/scenarios/builtin` で一覧、`POST /api/scenarios/builtin/{name}/run`（または
`POST /api/scenarios/run` の本文 `builtin:<name>`）で開始。MCP では `list_builtin_scenarios` のあと
`run_scenario` に `body: "builtin:<name>"`。参照する充電器が実行中の設定に存在する必要があります。

## HTTP API と MCP

| メソッド | パス | MCP ツール | 意味 |
|---|---|---|---|
| POST | `/api/scenarios/run` | `run_scenario` | 本文：シナリオの YAML（または JSON）テキスト。開始する（実行中は 409） |
| GET | `/api/scenarios/current` | `get_scenario_status` | `state`（`idle` / `running` / `done`）、トラックの進捗、ステップ数、prompt |
| POST | `/api/scenarios/abort` | `abort_scenario` | 停止（result `aborted`） |
| GET | `/api/scenarios/builtin` | `list_builtin_scenarios` | 組み込みシナリオ（name、title、description）。本文 `builtin:<name>` または `POST /api/scenarios/builtin/{name}/run` で実行 |
| GET | `/api/scenarios/result?format=json\|junit` | `get_scenario_result` | 終了したシナリオの判定結果 |
| POST | `/api/frames/expect` | `expect_frame` | 単発：`since` 以降に一致するフレームを最大 `within` 秒待つ |

Web UI では **シミュレーション → シナリオ…** でシナリオパネルを開きます。組み込みシナリオを選ぶかファイルを開き
（YAML はエディタに入り、編集できます）、**実行** / **中止**、ステップのライブ表（待機・実行中・合格・不合格・エラー・
スキップ・中止、シミュレーション時刻とメッセージ）、`await_csms` ステップのオペレーター向け表示、終了後は
**判定 JSON** / **JUnit XML** のダウンロード。シナリオの `log` 行と開始・結果はイベントログにも出ます。

## 制限

- 生成トラック（`steps` の代わりに `generate:`、`until` が必要）：[トラフィックジェネレーター](traffic.md#scenarios)を参照。`seed` はその種になります。
- `send` は充電器 → CSMS の CALL を注入します。CSMS 側のステップは `await_csms`（または Mini CSMS API）です。
- トレースファイルは現行のフレームログ形式です（実行中に書き出し）。バージョン付きトレース形式は後で追加されます。
- `repeat`（最大 1000 回）では `at: "+dur"` は各回の開始から数えます。時刻 / 日時の `at` は使えません。
- `clock.time_scale` は実行後に戻します（途中で速度が変更された場合を除く）。`clock.start_at` はエンジンの
  シミュレーション時刻を再開させ、実行後もそのままです。
- 保持するフレームは充電器ごとに最大 200 000 件です。破棄済みのフレームが必要な判定は
  "frame buffer overflowed" で fail します。
- `POST /api/frames/expect`：自分のアクションによるフレームを取りこぼさないよう、アクションの応答の `last_seq`
  （充電器のアクションはすべて返します。アクション前のフレームログのシーケンス番号）を `since` に渡します。
  `since` がフレームログより古いと、応答に `oldest_seq` と `warning` が付きます。
