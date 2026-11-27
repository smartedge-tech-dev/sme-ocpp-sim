# 安全配置文件

OCPP 1.6 的安全配置文件（Security Profile）通过 `csms_defaults.security_profile` 为整个站点统一设置。加载配置时，
每台充电桩的设置都会按它检查。

| Profile | 传输 | 认证 | 充电桩需要 |
|---|---|---|---|
| 0 | `ws://` | 无 | 不设 `password`，`ws://` URL |
| 1 | `ws://` | HTTP Basic | `password`，`ws://` URL |
| 2 | `wss://`（TLS，校验服务器证书） | HTTP Basic | `password`，`wss://` URL |
| 3 | `wss://`（TLS，校验服务器证书） | TLS 客户端证书（不用 Basic 认证） | `client_cert_file` 和 `client_key_file`，不设 `password`，`wss://` URL |

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

OCPP 1.6（Security Whitepaper 第 4 版，A01）：CSMS 可以用 ChangeConfiguration `AuthorizationKey` 修改 Basic 认证密码
（16〜40 个字符；Profile 3 时为 `Rejected`）。充电桩应答 `Accepted`，用新密码重连，并记录
ReconfigurationOfSecurityParameters（不记录密码）；新密码只保存在内存中（YAML 仍是 `password`）。`AuthorizationKey`
是只写的：GetConfiguration 不返回其值。`SecurityProfile` 在这里是只读的，报告当前使用的 Profile：Whitepaper 允许 CSMS
用 ChangeConfiguration 提升它（A05），但模拟器无法在运行中切换 Profile。

Profile 3（v0.2 起）：充电桩在 TLS 握手中出示 `client_cert_file`（PEM，可为证书链）和
`client_key_file`（PEM，不加密），不发送 `Authorization` 头。CSMS 因客户端证书拒绝的握手显示为
`client_cert_rejected`。证书可由 CSMS 更新（SignCertificate / CertificateSigned，两个版本均支持）；新证书从下一次
连接起使用，只保存在内存中（YAML 仍是配置的文件）。证书和 SecurityEventNotification 消息见
[OCPP 消息](messages.md)。

```yaml
csms_defaults:
  url: "wss://csms.example.com/ocpp"
  security_profile: 3

chargers:
  - id: "CP-0001"
    serial: "SN-0001"          # CSR 的 CN
    ca_file: "certs/csms-root.pem"
    client_cert_file: "certs/cp-0001.pem"
    client_key_file: "certs/cp-0001.key"
```
