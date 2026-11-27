# 配置

模拟器读取一个 YAML 文件（`-c FILE`）。所有块都可省略；未知字段会报错。完整示例见
[`configs/demo-site.yaml`](https://github.com/smartedge-tech-dev/sme-ocpp-sim/blob/main/configs/demo-site.yaml)。

```yaml
simulation:
  tick_seconds: 1
  time_scale: 1
  running: true

frame_log:
  size: 20000

csms_defaults:
  url: "ws://localhost:8180/steve/websocket/CentralSystemService"
  ocpp_version: "1.6"
  security_profile: 0
  reconnect: { min_s: 1, max_s: 60, jitter: 0.2 }

ev_models:
  - id: "demo-60kwh"
    capacity_kwh: 60
    max_ac_kw: 11
    max_current_a: 16
    phases: 3

chargers:
  - id: "CP-0001"
    max_power_w: 6000
    id_tags: ["04A1B2C3", "TAG-0002"]
    config_keys:
      HeartbeatInterval: 300
```

!!! warning "ID、标签和密码请加引号"
    在 YAML 1.1 中，不加引号的 `0123`、`1e5`、`ON`、`OFF`、`yes`、`no` 都不是字符串。标识和文本字段
    （`id`、`id_tags`、`password`、URL、`vendor` 等）只接受加引号的字符串，并提示你加上引号，而不会悄悄把 `0123` 变成 `83`。

## 从 Web UI 保存 { #saving-from-the-web-ui }

在 UI 中的修改会立即生效（只有被修改的充电桩会重连），也可以保存回 YAML 文件。保存时，会把发生变化的顶层块
（`simulation`、`frame_log`、`csms_defaults`、`ev_models`、`chargers`）整块替换：

- 变化块**之外**的注释会保留，文件的换行符和 UTF-8 BOM 也会保留。
- 变化块**之内**的注释会丢失。任意一台充电桩被修改时，整个 `chargers:` 块都会被重写，所以**不要在单台充电桩的条目里写注释**——
  请写在 `chargers:` 键的上方。
- 与默认值相同的值不会写出。
- 如果某个块无法拼接（不常见的排版），会重写整个文档并丢失所有注释，同时在日志中输出警告。

HTTP API 不会返回密码，而是显示为 `********`。带着掩码原样发回配置时，会保留已保存的密码。

## simulation

| 字段 | 默认值 | 含义 |
|---|---|---|
| `tick_seconds` | `1.0` | 功率模型的步长（秒，> 0） |
| `time_scale` | `1.0` | 只加速功率模型（爬坡、电能寄存器、SoC）。充电计划和 OCPP 时序始终使用真实时间 |
| `running` | `true` | `false` 冻结功率模型；OCPP 会话继续运行 |

## frame_log

| 字段 | 默认值 | 含义 |
|---|---|---|
| `size` | `20000` | 内存中保留的 OCPP 帧数（所有充电桩合计，≥ 100） |

## csms_defaults

| 字段 | 默认值 | 含义 |
|---|---|---|
| `url` | 无 | CSMS 的 WebSocket URL（`ws://` 或 `wss://`）；充电桩 ID 作为最后一段路径追加。除非每台充电桩都有 `csms_url`，否则必填 |
| `ocpp_version` | `"1.6"` | `"1.6"` 或 `"2.0.1"`；`"2.1"` 会报错，提示将在 v0.3 支持 |
| `security_profile` | `0` | `0`、`1` 或 `2`；每台充电桩都按它检查，见 [安全配置文件](security-profiles.md) |
| `reconnect.min_s` | `1.0` | 首次重连等待时间（秒） |
| `reconnect.max_s` | `60.0` | 重连等待时间上限（≥ `min_s`）；等待时间按指数增长 |
| `reconnect.jitter` | `0.2` | 等待时间的随机波动（0–1） |

## ev_models

自定义 EV 模型，由 `session.ev_model` 引用。同 ID 的条目会覆盖内置预设。
内置预设：`kei-20kwh`（20 kWh、3 kW、15 A、单相）、`compact-40kwh`（40 kWh、6 kW、30 A、单相）、
`midsize-60kwh`（60 kWh、11 kW、16 A、三相）。

| 字段 | 默认值 | 含义 |
|---|---|---|
| `id` | 必填 | 唯一 ID（加引号的字符串） |
| `capacity_kwh` | 必填 | 电池容量 |
| `max_ac_kw` | 必填 | AC 最大充电功率 |
| `max_current_a` | `32.0` | 每相最大电流 |
| `max_dc_kw` | 无 | 为向前兼容而接受；v0.1 不模拟 DC 充电 |
| `phases` | `1` | 车载充电机使用的相数（1、2 或 3） |
| `min_current_a` | `6.0` | 每相电流低于此值时 EV 无法充电（≤ `max_current_a`） |
| `efficiency` | `0.9` | 进入电池的能量比例（0–1] |
| `curve` | 内置 | `[SoC %, 相对最大值的比例]` 对的列表（充电衰减曲线） |

## chargers

| 字段 | 默认值 | 含义 |
|---|---|---|
| `id` | 必填 | 充电桩 ID（加引号的字符串）。用作 URL 的最后一段路径和 Basic 认证的用户 ID，因此不能包含 `:`；`bulk` 为保留字 |
| `csms_url` | `csms_defaults.url` | 单台充电桩的 CSMS URL |
| `ocpp_version` | `csms_defaults.ocpp_version` | 单台充电桩的 OCPP 版本（`"1.6"` 或 `"2.0.1"`）；同一站点可以混用 |
| `password` | 无 | Basic 认证密码（Security Profile 1 / 2） |
| `ca_file` | 系统信任库 | `wss://` 时用于校验 CSMS 证书的额外 CA 证书包（PEM） |
| `verify_tls` | `true` | `false` 跳过证书校验（仅限测试；日志中会输出警告） |
| `require_subprotocol` | `true` | `false` 时即使 CSMS 未回显 `ocpp1.6` / `ocpp2.0.1` 子协议也保持连接 |
| `vendor` | `"SmartEdge"` | BootNotification 的 `chargePointVendor`（最多 20 个字符） |
| `model` | `"SIM-AC6"` | `chargePointModel`（最多 20） |
| `serial` | 无 | `chargePointSerialNumber`（最多 25） |
| `firmware` | `"0.1.0"` | `firmwareVersion`（最多 50） |
| `connectors` | `1` | connector 数量（1–32）；所有 connector 共用下面的供电设置和 `max_power_w`。OCPP 2.0.1：EVSE 数量（每个一个 connector） |
| `config_keys` | `{}` | OCPP 1.6：配置键初始值，例如 `HeartbeatInterval: 300`；数字和布尔值可以不加引号。2.0.1 充电桩不使用（见 `variables`） |
| `auth_mode` | `local_then_csms` | `local_only`（仅 `id_tags`）、`csms_only`（始终 Authorize）、`local_then_csms`（先查 `id_tags`，再 Authorize）、`accept_all` |
| `id_tags` | `[]` | 本地接受的 ID 标签（每个最多 20 个字符，不可重复） |
| `boot_retry_s` | `60.0` | CSMS 以 interval 0 应答 Pending / Rejected 时的 Boot 重试间隔 |
| `max_power_w` | `6000.0` | 每个 connector 的硬件最大功率 |
| `voltage_v` | `200.0` | 供电标称电压 |
| `phases` | `1` | 供电相数（1、2 或 3） |
| `line_to_line` | `false` | `true` 表示三相供电的 `voltage_v` 为线电压（例如日本三相 200 V 三角形接法）；相电压 = `voltage_v / √3` |
| `ramp_up_w_per_s` | 无 | 每模拟秒的功率上升量（W）；无 = 阶跃变化 |
| `ramp_down_w_per_s` | 无 | 功率下降量；无 = 阶跃变化（限值降低在一个 tick 内生效） |
| `session.ev_model` | `compact-40kwh` | 插在 connector 上的 EV（预设或 `ev_models` 的 ID） |
| `session.initial_soc` | `30.0` | 每次交易开始时的 SoC（%） |
| `session.target_soc` | `90.0` | 达到后 EV 不再接受功率（SuspendedEV） |
| `meter_sample_interval_s` | 键的默认值 | `config_keys` 未设置时 `MeterValueSampleInterval` 的默认值；2.0.1 为 `SampledDataCtrlr.TxUpdatedInterval` 的初始值 |
| `variables` | `[]` | 仅 OCPP 2.0.1 充电桩：Device Model 的值覆盖与用户自定义变量（见下文） |

### variables（OCPP 2.0.1） { #variables }

2.0.1 充电桩用 Device Model 代替 `config_keys`。它自带模拟器实际使用的最小标准变量集，例如
`OCPPCommCtrlr.HeartbeatInterval`、`SampledDataCtrlr.TxUpdatedInterval`（初始值为 `meter_sample_interval_s`，
未设置则为 60）和 `TxUpdatedMeasurands`、`AuthCtrlr.AuthorizeRemoteStart`、`TxCtrlr.StopTxOnInvalidId`、
`OCPPCommCtrlr.MessageAttempts[TransactionEvent]`、`DeviceDataCtrlr.ItemsPerMessage[GetReport|GetVariables|SetVariables]`、
认证控制变量 `AuthCtrlr.Enabled` / `LocalPreAuthorize` / `LocalAuthorizeOffline` / `OfflineTxForUnknownIdEnabled` /
`DisableRemoteAuthorization`（ReadWrite；初始值由 `auth_mode` 决定，SetVariables 修改后立即生效）、
`SmartChargingCtrlr.Enabled`（ReadWrite，不影响行为）和 `NotifyChargingLimitWithSchedules`（默认 `false`）、
`SecurityCtrlr.SecurityProfile` / `Identity` / `BasicAuthPassword`（新密码被接受后充电桩用它重新连接，仅保存在内存中），
以及必需的 ChargingStation / EVSE / Connector 变量（`Available`、`AvailabilityState`、EVSE 的 `Power` 和
`SupplyPhases`）。OCPP 2.0.1 要求的其他变量（例如 `OCPPCommCtrlr.OfflineThreshold`、`TxCtrlr.EVConnectionTimeOut`、
`SmartChargingCtrlr.Entries[ChargingProfiles]`）以描述模拟器行为的值作为只读变量报告。CSMS 用 GetVariables / SetVariables 读写，用 GetBaseReport 获取列表；
`GET /api/chargers/{id}/configuration` 在 `variables` 中显示它们。SetVariables 的修改不会写回 YAML 文件。

`variables` 的每一项指向一个变量：`component`、可选的 `component_instance`、`evse`、`connector`、`variable`、
可选的 `variable_instance`。

- 指向标准变量的项设置它的 `value`（`Actual` 属性）。由其它设置推导出的变量（`auth_mode`、充电桩 ID、CSMS URL、
  EVSE 状态、交易开始 / 结束点）不能设置。
- 其它项是用户自定义变量，需要 `data_type`（`string`、`decimal`、`integer`、`dateTime`、`boolean`、`OptionList`、
  `SequenceList`、`MemberList`）和 `value`。可选：`mutability`（默认 `ReadWrite`，或 `ReadOnly`、`WriteOnly`）、
  `unit`、`min_limit` / `max_limit`（数值范围；`string` 为长度）、`values_list`（列表类型必填）、
  `target` / `min_set` / `max_set`（更多属性）、`reboot_required`（SetVariables 应答 `RebootRequired`）、
  `persistent`、`constant`、`summary`（包含在 SummaryInventory 报告中）。

```yaml
chargers:
  - id: "CP201"
    ocpp_version: "2.0.1"
    variables:
      - component: OCPPCommCtrlr
        variable: HeartbeatInterval
        value: 120
      - component: DeviceDataCtrlr
        variable: ItemsPerMessage
        variable_instance: GetReport
        value: 10
      - component: VendorCtrlr
        variable: ChargeMode
        data_type: OptionList
        values_list: [Eco, Fast]
        value: Eco
```

加载配置时会校验值（类型、范围、`values_list`）；在 1.6 充电桩上写 `variables` 会报错。

在 Web UI 中，2.0.1 充电桩的 **变量（Device Model）** 表只读显示这些值。

![OCPP 2.0.1 充电桩的变量](../assets/screenshots/zh/variables.png)

1.6 / 2.0.1 混合站点的示例见 [`configs/mixed-site.yaml`](https://github.com/smartedge-tech-dev/sme-ocpp-sim/blob/main/configs/mixed-site.yaml)。

尚不支持：充电桩模板、2.0.1 的 `evses` 树（每个 EVSE 多个 connector）和 `behaviors`。
