"""Static GTFS -> SQLite, plus the in-memory route network built from it.

A *pattern* is one physical way a bus runs (shape + ordered stops). Patterns are keyed by
shape_id (or "route:direction" when a feed has no shapes) and represented by the first trip
seen for them; every trip maps to its pattern so live vehicles can be placed on a route.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import threading
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np

from .db import connect
from .geo import PROJ, Polyline

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE stops(stop_id TEXT PRIMARY KEY, code TEXT, name TEXT, lat REAL, lon REAL);
CREATE TABLE routes(route_id TEXT PRIMARY KEY, short_name TEXT, long_name TEXT, color TEXT);
CREATE TABLE trips(trip_id TEXT PRIMARY KEY, route_id TEXT, pattern_id TEXT);
CREATE TABLE patterns(pattern_id TEXT PRIMARY KEY, route_id TEXT, direction_id INT, headsign TEXT);
CREATE TABLE pattern_stops(pattern_id TEXT, seq INT, stop_id TEXT, PRIMARY KEY(pattern_id, seq));
CREATE INDEX pattern_stops_stop ON pattern_stops(stop_id);
CREATE TABLE shape_points(pattern_id TEXT, seq INT, lat REAL, lon REAL, PRIMARY KEY(pattern_id, seq));
"""


def _rows(zf: zipfile.ZipFile, name: str):
    for info in zf.infolist():
        if info.filename.rsplit("/", 1)[-1] == name:
            with zf.open(info) as raw:
                yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
            return


def _has(zf: zipfile.ZipFile, name: str) -> bool:
    return any(i.filename.rsplit("/", 1)[-1] == name for i in zf.infolist())


def build_static_db(zip_path: Path, db_path: Path) -> None:
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = connect(tmp)
    con.executescript(SCHEMA)
    with zipfile.ZipFile(zip_path) as zf:
        con.executemany(
            "INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?)",
            (
                (r["stop_id"], r.get("stop_code") or "", r.get("stop_name") or "",
                 float(r["stop_lat"]), float(r["stop_lon"]))
                for r in _rows(zf, "stops.txt")
                if r.get("stop_lat") and r.get("stop_lon")
            ),
        )
        con.executemany(
            "INSERT OR REPLACE INTO routes VALUES (?,?,?,?)",
            (
                (r["route_id"], r.get("route_short_name") or "", r.get("route_long_name") or "",
                 r.get("route_color") or "")
                for r in _rows(zf, "routes.txt")
            ),
        )

        rep_trip: dict[str, str] = {}
        pattern_meta: dict[str, tuple[str, int, str]] = {}
        trip_rows = []
        for r in _rows(zf, "trips.txt"):
            direction = int(r["direction_id"]) if r.get("direction_id") else 0
            pid = r.get("shape_id") or f"{r['route_id']}:{direction}"
            trip_rows.append((r["trip_id"], r["route_id"], pid))
            if pid not in rep_trip:
                rep_trip[pid] = r["trip_id"]
                pattern_meta[pid] = (r["route_id"], direction, r.get("trip_headsign") or "")
        con.executemany("INSERT OR REPLACE INTO trips VALUES (?,?,?)", trip_rows)

        wanted = {t: p for p, t in rep_trip.items()}
        stop_seq: dict[str, list[tuple[int, str]]] = {}
        for r in _rows(zf, "stop_times.txt"):
            pid = wanted.get(r["trip_id"])
            if pid is not None:
                stop_seq.setdefault(pid, []).append((int(r["stop_sequence"]), r["stop_id"]))
        for pid, seq in stop_seq.items():
            route_id, direction, headsign = pattern_meta[pid]
            con.execute("INSERT INTO patterns VALUES (?,?,?,?)", (pid, route_id, direction, headsign))
            seq.sort()
            con.executemany(
                "INSERT OR IGNORE INTO pattern_stops VALUES (?,?,?)",
                ((pid, i, stop_id) for i, (_, stop_id) in enumerate(seq)),
            )

        if _has(zf, "shapes.txt"):
            batch = []
            for r in _rows(zf, "shapes.txt"):
                pid = r["shape_id"]
                if pid in stop_seq:
                    batch.append((pid, int(float(r["shape_pt_sequence"])),
                                  float(r["shape_pt_lat"]), float(r["shape_pt_lon"])))
                    if len(batch) >= 50_000:
                        con.executemany("INSERT OR REPLACE INTO shape_points VALUES (?,?,?,?)", batch)
                        batch.clear()
            con.executemany("INSERT OR REPLACE INTO shape_points VALUES (?,?,?,?)", batch)

    # Patterns without a shape get one drawn through their stops.
    missing = con.execute(
        "SELECT pattern_id FROM patterns WHERE pattern_id NOT IN (SELECT pattern_id FROM shape_points)"
    ).fetchall()
    for (pid,) in missing:
        con.execute(
            "INSERT INTO shape_points SELECT ps.pattern_id, ps.seq, s.lat, s.lon FROM pattern_stops ps "
            "JOIN stops s USING(stop_id) WHERE ps.pattern_id=?",
            (pid,),
        )
    con.commit()
    con.close()
    os.replace(tmp, db_path)
    log.info("static GTFS loaded: %d patterns (%d without shapes)", len(stop_seq), len(missing))


def ensure_static(url: str, data_dir: Path, max_age_days: float = 7.0) -> Path:
    """Return the path to a current static DB, downloading/rebuilding when stale."""
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path, zip_path = data_dir / "static.db", data_dir / "GTFS.zip"
    if db_path.exists() and time.time() - db_path.stat().st_mtime < max_age_days * 86400:
        return db_path
    try:
        log.info("downloading static GTFS from %s", url)
        with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        build_static_db(zip_path, db_path)
    except Exception:
        if db_path.exists():
            log.exception("static GTFS refresh failed; keeping the existing copy")
            return db_path
        raise
    return db_path


@dataclass(slots=True)
class Stop:
    id: str
    code: str
    name: str
    lat: float
    lon: float
    norm: str = ""


@dataclass(slots=True)
class Pattern:
    id: str
    route_id: str
    direction: int
    headsign: str
    stop_ids: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn"
    )


class Network:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        con = connect(db_path)
        self.stops = {
            r["stop_id"]: Stop(r["stop_id"], r["code"], r["name"], r["lat"], r["lon"], _norm(r["name"]))
            for r in con.execute("SELECT * FROM stops")
        }
        self.routes = {r["route_id"]: dict(r) for r in con.execute("SELECT * FROM routes")}
        self.patterns: dict[str, Pattern] = {
            r["pattern_id"]: Pattern(r["pattern_id"], r["route_id"], r["direction_id"], r["headsign"])
            for r in con.execute("SELECT * FROM patterns")
        }
        for r in con.execute("SELECT pattern_id, stop_id FROM pattern_stops ORDER BY pattern_id, seq"):
            p = self.patterns.get(r["pattern_id"])
            if p and r["stop_id"] in self.stops:
                p.stop_ids.append(r["stop_id"])
        self.patterns = {k: p for k, p in self.patterns.items() if len(p.stop_ids) >= 2}
        self.trip_pattern = {
            r["trip_id"]: r["pattern_id"]
            for r in con.execute("SELECT trip_id, pattern_id FROM trips")
            if r["pattern_id"] in self.patterns
        }
        con.close()

        self.route_patterns: dict[str, list[Pattern]] = {}
        self.stop_patterns: dict[str, dict[str, list[int]]] = {}
        for p in self.patterns.values():
            self.route_patterns.setdefault(p.route_id, []).append(p)
            for i, sid in enumerate(p.stop_ids):
                self.stop_patterns.setdefault(sid, {}).setdefault(p.id, []).append(i)
        self._short = {}
        for rid, r in self.routes.items():
            self._short.setdefault(r["short_name"].strip().lower(), rid)
        self._polys: dict[str, Polyline | None] = {}
        self._offsets: dict[str, np.ndarray | None] = {}
        self._lock = threading.Lock()
        served = [s for s in self.stops.values() if s.id in self.stop_patterns]
        self._served = served
        self._served_xy = np.array([PROJ.to_xy(s.lat, s.lon) for s in served]).reshape(-1, 2)
        log.info("network: %d stops, %d routes, %d patterns", len(self.stops), len(self.routes),
                 len(self.patterns))

    # -- lookups ---------------------------------------------------------------------------
    def resolve_route(self, route_id: str | None) -> str | None:
        """Live feeds don't always use the static route_id; fall back to the short name."""
        if not route_id:
            return None
        if route_id in self.route_patterns:
            return route_id
        return self._short.get(route_id.strip().lower())

    def route_name(self, route_id: str) -> str:
        r = self.routes.get(route_id)
        return (r["short_name"] or r["long_name"] or route_id) if r else route_id

    def routes_at_stop(self, stop_id: str) -> list[str]:
        ids = {self.patterns[p].route_id for p in self.stop_patterns.get(stop_id, {})}
        return sorted(ids, key=lambda r: self.route_name(r))

    def polyline(self, pattern: Pattern) -> Polyline | None:
        with self._lock:
            if pattern.id in self._polys:
                return self._polys[pattern.id]
        con = connect(self.db_path)
        pts = con.execute(
            "SELECT lat, lon FROM shape_points WHERE pattern_id=? ORDER BY seq", (pattern.id,)
        ).fetchall()
        con.close()
        poly = None
        if len(pts) >= 2:
            try:
                poly = Polyline([p[0] for p in pts], [p[1] for p in pts])
            except ValueError:
                poly = None
        with self._lock:
            self._polys[pattern.id] = poly
        return poly

    def stop_offsets(self, pattern: Pattern) -> np.ndarray | None:
        """Distance along the shape of each stop in the pattern (monotonic)."""
        with self._lock:
            if pattern.id in self._offsets:
                return self._offsets[pattern.id]
        poly = self.polyline(pattern)
        out = None
        if poly is not None:
            prev, vals = 0.0, []
            for sid in pattern.stop_ids:
                st = self.stops[sid]
                pr = poly.project(st.lat, st.lon, s_min=prev - 1, s_max=prev + 8000)
                if pr.off > 120:
                    pr = poly.project(st.lat, st.lon, s_min=prev - 1)
                prev = max(prev, pr.s)
                vals.append(prev)
            out = np.array(vals)
        with self._lock:
            self._offsets[pattern.id] = out
        return out

    # -- search ----------------------------------------------------------------------------
    def search_stops(self, q: str, limit: int = 20) -> list[Stop]:
        q = _norm(q.strip())
        if not q:
            return []
        tokens = q.split()
        scored = []
        for s in self._served:
            if s.code and s.code.lower() == q:
                score = 0
            elif all(t in s.norm for t in tokens):
                score = 1 if s.norm.startswith(tokens[0]) else 2
            else:
                continue
            scored.append((score, s.name, s))
        scored.sort(key=lambda t: t[:2])
        return [s for _, _, s in scored[:limit]]

    def nearest_stops(self, lat: float, lon: float, limit: int = 10) -> list[tuple[Stop, float]]:
        if not self._served:
            return []
        x, y = PROJ.to_xy(lat, lon)
        d = np.hypot(self._served_xy[:, 0] - x, self._served_xy[:, 1] - y)
        idx = np.argsort(d)[:limit]
        return [(self._served[i], float(d[i])) for i in idx]
