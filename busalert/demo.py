"""Synthetic city + moving buses, so the whole stack can run without the live feed.

Two routes (a north-south trunk "K10" and a cross-town "B74"), each in both directions, with a
congested stretch on K10 so the speed model has something to learn.
"""
from __future__ import annotations

import io
import math
import random
import time
import zipfile
from pathlib import Path

import numpy as np

from .geo import PROJ, Polyline
from .model import Vehicle

STOP_SPACING_M = 500.0


def _line(p0, p1, wiggle_m: float, lane_offset_m: float = 0.0, step_m: float = 50.0):
    (x0, y0), (x1, y1) = (PROJ.to_xy(*p0), PROJ.to_xy(*p1))
    length = float(np.hypot(x1 - x0, y1 - y0))
    n = int(length // step_m) + 1
    t = np.linspace(0, 1, n)
    nx, ny = -(y1 - y0) / length, (x1 - x0) / length
    off = wiggle_m * np.sin(t * 9) + lane_offset_m
    x, y = x0 + t * (x1 - x0) + nx * off, y0 + t * (y1 - y0) + ny * off
    lat, lon = PROJ.to_latlon(x, y)
    return lat, lon


ROUTES = {
    "K10": dict(name="K10", long="Portal Norte - Tunal", a=(4.60, -74.08), b=(4.69, -74.06)),
    "B74": dict(name="B74", long="Suba - Kennedy", a=(4.655, -74.12), b=(4.640, -74.04)),
}


def _patterns():
    out = {}
    for rid, r in ROUTES.items():
        for direction in (0, 1):
            a, b = (r["a"], r["b"]) if direction == 0 else (r["b"], r["a"])
            lat, lon = _line(a, b, wiggle_m=120.0, lane_offset_m=-12.0 if direction == 0 else 12.0)
            poly = Polyline(lat, lon)
            n_stops = int(poly.length // STOP_SPACING_M)
            stops = []
            for i in range(n_stops + 1):
                s = min(poly.length, i * STOP_SPACING_M)
                sl, so = poly.point_at(s)
                stops.append((f"{rid}-{direction}-{i}", f"{r['name']} {'N' if direction == 0 else 'S'}-{i:02d}", sl, so, s))
            out[f"{rid}-{direction}"] = dict(route=rid, direction=direction, poly=poly, lat=lat, lon=lon,
                                             stops=stops, head=r["long"].split(" - ")[direction])
    return out


PATTERNS = _patterns()


def build_demo_gtfs(path: Path) -> None:
    buf = io.StringIO
    files: dict[str, str] = {}
    f = buf()
    f.write("stop_id,stop_code,stop_name,stop_lat,stop_lon\n")
    for p in PATTERNS.values():
        for sid, name, lat, lon, _ in p["stops"]:
            f.write(f"{sid},{sid.replace('-', '')},{name},{lat:.6f},{lon:.6f}\n")
    files["stops.txt"] = f.getvalue()
    files["routes.txt"] = "route_id,route_short_name,route_long_name\n" + "".join(
        f"{rid},{r['name']},{r['long']}\n" for rid, r in ROUTES.items())
    t, st, sh = buf(), buf(), buf()
    t.write("route_id,service_id,trip_id,direction_id,shape_id,trip_headsign\n")
    st.write("trip_id,stop_id,stop_sequence,arrival_time,departure_time\n")
    sh.write("shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n")
    for pid, p in PATTERNS.items():
        for k in range(2):
            t.write(f"{p['route']},WK,{pid}-T{k},{p['direction']},{pid},{p['head']}\n")
        for i, (sid, *_rest) in enumerate(p["stops"]):
            st.write(f"{pid}-T0,{sid},{i},00:00:00,00:00:00\n")
        for i, (la, lo) in enumerate(zip(p["lat"], p["lon"])):
            sh.write(f"{pid},{la:.6f},{lo:.6f},{i}\n")
    files["trips.txt"], files["stop_times.txt"], files["shapes.txt"] = t.getvalue(), st.getvalue(), sh.getvalue()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)


class _Bus:
    def __init__(self, pid: str, vid: str, s: float, rng: random.Random, unresolved: bool):
        self.pid, self.id, self.s, self.rng = pid, vid, s, rng
        self.cruise = rng.uniform(7.5, 11.0)
        self.dwell_left = 0.0
        self.wait_left = 0.0
        self.unresolved = unresolved
        self.trip = f"{pid}-T{rng.randint(0, 1)}"
        self.last_stop = int(s // STOP_SPACING_M)


class DemoSource:
    """Simulates buses at `speed`x real time. `clock()` is the shared (virtual) time source."""

    def __init__(self, speed: float = 1.0, buses_per_pattern: int = 5, seed: int = 7):
        self.speed = speed
        self.rng = random.Random(seed)
        self._t0 = time.time()
        self._last_virtual = self._t0
        self.buses: list[_Bus] = []
        for pid, p in PATTERNS.items():
            for k in range(buses_per_pattern):
                s = p["poly"].length * (k + self.rng.random() * 0.5) / buses_per_pattern
                self.buses.append(_Bus(pid, f"{pid}-bus{k}", s, self.rng, unresolved=self.rng.random() < 0.15))

    def clock(self) -> float:
        return self._t0 + (time.time() - self._t0) * self.speed

    def _step(self, dt: float) -> None:
        for b in self.buses:
            p = PATTERNS[b.pid]
            if b.wait_left > 0:
                b.wait_left -= dt
                continue
            if b.dwell_left > 0:
                b.dwell_left -= dt
                continue
            v = b.cruise * (0.45 if b.pid.startswith("K10") and 3000 < b.s < 4500 else 1.0)
            b.s += v * dt
            idx = int(b.s // STOP_SPACING_M)
            if idx > b.last_stop:
                b.last_stop = idx
                b.dwell_left = self.rng.uniform(12, 35)
            if b.s >= p["poly"].length:
                b.s, b.last_stop, b.wait_left = 0.0, 0, 30.0

    async def fetch(self) -> tuple[list[Vehicle], float]:
        now = self.clock()
        dt, self._last_virtual = now - self._last_virtual, now
        # sub-step so large virtual jumps don't skip stops
        for _ in range(max(1, int(dt // 2))):
            self._step(dt / max(1, int(dt // 2)))
        out = []
        for b in self.buses:
            p = PATTERNS[b.pid]
            lat, lon = p["poly"].point_at(b.s)
            j = self.rng.gauss(0, 3.0)  # GPS noise (m)
            lat2, lon2 = PROJ.to_latlon(*(np.array(PROJ.to_xy(lat, lon)) + j))
            ax, ay = PROJ.to_xy(*p["poly"].point_at(b.s + 20))
            x, y = PROJ.to_xy(lat, lon)
            bearing = (math.degrees(math.atan2(float(ax - x), float(ay - y))) + 360) % 360
            out.append(Vehicle(
                id=b.id, lat=float(lat2), lon=float(lon2), ts=now,
                trip_id=("UNKNOWN-" + b.trip) if b.unresolved else b.trip,
                route_id=p["route"], direction_id=p["direction"], bearing=bearing,
                status="STOPPED_AT" if b.dwell_left > 0 else "IN_TRANSIT_TO",
            ))
        return out, now
