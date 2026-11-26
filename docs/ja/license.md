# ライセンス

SME OCPPSim は [MIT ライセンス](https://github.com/smartedge-tech-dev/sme-ocpp-sim/blob/main/LICENSE) で公開しています。
サードパーティのコンポーネントとそのライセンスは
[THIRD-PARTY-NOTICES](https://github.com/smartedge-tech-dev/sme-ocpp-sim/blob/main/THIRD-PARTY-NOTICES) に記載しています。

## OCPP

OCPP（Open Charge Point Protocol）は [Open Charge Alliance](https://openchargealliance.org/) が公開している仕様です。
SME OCPPSim は、公開されている仕様書に基づく独立した実装です。

- SME OCPPSim は **OCA の認証を受けておらず**、**OCTT（OCA の適合性試験ツール）による試験も行っていません**。
- OCA 公式の JSON スキーマは **同梱していません**。メッセージモデルは本プロジェクトで独自に記述しています。
- ISO 15118 / Plug & Charge には **対応していません**（規格が無償で公開されていないため）。

「OCPP」および「Open Charge Alliance」はプロトコルを説明する目的でのみ使用しています。本プロジェクトは
Open Charge Alliance と提携しておらず、その承認も受けていません。
