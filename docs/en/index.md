---
title: Free OCPP charging station simulator (OCPP 1.6J / 2.0.1) with a web UI
description: Free, open-source OCPP charging station (charge point) simulator for OCPP 1.6J and 2.0.1 with a live web UI, for Windows, Linux and Docker. Test a CSMS or EMS, including Smart Charging, without real EV chargers.
---

# SME OCPPSim

A free, open-source OCPP charging station simulator (a charge point / EV charger simulator) with a live web UI, for Windows, Linux and Docker. Use it to test a CSMS or an EMS. Simulate dozens to
hundreds of AC chargers in one process, plug in virtual EVs, start and stop transactions, and watch how the
charging power follows the Smart Charging profiles your CSMS sends.

- **OCPP 1.6J and OCPP 2.0.1.** Each simulated charger is a WebSocket client that connects out to your CSMS
  (subprotocol `ocpp1.6` or `ocpp2.0.1`, chosen per charger, so one process can run a mixed site), boots, sends
  heartbeats, status notifications, transactions (1.6 StartTransaction / StopTransaction, 2.0.1 TransactionEvent)
  and meter values, and answers the CSMS commands listed in [OCPP messages](messages.md). OCPP 2.1 is also
  supported (`ocpp2.1`, see [OCPP 2.1](messages.md#ocpp-21)).
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
- **Authorization, reservations and maintenance (1.6 and 2.0.1).** Local authorization list and
  authorization cache, ReserveNow / CancelReservation, and simulated firmware update, diagnostics and log
  upload with status notifications and failure injection. See [OCPP messages](messages.md).
- **Security profiles 0 / 1 / 2 / 3.** Plain `ws://`, HTTP Basic authentication, `wss://` with Basic
  authentication and `wss://` with a TLS client certificate; certificate management and
  SecurityEventNotification. See [Security profiles](security-profiles.md).
- **Built-in Mini CSMS.** A simple OCPP 1.6J / 2.0.1 CSMS on port 9000 in the same process: try the
  simulator without an external CSMS and send any CALL from a JSON editor. See [Mini CSMS](mini-csms.md).
- **Excel and charger templates.** Import / export the charger list as an Excel workbook and add many
  chargers at once from a template. See [Excel import / export and charger templates](excel-templates.md).
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

## Typical uses

- **Test a CSMS or EMS without real chargers.** Point your CSMS at dozens of simulated chargers instead of
  hardware; see the [quick start](quick-start.md) and [OCPP messages](messages.md).
- **Verify the Smart Charging power loop.** Send charging profiles and check that the meter values follow the
  limit, see [Smart Charging](smart-charging.md).
- **Test scale and reconnects.** Run 200+ chargers in one process and exercise disconnect / reconnect
  scenarios, see [Configuration](configuration.md) and the [HTTP API](http-api.md).

## More simulators from smart-edge

SME OCPPSim is one of a series of free, MIT-licensed protocol simulators by smart-edge. They share the
same web UI and conventions, so what you learn in one carries over to the others.

| Simulator | What it simulates | Docs |
|---|---|---|
| [SME BACnetSim](https://github.com/smartedge-tech-dev/sme-bacnet-sim) | BACnet/IP device simulator | [Docs](https://smartedge-tech-dev.github.io/sme-bacnet-sim/) |
| [SME ModbusSim](https://github.com/smartedge-tech-dev/sme-modbus-sim) | Modbus TCP / RTU / UDP / TLS slave (server) simulator | [Docs](https://smartedge-tech-dev.github.io/sme-modbus-sim/) |
| **SME OCPPSim** (this project) | OCPP 1.6J / 2.0.1 charging station (charge point) simulator | [Docs](https://smartedge-tech-dev.github.io/sme-ocpp-sim/) |

All open-source tools from smart-edge: <https://smartedge-tech.com/open-source/>
