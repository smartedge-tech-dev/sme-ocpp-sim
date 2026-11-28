# セキュリティプロファイル

OCPP 1.6 のセキュリティプロファイルは、`csms_defaults.security_profile` でサイト全体に 1 つだけ設定します。
設定の読み込み時に、各充電器の設定をこれに照らして検査します。

| プロファイル | 通信 | 認証 | 充電器に必要な設定 |
|---|---|---|---|
| 0 | `ws://` | なし | `password` なし、`ws://` の URL |
| 1 | `ws://` | HTTP Basic | `password`、`ws://` の URL |
| 2 | `wss://`（TLS、サーバー証明書を検証） | HTTP Basic | `password`、`wss://` の URL |
| 3 | `wss://`（TLS、サーバー証明書を検証） | TLS クライアント証明書（Basic 認証なし） | `client_cert_file` と `client_key_file`、`password` なし、`wss://` の URL |

- **Basic 認証**: ユーザー ID = 充電器 ID、パスワード = 充電器の `password`。このため充電器 ID に `:` は使えません（RFC 7617）。
- **サーバー証明書**: システムの信頼ストア、または `ca_file`（PEM）を指定した場合はそれで検証します。
  `verify_tls: false` で試験用に検証を省略できます（警告をログに出力します）。
- **秘密情報**: パスワードは HTTP API では `********` にマスクし、フレームログには書きません。ハンドシェイクの
  `Authorization` ヘッダーもログ出力から除去します。

```yaml
csms_defaults:
  url: "wss://csms.example.com/ocpp"
  security_profile: 2

chargers:
  - id: "CP-0001"
    password: "change-me"
    ca_file: "certs/test-ca.pem"
```

OCPP 1.6（Security Whitepaper 第 4 版、A01）：CSMS は ChangeConfiguration `AuthorizationKey` で Basic 認証パスワードを
変更できます（16〜40 文字。プロファイル 3 では `Rejected`）。充電器は `Accepted` と応答し、新しいパスワードで再接続して
ReconfigurationOfSecurityParameters を記録します（パスワードは記録しません）。新しいパスワードはメモリ上のみに保持されます
（YAML は `password` のまま）。`AuthorizationKey` は書込専用で、GetConfiguration では値なしで返します。`SecurityProfile`
はここでは読取専用で、使用中のプロファイルを返します。Whitepaper では CSMS が ChangeConfiguration で上げられます（A05）が、
シミュレーターは実行中にプロファイルを切り替えられません。

プロファイル 3（v0.2 から）：充電器は TLS ハンドシェイクで `client_cert_file`（PEM、チェーン可）と
`client_key_file`（PEM、暗号化なし）を提示し、`Authorization` ヘッダーは送りません。クライアント証明書を理由に
CSMS が拒否したハンドシェイクは `client_cert_rejected` と表示されます。クライアント証明書を設定していない
TLS 接続が同じように拒否された場合（CSMS がプロファイル 3 を要求していると考えられる場合）は
`client_cert_required` と表示されます。どちらも、HTTP 400 で「SSL certificate」のページを返す TLS プロキシ
（nginx の `ssl_verify_client on`）の背後でも判別されます。証明書は CSMS から更新できます
（SignCertificate / CertificateSigned、両バージョン）。新しい証明書は次の接続から使われ、メモリ上のみに保持されます
（YAML は設定したファイルのまま）。証明書と SecurityEventNotification のメッセージは
[OCPP メッセージ](messages.md) を参照してください。

```yaml
csms_defaults:
  url: "wss://csms.example.com/ocpp"
  security_profile: 3

chargers:
  - id: "CP-0001"
    serial: "SN-0001"          # CSR の CN
    ca_file: "certs/csms-root.pem"
    client_cert_file: "certs/cp-0001.pem"
    client_key_file: "certs/cp-0001.key"
```
