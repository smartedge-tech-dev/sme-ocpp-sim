# MCP、健康检查与日志

## MCP 端点

SME OCPPSim 在 Web 端口上提供 [MCP](https://modelcontextprotocol.io/) 端点，AI 助手（Claude Code、Claude
Desktop 及其他 MCP 客户端）可以借此操作模拟充电桩：列出充电桩、插枪、开始和停止交易、注入故障和网络异常、查看帧日志。

- URL：`http://127.0.0.1:8082/mcp`（MCP streamable HTTP，无状态，JSON 应答；与 Web UI 同一端口）
- 没有认证，与 Web UI 和 [HTTP API](http-api.md) 相同。默认的 `--host 127.0.0.1` 只接受 `Host` / `Origin`
  头为回环地址的请求（防 DNS 重绑定）；`--host 0.0.0.0`（Docker、局域网）时，任何能访问该端口的人都可使用。

Claude Code：

DNS 重绑定防护只作用于 `/mcp`：HTTP API 的其余路由与 Web UI 一样没有这项防护，请绑定到回环地址或可信网络。

```bash
claude mcp add --transport http ocpp http://127.0.0.1:8082/mcp
```

只支持 stdio 的客户端（如 Claude Desktop）可以使用 `npx mcp-remote http://127.0.0.1:8082/mcp` 之类的桥接。

每个工具在进程内调用一个 [HTTP API](http-api.md) 路由：校验、错误（HTTP 错误变为带其消息的工具错误）和效果都与
Web UI 中的按钮相同。充电桩相关工具通过 `cid` 接收充电桩 ID；连接器编号从 1 开始。

| 工具 | HTTP 路由 | 含义 |
|---|---|---|
| `get_version` | `GET /api/version` | 产品与库的版本 |
| `get_state` | `GET /api/state` | 所有充电桩及其状态（Web UI 显示的快照） |
| `export_config` | `GET /api/config` | 运行中的配置（YAML，密码已遮蔽） |
| `set_simulation` | `POST /api/simulation` | 运行 / 暂停、时间倍率 |
| `get_charger` | `GET /api/chargers/{id}` | 单个充电桩及其故障代码和 CSMS 动作 |
| `get_charger_settings` | `GET /api/chargers/{id}/settings` | 连接 / 授权设置 |
| `get_charging_profiles` | `GET /api/chargers/{id}/profiles` | 已安装的充电配置文件 |
| `get_composite_schedule` | `GET /api/chargers/{id}/composite` | 合成计划（本地计算） |
| `get_frames` | `GET /api/frames` | 帧日志（过滤 `charger`、`action`、`dir`） |
| `get_impairments` | `GET /api/chargers/{id}/impairments` | 故障注入设置 |
| `connect_charger` / `disconnect_charger` | `POST /api/chargers/{id}/connect` / `disconnect` | 连接循环开 / 关 |
| `plug_in` / `unplug` | `POST /api/chargers/{id}/plug` / `unplug` | EV 充电枪 |
| `authorize` | `POST /api/chargers/{id}/swipe` | 出示 ID 标签（开始 / 停止） |
| `start_transaction` / `stop_transaction` | `POST /api/chargers/{id}/start` / `stop` | 插枪 + ID 标签 / 本地停止 |
| `data_transfer` | `POST /api/chargers/{id}/datatransfer` | 向 CSMS 发送 DataTransfer |
| `set_fault` / `clear_fault` | `POST /api/chargers/{id}/fault` / `clear-fault` | 连接器故障 |
| `set_external_limit` | `POST /api/chargers/{id}/external-limit` | 外部充电限制（2.0.1） |
| `set_impairments` | `PUT /api/chargers/{id}/impairments` | 命令屏蔽、延迟、周期断线、远程启动 / 计量值 / 慢响应异常 |
| `drop_connection` | `POST /api/chargers/{id}/drop` | 立即断开连接 |
| `send_frame` | `POST /api/chargers/{id}/send` | 注入 CALL 或原始帧（专家用途） |
| `mini_csms_status` | `GET /api/csms` | 内置 [Mini CSMS](mini-csms.md) 的状态 |
| `mini_csms_actions` | `GET /api/csms/actions` | Mini CSMS 可发送的 CSMS → 充电桩动作 |
| `mini_csms_call` | `POST /api/csms/chargers/{id}/call` | 从 Mini CSMS 发送 CSMS → 充电桩的 CALL |
| `run_scenario` | `POST /api/scenarios/run` | 启动[场景](scenarios.md)（参数 `body`：YAML 文本，或 `builtin:<name>`） |
| `get_scenario_status` | `GET /api/scenarios/current` | 场景状态与进度 |
| `get_scenario_result` | `GET /api/scenarios/result` | 已结束场景的判定结果 |
| `abort_scenario` | `POST /api/scenarios/abort` | 停止正在运行的场景 |
| `list_builtin_scenarios` | `GET /api/scenarios/builtin` | 内置场景（name、title、description） |
| `get_traffic` | `GET /api/traffic` | 常驻[流量](traffic.md)的状态、计数、功率 |
| `set_traffic` | `PUT /api/traffic` | 替换 `traffic:` 块 |
| `start_traffic` / `stop_traffic` | `POST /api/traffic/start` / `stop` | 启动 / 停止常驻流量 |
| `preview_traffic_plan` | `GET /api/traffic/plan` | 计划的到达（对 seed 确定） |
| `expect_frame` | `POST /api/frames/expect` | 等待匹配条件与 payload matcher 的帧 |

充电桩动作类工具（`plug_in`、`start_charging`、`send_frame` 等）返回 `last_seq`（动作之前的帧日志序号）：
作为 `since` 传给 `expect_frame`，就不会漏掉动作引起的帧。

只读工具标有 `readOnlyHint`；`disconnect_charger`、`drop_connection`、`set_impairments`、`send_frame` 和 `mini_csms_call` 标有 `destructiveHint`，
客户端可以在调用前请求确认。

## 健康检查

Web 服务器运行期间，`GET /healthz` 返回 `200` 和 `{"status": "ok", "name", "version", "common_version"}`，
用于 Docker / Kubernetes 探针：

```bash
curl -fsS http://127.0.0.1:8082/healthz
```

## JSON 日志

`--log-format json` 将每条日志输出为一行一个 JSON 对象（`ts`、`level`、`logger`、`message`、附加字段、`exc`），
便于日志采集；Web 服务器自身的日志也使用同一格式。默认为 `--log-format text`。`-v` / `--verbose`
在两种格式下都切换到调试级别。

```bash
sme-ocpp-sim --log-format json
```
