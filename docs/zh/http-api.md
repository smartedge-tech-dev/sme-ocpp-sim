# HTTP API

Web UI 使用的 HTTP API 同样可以从测试脚本调用。基础 URL：`http://127.0.0.1:8082`。
错误：未知充电桩为 `404`；输入无效或当前状态不允许的操作为 `400`（请求校验错误也是 `400`）。
CSMS 未在时限内应答的 DataTransfer 为 `504`，CSMS 以 CALLERROR 应答时为 `502`。

成功的请求返回 `{"ok": true, ...}`（GET 路由直接返回数据本身）。

## 通用路由

| 方法 | 路径 | 含义 |
|---|---|---|
| GET | `/api/version` | 版本 |
| GET | `/api/state` | 完整状态快照：充电桩、connector、数值 |
| GET / PUT | `/api/config` | 当前配置（PUT 应用新配置；只有变化的充电桩会重连） |
| POST | `/api/config/save` | 把配置写回 YAML 文件（见 [配置](configuration.md#saving-from-the-web-ui)）；文件写入失败时为 `500` |
| POST | `/api/simulation` | 运行 / 暂停与时间倍率：`{"running": true, "time_scale": 10}`（均可省略；其他字段会被拒绝） |
| GET / PUT | `/api/csms-defaults` | `csms_defaults` 块（PUT 整块替换并重新加载） |
| GET / PATCH | `/api/chargers/{id}/settings` | 单台充电桩的连接 / 认证设置（见下文） |
| GET | `/api/chargers/{id}/configuration` | GetConfiguration 所见的运行时配置键；OCPP 2.0.1 为 Device Model 变量（见下文） |
| POST | `/api/devices` | 添加充电桩 |
| GET / PUT | `/api/devices/{id}/config` | 单台充电桩的配置（PUT 整项替换） |
| POST | `/api/devices/{id}/duplicate` | 复制充电桩（ID 递增） |
| DELETE | `/api/devices/{id}` | 删除充电桩 |

密码始终以 `********` 返回；原样发回掩码会保留已保存的密码。由于 PUT 整项替换，省略 `password` 表示没有密码。

`/api/chargers/{id}/settings` 包含 `csms_url`、`ocpp_version`、`password`、`ca_file`、`client_cert_file`、`client_key_file`、`verify_tls`、`id_tags`、
`config_keys`，以及实际生效的 CSMS URL / OCPP 版本（`effective`）。PATCH 只修改请求中出现的字段；`null` 表示
取消该设置（`id_tags` / `config_keys` 变为空；`verify_tls` 只能是 true 或 false）。密码掩码同上可原样发回。
修改通过重新加载配置生效，只有 `POST /api/config/save` 才写入文件。

对 OCPP 2.0.1 充电桩，`/api/chargers/{id}/configuration` 返回 `{"keys": [], "ocpp_version": "2.0.1",
"variables": [...]}`：每个 Device Model 变量一项，含 `component`、`component_instance`、`evse_id`、`connector_id`、
`variable`、`variable_instance`、`data_type`、`mutability`、`unit`、`min_limit`、`max_limit`、`values_list`、
`reboot_required` 和 `attributes`（该变量具有的 `Actual` / `Target` / `MinSet` / `MaxSet` 值；WriteOnly 变量为
`null`），并包含 CSMS 通过 SetVariables 修改的值。没有修改变量的路由：CSMS 使用 SetVariables，初始值由 YAML 文件
（`variables:`）设置。

## Excel 与模板 { #excel-and-templates }

工作簿结构与模板规则见 [Excel 导入导出与充电桩模板](excel-templates.md)。

| 方法 | 路径 | 含义 |
|---|---|---|
| GET | `/api/config/xlsx` | 以 Excel 工作簿导出充电桩、配置键、变量和 EV 型号（密码为 `********`） |
| GET | `/api/config/xlsx-template` | 只有列标题的空白工作簿 |
| POST | `/api/config/import` | multipart 字段 `file`：应用工作簿（全部或全不）。返回 `{"ok": true, "warnings": [...]}`，或 `400` 加 `errors`（每个问题一条，含工作表、行、列） |
| GET | `/api/templates` | 充电桩模板：`name`、`title`、`description`、`params`、`source`（`builtin` / `user`） |
| POST | `/api/templates/reload` | 重新读取用户模板目录：`{"dir", "loaded", "errors", "templates"}` |
| POST | `/api/templates/from-device/{id}` | `{"name", "title"?, "description"?, "overwrite"?}`：把充电桩的模板字段保存为用户模板（已存在且未设 `overwrite` 时 `409`） |
| POST | `/api/chargers/bulk/create` | `{"id": "CP-0001", "count": 10, "template"?, "params"?, "password"?, "fields"?}`：一次重载添加 `count` 台；ID 和末尾为数字的密码递增，`fields` 为每台共用的其他字段（`csms_url`、`ocpp_version`、`id_tags` …）。ID 已存在时 `409`（一台也不添加） |

## 充电桩操作

| 方法 | 路径 | 请求体 |
|---|---|---|
| POST | `/api/chargers/{id}/connect` | 无（启动连接循环） |
| GET | `/api/chargers/{id}` | 无——充电桩快照，外加其 OCPP 版本的故障码 `fault_codes` |
| POST | `/api/chargers/{id}/disconnect` | 无（在 `connect` 之前保持断开；交易像网络中断一样保留） |
| POST | `/api/chargers/{id}/plug` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/unplug` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/swipe` | `{"connector": 1, "id_tag": "04A1B2C3"}` — `id_tag` 必填 |
| POST | `/api/chargers/{id}/start` | `{"connector": 1, "id_tag": "04A1B2C3"}` — 插枪并刷卡；`id_tag` 默认为第一个配置的 tag（没有则 `400`）；connector 已有 transaction 或处于故障时为 `400` |
| POST | `/api/chargers/{id}/stop` | `{"connector": 1}` |
| POST | `/api/chargers/{id}/fault` | `{"connector": 1, "error_code": "OtherError"}`——该桩 `fault_codes` 之一（1.6 为 `ChargePointErrorCode`，2.0.1 为 `ReasonEnumType` 中的故障值，否则 `400`）；结束进行中的交易（1.6 原因为 `Other`，2.0.1 的 `stoppedReason` 为该故障码）并上报 `Faulted` |
| POST | `/api/chargers/{id}/clear-fault` | `{"connector": 1}`——恢复为 Available / Preparing |
| POST | `/api/chargers/{id}/datatransfer` | `{"vendor_id": "...", "message_id": "...", "data": "..."}`（充电桩 → CSMS） |
| GET / PUT | `/api/chargers/{id}/impairments` | `{"block": {"RemoteStartTransaction": "NotSupported"}, "latency_ms": 200, ...}` — 命令屏蔽、延迟、周期断线，见[下文](#command-blocking-and-frame-injection) |
| POST | `/api/chargers/{id}/drop` | `{"mode"?: "abort" \| "close", "reconnect_after_s"?}` — 立即断线，见[下文](#network-impairments) |
| POST | `/api/chargers/{id}/send` | `{"action": "Heartbeat", "payload": {}, "validate": true}` 或 `{"raw": "[2,\"id-1\",\"Heartbeat\",{}]"}` — 帧注入，见[下文](#command-blocking-and-frame-injection) |
| POST | `/api/chargers/bulk/connect` | `{"ids": ["CP-0001", "CP-0002"]}`、`["CP-0001"]` 或 `{"all": true}` |
| POST | `/api/chargers/bulk/disconnect` | 同上 |

批量操作不是原子的：逐台处理，出错即停止，之前的充电桩已经被修改。

## 命令屏蔽与帧注入 { #command-blocking-and-frame-injection }

**命令屏蔽**（充电桩的 `impairments.block`，见[配置](configuration.md#chargers)）：
`GET /api/chargers/{id}/impairments` 返回当前设置，`PUT` 整块替换（充电桩的 OCPP 版本不接收的 action 返回 `400`；
可用的 action 见 `GET /api/chargers/{id}` 的 `csms_actions`）。修改立即作用于运行中的充电桩，不重连，
并由 `POST /api/config/save` 写回文件。被屏蔽的 CSMS → 充电桩 CALL 以 CALLERROR `NotImplemented` / `NotSupported`
应答（OCPP-J 1.6 与 2.0.1 名称相同），`NoReply` 则完全不应答，CSMS 侧自行超时。帧日志中收到的 CALL 标注
`blocked: <code>` / `blocked: no reply`，充电桩快照带 `impaired: true`（UI 徽章上显示 `!`）。UI：充电桩详情面板的
**网络异常**。

### Transaction and response anomalies

同样在 `impairments` 块里、即时生效（v0.3）：`remote_start: accept_no_tx`（远程启动回 `Accepted`，但不会有交易，
connector 状态也不变）、`meter`（`drop_ratio`、`reorder_window`、`end_after_stop`、`voltage_noise_v`）以及
`response_delay_ms`（action → 应答延迟的 ms），见 [配置](configuration.md#chargers)。CSMS 会看到：

- `accept_no_tx`：一个被接受的 RemoteStartTransaction / RequestStartTransaction，之后什么都没有（没有 StartTransaction /
  TransactionEvent `Started`，也没有 StatusNotification）。
- `drop_ratio`：周期 MeterValues（1.6）/ `Updated` event（2.0.1）缺失；2.0.1 交易的 `seqNo` 出现缺口。
- `reorder_window`：周期计量消息成批、乱序到达（时间戳与 2.0.1 `seqNo` 不单调）。
- `end_after_stop`：交易的 StopTransaction / Ended event 之后还来一条计量消息。
- `voltage_noise_v`：围绕额定电压抖动的 Voltage。
- `response_delay_ms`：对该 action 的应答迟到（或在 CSMS 超时之后才到）；心跳等其它通信不受影响。

随机抽取（丢弃、顺序、噪声）使用 `seed`（默认：充电桩 id 的 CRC32），每个 connector 一条独立序列，可复现。
即时修改该块时序列继续（只有改 `seed` 才重新开始），充电桩重启后从头开始。`reorder_window` 攒着的消息在窗口改为 0 / 1 时
以及交易结束时立即发出；攒着的 2.0.1 event 的 `offline` 标志按入队时的连接状态设置，时间戳与 `seqNo` 保持原值。充电桩重启会丢弃攒着的消息。
`end_after_stop` 且 `meter.begin_end_messages` 关闭时，OCPP 1.6 在 StopTransaction 之后额外发一条停止时刻读取的
`Sample.Periodic` MeterValues（迟到的结束读数）。

### Network impairments

同一个 `impairments` 块还包含延迟（`latency_ms`、`jitter_ms`、`direction`、`seed`）与周期断线（`drop_every_s`、
`drop_jitter_s`、`drop_mode`、`down_s`），见[配置](configuration.md#chargers)。`PUT` 接收整块（省略的字段取缺省值；
`jitter_ms` > `latency_ms` 等越界值返回 `400`），不重连即作用于当前连接：下一帧就使用新的延迟。

`POST /api/chargers/{id}/drop` 立即断开连接，返回 `{"ok": true, "mode", "reconnect_after_s"}`。`mode` 缺省为
`drop_mode`，`reconnect_after_s`（0 – 86400）缺省为 `down_s`；未连接时返回 `409`。充电桩在该等待时间后重连，
不走 back-off。模拟断线**不算**连接失败：即使在 Boot Accepted 之前，也不计入 `reconnect.max_retries` /
`boot_max_retries`。交易与待发消息保留。快照在下一次 Boot Accepted 之前 `last_error.kind` 为 `simulated_drop`，
断线次数见 `drops`，任一异常开启时 `impaired` 为 `true`。UI：充电桩详情面板的 **网络异常**（含 **立即断线**）。

**帧注入**（`POST /api/chargers/{id}/send`）是专家用入口，用于测试 CSMS 如何处理模拟器自己不会发送的消息：

- `{"action", "payload", "validate": true, "timeout"}` 发送一个 CALL，返回 `{"ok": true, "result": {...}}`，
  CALLERROR 时返回 `{"ok": false, "error": {"code", "description", "details"}}`；超时 `504`（`timeout` 单位秒，
  默认为调用超时）。`validate: true` 时 action 必须是该版本的充电桩 → CSMS action，payload 必须符合请求 schema
  （否则 `400`）；应答不符合应答 schema 时附带 `warning`。`validate: false` 可发送任意 action 和任意 JSON payload。
- `{"raw": "<text>"}` 把文本原样写入 WebSocket，不等待任何应答；对它的应答在帧日志中显示为 unsolicited response。
- 不改变站内状态：注入的 StartTransaction 不会生成交易，注入的 StatusNotification 不改变连接器。注入的帧在帧日志中
  标注 `injected` / `injected (raw)`。充电桩未连接时返回 `409`。
- 每个连接只能有一个未应答的 CALL（OCPP-J），因此充电桩自己的 CALL（Heartbeat、MeterValues 等）会排在注入的 CALL
  之后，直到它得到应答或超时；所以 `timeout` 最大 60 秒。
- `raw` 与序列化后的 `payload` 最多 1,000,000 字符（`400` / `413`）。uniqueId 与未应答的充电桩 CALL 相同的 raw 帧
  以 `409` 拒绝（其应答会错误地完成该 CALL）。

## Smart Charging

| 方法 | 路径 | 含义 |
|---|---|---|
| GET | `/api/chargers/{id}/profiles?connector=` | 已安装的充电 profile |
| GET | `/api/chargers/{id}/composite?connector=&duration=&unit=` | 在本地计算的 GetCompositeSchedule 应答（`connector` 0 = 整台充电桩；也接受别名 `evse`） |
| POST | `/api/chargers/{id}/external-limit` | 请求体 `{"connector": 0, "limit_w": 7000}`（`limit_w` 为 `null` 表示解除）。外部（能源管理）限值：OCPP 2.0.1 安装 ChargingStationExternalConstraints profile 并发送 NotifyChargingLimit / ClearedChargingLimit；OCPP 1.6 返回 400（不支持） |

对 OCPP 2.0.1 充电桩，`connector` 即 EVSE id（0 = 整个充电站）。

## 帧日志

| 方法 | 路径 | 含义 |
|---|---|---|
| GET | `/api/frames?since=&until=&charger=&action=&dir=&limit=` | `since < seq <= until` 的 OCPP 帧，可按充电桩、action 和方向过滤 |
| GET | `/api/frames.jsonl?...` | 同样内容的 JSON Lines 流式下载（不指定 `limit` 时为缓冲区中所有匹配的帧） |

`/api/frames` 返回 `{"frames": [...], "last_seq": n, "truncated": bool}`：匹配帧中最新的至多 `limit` 条
（默认 500，最大 5000），按从旧到新排列。`truncated: true` 表示更早的匹配帧被省略；`last_seq` 是整个日志的
最新序号（不受过滤影响）。

帧日志只包含 OCPP 帧，不含 HTTP 头和密码。

## Mini CSMS { #mini-csms }

内置 [Mini CSMS](mini-csms.md) 的路由。站点 ID 是站点连接时使用的 ID。

| 方法 | 路径 | 含义 |
|---|---|---|
| GET | `/api/csms` | 状态（`enabled`、`running`、`error`、`url`、`versions` 等）、站点（`chargers`）与被拒绝的连接（`rejections`） |
| GET | `/api/csms/chargers/{id}` | 单个站点：Boot 信息、各连接器 / EVSE 最新状态、交易、最近计量值、各动作次数与最新载荷、警告、已发送 CALL、计划状态 |
| GET | `/api/csms/transactions` | 所有站点的交易 |
| GET | `/api/csms/actions?version=` | `1.6` / `2.0.1` 可发送的 CSMS → CP 动作 |
| POST | `/api/csms/chargers/{id}/call` | 请求体 `{"action", "payload", "timeout"?}`。先用 schema 校验（不匹配返回 400 并指出字段）再发送。应答为 `{"ok": true, "result"}`（应答不符合 schema 时附 `warning`），CALLERROR 时为 `{"ok": false, "error": {"code", "description", "details"}}` |
| GET | `/api/csms/frames?since=&charger=&limit=` | Mini CSMS 自有帧日志（方向以 CSMS 视角） |

错误：404 未知站点，400 schema 不匹配，409 站点未连接，504 超时未应答，409 Mini CSMS 未运行（`GET /api/csms` 除外）。

## WebSocket `/ws`

需要实时更新的客户端连接 `ws://127.0.0.1:8082/ws`：

1. 第一条消息是 `{"type": "snapshot", "data": ...}`，内容与 `GET /api/state` 相同。
2. 之后引擎推送 `charger_state`、`values`、`frame` 消息，每条都带有按连接计数 1、2、3 …… 的 `push_seq`。
3. 引擎事件以 `{"type": "log", "event": {...}}` 推送，使用自己的 `event.seq` 编号（没有 `push_seq`）。
4. 配置重新加载后会再发送一条 `{"type": "snapshot"}`。

`push_seq` 出现缺号，表示因客户端过慢而丢弃了消息。如果其中有状态推送或重新加载通知，下一条状态推送将是完整推送
（`"full": true`），状态会自行恢复（也可以重新获取 `/api/state`）。丢失的帧可通过
`GET /api/frames?since=<最后收到的 frame seq>` 获取。完整的 `charger_state` 列出*所有*充电桩：请用它替换客户端的充电桩列表
（其中缺少的充电桩已在重新加载时被删除）；完整的 `values` 同样替换全部数值。不要依赖两条完整推送的先后顺序。
