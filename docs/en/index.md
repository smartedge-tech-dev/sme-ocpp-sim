# SME OCPPSim

A free OCPP charging station simulator with a live web UI, for testing a CSMS or an EMS. Simulate dozens to
hundreds of AC chargers in one process, plug in virtual EVs, start and stop transactions, and watch how the
charging power follows the Smart Charging profiles your CSMS sends.

- **OCPP 1.6J and OCPP 2.0.1.** Each simulated charger is a WebSocket client that connects out to your CSMS
  (subprotocol `ocpp1.6` or `ocpp2.0.1`, chosen per charger, so one process can run a mixed site), boots, sends
  heartbeats, status notifications, transactions (1.6 StartTransaction / StopTransaction, 2.0.1 TransactionEvent)
  and meter values, and answers the CSMS commands listed in [OCPP messages](messages.md). OCPP 2.1 is planned
  (v0.3) and shown greyed out in the UI.
- **OCPP 2.0.1 Device Model and external limits.** GetVariables / SetVariables / GetBaseReport on a minimal set
  of standard variables plus your own (`variables:` in the YAML file), shown read-only in the web UI; an external
  (EMS) limit per EVSE or station with NotifyChargingLimit / ClearedChargingLimit.
- **Smart Charging with a closed power loop (1.6 and 2.0.1).** SetChargingProfile / ClearChargingProfile /
  GetCompositeSchedule; the limit applied to the simulated power is computed by the same function that answers
  GetCompositeSchedule, so what the CSMS is told and what the meter values show cannot diverge.
  See [Smart Charging](smart-charging.md).
- **EV and power model.** Battery capacity, SoC, charging curve, phases, minimum current, ramps, Japanese
  200 V single-phase and 3-phase delta supplies; status moves between Charging, SuspendedEV and SuspendedEVSE.
- **Many chargers.** 200+ chargers in one process; connect / disconnect them individually or in bulk.
- **Security profiles 0 / 1 / 2.** Plain `ws://`, HTTP Basic authentication, and `wss://` with Basic
  authentication. See [Security profiles](security-profiles.md).
- **Frame log.** Every OCPP frame, filterable by charger, action and direction, with JSON Lines export.
- **YAML configuration.** Edit chargers in the browser and save back to the file. See [Configuration](configuration.md).
- **English, Japanese and Simplified Chinese** UI, light and dark theme.

SME OCPPSim is free and MIT-licensed, developed by [smart-edge (智届科技)](https://smartedge-tech.com) —
custom development is available, see [Commercial support & custom development](commercial-support.md).

!!! note "Not OCA certified"
    SME OCPPSim implements the publicly available OCPP specification. It is not certified by the Open Charge
    Alliance and has not been tested with OCTT. See [License](license.md).

## Next steps

- [Quick start](quick-start.md): Windows exe, pip or Docker, and a first connection to SteVe
- [Configuration](configuration.md): every YAML field
- [HTTP API](http-api.md): drive the simulator from your test scripts
