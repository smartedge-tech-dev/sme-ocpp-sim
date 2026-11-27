# Smart Charging

SME OCPPSim implements the OCPP 1.6 Smart Charging profile: SetChargingProfile, ClearChargingProfile,
GetCompositeSchedule, and charging profiles carried by RemoteStartTransaction.

## One evaluator for the schedule and the power

The limit that the power model applies every tick is the first period of a one-second composite schedule,
computed by the same function that answers GetCompositeSchedule. The schedule reported to the CSMS and the
power the simulated EV actually draws (and that appears in MeterValues) therefore cannot diverge.

Each tick: composite limit → `min(hardware maximum, limit, EV acceptance)` → power (with optional ramp) →
energy register and SoC. A new profile takes effect on the tick right after the CALLRESULT has been sent.
When the limit drops to 0 or below the EV's minimum usable power, the connector goes to **SuspendedEVSE**; when
the EV is full (target SoC) or tapers below its minimum, it goes to **SuspendedEV**.

`simulation.time_scale` speeds up only the power model (ramp, energy, SoC). Profiles are evaluated on the wall
clock, because schedules are in CSMS time: a Relative 60 s period lasts 60 real seconds at any time scale.

## Rules implemented from the specification

References are to OCPP 1.6 edition 2 and its errata sheet v4.0.

- **3.13.1**: ChargePointMaxProfile only on connectorId 0; TxProfile only on connectorId > 0 and only while a
  transaction is active on that connector. TxProfiles cease to be valid when their transaction ends (7.10).
- **3.13.2 / 5.16.3 / 5.16.4**: a new profile replaces an installed one with the same chargingProfileId, or with
  the same stackLevel and purpose.
- **3.13.2**: per purpose, the prevailing profile is the valid one (validFrom / validTo) with the highest
  stackLevel; when its schedule is not running (not started, `duration` elapsed) the next lower stackLevel
  applies (errata 3.43). Without any profile there is no limit.
- **3.13.1 / 3.13.3**: TxProfile overrules TxDefaultProfile for the current transaction; the result is the
  minimum of that and ChargePointMaxProfile. A connector-specific TxDefaultProfile replaces the connector-0 one
  with the same stackLevel for that connector only.
- **7.9 / 7.13**: Absolute starts at `startSchedule`; Relative, and Absolute without `startSchedule`, start at
  the transaction start; Recurring restarts every day / week; a `duration` longer than the recurrence is cut to
  the recurrence (errata 3.43 / 3.44). The first period starts at 0 (errata 3.45); stackLevel ≥ 0 (7.8).
- **7.12 / 7.14**: W = A × voltage × phases, phases = `numberPhases` or 3 if absent. Errata 3.34: a profile
  with more phases than the charger has applies its limit to the phases actually available.
- **6.13 / errata 3.25–3.26**: ClearChargingProfile without fields clears everything; otherwise the given fields
  are ANDed; if `id` is given all other fields are ignored. Nothing matched → `Unknown` (7.21).
- **5.7**: the composite schedule covers [start, start + duration); connectorId 0 reports the whole charger.
- An invalid `chargingRateUnit` (like any invalid enum value) is CALLERROR `PropertyConstraintViolation`
  (errata 3.24).

## Interpretations in this implementation

Where the specification leaves a point open, this implementation decides as follows. The section numbers are
the places that were checked.

| Topic | Interpretation | Spec checked |
|---|---|---|
| Relative schedule without a transaction | Relative schedules (and Absolute without `startSchedule`) on a connector with no transaction use the start of the requested window as origin; ChargePointMaxProfiles always use the window start. The live limit without a transaction is therefore the first period of a Relative schedule | 5.7, 7.13 |
| Recurring without `startSchedule` | Anchored at 00:00 UTC (Daily) or Monday 00:00 UTC (Weekly). The recurrence also extends before `startSchedule`, which only fixes the time of day / week | 7.8, 7.9 |
| validFrom / validTo | validFrom inclusive, validTo exclusive | 7.8 |
| connectorId 0 composite | min(ChargePointMax, sum over connectors 1..N of their TxProfile / TxDefault limit). If any connector is unconstrained the sum is unconstrained. Idle connectors count with their TxDefault limit (or make the sum unconstrained), so the value is the most the charger *may* draw, not what it draws now. A single connector gets the full ChargePointMax value (no sharing between connectors) | 5.7, 3.13.3 |
| `minChargingRate` | Does not change the limit; refusing to charge below it is the EV model's job | 7.13 |
| "No limit" in GetCompositeSchedule | Unconstrained intervals are reported as the charger's hardware maximum (`max_power_w`, times the connector count for connectorId 0), so the answer is Accepted even with no profile installed (1.6 has no encoding for "no limit") | 5.7 |
| GetCompositeSchedule answer | `scheduleStart` = `chargingSchedule.startSchedule` = time of the request; `duration` = the requested duration; limits rounded to 1 decimal; adjacent periods equal after rounding are merged. Unknown connector or duration < 1 → Rejected | 5.7, 7.13 |
| Fractional period boundaries | A boundary between whole seconds starts at the next whole second (`startPeriod` is an integer) | 7.13 |
| Unit when none given | W | 7.13 |
| Limit in A and phases | Applies to the phases the EV uses while one charges, and to the supply's phases otherwise | errata 3.34 |
| Replacement scope | Same stackLevel + purpose replacement is per connectorId (a connector-specific TxDefaultProfile replaces the connector-0 one "only for that specific connector") | 3.13.1 |
| TxProfile for another transaction | A TxProfile whose `transactionId` differs from the connector's active transaction is Rejected | 3.13.1 |
| Malformed profiles | Recurring without `recurrencyKind`, non-increasing `startPeriod`, an empty period list, a connectorId outside 0..N, or `validTo` ≤ `validFrom` → Rejected | 7.13 |
| Value ranges the schemas do not carry | Negative `limit` / `startPeriod` / `minChargingRate`, `numberPhases` outside 1..3 and `duration` ≤ 0 → Rejected | 7.13, 7.14 |
| RemoteStartTransaction profile | Must be a TxProfile without `transactionId` ("SHALL NOT be set"); otherwise Rejected without starting a transaction. It is installed, bound to the new transactionId, once StartTransaction is answered | 5.16.2, 6.33 |
| Connector out of range | SetChargingProfile / ClearChargingProfile with a connectorId outside 0..N → CALLERROR `PropertyConstraintViolation`, like other connector-addressed requests | — |
| Offline gap | Ticks run only while connected; after an offline gap the first step integrates the gap at the limit in force on reconnect | — |

## Power model conventions

These are simulator conventions, not OCPP rules:

- Electrical power is `V_phase × I × phases`. With `line_to_line: true` on a 3-phase supply the per-phase
  voltage is `voltage_v / √3`, so P = √3 × V_LL × I. Phases in use = min(EV phases, supply phases).
- Energy is integrated with the trapezoid rule; the energy register only grows. SoC may overshoot the target
  by at most one step.
- Ramps (`ramp_up_w_per_s`, `ramp_down_w_per_s`) are in W per simulated second; the default is a step change.

## OCPP 2.0.1

A 2.0.1 charger has its own copy of these rules (`protocol/v201/profiles.py` / `composite.py`); the power model
is shared, so the same closed loop holds: GetCompositeSchedule equals the `Power.Active.Import` of the following
TransactionEvent readings. Messages: SetChargingProfile, ClearChargingProfile, GetCompositeSchedule,
GetChargingProfiles (answered by ReportChargingProfiles parts, one per EVSE and `chargingLimitSource`, `tbc` on
all but the last), NotifyChargingLimit and ClearedChargingLimit, and the `chargingProfile` of
RequestStartTransaction (installed bound to the transaction it starts).

| Topic | Behaviour (interpretation where the schemas are silent) |
|---|---|
| Purposes | ChargingStationMaxProfile (evseId 0 only), TxDefaultProfile, TxProfile (needs the `transactionId` of the EVSE's running transaction), ChargingStationExternalConstraints (never from the CSMS: Rejected) |
| Limit of an EVSE | min(ChargingStationMax, external constraints of evseId 0 and of the EVSE, TxProfile else TxDefaultProfile) |
| evseId 0 | min(ChargingStationMax, external constraints of evseId 0, sum of the EVSE limits); unconstrained if one EVSE is |
| Stack level conflict | another profile with the same EVSE, purpose and stackLevel and an overlapping validity period is Rejected (a same-id profile replaces) |
| `chargingSchedule` array | every schedule is validated and reported; only the first is evaluated (no ISO 15118 EV is simulated) |
| ClearChargingProfile | `chargingProfileId` wins over the criteria; the criteria are ANDed; external constraints are never cleared by the CSMS |
| Device Model | SmartChargingCtrlr ProfileStackLevel (100), PeriodsPerSchedule (1024) and RateUnit (`A,W`) are enforced; ExternalControlSignalsEnabled `false` refuses the external limit; NotifyChargingLimitWithSchedules decides whether NotifyChargingLimit carries the schedule |

**External limit.** `POST /api/chargers/{id}/external-limit` stands in for an energy management system: it
installs a ChargingStationExternalConstraints profile (stackLevel 0, one period in W, negative profile id) on the
EVSE or the station and sends NotifyChargingLimit (`chargingLimitSource` `EMS`); removing it sends
ClearedChargingLimit. Both are sent only while the boot is accepted (a limit set offline applies but is not
reported later). GetChargingProfiles reports these profiles with source `EMS`, the CSMS ones with `CSO`.
OCPP 1.6 has no external limits and answers 400.

The 2.0.1 rules recalled from the specification text (TxProfile needs a `transactionId`, the stack level
conflict, external constraints not settable by the CSMS) were not verified against the spec document; see the
module docstrings.
