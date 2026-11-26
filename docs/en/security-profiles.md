# Security profiles

OCPP 1.6 security profiles are set once for the whole site with `csms_defaults.security_profile`. Every
charger is checked against it when the configuration is loaded.

| Profile | Transport | Authentication | Charger needs |
|---|---|---|---|
| 0 | `ws://` | none | no `password`, a `ws://` URL |
| 1 | `ws://` | HTTP Basic | `password`, a `ws://` URL |
| 2 | `wss://` (TLS, server certificate verified) | HTTP Basic | `password`, a `wss://` URL |
| 3 | `wss://` with client certificates | — | planned for v0.2 |

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

Security profile 3, certificate management messages (InstallCertificate, SignCertificate, ...) and the other
1.6 Security Whitepaper messages are planned for v0.2.
