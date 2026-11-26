# SME OCPPSim

[English](README.md) | [日本語](README.ja.md) | **简体中文**

面向 CSMS 与 EMS 测试的免费 OCPP 充电桩模拟器（支持 OCPP 1.6J，2.0.1 / 2.1 规划中），含 Smart Charging。

SME OCPPSim 是免费的 MIT 许可软件，由 [smart-edge（智届科技）](https://smartedge-tech.com) 开发。
如需定制开发，请参阅 [商业支持与定制开发](#商业支持与定制开发)。

## 快速入门

```powershell
python -m venv .venv
.venv\Scripts\pip install sme-ocpp-sim
.venv\Scripts\sme-ocpp-sim --config configs/demo-site.yaml
```

打开 http://127.0.0.1:8082/ 。通用选项（由 sme-sim-common 提供）：

| 选项 | 含义 |
|---|---|
| `-c, --config FILE` | YAML 配置文件 |
| `--host 127.0.0.1` | Web UI 监听地址（默认仅本机） |
| `--http-port 8082` | Web UI 端口 |
| `--no-browser` | 启动时不打开浏览器 |
| `--version`, `-v` | 显示版本 / 详细日志 |

## 配置

TODO

## HTTP API

TODO

## 开发

TODO

## 商业支持与定制开发

TODO（从 SME BACnetSim 的 README.zh-CN.md 转载）

## 许可

MIT — 参见 [LICENSE](LICENSE)。
