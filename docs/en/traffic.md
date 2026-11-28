# Traffic generator

The traffic generator (ATG) produces random but reproducible charging sessions on idle connectors: cars arrive,
plug in, present an id tag, charge, stop and leave. It is meant for load and EMS tests (demand peaks, occupancy,
idle power) that need many realistic sessions over hours of simulated time.

It runs on the sim clock (it follows `simulation.time_scale` and pause), uses the same actions as the web UI
and the [scenario runner](scenarios.md) (plug / swipe / stop / unplug), and draws its vehicles from the
[vehicle pool](configuration.md).

## Configuration { #configuration }

Standing traffic is the top-level `traffic:` block of the configuration (see `configs/traffic-demo.yaml`):

```yaml
traffic:
  enabled: true             # start with the app (POST /api/traffic/start starts it without saving)
  seed: 42
  chargers: "*"             # or [CP001, CP002]
  interval_s: 300           # sim seconds between arrival checks per connector
  probability: 0.2          # arrival probability per check
  hourly_weights: [0.2,0.1,0.1,0.1,0.2,0.5,1,2,2,1.5,1,1,1,1,1,1,1.5,2,2,1.5,1,0.8,0.5,0.3]
  auth_order: plug_first    # plug_first | tag_first
  stop_after: first         # duration | soc_target | first
  duration_s: [1800, 7200]
  stop_by: swipe            # swipe | unplug | remote_only
  max_session_s: 43200
  idle_after_s: [0, 900]
  id_tags: [TAG001, TAG002, TAG003]   # default: each charger's id_tags
  id_tag_strategy: affinity           # random | round_robin | affinity
  affinity_prob: 0.8
  affinity_map: {TAG001: [CP001]}     # default: one home charger per tag, from the seed
  vehicles: {kei-20kwh: 1, compact-40kwh: 3, suv-90kwh: 1}   # default: the whole pool, own weights
```

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Run the standing traffic with the app |
| `seed` | `0` | Seeds the plan and the vehicle pool streams of every charger (also `session.ev_model: pool` draws) |
| `chargers` | `"*"` | The chargers it drives |
| `interval_s` / `probability` | `300` / `0.2` | Every `interval_s` (sim) each connector checks for an arrival with `probability` |
| `hourly_weights` | `null` | 24 factors on `probability` by the hour of the sim clock (morning peak, night low) |
| `auth_order` | `plug_first` | Plug then present the tag, or the tag first |
| `stop_after` | `first` | End of charging: `duration` (uniform in `duration_s`), `soc_target` (the vehicle's target SoC), or whichever comes `first` |
| `stop_by` | `swipe` | `swipe` (same tag), `unplug` (the transaction lasts until the car leaves) or `remote_only` (wait for a CSMS RemoteStop; a local stop after `max_session_s`) |
| `max_session_s` | `43200` | Longest session (sim s), for every `stop_by` |
| `idle_after_s` | `[0, 900]` | The car stays plugged this long after charging (occupancy / idle power), then unplugs |
| `id_tags` | each charger's `id_tags` | The tag pool |
| `id_tag_strategy` | `random` | `random` (uniform), `round_robin` (in turn across all chargers, in plan order), `affinity` (`affinity_prob` = chance of a tag whose home is this charger, else random) |
| `vehicles` | whole pool | Vehicle ids of the pool (presets + `ev_models`) and their weights |

A session that the CSMS rejects (tag not Accepted) unplugs at once (`rejected`); a transaction that ends by
itself (RemoteStop, reset) ends the session. A connector that is not idle when a car arrives (a session still
running, a fault, offline, someone else plugged in) **skips** that arrival; it is not postponed.

Changing `traffic:` (also via `PUT /api/traffic`) or the charger list restarts running traffic with a new
plan from that moment; toggling `enabled` starts or stops it. The vehicle seed is applied live.

## Reproducibility { #reproducibility }

The same seed, configuration and start time give the same **plan**: the arrival times, vehicles, SoC values,
tags, durations and idle times of each connector. Each connector has its own random streams
(`{seed}:traffic:{charger}:{connector}` and the pool stream `vehicle:{charger}:{connector}`) and every check
takes the same random numbers whatever happens at run time, so adding a charger does not change the plans of
the others (except `round_robin` tags, which count across chargers) and the plan does not depend on CSMS
answers. Which arrivals are skipped, and frame-level timing, are not part of the promise.

`GET /api/traffic/plan?hours=4` previews the plan (from the running traffic's start, or now); pass `start=` for
a fixed reference.

## Scenarios and priority { #scenarios }

A scenario track can be a generator: `generate:` takes the same fields (except `enabled` / `chargers`) instead
of `steps`; `use: traffic` starts from the config's `traffic:` block. `for_each` makes one plan for all its
chargers. Generator tracks run until the scenario's `until` (required); their verdict record (kind
`generate`) passes with the session counters. The scenario `seed` overrides the traffic seed.

```yaml
tracks:
  - for_each: {chargers: [CP002, CP003]}
    generate: {use: traffic, probability: 0.5, duration_s: [600, 3600]}
until: "+4h"
```

A running scenario owns its chargers: before it starts, the standing traffic stops and unplugs its sessions
there (`preempted`) and skips arrivals there until the scenario ends; its other chargers go on.

## API and MCP { #api }

| Method | Path | MCP tool | Meaning |
|---|---|---|---|
| GET | `/api/traffic` | `get_traffic` | State, settings, counters (arrivals, skipped, started, rejected, completed, failed, preempted), active sessions, total power, recent events |
| PUT | `/api/traffic` | `set_traffic` | Replace the `traffic:` block (a reload; save with the config) |
| POST | `/api/traffic/start` | `start_traffic` | Start now (not saved) |
| POST | `/api/traffic/stop` | `stop_traffic` | Stop; its sessions are stopped and unplugged |
| GET | `/api/traffic/plan?hours=&charger=&limit=&start=` | `preview_traffic_plan` | The planned arrivals |

While it runs, the charger list header shows the active sessions and their total power.

Memory stays bounded for long runs: the plan is made lazily, generator tracks keep no frame buffer and the
status keeps only the last 50 events.
