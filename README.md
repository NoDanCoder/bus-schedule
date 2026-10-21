# Bus Alert — TransMilenio / SITP live tracking, arrival prediction and alerts

Pick the stop you'll be waiting at (and optionally which routes). The app follows every bus heading
there on a live map, predicts when each one arrives, and alerts you **10, 5, 2 and 1 minutes before**
(configurable). Alerts show in the browser (toast, sound, vibration, system notification) and can be
pushed to your phone with [ntfy](https://ntfy.sh) even when the page is closed.

```
 TransMilenio GTFS-RT ──► tracker ──► predictor ──► alert engine ──► browser (SSE) + ntfy push
 vehiclepos.pb (3 s)      place each   distance to   10/5/2/1 min,
 static GTFS.zip          bus on its   stop ÷ learned once per bus
                          route shape  speed          per threshold
                              └────► speed model (SQLite) learns from every bus's movement
```

## Data sources

Nothing needs to be collected by hand: TransMilenio publishes it.

| Data | Default URL | Used for |
|---|---|---|
| Vehicle positions (GTFS-RT) | `https://gis.transmilenio.gov.co/gtfs/vehiclepos.pb` | the map and ETAs |
| Static GTFS | `https://gtfs.transmilenio.gov.co/GTFS.zip` | stops, routes, shapes, stop order |
| Trip updates (GTFS-RT) | `https://gis.transmilenio.gov.co/gtfs/tripupdate.pb` | optional: shown next to our ETA as "official" |
| Service alerts (GTFS-RT) | `https://gis.transmilenio.gov.co/gtfs/serviceAlerts.pb` | optional: `/api/service-alerts` |

The official trip updates only cover a bus's *next* stop, and some live trip ids aren't in the static
feed, so they can't give an ETA for an arbitrary stop. ETAs are therefore computed here from positions.

> **Not verified against the live feed.** The environment this was built in cannot reach
> `transmilenio.gov.co`, so the live path is tested against the GTFS-RT format (encoded fixtures and a
> local fake server), not against the real feed. **Run `python -m busalert --check` first** — it polls the
> real feeds and reports the refresh rate, which fields are present, and what share of live
> `trip_id`/`route_id` values match the static GTFS. If ids don't line up, that is the one place to adapt
> (`Network.resolve_route` in `busalert/static.py`).

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m busalert --check          # probe the live feeds (do this first)
python -m busalert                  # live data -> http://127.0.0.1:8000
python -m busalert --demo           # simulated city, works offline
```

The first live start downloads and indexes the static GTFS (a large file, so the first start can take a while) and caches it
in `data/`. For alerts to arrive while your phone is asleep, run it somewhere that stays on and set a
push topic (below).

### Phone push

In the "Watch a stop" form, enter a hard-to-guess topic, install the ntfy app and subscribe to the same
topic. Anyone who knows the topic can read the alerts, so treat it like a password. Self-hosting: set
`BUSALERT_NTFY_SERVER`.

## How the ETA works

1. **Place the bus.** Each position is projected onto its route's shape (the trip id gives the shape;
   when it's unknown the route + travel direction/bearing pick between the two directions' lanes).
2. **Distance** along the shape from the bus to your stop.
3. **Time = Σ segment length ÷ learned speed**, per 250 m segment and hour of the week. Speeds are learned
   continuously from every tracked bus's movement (stored in `data/history.db`), so predictions improve the
   longer the service runs. A segment with no history starts from a prior (5 m/s); a new hour of the week
   starts from what that segment does overall.
4. **Live correction.** If this bus is running faster/slower than the model expects, speeds ahead are scaled,
   with the effect fading over ~2 km.
5. Each ETA is tagged `learned` / `mixed` / `default` (● ◐ ○ in the UI) so you can see how much data backs it.

Learned speeds include time at stops, but a bus *arrives* before it dwells at yours, so
`BUSALERT_ARRIVAL_CREDIT` (default 15 s) is credited back.

### Accuracy is measured, not assumed

Every alert records the predicted time to arrival; when the bus later reaches the stop (within 40 m), the
actual time is stored. `GET /api/accuracy` reports bias and mean absolute error per threshold. On the
built-in simulator (3 h run) the mean absolute error is roughly 13 s at the 1-min alert, 9 s at 2 min,
~22 s at 5 min and ~35 s at 10 min. **Those numbers are from the simulator**, whose buses are far better
behaved than real traffic; check `/api/accuracy` on real data and tune `BUSALERT_ARRIVAL_CREDIT` /
`BUSALERT_DEFAULT_SPEED` if it shows a consistent bias.

## Alert rules

* Each bus triggers each threshold at most once per approach.
* If a bus is first seen already inside a threshold (or the feed skips ahead), only the tightest crossed
  threshold is announced, never a burst.
* ETA jitter doesn't re-fire; a bus whose ETA jumps well back up re-arms.
* Positions older than 2 minutes are ignored, so a stalled feed never produces false alerts. The UI shows a
  "feed delayed" banner when the feed is stale.

## Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `BUSALERT_DATA_DIR` | `data` | databases and cached GTFS |
| `BUSALERT_POLL_INTERVAL` | `3` | seconds between vehicle polls (extra polls of an unchanged feed are skipped) |
| `BUSALERT_VEHICLE_URL`, `_STATIC_URL`, `_TRIPUPDATE_URL`, `_ALERTS_URL` | TransMilenio URLs above | |
| `BUSALERT_DEFAULT_SPEED` | `5.0` | m/s prior for unlearned segments (includes dwell) |
| `BUSALERT_ARRIVAL_CREDIT` | `15` | seconds, see above |
| `BUSALERT_NTFY_SERVER` | `https://ntfy.sh` | |
| `BUSALERT_LOG_POSITIONS` | `watched` | `off` \| `watched` \| `all` raw position log for offline analysis (pruned after `BUSALERT_RETENTION_DAYS`, default 7) |
| `BUSALERT_DEMO`, `BUSALERT_DEMO_SPEED` | off, `1` | simulator, and its time multiplier |

## API

`GET /api/stops?q=` or `?lat=&lon=` · `GET/POST /api/watches` · `DELETE /api/watches/{id}` ·
`GET /api/stream?client_id=` (SSE: `tick`, `alert`) · `GET /api/alerts` · `GET /api/accuracy` ·
`GET /api/service-alerts` · `GET /api/health`. Per-user endpoints take an `X-Client-Id` header (the browser
generates one).

## Limits to know about

* **No accounts.** The client id stored in the browser is the only thing separating one user's watches from
  another's. Fine for personal/household use; put it behind auth before exposing it publicly.
* **Update rate is capped by the feed.** The map glides buses between updates, but positions only change as
  often as TransMilenio publishes them (`--check` tells you how often that is).
* Map tiles come from OpenStreetMap's public tile server (fine for light use; use your own tile provider for
  anything heavier). Leaflet is vendored in `web/vendor/`.
* Terminal layovers and unscheduled stops aren't modelled; a bus waiting at its origin looks "late".
* One representative stop pattern per shape is used; express/local services sharing a shape share stops.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Covers geometry, the speed model, alert rules, GTFS-RT decoding/fetching, the `--check` probe, and an
end-to-end three-hour simulation (tracking, ETAs, alerts, accuracy, persistence).
