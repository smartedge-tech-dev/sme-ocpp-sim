---
title: 免费 OCPP 充电桩模拟器（OCPP 1.6J / 2.0.1，仿真软件）
description: 免费开源的 OCPP 充电桩模拟器（仿真软件），支持 OCPP 1.6J / 2.0.1，带实时 Web UI，支持 Windows、Linux 和 Docker。无需真实充电桩即可测试 CSMS 或 EMS（含 Smart Charging）。
---

# SME OCPPSim

一个带实时 Web UI 的免费开源 OCPP 充电桩模拟器（仿真软件），支持 Windows、Linux 和 Docker，用于测试 CSMS 或 EMS。可在一个进程中模拟几十到几百台 AC 充电桩，
插入虚拟 EV、启动和停止交易，并观察充电功率如何跟随 CSMS 下发的 Smart Charging profile 变化。

- **OCPP 1.6J 与 OCPP 2.0.1。** 每台模拟桩都是一个主动连接 CSMS 的 WebSocket 客户端（子协议 `ocpp1.6` 或
  `ocpp2.0.1`，按充电桩选择，因此一个进程可以运行混合站点），发送 Boot、Heartbeat、StatusNotification、交易
  （1.6 为 StartTransaction / StopTransaction，2.0.1 为 TransactionEvent）和 MeterValues，并应答
  [OCPP 消息](messages.md) 中列出的 CSMS 请求。OCPP 2.1 计划在 v0.3 支持，UI 中以灰色显示。
- **OCPP 2.0.1 Device Model 与外部限值。** 对最小标准变量集和用户自定义变量（YAML 中的 `variables:`）支持
  GetVariables / SetVariables / GetBaseReport，Web UI 中只读显示；可按 EVSE 或整站设置外部（EMS）限值，
  并发送 NotifyChargingLimit / ClearedChargingLimit。
- **功率闭环的 Smart Charging（1.6 与 2.0.1）。** 支持 SetChargingProfile / ClearChargingProfile / GetCompositeSchedule；
  施加到模拟功率上的限值与应答 GetCompositeSchedule 的是同一个函数，因此报给 CSMS 的计划与 MeterValues 的数值不会不一致。
  详见 [Smart Charging](smart-charging.md)。
- **EV 与功率模型。** 电池容量、SoC、充电曲线、相数、最小电流、功率爬坡，以及日本 200 V 单相和三相三角形接法供电；
  状态在 Charging / SuspendedEV / SuspendedEVSE 之间切换。
- **大量充电桩。** 单进程 200 台以上；可单独或批量连接 / 断开。
- **认证、预约与维护（1.6 与 2.0.1）。** 本地授权列表与授权缓存、ReserveNow / CancelReservation，
  以及带状态通知和故障注入的固件升级、诊断 / 日志上传模拟。详见 [OCPP 消息](messages.md)。
- **安全配置文件（Security Profile）0 / 1 / 2 / 3。** 支持 `ws://`、HTTP Basic 认证、带 Basic 认证的 `wss://`
  以及带 TLS 客户端证书的 `wss://`，并支持证书管理和 SecurityEventNotification。详见 [安全配置文件](security-profiles.md)。
- **内置 Mini CSMS。** 在同一进程中运行于端口 9000 的简易 OCPP 1.6J / 2.0.1 CSMS：无需外部 CSMS 即可试用，
  并可用 JSON 编辑器发送任意 CALL。详见 [Mini CSMS](mini-csms.md)。
- **Excel 与充电桩模板。** 以 Excel 工作簿导入 / 导出充电桩列表，并可从模板一次添加多台充电桩。
  详见 [Excel 导入导出与充电桩模板](excel-templates.md)。
- **帧日志。** 记录每一个 OCPP 帧，可按充电桩、action 和方向过滤，并导出为 JSON Lines。
- **YAML 配置。** 在浏览器中编辑充电桩并保存回文件。详见 [配置](configuration.md)。
- **英语、日语和简体中文** UI，支持浅色 / 深色主题。

SME OCPPSim 免费且采用 MIT 许可证，由 [smart-edge (智届科技)](https://smartedge-tech.com) 开发——可提供定制开发，
详见 [商业支持与定制开发](commercial-support.md)。

!!! note "未经 OCA 认证"
    SME OCPPSim 基于公开的 OCPP 规范实现，未经 Open Charge Alliance 认证，也未使用 OCTT 测试。详见 [许可证](license.md)。

## 下一步

- [快速开始](quick-start.md)：Windows exe、pip、Docker，以及首次连接 SteVe
- [配置](configuration.md)：YAML 的全部字段
- [HTTP API](http-api.md)：从测试脚本操作模拟器

## 典型用途

- **无需真实充电桩即可测试 CSMS 或 EMS。** 让 CSMS 连接几十台模拟桩，而不是实体设备；详见 [快速开始](quick-start.md) 和 [OCPP 消息](messages.md)。
- **验证 Smart Charging 的功率闭环。** 下发充电 profile，确认 MeterValues 跟随限值，详见 [Smart Charging](smart-charging.md)。
- **测试规模与断线重连。** 单进程运行 200 台以上，并演练断开 / 重连场景，详见 [配置](configuration.md) 和 [HTTP API](http-api.md)。

## smart-edge 模拟器系列

SME OCPPSim 是 smart-edge 免费、MIT 许可的协议模拟器系列之一。各模拟器共用同一套 Web UI 和操作方式，学会一个即可上手其他几个。

| 模拟器 | 模拟对象 | 文档 |
|---|---|---|
| [SME BACnetSim](https://github.com/smartedge-tech-dev/sme-bacnet-sim) | BACnet/IP 设备模拟器 | [文档](https://smartedge-tech-dev.github.io/sme-bacnet-sim/zh/) |
| [SME ModbusSim](https://github.com/smartedge-tech-dev/sme-modbus-sim) | Modbus TCP / RTU / UDP / TLS 从站（Slave）模拟器 | [文档](https://smartedge-tech-dev.github.io/sme-modbus-sim/zh/) |
| **SME OCPPSim**（本项目） | OCPP 1.6J / 2.0.1 充电桩模拟器 | [文档](https://smartedge-tech-dev.github.io/sme-ocpp-sim/zh/) |

smart-edge 全部开源工具：<https://smartedge-tech.com/zh/open-source/>
