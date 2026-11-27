# Security profiles

OCPP 1.6 security profiles are set once for the whole site with `csms_defaults.security_profile`. Every
charger is checked against it when the configuration is loaded.

| Profile | Transport | Authentication | Charger needs |
|---|---|---|---|
| 0 | `ws://` | none | no `password`, a `ws://` URL |
| 1 | `ws://` | HTTP Basic | `password`, a `ws://` URL |
| 2 | `wss://` (TLS, server certificate verified) | HTTP Basic | `password`, a `wss://` URL |
| 3 | `wss://` (TLS, server certificate verified) | TLS client certificate (no Basic auth) | `client_cert_file` and `client_key_file`, no `password`, a `wss://` URL |

- **Basic authentication**: user-id = the charger id, password = the charger's `password`. For this reason a
  charger id must not contain `:` (RFC 7617).
- **Server certificate**: verified against the system trust store, or against `ca_file` (PEM) when set.
  `verify_tls: false` disables verification for testing; a warning is logged.
- **Secrets**: passwords are masked as `********` in the HTTP API, never written to the frame log, and the
  `Authorization` header of the handshake is redacted from the log output.

```yaml
csms_defaults:
  url: "wss://csms.example.com/ocpp"
  security_profile: 2

chargers:
  - id: "CP-0001"
    password: "change-me"
    ca_file: "certs/test-ca.pem"
```

Profile 3 (since v0.2): the charger presents `client_cert_file` (PEM, a chain is allowed) with
`client_key_file` (PEM, not encrypted) in the TLS handshake and sends no `Authorization` header. A handshake
the CSMS refuses because of the client certificate is shown as `client_cert_rejected`. Certificates can be
renewed by the CSMS (SignCertificate / CertificateSigned, both OCPP versions); the new certificate is used from
the next connection and lives in memory only (the YAML keeps the configured files). See
[OCPP messages](messages.md) for the certificate and SecurityEventNotification messages.

```yaml
csms_defaults:
  url: "wss://csms.example.com/ocpp"
  security_profile: 3

chargers:
  - id: "CP-0001"
    serial: "SN-0001"          # CN of a CSR
    ca_file: "certs/csms-root.pem"
    client_cert_file: "certs/cp-0001.pem"
    client_key_file: "certs/cp-0001.key"
```
