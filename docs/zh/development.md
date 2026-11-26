# 开发

```powershell
git clone https://github.com/smartedge-tech-dev/sme-ocpp-sim
cd sme-ocpp-sim
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\sme-ocpp-sim -c configs/demo-site.yaml
```

Web UI 没有构建步骤：`sme_ocpp_sim/static/` 在 `sme-sim-common` 的公共外壳之上原样提供。

## 测试

```powershell
.venv\Scripts\python -m pytest -q
```

集成测试让模拟充电桩连接同进程内的 OCPP 1.6 对端（`ocpp` 库，仅为开发依赖）。sme-sim-common 的 pytest 插件把每个
`asyncio.run` 限制在 `SIM_TEST_TIMEOUT` 秒内（默认 60）；在较慢的机器上可以调大，例如 `$env:SIM_TEST_TIMEOUT = "180"`。

### 负载测试

负载测试默认不执行（marker `load`）：

```powershell
.venv\Scripts\python -m pytest -m load -s
```

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `SME_LOAD_CHARGERS` | `200` | 充电桩数量 |
| `SME_LOAD_SECONDS` | `600` | 持续时间（秒） |
| `SME_LOAD_HEARTBEAT_S` | `10` | 对端在 BootNotification 中返回的 Heartbeat 间隔 |
| `SME_LOAD_METER_S` | `60` | MeterValueSampleInterval；每台充电桩执行一笔交易 |

负载测试会自行把时限提高到 `SME_LOAD_SECONDS` + 300 秒。

## 从脚本打开对话框

为了截图和 UI 测试，主要对话框可以通过浏览器中的全局函数打开：

| 函数 | 打开 |
|---|---|
| `window.openChargerDialog(id)` | 充电桩配置对话框 |
| `window.openSettingsDialog()` | 设置对话框（CSMS URL、OCPP 版本、Security Profile） |
| `window.openProfileDialog(id)` | 充电桩的 Smart Charging profile |
| `window.openFrameDetail(seq)` | 帧日志中的一帧 |
| `window.openAddChargerDialog()` | 添加充电桩对话框 |

语言和主题保存在 `localStorage` 的 `sme-ocpp-sim.lang` 和 `sme-ocpp-sim.theme` 中。

## 文档

```powershell
.venv\Scripts\pip install -r requirements-docs.txt
.venv\Scripts\mkdocs serve
```

页面放在 `docs/en`、`docs/ja`、`docs/zh` 中，文件名相同。CI 使用 `mkdocs build --strict` 构建。
