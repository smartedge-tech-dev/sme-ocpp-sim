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
| POST | `/api/chargers/bulk/connect` | `{"ids": ["CP-0001", "CP-0002"]}`、`["CP-0001"]` 或 `{"all": true}` |
| POST | `/api/chargers/bulk/disconnect` | 同上 |

批量操作不是原子的：逐台处理，出错即停止，之前的充电桩已经被修改。

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
