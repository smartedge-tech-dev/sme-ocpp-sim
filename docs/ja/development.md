# 開発

```powershell
git clone https://github.com/smartedge-tech-dev/sme-ocpp-sim
cd sme-ocpp-sim
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\sme-ocpp-sim -c configs/demo-site.yaml
```

Web UI にビルド手順はありません。`sme_ocpp_sim/static/` を `sme-sim-common` の共通シェルの上でそのまま配信します。

## テスト

```powershell
.venv\Scripts\python -m pytest -q
```

統合テストでは、模擬充電器を同一プロセス内の OCPP 1.6 対向（開発時のみの依存である `ocpp` ライブラリ）に接続します。
sme-sim-common の pytest プラグインは、各 `asyncio.run` を `SIM_TEST_TIMEOUT` 秒（既定 60）に制限します。
遅いマシンでは引き上げてください（例: `$env:SIM_TEST_TIMEOUT = "180"`）。

### 負荷テスト

負荷テストは既定では除外されています（マーカー `load`）。

```powershell
.venv\Scripts\python -m pytest -m load -s
```

| 環境変数 | 既定値 | 意味 |
|---|---|---|
| `SME_LOAD_CHARGERS` | `200` | 充電器の台数 |
| `SME_LOAD_SECONDS` | `600` | 実行時間（秒） |
| `SME_LOAD_HEARTBEAT_S` | `10` | 対向が BootNotification で返す Heartbeat 間隔 |
| `SME_LOAD_METER_S` | `60` | MeterValueSampleInterval。各充電器が 1 件ずつトランザクションを実行します |

負荷テストは自身の制限時間を `SME_LOAD_SECONDS` + 300 秒に引き上げます。

## スクリプトからダイアログを開く

スクリーンショットや UI テスト用に、主なダイアログはブラウザのグローバル関数で開けます。

| 関数 | 開くもの |
|---|---|
| `window.openChargerDialog(id)` | 充電器の設定ダイアログ |
| `window.openSettingsDialog()` | 設定ダイアログ（CSMS URL、OCPP バージョン、セキュリティプロファイル） |
| `window.openProfileDialog(id)` | 充電器の Smart Charging プロファイル |
| `window.openFrameDetail(seq)` | フレームログの 1 フレーム |
| `window.openAddChargerDialog()` | 充電器の追加ダイアログ |

言語とテーマは `localStorage` の `sme-ocpp-sim.lang` と `sme-ocpp-sim.theme` に保存されます。

## ドキュメント

```powershell
.venv\Scripts\pip install -r requirements-docs.txt
.venv\Scripts\mkdocs serve
```

ページは `docs/en`、`docs/ja`、`docs/zh` に同じファイル名で置きます。CI は `mkdocs build --strict` でビルドします。
