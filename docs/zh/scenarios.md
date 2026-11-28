# 场景

**场景**是一个 YAML 文件：按时间步骤驱动模拟充电桩，并检查 CSMS 的反应——插枪、刷卡、等两小时、拔枪，
再断言 CSMS 接受了这笔交易。它可以在 CI 中无界面运行（判定 JSON、可选 JUnit XML、退出码），也可以在运行中的应用里执行（HTTP API、MCP）。

- 场景是独立文件（`format: ocppsim-scenario/1`），按 id 引用当前配置里的充电桩，本身不含充电桩或 CSMS 设置。
  同一时间只运行一个场景。
- 时间按**仿真时钟**计：`after`、`wait`、`at` 随 `time_scale` 加速，仿真暂停时停走。
  `clock: {time_scale: 3600}` 时，两小时的会话只需两秒实时。OCPP 线上的时间戳仍是实际时间
  （CSMS 看到的会话被压缩，电量按仿真时长累计）。
- 断言按**实际时间**秒等待（`within`，默认 30 秒）：CSMS 的应答速度与加速无关。`within_sim` 则按仿真时间等待。

仓库的 `scenarios/` 目录里有示例，可配合 `configs/mini-csms-demo.yaml` 运行：

```bash
sme-ocpp-sim scenario run scenarios/plug-charge-unplug.yaml -c configs/mini-csms-demo.yaml --out verdict.json
```

## 文件格式

```yaml
format: ocppsim-scenario/1
name: plug-charge-unplug
seed: 7                                   # 记录在判定结果中
clock: {start_at: "2026-12-01T07:55:00+09:00", time_scale: 3600}   # 均可选
params: {tag: "TAG001"}                   # 下面任意字符串中的 ${tag}
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
until: "+4h"                              # 可选的结束时刻（仿真时间）
```

| 键 | 含义 |
|---|---|
| `tracks` | 并行运行；track 内的步骤在其 `charger` 上依次执行 |
| `for_each: {chargers: [A, B]}` | 代替 `charger`：每个充电桩一个 track（`"*"` = 全部）；`${charger}` / `${i}` 为其 id / 从 0 开始的序号 |
| `repeat: n` | 步骤重复 n 次（`${round}` = 从 0 开始的轮次；步骤 id 加 `#1`、`#2` …） |
| `clock.start_at` | 运行时让仿真时钟从此时刻重新开始（`at: "HH:MM"` 的基准） |
| `clock.time_scale` | 本次运行的速度（结束后恢复）；CLI 的 `--time-scale` 优先 |
| `until` | 运行结束；届时仍在执行的步骤判 fail，尚未开始的步骤为 skipped（数量写入事件日志） |

所有键都会检查（拼写错误报错并带位置，例如 `tracks.0.steps.2.do`）；充电桩 id 和 `${...}` 名称在运行前与当前配置核对。
整个字符串为 `"${n}"` 时保持参数类型（数字、布尔）。时刻请加引号（`"08:00"`）：不加引号时 YAML 读成数字，会报错。

### 定时

- `after: <时长>`：上一步之后等待这么久（仿真）再执行。
- `at: "HH:MM[:SS]"`：场景开始后最近的该仿真时刻；`at: "+90m"`：开始后的仿真时间；也接受 ISO 日期时间。
- 时长为秒数，或 `500ms`、`30s`、`5m`、`2h`、`1h30m`、`1d` 这样的文本。

### 动作（`do:`）

| 动作 | 参数 | 效果（与 Web UI 按钮 / HTTP 路由相同） |
|---|---|---|
| `plug` / `unplug` | `connector`（默认 1） | 插 / 拔充电枪（拔枪结束交易） |
| `swipe` | `connector`、`id_tag` | 出示 id tag（开始或停止） |
| `start` | `connector`、`id_tag`（默认：充电桩的第一个） | `plug` + `swipe` |
| `stop` | `connector` | 本地停止交易 |
| `fault` / `clear_fault` | `connector`、`error_code` | 连接器故障 / 清除 |
| `connect` / `disconnect` | | 启动连接循环 / 保持断开 |
| `drop` | `mode`（`abort` / `close`）、`reconnect_after_s` | 立即断开连接 |
| `impair` | `impairments`（整个块） | 实时替换充电桩的 [impairments](http-api.md#transaction-and-response-anomalies) |
| `send` | `action` + `payload` 或 `raw`、`validate`、`timeout` | 注入 CALL / 原始帧 |
| `set_ev` | `connector`、`ev_model`、`initial_soc`、`target_soc` | 该连接器下一笔交易的 EV（仅一次） |
| `wait` | `duration` | 等待（仿真时间） |
| `log` | `message` | 写入事件日志 |

被拒绝的动作（连接器不存在、没有可停止的交易等）是步骤 **error**。

### 断言

```yaml
- expect: {frame: {dir: tx, action: MeterValues, payload: {meterValue.0.sampledValue.0.value: {gt: 0}}}}
- expect: {state: {connector: 1, status: Charging, power_w: {ge: 3000}, has_transaction: true}}
  within_sim: 10m
- expect_not: {dir: tx, action: StartTransaction}    # 窗口内没有该帧则 pass
  within: 20s
- await_csms: {action: RemoteStartTransaction, prompt: "请在 CSMS 上为 CP001 启动充电"}
  within: 5m
```

- `expect.frame`：`dir`（`tx` = 充电桩发送，`rx` = 接收）、`action`、`message_type`（`CALL` / `CALLRESULT` /
  `CALLERROR`）、`payload` matcher。`then:` 检查所匹配 CALL 的应答（同一消息 id）：`message_type` 和 `payload`
  （CALLERROR 的 payload 为 `errorCode` / `errorDescription` / `errorDetails`）。
- `expect.state`：`connector`、`status`、`has_transaction`、`power_w`、`soc_pct`、`meter_wh`、`online`、`connected`。
- `expect_not`：帧条件；窗口内一出现匹配即 fail。
- `await_csms`：等待来自 CSMS 的 CALL（CSMS 侧的操作员动作）；等待期间 `prompt` 显示在状态和事件日志中。
- 每个 track 有一个**帧游标**：从场景开始处起步；通过的 `expect.frame` / `await_csms` 把它移到匹配的帧
  （有 `then` 时移到应答）；动作步骤把它移到动作开始时的最新帧（因此断言开始前已到达的应答也能找到）。
  `expect_not` 和 `expect.state` 不移动它。帧断言默认在游标之后查找，所以同一动作写两个 `expect` 需要两帧。
  `from: action` 改为从上一个动作开始处查找，`from: scenario_start` 从场景开始处查找。
- `await_csms` 未设 `within` 时同样只等 30 秒实际时间；等待操作员时请设长一些。
- `on_fail: stop`（默认）在步骤失败或被拒绝时结束整个场景；`continue` 则继续。

**Matcher**（Q6：内置，不用 JSONPath，不嵌代码）：路径用点分隔，整数段为列表下标
（`meterValue.0.sampledValue.0.value`）。值为字面量（相等），或须全部成立的运算符：`in: [...]`、
`gt` / `ge` / `lt` / `le: n`（数字字符串按数值比较）、`re: "正则"`（search）、`exists: true|false`。

## 判定结果（verdict）

```json
{"format": "ocppsim-verdict/1", "scenario": "plug-charge-unplug", "result": "pass", "error": null, "seed": 7,
 "started": "2026-11-28T17:12:16+09:00", "wall_s": 2.1, "sim_s": 7423.0, "tool_version": "0.3.0",
 "trace": null, "steps": [{"id": "4", "track": "session", "kind": "expect", "status": "pass",
 "t_wall": 0.02, "t_sim": 60.0, "message": "StartTransaction: seq 7, answer seq 8", "evidence": [7, 8]}]}
```

`result`：`pass`、`fail`（断言失败）、`error`（动作被拒绝或场景无法启动）、`aborted`（被外部停止）。
步骤 `status`：`pass` / `fail` / `error` / `skipped` / `aborted`。`evidence` 是帧序号（帧日志 / `--trace` 文件）。

## 命令行

```bash
sme-ocpp-sim scenario run FILE [-c CONFIG] [--out verdict.json] [--junit junit.xml] [--trace frames.jsonl]
                              [--time-scale N] [-v] [--log-format json]
sme-ocpp-sim scenario validate FILE [-c CONFIG]
```

`run` 不带 Web UI 启动引擎（配置启用时也启动 Mini CSMS），运行场景并写出判定结果（无 `--out` 时输出到 stdout），
退出码 **0** pass、**1** fail、**2** error / aborted / 场景或配置无效。`--junit` 每个断言（及每个失败的动作）一个测试用例；
`--trace` 以 JSON Lines（帧日志导出格式）写出场景所用充电桩的帧。

## HTTP API 与 MCP

| 方法 | 路径 | MCP 工具 | 含义 |
|---|---|---|---|
| POST | `/api/scenarios/run` | `run_scenario` | 请求体：场景 YAML（或 JSON）文本；启动（已有场景运行时 409） |
| GET | `/api/scenarios/current` | `get_scenario_status` | `state`（`idle` / `running` / `done`）、track 进度、步骤计数、prompt |
| POST | `/api/scenarios/abort` | `abort_scenario` | 停止（result `aborted`） |
| GET | `/api/scenarios/result?format=json\|junit` | `get_scenario_result` | 已结束场景的判定结果 |
| POST | `/api/frames/expect` | `expect_frame` | 一次性：最多等 `within` 秒，等 `since` 之后出现匹配的帧 |

Web UI 暂无场景面板；场景的 `log` 行及开始 / 结果显示在事件日志中。

## 限制

- 生成器 track（`generate:`，流量生成器）随 v0.3 M4 提供；在此之前 `seed` 只做记录。
- `send` 注入充电桩 → CSMS 的 CALL；CSMS 侧步骤用 `await_csms`（或 Mini CSMS API）。
- trace 文件是当前的帧日志格式（运行中边跑边写）；带版本的 trace 格式以后提供。
- `repeat`（最多 1000 轮）中 `at: "+dur"` 从每轮开始计；不能用时刻 / 日期时间形式的 `at`。
- `clock.time_scale` 运行后恢复（期间速度被改过则不恢复）；`clock.start_at` 让引擎的仿真时钟重新开始，运行后保持原样。
- 每个充电桩最多保留 200 000 帧；需要已丢弃帧的断言以 "frame buffer overflowed" 判 fail。
- `POST /api/frames/expect`：为免漏掉自己动作引起的帧，动作前先读 `last_seq`（`GET /api/frames`）并作为 `since`
  传入；`since` 早于帧日志时，返回中带 `oldest_seq` 和 `warning`。
