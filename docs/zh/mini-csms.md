# Mini CSMS

SME OCPPSim 内置一个简易 CSMS：同一进程内的 WebSocket 服务器，在 Web UI 中有独立面板。可用于无后端试用模拟器（本工具的充电桩
连接本工具的 CSMS）、演示，或作为被测真实充电桩的 CSMS。

它刻意保持精简：对站点发来的每条消息返回有效的最小应答，记录所获信息，并发送在 JSON 编辑器中输入或在计划（plan）中登记的
CALL。没有数据库、用户管理或能量管理逻辑。

## 快速开始

```bash
sme-ocpp-sim run -c configs/mini-csms-demo.yaml
```

[演示配置](https://github.com/smartedge-tech-dev/sme-ocpp-sim/blob/main/configs/mini-csms-demo.yaml) 在 `ws://127.0.0.1:9000/ocpp` 启动 Mini CSMS，并启动两台连接它的充电桩（OCPP 1.6 与 2.0.1）。
每台充电桩的 BootNotification 被接受 30 秒后，计划会发送 SetChargingProfile（TxDefaultProfile，3 kW），随后显示在
充电桩的 Smart Charging 面板中。通过 **模拟 → Mini CSMS** 查看站点。

`--mini-csms`、`--csms-host`、`--csms-port` 可在不修改配置的情况下仅对本次运行启用或更改监听地址。

## 功能

| 项目 | 行为 |
|---|---|
| 版本 | OCPP 1.6J 与 2.0.1（子协议 `ocpp1.6` / `ocpp2.0.1`），可用 `mini_csms.versions` 限定 |
| 子协议 | 取站点提供的第一个已启用子协议。若无匹配，握手完成但不带 `Sec-WebSocket-Protocol`，随即关闭连接（代码 1002），拒绝记录显示在面板中 |
| 站点 ID | URL 路径的最后一段：`ws://host:9000/<任意>/<站点 ID>` |
| 安全 | Profile 0（无认证）或 Profile 1（HTTP Basic 认证，`auth.basic: true`）。无 TLS 服务器，不支持 Profile 2 / 3 |
| BootNotification | `boot.status`（`Accepted` / `Pending` / `Rejected`）与 `boot.interval` |
| 授权 | `authorize.default`，按标签覆盖 `authorize.tags`（Authorize、StartTransaction、StopTransaction、TransactionEvent） |
| 交易 | 1.6 StartTransaction 从一个计数器分配 transactionId；2.0.1 TransactionEvent Started / Updated / Ended 按 transactionId 跟踪 |
| 其他动作 | 该版本的所有 CP → CSMS 动作都返回有效的最小应答（见下文）；未知动作返回 CALLERROR `NotImplemented` |
| 容错 | 不符合 schema 的载荷照常应答并记录为警告；`strict: true` 时改为返回 CALLERROR |
| 重连 | 同一 ID 再次连接时替换旧连接 |

带有决策的固定应答：DataTransfer → `UnknownVendorId`，SignCertificate → `Rejected`（无 CA），2.0.1 的
Get15118EVCertificate / GetCertificateStatus → `Failed`，NotifyEVChargingNeeds → `Rejected`，
NotifyEVChargingSchedule → `Accepted`。其余应答均为空对象。所有应答都在测试中用 `ocpp` 库的响应 schema 校验。

## 面板

**模拟 → Mini CSMS** 显示：

- 监听 URL（或未运行的原因）以及被拒绝的连接（密码错误、不支持的子协议）；
- 站点列表：版本、Boot 状态、最近 Heartbeat、进行中的交易（每个 ID 的最新连接在断开后仍保留）；
- 所选站点的 BootNotification、各连接器 / EVSE 的最新状态、交易、最近计量值、计划状态、载荷警告及 CSMS 视角的帧；
- **JSON 编辑器**：选择 CSMS → CP 动作、输入载荷并发送。发送前先用该版本的 schema 校验，不匹配时显示出错字段且不发送。
  应答（CALLRESULT 或 CALLERROR）显示在编辑器下方。

## 计划（plan）

`mini_csms.plan` 列出每次连接时自动发送的 CALL：

```yaml
mini_csms:
  enabled: true
  plan:
    - at_s: 30              # 触发后的秒数
      after: boot           # boot = 连接后首次被接受的 BootNotification，connect = WebSocket 连接时
      ocpp_version: "1.6"   # 仅该版本的站点（省略：所有已启用版本）
      charger: "CP-*"       # 仅匹配的站点 ID（省略：所有站点）
      action: SetChargingProfile
      payload:
        connectorId: 1
        csChargingProfiles:
          chargingProfileId: 1
          stackLevel: 0
          chargingProfilePurpose: TxDefaultProfile
          chargingProfileKind: Relative
          chargingSchedule:
            chargingRateUnit: W
            chargingSchedulePeriod: [{ startPeriod: 0, limit: 3000 }]
```

载荷按报文形式（camelCase）书写，加载配置时用 schema 校验。省略 `ocpp_version` 时载荷必须适用于所有已启用版本，因此版本特有的
动作需要指定。每项在每次连接中发送一次；站点重连后计划重新执行。

## 测试真实充电桩

设置 `host: "0.0.0.0"`（或 `--csms-host 0.0.0.0`），并把充电桩指向 `ws://<本机>:9000/ocpp`。在 Docker 中还需发布端口
（`-p 9000:9000`）。Profile 1 时需在 `auth.credentials` 中登记站点。

!!! warning "暴露范围"
    `host: "0.0.0.0"` 且 `auth.basic: false` 时，任何能访问该端口的人都能以任意站点 ID 连接并得到应答（启动时会记录警告日志）。
    Mini CSMS 没有 TLS（profile 2 / 3），也没有限流：只在可信网络中使用，监听 `127.0.0.1` 以外的地址时请开启 Basic Auth。
    固定上限：同时连接 500（超出的握手返回 HTTP 503），记住的站点 200（先忘记最早断开的）。Basic Auth 失败无论原因都返回同样的
    `401` 和 `WWW-Authenticate`（原因只显示在面板的拒绝列表中）。CSMS 发送的秘密值（1.6 `AuthorizationKey`、2.0.1
    `BasicAuthPassword`、SetNetworkProfile 的 VPN / APN 密码）在报文日志和发送记录中显示为 `********`，充电桩收到的是真实值。

Mini CSMS 不能替代用于一致性测试的真实 CSMS；本项目的 CI 也保留独立实现的 `ocpp` 库对端作为测试对象。

配置字段：[mini_csms](configuration.md#mini_csms)。HTTP 路由：[Mini CSMS API](http-api.md#mini-csms)。
