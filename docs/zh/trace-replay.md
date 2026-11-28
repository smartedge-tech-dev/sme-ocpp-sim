# Trace 录制与分析

**trace** 是一次运行中所有 OCPP 帧以及相关事件（连接、操作员与场景的操作、时钟变化）的记录文件。
可用于保存测试证据、比较多次运行，并通过 `analyze` 得到汇总报告。向 CSMS 回放 trace（replay）将在后续版本提供，
格式已包含回放所需的信息。

## 格式 `ocppsim-trace/1`

JSON Lines（每行一个 JSON 对象），每行都有 `type`：

| `type` | 内容 |
|---|---|
| `header` | 总是第一行。`format: "ocppsim-trace"`、`version: 1`、`tool_version`、`created`（墙钟时间）、`time_scale`、`sim_start`（开始录制时的仿真时间）、`masked: true`、`part`、`chargers`（`id`、`ocpp_version`、`connectors`、去掉用户名和密码的 `csms_url`） |
| `frame` | 一条 OCPP-J 帧，字段与帧日志相同（`seq`、`ts`、`charger`、`direction`、`message_type`、`uid`、`action`、`raw`、`note`），另加 `sim_t`（自录制开始的仿真秒数）。`direction` 以充电桩为视角：`tx` = 发往 CSMS，`rx` = 收到 |
| `event` | `ts`、`sim_t`、`charger`（全局事件为 `null`）、`event`、`detail`。事件：`connected`、`disconnected`（`detail.reason`）、`connect_failed`、每个操作员 / 场景操作（`plug`、`unplug`、`swipe`、`start`、`stop`、`fault`、`drop`、`send` 等；`detail.since_seq` = 操作前的帧 seq）、`clock`（倍速或暂停变化）、`chargers`（重载改变了充电桩列表） |
| `footer` | 正常关闭的录制的最后一行：`frames`、`events`、`dropped`（磁盘跟不上而丢失的行数）、`next`（下一分段，见轮转） |

密钥与帧日志一样被脱敏（例如 ChangeConfiguration 的 `AuthorizationKey` 值为 `********`）。trace 中不含密码；
将来的回放通过参数传入凭据。

没有 header 的文件（如 `/api/frames.jsonl` 导出的帧日志）按 **版本 0** 读取：每行都是一帧。`analyze` 两种都接受。

### header 的充电桩条目与 footer

`chargers` 的每个条目除 `id`、`ocpp_version`、`connectors`、`csms_url` 外，还带有回放重建充电桩所需的
`vendor`、`model`、`serial`、`security_profile`、`subprotocol`（WebSocket 子协议）、`path`（含充电桩标识的
WebSocket 路径，如 `/ocpp/CP001`）、`ev_model`、`seed`（`traffic.seed`，同时决定车辆随机流）和 `impairments`
（开始时该充电桩配置的故障注入）。其中没有任何秘密：`csms_url` 不含用户、密码、query 与 fragment（无法解析的
URL 写作 `<invalid url>`），`connected` 事件的 `detail.path` 只有连接路径。这些字段都是追加的，格式版本仍为 1，
读取方忽略不认识的字段。

**footer 是累计值。** footer 的 `frames`、`events`、`dropped` 是截至该 footer 的整个录制的合计，而不是本分段的；
读取方取最后一个 footer（按分段求和会重复计数）。footer 还可能带 `error`（录制提前结束，例如无法创建下一分段；
此时 `next` 为 `null`）。最后一个 footer 缺失、带 `error`、或其 `next` 分段文件不存在时，`analyze` 报告 `complete: false`。

## 录制

| 方式 | 录制范围 | 文件 |
|---|---|---|
| 配置 `trace.path` | 所有充电桩，从模拟器启动到停止 | `trace.path`（覆盖） |
| `sme-ocpp-sim --trace FILE` | 同上（覆盖 `trace.path`） | `FILE` |
| Web UI 帧日志工具栏的 **录制 trace** 按钮、`POST /api/trace/start` | 所有充电桩，直到 **停止录制** / `POST /api/trace/stop` | trace 目录 |
| 从 Web UI / API 启动的场景 | 场景的充电桩 | trace 目录下的 `scenario-<名称>-<时间>.jsonl`；判定结果的 `trace` 字段给出路径 |
| `sme-ocpp-sim scenario run ... --trace FILE` | 场景的充电桩 | `FILE` |

```yaml
trace:
  path: traces/run.jsonl   # null（默认）= 启动时不录制
  rotate_mb: 100           # 达到该大小时开始新分段；null = 不轮转
```

**trace 目录**：设置了 `trace.path` 时为其所在目录，否则为配置文件旁的 `traces/`（Windows exe：`~/.sme-ocpp-sim/traces`）。
HTTP API 只在该目录内读写文件。

**轮转**：分段达到 `rotate_mb` 时以 footer 关闭，footer 的 `next` 给出下一分段名（`run.jsonl` → `run.1.jsonl` →
`run.2.jsonl` …）。每个分段都以自己的 header 开头。把第一个分段交给 `analyze` 即可读完整条链。

录制不会拖慢模拟器：行经有界队列交给后台线程写入；磁盘跟不上时丢弃并计入 footer 的 `dropped`。

相对路径的 `trace.path`（以及相对路径的 `--trace FILE`）与配置中其他文件路径（`ca_file`、`client_cert_file`）一样，
按进程的工作目录解析，而不是按配置文件所在目录。`--trace` 不会修改 `trace.path`，所以 Web UI 写回的配置文件保持原值。
HTTP API（及 MCP 的 `start_trace`）只在 trace 目录内写文件，文件名已存在时返回 409；命令行 `--trace FILE` 可以是任意
路径，并会覆盖该文件。

轮转时若无法打开下一分段，录制以带 `next: null` 和 `error` 的 footer 结束。写线程在队列变空时、以及至少每秒一次
flush，所以录制中的文件也可读取。

## analyze

```
sme-ocpp-sim analyze traces/run.jsonl                 # 文本摘要
sme-ocpp-sim analyze traces/run.jsonl --json          # ocppsim-analysis/1 JSON
sme-ocpp-sim analyze frames.jsonl --charger CP001 --ocpp-version 2.0.1
```

`--ocpp-version` 指定 v0 文件中未注明的充电桩版本（默认按 action 名判断）。每个充电桩的报告：

- **Action**（按方向）：CALL 数、CALLRESULT、按错误码的 CALLERROR、无应答 CALL、应答延迟 p50 / p95 / max
  （ms；CALL 与应答按消息 ID 配对）。
- **连接**：连接 / 断线次数与在线时长（v1 trace）、BootNotification 结果序列、Heartbeat 间隔分布。
- **交易**：开始、结束、时长、Δ电表（Wh）、结束原因。OCPP 2.0.1 / 2.1：每笔交易的 `seqNo` 缺口与重复。
- **协议异常**：无法解析的帧、没有 CALL 的应答、重复的消息 ID、同一方 CALL 未决期间又发出的 CALL（未决超过 60 秒的 CALL、或断线时仍未决的 CALL 改计为“未应答”）。帧与事件按时间顺序处理；缺少 `seq`、`ts` 或 `direction` 的帧行被跳过并计为坏行。
- **Smart Charging**：CSMS 下发的 SetChargingProfile / ClearChargingProfile / UpdateDynamicSchedule 及其应答状态的时间线。

分析只读取帧和事件，不读模拟器状态，因此也适用于真实充电桩的 trace。不生成图表，可用 JSON 自行绘制。

## HTTP API

| 方法 | 路径 | 含义 |
|---|---|---|
| GET | `/api/trace` | 录制状态（`recording`、`path`、`frames`、`events`、`dropped`）与 trace 目录 |
| POST | `/api/trace/start` | 请求体 `{"file": "run1.jsonl"}`（可省略；默认 `trace-<时间>.jsonl`）。录制中、或文件已存在时返回 409 |
| POST | `/api/trace/stop` | 停止并写 footer。未在录制时返回 409 |
| GET | `/api/trace/files` | trace 目录中的文件（新的在前） |
| GET | `/api/trace/files/{file}` | 下载 trace 文件 |
| GET | `/api/trace/files/{file}/analyze?charger=&format=json\|text` | 分析 trace 文件 |
| GET | `/api/frames/analyze?<帧筛选>&format=json\|text` | 分析内存中的帧日志（无连接事件） |
| GET | `/api/frames.jsonl?format=v1` | 带 v1 header 的帧日志导出（默认仍为原格式） |

MCP 工具：`get_trace`、`start_trace`、`stop_trace`、`analyze_frames`。
