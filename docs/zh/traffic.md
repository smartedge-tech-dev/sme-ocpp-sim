# 流量生成器

流量生成器（ATG）在空闲的 connector 上产生随机但可复现的充电会话：车辆到达、插枪、刷卡、充电、停止、离开。
用于需要在数小时仿真时间内产生大量真实会话的负载测试和 EMS 测试（需量峰值、占位、空闲功率）。

它运行在仿真时钟上（跟随 `simulation.time_scale` 和暂停），使用与 Web UI 和[场景](scenarios.md)相同的操作
（plug / swipe / stop / unplug），车辆从[车型池](configuration.md)中抽取。

## 配置 { #configuration }

常驻流量是配置的顶层 `traffic:` 块（参见 `configs/traffic-demo.yaml`）：

```yaml
traffic:
  enabled: true             # 随应用启动（POST /api/traffic/start 启动但不保存）
  seed: 42
  chargers: "*"             # 或 [CP001, CP002]
  interval_s: 300           # 每个 connector 判定到达的间隔（仿真秒）
  probability: 0.2          # 每次判定的到达概率
  hourly_weights: [0.2,0.1,0.1,0.1,0.2,0.5,1,2,2,1.5,1,1,1,1,1,1,1.5,2,2,1.5,1,0.8,0.5,0.3]
  auth_order: plug_first    # plug_first | tag_first
  stop_after: first         # duration | soc_target | first
  duration_s: [1800, 7200]
  stop_by: swipe            # swipe | unplug | remote_only
  max_session_s: 43200
  idle_after_s: [0, 900]
  id_tags: [TAG001, TAG002, TAG003]   # 默认：各桩自己的 id_tags
  id_tag_strategy: affinity           # random | round_robin | affinity
  affinity_prob: 0.8
  affinity_map: {TAG001: [CP001]}     # 默认：按 seed 为每个 tag 选一台常用桩
  vehicles: {kei-20kwh: 1, compact-40kwh: 3, suv-90kwh: 1}   # 默认：整个车型池，用各自的权重
```

| 字段 | 默认值 | 含义 |
|---|---|---|
| `enabled` | `false` | 随应用运行常驻流量 |
| `seed` | `0` | 计划和所有桩车型池随机流的种子（`session.ev_model: pool` 的抽取也用它） |
| `chargers` | `"*"` | 作用的充电桩 |
| `interval_s` / `probability` | `300` / `0.2` | 每 `interval_s`（仿真秒）每个 connector 以 `probability` 判定一次到达 |
| `hourly_weights` | `null` | 按仿真时钟的小时乘到 `probability` 上的 24 个系数（早高峰、夜间低谷） |
| `auth_order` | `plug_first` | 先插枪还是先刷卡 |
| `stop_after` | `first` | 充电结束条件：`duration`（`duration_s` 内均匀）、`soc_target`（车辆目标 SoC），或两者先到者（`first`） |
| `stop_by` | `swipe` | `swipe`（同一 tag）、`unplug`（交易持续到车辆离开）或 `remote_only`（等 CSMS RemoteStop，`max_session_s` 后本地停止） |
| `max_session_s` | `43200` | 会话最长时间（仿真秒，对所有 `stop_by` 生效） |
| `idle_after_s` | `[0, 900]` | 充电结束后保持插枪的时间（占位 / 空闲功率），然后拔枪 |
| `id_tags` | 各桩的 `id_tags` | tag 池 |
| `id_tag_strategy` | `random` | `random`（均匀）、`round_robin`（按计划顺序跨所有桩轮转）、`affinity`（以 `affinity_prob` 概率选以本桩为常用桩的 tag，否则随机） |
| `vehicles` | 整个车型池 | 车型池（preset + `ev_models`）中的车型 ID 及权重 |

CSMS 拒绝（tag 非 Accepted）的会话立即拔枪（`rejected`）；交易自行结束（RemoteStop、重置）时会话也结束。
车辆到达时 connector 不空闲（上一个会话未结束、故障、离线、已被别处插枪）则该次到达**跳过**（skipped），不顺延。

修改 `traffic:`（包括 `PUT /api/traffic`）或充电桩列表会让运行中的流量从此刻起以新计划重启；切换 `enabled`
会启动或停止它。车型种子即时生效。

## 可复现性 { #reproducibility }

同一 seed、同一配置、同一开始时刻得到同一**计划**：每个 connector 的到达时刻、车型、SoC、tag、充电时长和占位时长。
每个 connector 有独立的随机流（`{seed}:traffic:{charger}:{connector}` 和车型池的 `vehicle:{charger}:{connector}`），
每次判定不论运行时发生什么都消耗相同数量的随机数，所以增加充电桩不改变其它桩的计划（跨桩计数的 `round_robin`
tag 除外），计划也不依赖 CSMS 的应答。哪些到达被跳过、帧级时序不在可复现承诺内。

`GET /api/traffic/plan?hours=4` 可预览计划（从运行中流量的开始时刻，否则从现在起）；需要固定基准时传 `start=`。

## 场景与优先级 { #scenarios }

场景的 track 可以是生成器：用 `generate:`（同样的字段，`enabled` / `chargers` 除外）代替 `steps`；`use: traffic`
以配置的 `traffic:` 块为起点。`for_each` 为其所有桩生成一个计划。生成器 track 一直运行到场景的 `until`（必填）；
其 verdict 记录（kind `generate`）为 pass 并带会话计数。场景的 `seed` 优先于流量的 seed。

```yaml
tracks:
  - for_each: {chargers: [CP002, CP003]}
    generate: {use: traffic, probability: 0.5, duration_s: [600, 3600]}
until: "+4h"
```

运行中的场景独占它的充电桩：开始前，常驻流量会停止这些桩上自己的会话并拔枪（`preempted`），并在场景结束前跳过
这些桩上的到达；其它桩照常运行。

## API 与 MCP { #api }

| 方法 | 路径 | MCP 工具 | 含义 |
|---|---|---|---|
| GET | `/api/traffic` | `get_traffic` | 状态、设置、计数（arrivals / skipped / started / rejected / completed / failed / preempted）、在充会话数、总功率、最近事件 |
| PUT | `/api/traffic` | `set_traffic` | 替换 `traffic:` 块（重新加载；随配置保存） |
| POST | `/api/traffic/start` | `start_traffic` | 立即启动（不保存） |
| POST | `/api/traffic/stop` | `stop_traffic` | 停止；其会话会停止并拔枪 |
| GET | `/api/traffic/plan?hours=&charger=&limit=&start=` | `preview_traffic_plan` | 计划的到达 |

运行时，充电桩列表标题会显示在充会话数和总功率。

长时间运行时内存不会无限增长：计划按需生成，生成器 track 不缓存帧，状态只保留最近 50 条事件。
