# トラフィックジェネレーター

トラフィックジェネレーター（ATG）は、空いているコネクターにランダムかつ再現可能な充電セッションを発生させます。
車が到着し、プラグを挿し、ID タグを提示し、充電し、停止して去ります。何時間ものシミュレーション時間にわたる
多数のセッションが必要な負荷試験・EMS 試験（需要ピーク、占有、待機電力）向けです。

シミュレーション時刻で動作し（`simulation.time_scale` と一時停止に従う）、Web UI や
[シナリオ](scenarios.md)と同じ操作（plug / swipe / stop / unplug）を使い、車両は
[車両プール](configuration.md)から選びます。

## 設定 { #configuration }

常駐トラフィックは設定のトップレベル `traffic:` ブロックです（`configs/traffic-demo.yaml` を参照）。

```yaml
traffic:
  enabled: true             # アプリと一緒に起動（POST /api/traffic/start は保存せずに起動）
  seed: 42
  chargers: "*"             # または [CP001, CP002]
  interval_s: 300           # コネクターごとの到着判定の間隔（シミュレーション秒）
  probability: 0.2          # 1 回の判定での到着確率
  hourly_weights: [0.2,0.1,0.1,0.1,0.2,0.5,1,2,2,1.5,1,1,1,1,1,1,1.5,2,2,1.5,1,0.8,0.5,0.3]
  auth_order: plug_first    # plug_first | tag_first
  stop_after: first         # duration | soc_target | first
  duration_s: [1800, 7200]
  stop_by: swipe            # swipe | unplug | remote_only
  max_session_s: 43200
  idle_after_s: [0, 900]
  id_tags: [TAG001, TAG002, TAG003]   # 既定：各充電器の id_tags
  id_tag_strategy: affinity           # random | round_robin | affinity
  affinity_prob: 0.8
  affinity_map: {TAG001: [CP001]}     # 既定：シードからタグごとに 1 台のホーム充電器
  vehicles: {kei-20kwh: 1, compact-40kwh: 3, suv-90kwh: 1}   # 既定：プール全体（各自の重み）
```

| 項目 | 既定値 | 意味 |
|---|---|---|
| `enabled` | `false` | アプリと一緒に常駐トラフィックを動かす |
| `seed` | `0` | 計画と全充電器の車両プールの乱数ストリームの種（`session.ev_model: pool` の選択にも適用） |
| `chargers` | `"*"` | 対象の充電器 |
| `interval_s` / `probability` | `300` / `0.2` | `interval_s`（シミュレーション秒）ごとに各コネクターが確率 `probability` で到着を判定 |
| `hourly_weights` | `null` | シミュレーション時刻の時（0〜23 時）ごとに `probability` に掛ける 24 個の係数（朝のピーク、夜間の谷） |
| `auth_order` | `plug_first` | プラグが先か、タグが先か |
| `stop_after` | `first` | 充電の終了条件：`duration`（`duration_s` の一様分布）、`soc_target`（車両の目標 SoC）、早い方（`first`） |
| `stop_by` | `swipe` | `swipe`（同じタグ）、`unplug`（車が去るまでトランザクション継続）、`remote_only`（CSMS の RemoteStop を待ち、`max_session_s` 後はローカル停止） |
| `max_session_s` | `43200` | セッションの最長時間（シミュレーション秒、どの `stop_by` でも） |
| `idle_after_s` | `[0, 900]` | 充電後もこの時間プラグを挿したまま（占有・待機電力）、その後抜く |
| `id_tags` | 各充電器の `id_tags` | タグのプール |
| `id_tag_strategy` | `random` | `random`（一様）、`round_robin`（全充電器を通して計画順に順番）、`affinity`（`affinity_prob` の確率でこの充電器をホームとするタグ、それ以外はランダム） |
| `vehicles` | プール全体 | プール（プリセット + `ev_models`）の車両 ID と重み |

CSMS が拒否した（タグが Accepted でない）セッションはすぐにプラグを抜きます（`rejected`）。トランザクションが
自然に終わった場合（RemoteStop、リセット）もセッションは終わります。車の到着時にコネクターが空いていない場合
（前のセッション中、故障、オフライン、他の操作でプラグ済み）は、その到着を**スキップ**します（後ろにずらしません）。

`traffic:`（`PUT /api/traffic` を含む）や充電器一覧を変更すると、動作中のトラフィックはその時点から新しい計画で
再起動します。`enabled` の切り替えで起動・停止します。車両のシードは即時に反映されます。

## 再現性 { #reproducibility }

同じシード・同じ設定・同じ開始時刻なら同じ**計画**（各コネクターの到着時刻、車両、SoC、タグ、充電時間、
待機時間）になります。コネクターごとに独立した乱数ストリーム（`{seed}:traffic:{charger}:{connector}` と
プールのストリーム `vehicle:{charger}:{connector}`）を使い、各判定は実行時の状況に関係なく同じ数の乱数を使うため、
充電器を追加しても他の充電器の計画は変わらず（充電器をまたいで数える `round_robin` のタグを除く）、計画は
CSMS の応答に依存しません。どの到着がスキップされるかと、フレーム単位のタイミングは保証の対象外です。

`GET /api/traffic/plan?hours=4` で計画をプレビューできます（動作中のトラフィックの開始時刻、なければ現在から）。
固定の基準には `start=` を指定します。

## シナリオと優先順位 { #scenarios }

シナリオのトラックは生成トラックにできます。`steps` の代わりに `generate:` に同じ項目（`enabled` / `chargers`
を除く）を書き、`use: traffic` で設定の `traffic:` ブロックを起点にします。`for_each` は全対象充電器で 1 つの計画を
作ります。生成トラックはシナリオの `until`（必須）まで動き、判定レコード（kind `generate`）はセッションの
カウンター付きで pass になります。シナリオの `seed` はトラフィックのシードより優先されます。

```yaml
tracks:
  - for_each: {chargers: [CP002, CP003]}
    generate: {use: traffic, probability: 0.5, duration_s: [600, 3600]}
until: "+4h"
```

実行中のシナリオは自分の充電器を占有します。開始前に常駐トラフィックはその充電器のセッションを停止してプラグを
抜き（`preempted`）、シナリオが終わるまでその充電器への到着をスキップします。他の充電器は動き続けます。

## API と MCP { #api }

| メソッド | パス | MCP ツール | 意味 |
|---|---|---|---|
| GET | `/api/traffic` | `get_traffic` | 状態、設定、カウンター（arrivals / skipped / started / rejected / completed / failed / preempted）、実行中セッション数、合計電力、最近のイベント |
| PUT | `/api/traffic` | `set_traffic` | `traffic:` ブロックを置き換え（リロード。保存は設定の保存で） |
| POST | `/api/traffic/start` | `start_traffic` | 今すぐ起動（保存しない） |
| POST | `/api/traffic/stop` | `stop_traffic` | 停止。セッションは停止してプラグを抜く |
| GET | `/api/traffic/plan?hours=&charger=&limit=&start=` | `preview_traffic_plan` | 計画された到着 |

動作中は、充電器一覧の見出しに実行中のセッション数と合計電力が表示されます。

長時間の実行でもメモリは増え続けません。計画は必要な分だけ作り、生成トラックはフレームを保持せず、
状態には最新 50 件のイベントだけを残します。
