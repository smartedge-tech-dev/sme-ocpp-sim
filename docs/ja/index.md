# SME OCPPSim

CSMS や EMS の試験に使える、ライブ Web UI 付きの無償 OCPP 充電器シミュレーターです。1 プロセスで数十〜数百台の
AC 充電器を模擬し、仮想 EV の接続、トランザクションの開始・停止を行いながら、CSMS が送る Smart Charging
プロファイルに充電電力が追従する様子を確認できます。

- **OCPP 1.6J と OCPP 2.0.1。** 各充電器は CSMS へ接続しに行く WebSocket クライアント（サブプロトコル
  `ocpp1.6` または `ocpp2.0.1`。充電器ごとに選べるので 1 プロセスで混在サイトも構成できます）です。Boot、
  Heartbeat、StatusNotification、トランザクション（1.6 は StartTransaction / StopTransaction、2.0.1 は
  TransactionEvent）、MeterValues を送信し、[OCPP メッセージ](messages.md) に挙げた CSMS からの要求に応答します。
  OCPP 2.1 は v0.3 で対応予定で、UI ではグレー表示になっています。
- **OCPP 2.0.1 の Device Model と外部制限値。** 最小限の標準変数とユーザー定義変数（YAML の `variables:`）に対する
  GetVariables / SetVariables / GetBaseReport。Web UI では参照のみで表示します。EVSE またはステーション単位の
  外部（EMS）制限値を NotifyChargingLimit / ClearedChargingLimit 付きで設定できます。
- **電力の閉ループを持つ Smart Charging（1.6 と 2.0.1）。** SetChargingProfile / ClearChargingProfile / GetCompositeSchedule
  に対応します。模擬電力に適用する制限値は GetCompositeSchedule に応答するのと同じ関数で計算されるため、
  CSMS に報告する計画と MeterValues の値が食い違うことはありません。[Smart Charging](smart-charging.md) をご覧ください。
- **EV と電力のモデル。** 電池容量、SoC、充電カーブ、相数、最小電流、ランプ、日本の 200 V 単相・三相デルタ
  電源を扱います。状態は Charging / SuspendedEV / SuspendedEVSE の間を遷移します。
- **多数の充電器。** 1 プロセスで 200 台以上。個別または一括で接続・切断できます。
- **セキュリティプロファイル 0 / 1 / 2。** `ws://`、HTTP Basic 認証、Basic 認証付きの `wss://` に対応します。
  [セキュリティプロファイル](security-profiles.md) をご覧ください。
- **フレームログ。** すべての OCPP フレームを充電器・アクション・方向で絞り込め、JSON Lines でエクスポートできます。
- **YAML 設定。** ブラウザ上で充電器を編集し、ファイルへ保存できます。[設定](configuration.md) をご覧ください。
- **英語、日本語、簡体字中国語** の UI と、ライト / ダークテーマに対応しています。

SME OCPPSim は無償の MIT ライセンスで、[smart-edge (智届科技)](https://smartedge-tech.com) が開発しています —
カスタム開発も承っています。[商用サポートとカスタム開発](commercial-support.md) をご覧ください。

!!! note "OCA 認証は受けていません"
    SME OCPPSim は公開されている OCPP 仕様に基づいて実装しています。Open Charge Alliance の認証は受けておらず、
    OCTT による試験も行っていません。[ライセンス](license.md) をご覧ください。

## 次のステップ

- [クイックスタート](quick-start.md): Windows exe、pip、Docker と、SteVe への初回接続
- [設定](configuration.md): YAML の全フィールド
- [HTTP API](http-api.md): テストスクリプトからシミュレーターを操作する
