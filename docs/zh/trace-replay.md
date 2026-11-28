# Trace 录制、分析与回放

**trace** 是一次运行中所有 OCPP 帧以及相关事件（连接、操作员与场景的操作、时钟变化）的记录文件。
可用于保存测试证据、比较多次运行，并通过 `analyze` 得到汇总报告。还可以用 `replay` 对 CSMS 回放 trace 来验证 CSMS。

## 格式 `ocppsim-trace/1`

JSON Lines（每行一个 JSON 对象），每行都有 `type`：

| `type` | 内容 |
|---|---|
| `header` | 总是第一行。`format: "ocppsim-trace"`、`version: 1`、`tool_version`、`created`（墙钟时间）、`time_scale`、`sim_start`（开始录制时的仿真时间）、`masked: true`、`part`、`chargers`（`id`、`ocpp_version`、`connectors`、去掉用户名和密码的 `csms_url`） |
| `frame` | 一条 OCPP-J 帧，字段与帧日志相同（`seq`、`ts`、`charger`、`direction`、`message_type`、`uid`、`action`、`raw`、`note`），另加 `sim_t`（自录制开始的仿真秒数）。`direction` 以充电桩为视角：`tx` = 发往 CSMS，`rx` = 收到 |
| `event` | `ts`、`sim_t`、`charger`（全局事件为 `null`）、`event`、`detail`。事件：`connected`、`disconnected`（`detail.reason`）、`connect_failed`、每个操作员 / 场景操作（`plug`、`unplug`、`swipe`、`start`、`stop`、`fault`、`drop`、`send` 等；`detail.since_seq` = 操作前的帧 seq）、`clock`（倍速或暂停变化）、`chargers`（重载改变了充电桩列表） |
| `footer` | 正常关闭的录制的最后一行：`frames`、`events`、`dropped`（磁盘跟不上而丢失的行数）、`next`（下一分段，见轮转） |

密钥与帧日志一样被脱敏（例如 ChangeConfiguration 的 `AuthorizationKey` 值为 `********`）。trace 中不含密码；
[回放](#replay)时通过参数传入凭据。

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

## Replay

`replay` 把 trace 中的**一台充电桩**对 CSMS 回放：在新的 WebSocket 连接上重新发送该桩发出的 CALL
（`direction: tx`），并把本次 CSMS 的应答与 trace 中记录的应答比较。可用于 CSMS 升级后用录好的会话回归验证，
或用真实充电桩的 trace 复现问题。

```
python -m sme_ocpp_sim replay run1.jsonl --csms ws://csms.example:9000/ocpp \
    [--charger CP001] [--as CP900] [--timing original|scale:N|asap] [--password PW] \
    [--out report.json] [--trace replay1.jsonl] [--json] [--fail-on-diff]
```

`--csms` 是不含充电桩 ID 的 CSMS URL（与模拟桩一样，ID 会追加在末尾）。trace 中有多台桩时需要 `--charger`。
OCPP 版本（及 WebSocket 子协议）取自 trace 的 header；没有 header 的 v0 文件按 action 判定，或用 `--ocpp-version` 指定。

### What is replayed

回放是**充电桩侧**、**按原始帧**进行的：CSMS 是被测对象，模拟器扮演录制下来的充电桩，不经过模拟器的 station
状态机。发送的是记录的帧，只在第二次运行必须改的地方改写：

| 改写 | 规则 |
|---|---|
| uniqueId | 每个 CALL 新生成 |
| 时间戳 | payload 中所有 ISO 8601 时间整体平移同一偏移量：被回放 CALL 中最早的时间变为「现在」，时间间隔保持不变 |
| 1.6 `transactionId` | 用本次 CSMS 对 StartTransaction 应答的 ID 替换后续帧（StopTransaction、MeterValues）中记录的 ID |
| 2.0.1 / 2.1 `transactionId` | 该 ID 由充电桩生成，因此追加本次运行的后缀 `-<replay id>`（保持在 36 字符以内）。`--no-tx-suffix` 保留记录的 ID |
| `--as ID` | 以另一个充电桩 ID 连接（URL 路径与 Basic 认证用户名），payload 不变 |

CALL 按顺序逐个发送：按 OCPP-J 的要求，收到应答或超时（`--call-timeout`，默认 30 秒）后才发下一个。
2.1 的 SEND 帧也按顺序重发（无应答）。

回放不重现连接事件：整个回放只用一条连接，trace 中的断线不会重复。CSMS 关闭连接时回放停止，剩余 CALL
在报告中记为 `not_sent`。

### Timing

| `--timing` | 两个 CALL 之间的间隔 |
|---|---|
| `original`（默认） | 按记录的间隔（从上一次发送开始计）；应答慢时下一个 CALL 顺延 |
| `scale:N` | 记录的间隔除以 N（`scale:10` = 10 倍速） |
| `asap` | 无间隔：收到应答后立即发送下一个 |

### CALLs from the CSMS

回放期间 CSMS 发来的 CALL（如 TriggerMessage、GetConfiguration）用 trace 中充电桩当时的应答回复，按 action 与出现序号
对应（第 2 次 GetConfiguration 用第 2 个记录的应答）；记录的应答用完后重复使用最后一个。记录为 CALLERROR 时回同样的
CALLERROR；trace 中未应答的 CALL 不应答。trace 中从未应答过的 action 由 `--unknown-call` 决定：`reject`（默认）回
CALLERROR `NotImplemented`，`noreply` 不应答。

### Credentials

trace 已脱敏，不含密码。凭据通过参数传入：`--password`（Security Profile 1 / 2 的 Basic 认证）、`wss://` 用的
`--ca-file` 或 `--insecure`、Profile 3 用的 `--client-cert` / `--client-key`。报告中不会出现密码。

### Report `ocppsim-replay/1`

`--out` 把 JSON 报告写入文件，`--json` 输出到标准输出，默认输出文本摘要。`--trace` 把本次回放录制成新的 trace 文件，
其 header 条目带 `replay_of`（源 trace、充电桩、replay id）。

| 字段 | 内容 |
|---|---|
| `id`、`charger`、`as`、`ocpp_version`、`csms_url`、`security_profile`、`timing` | 回放了什么、怎么回放的 |
| `result` | `match`、`diff`（至少一个应答不同）或 `error`（连接失败或断开，或被停止；原因见 `error`） |
| `summary` | `calls`、`sent`、`matched`、`differed`、`not_compared`、`timeouts`、`not_sent`、`inbound`（CSMS 的 CALL）、`inbound_not_in_trace` |
| `calls[]` | 每个 CALL 的 `action`、trace 中的 `seq`、`recorded` 与 `replayed` 结果、`match` 与 `diff` |
| `inbound[]` | 每个 CSMS CALL 的 `action`、`occurrence`、`answer`（`result`、`error <code>`、`not_implemented`、`no_reply`）、`in_trace`、`reused` |
| `notes` | 未能改写的内容（例：1.6 StartTransaction 本次没有返回 `transactionId`） |

结果为 `result`（附 `idTagInfo.status` 等 status 类字段）、`error`（附 CALLERROR 的 `code`）、`timeout`、`closed` 或
`not_sent`。trace 中没有应答时记录侧结果为 `none`，该 CALL 不参与比较。status 类字段指键名为 `status` 或以 `Status`
结尾的字段；`currentTime`、`interval` 等每次运行都会变化的字段不比较。

差异不会让回放失败。退出码：0 = 完成（有差异也是 0），1 = 指定 `--fail-on-diff` 时有差异，2 = 错误（trace 无法读取、
参数错误、连接失败或断开）。

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
| POST | `/api/replay` | 回放 trace 目录中的 trace 文件：body `{"file", "csms_url", "charger", "as_id", "timing", "password", "ocpp_version", "unknown_call", "tx_suffix", "call_timeout", "verify_tls"}`（`file` 与 `csms_url` 必填）。使用独立连接，不占用模拟桩，并录制到 trace 目录的 `replay-<id>.jsonl`。返回 `id` |
| GET | `/api/replay` | 本次启动以来的回放列表 |
| GET | `/api/replay/{id}` | 状态（`running` / `done`、`sent` / `total`），完成后带 `report` |
| POST | `/api/replay/{id}/stop` | 当前 CALL 完成后停止 |

MCP 工具：`get_trace`、`start_trace`、`stop_trace`、`analyze_frames`、`start_replay`、`get_replay`。
