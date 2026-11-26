# 安全配置文件

OCPP 1.6 的安全配置文件（Security Profile）通过 `csms_defaults.security_profile` 为整个站点统一设置。加载配置时，
每台充电桩的设置都会按它检查。

| Profile | 传输 | 认证 | 充电桩需要 |
|---|---|---|---|
| 0 | `ws://` | 无 | 不设 `password`，`ws://` URL |
| 1 | `ws://` | HTTP Basic | `password`，`ws://` URL |
| 2 | `wss://`（TLS，校验服务器证书） | HTTP Basic | `password`，`wss://` URL |
| 3 | 带客户端证书的 `wss://` | — | 计划在 v0.2 支持 |

- **Basic 认证**：用户 ID = 充电桩 ID，密码 = 充电桩的 `password`。因此充电桩 ID 不能包含 `:`（RFC 7617）。
- **服务器证书**：使用系统信任库校验；设置了 `ca_file`（PEM）时使用它。`verify_tls: false` 可在测试时关闭校验，日志中会输出警告。
- **敏感信息**：HTTP API 中密码以 `********` 掩码显示，不会写入帧日志；握手的 `Authorization` 头也会从日志输出中去除。

```yaml
csms_defaults:
  url: "wss://csms.example.com/ocpp"
  security_profile: 2

chargers:
  - id: "CP-0001"
    password: "change-me"
    ca_file: "certs/test-ca.pem"
```

Security Profile 3、证书管理消息（InstallCertificate、SignCertificate 等）以及其它 1.6 Security Whitepaper 消息计划在 v0.2 支持。
