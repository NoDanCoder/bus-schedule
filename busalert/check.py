"""`python -m busalert --check`: probe the live feeds and report whether they fit this app's
assumptions (ids matching the static GTFS, update rate, which optional fields are present)."""
from __future__ import annotations

import statistics
import time

import httpx

from .config import Settings
from .feed import decode, parse_service_alerts, parse_trip_updates, parse_vehicles
from .static import Network, ensure_static


def run_check(cfg: Settings, samples: int = 4, gap_s: float = 5.0, out=print) -> bool:
    ok = True
    client = httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "busalert-check/0.1"})

    def fetch(url):
        t = time.time()
        r = client.get(url)
        r.raise_for_status()
        return decode(r.content), time.time() - t, len(r.content)

    out(f"vehicle feed: {cfg.vehicle_url}")
    headers, last = [], None
    for i in range(samples):
        try:
            msg, secs, size = fetch(cfg.vehicle_url)
        except Exception as exc:
            out(f"  FAIL: {exc!r}")
            return False
        vehicles, hdr = parse_vehicles(msg)
        headers.append(hdr)
        out(f"  poll {i + 1}: {len(vehicles)} vehicles, {size / 1024:.0f} KiB in {secs:.2f}s, "
            f"header {time.time() - hdr:.0f}s old")
        last = vehicles
        if i < samples - 1:
            time.sleep(gap_s)
    changes = sorted(set(headers))
    if len(changes) > 1:
        steps = [b - a for a, b in zip(changes, changes[1:])]
        out(f"  feed refreshes about every {statistics.median(steps):.0f}s "
            f"(polling faster than that gains nothing)")
    else:
        out(f"  header time did not change in {samples * gap_s:.0f}s: refresh is slower than that")
    if not last:
        out("  FAIL: no vehicles in feed")
        return False
    n = len(last)

    def pct(k: int) -> str:
        return f"{100 * k / n:.0f}%"

    out(f"  fields present: trip_id {pct(sum(bool(v.trip_id) for v in last))}, "
        f"route_id {pct(sum(bool(v.route_id) for v in last))}, "
        f"bearing {pct(sum(v.bearing is not None for v in last))}, "
        f"speed {pct(sum(v.speed is not None for v in last))}, "
        f"direction {pct(sum(v.direction_id is not None for v in last))}")
    ages = sorted(time.time() - v.ts for v in last)
    out(f"  position age: median {statistics.median(ages):.0f}s, p90 {ages[int(n * 0.9)]:.0f}s")

    out(f"static GTFS: {cfg.static_url}")
    try:
        net = Network(ensure_static(cfg.static_url, cfg.data_dir))
    except Exception as exc:
        out(f"  FAIL: {exc!r}")
        return False
    out(f"  {len(net.stops)} stops, {len(net.routes)} routes, {len(net.patterns)} route patterns")
    trip_ok = sum(v.trip_id in net.trip_pattern for v in last)
    route_ok = sum(net.resolve_route(v.route_id) is not None for v in last)
    out(f"  live trip_id found in static:  {pct(trip_ok)}")
    out(f"  live route_id found in static: {pct(route_ok)}")
    if trip_ok / n < 0.5 and route_ok / n < 0.5:
        ok = False
        ex = last[0]
        out(f"  PROBLEM: ids do not line up (example live trip_id={ex.trip_id!r} route_id={ex.route_id!r}; "
            f"static route example {next(iter(net.routes), None)!r}). Vehicles can't be placed on routes; "
            "see Network.resolve_route in static.py.")

    for label, url, parse in (("trip updates", cfg.tripupdate_url, parse_trip_updates),
                              ("service alerts", cfg.alerts_url, parse_service_alerts)):
        try:
            msg, _, size = fetch(url)
            out(f"{label}: {len(parse(msg))} entries ({size / 1024:.0f} KiB), optional")
        except Exception as exc:
            out(f"{label}: unavailable ({exc!r}), optional, the app works without it")
    return ok
