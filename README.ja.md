# SME OCPPSim

[English](README.md) | **日本語** | [简体中文](README.zh-CN.md)

CSMS・EMS のテスト向け無償 OCPP 充電器シミュレーター（OCPP 1.6J 対応、2.0.1 / 2.1 は対応予定）。Smart Charging にも対応。

SME OCPPSim は MIT ライセンスの無償ソフトウェアで、[smart-edge（智届科技）](https://smartedge-tech.com) が開発しています。
カスタム開発のご相談は [商用サポートとカスタム開発](#商用サポートとカスタム開発) をご覧ください。

## クイックスタート

```powershell
python -m venv .venv
.venv\Scripts\pip install sme-ocpp-sim
.venv\Scripts\sme-ocpp-sim --config configs/demo-site.yaml
```

http://127.0.0.1:8082/ を開きます。共通オプション（sme-sim-common 提供）：

| オプション | 意味 |
|---|---|
| `-c, --config FILE` | YAML 設定ファイル |
| `--host 127.0.0.1` | Web UI の待ち受けアドレス（既定はローカルのみ） |
| `--http-port 8082` | Web UI のポート |
| `--no-browser` | 起動時にブラウザを開かない |
| `--version`, `-v` | バージョン表示／詳細ログ |

## 設定

TODO

## HTTP API

TODO

## 開発

TODO

## 商用サポートとカスタム開発

TODO（SME BACnetSim の README.ja.md から転記）

## ライセンス

MIT — [LICENSE](LICENSE) を参照。
