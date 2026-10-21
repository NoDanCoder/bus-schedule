"""Places each live vehicle on its route pattern and learns from its movement."""
from __future__ import annotations

import logging
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .geo import PROJ, heading_diff
from .model import Vehicle
from .speedmodel import SpeedModel
from .static import Network

log = logging.getLogger(__name__)

MAX_OFF_M = 100.0  # farther than this from the shape = not on this pattern
STICKY_OFF_M = 60.0
HISTORY_S = 240.0
DROP_AFTER_S = 300.0
TERMINAL_M = 100.0  # ignore speed observations near the ends (layovers)
LEARN_MIN_DT = 20.0  # observe speed over spans at least this long (fixes may arrive every 3 s)
LEARN_MAX_DT = 180.0


@dataclass
class TrackState:
    vehicle: Vehicle
    pattern_id: str
    s: float
    off: float
    t: float
    resolved: bool
    hist: deque = field(default_factory=deque)  # (t, s)
    anchor: tuple[float, float] = (0.0, 0.0)  # last (t, s) already fed to the speed model
    seen_at: float = 0.0


class Tracker:
    def __init__(self, net: Network, model: SpeedModel):
        self.net, self.model = net, model
        self.states: dict[str, TrackState] = {}
        self.by_pattern: dict[str, set[str]] = defaultdict(set)
        self._raw: dict[str, tuple[float, float]] = {}  # last position that moved >15 m
        self._heading: dict[str, float] = {}

    # -- public ----------------------------------------------------------------------------
    def update_all(self, vehicles: list[Vehicle], now: float) -> None:
        for v in vehicles:
            try:
                self._update(v, now)
            except Exception:
                log.exception("tracking failed for vehicle %s", v.id)
        for vid in [k for k, st in self.states.items() if now - st.seen_at > DROP_AFTER_S]:
            self._drop(vid)

    def live_factor(self, st: TrackState, when: float) -> float:
        """Observed vs expected progress over the last ~3 minutes (1.0 = as expected)."""
        if len(st.hist) < 2:
            return 1.0
        t1, s1 = st.hist[-1]
        t0, s0 = next((x for x in st.hist if t1 - x[0] <= 180), st.hist[-1])
        if t1 - t0 < 45:
            return 1.0
        expected, _ = self.model.travel_time(st.pattern_id, s0, max(s1, s0 + 1), when)
        if expected <= 0:
            return 1.0
        return min(max(expected / (t1 - t0), 0.5), 1.5)

    # -- internals -------------------------------------------------------------------------
    def _drop(self, vid: str) -> None:
        st = self.states.pop(vid, None)
        if st:
            self.by_pattern[st.pattern_id].discard(vid)
        self._raw.pop(vid, None)
        self._heading.pop(vid, None)

    def _vehicle_heading(self, v: Vehicle) -> float | None:
        if v.bearing is not None:
            return v.bearing
        prev = self._raw.get(v.id)
        if prev is None:
            self._raw[v.id] = (v.lat, v.lon)
            return self._heading.get(v.id)
        (x0, y0), (x1, y1) = PROJ.to_xy(*prev), PROJ.to_xy(v.lat, v.lon)
        dx, dy = float(x1 - x0), float(y1 - y0)
        if dx * dx + dy * dy > 15.0**2:
            self._heading[v.id] = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
            self._raw[v.id] = (v.lat, v.lon)
        return self._heading.get(v.id)

    def _locate(self, v: Vehicle, pattern_id: str, st: TrackState | None):
        pat = self.net.patterns[pattern_id]
        poly = self.net.polyline(pat)
        if poly is None:
            return None
        if st is not None and st.pattern_id == pattern_id:
            pr = poly.project(v.lat, v.lon, s_min=st.s - 150, s_max=st.s + 2500)
            if pr.off <= STICKY_OFF_M:
                return pr
        pr = poly.project(v.lat, v.lon)
        return pr if pr.off <= MAX_OFF_M else None

    def _choose_pattern(self, v: Vehicle, st: TrackState | None):
        """Vehicle's trip isn't in the static feed: pick the route pattern it is travelling on."""
        route = self.net.resolve_route(v.route_id)
        cands = self.net.route_patterns.get(route, []) if route else []
        if v.direction_id is not None:
            same_dir = [p for p in cands if p.direction == v.direction_id]
            cands = same_dir or cands
        if st is not None and not st.resolved and st.pattern_id in {p.id for p in cands}:
            pr = self._locate(v, st.pattern_id, st)
            if pr is not None and pr.off <= STICKY_OFF_M:
                return st.pattern_id, pr
        heading = self._vehicle_heading(v)
        best = None
        for p in cands:
            poly = self.net.polyline(p)
            if poly is None:
                continue
            pr = poly.project(v.lat, v.lon)
            if pr.off > MAX_OFF_M:
                continue
            score = pr.off + (0 if heading is None or heading_diff(heading, pr.heading) <= 60 else 250)
            if best is None or score < best[0]:
                best = (score, p.id, pr)
        return (best[1], best[2]) if best else (None, None)

    def _update(self, v: Vehicle, now: float) -> None:
        st = self.states.get(v.id)
        if st is not None:
            st.seen_at = now
            if v.ts <= st.t:
                return
        pid = self.net.trip_pattern.get(v.trip_id) if v.trip_id else None
        resolved = pid is not None
        if resolved:
            pr = self._locate(v, pid, st)
        else:
            pid, pr = self._choose_pattern(v, st)
        if pid is None or pr is None:
            if st is not None:
                self._drop(v.id)
            return

        prev = st if (st is not None and st.pattern_id == pid) else None
        if st is not None and prev is None:
            self._drop(v.id)
        new = TrackState(v, pid, pr.s, pr.off, v.ts, resolved, anchor=(v.ts, pr.s), seen_at=now)
        if prev is not None:
            new.hist, new.anchor = prev.hist, prev.anchor
            at, a_s = new.anchor
            dt, ds = v.ts - at, pr.s - a_s
            if dt > LEARN_MAX_DT:
                new.anchor = (v.ts, pr.s)
            elif dt >= LEARN_MIN_DT:
                length = self.net.polyline(self.net.patterns[pid]).length
                if 0 < ds <= dt * 25 and a_s > TERMINAL_M and pr.s < length - TERMINAL_M:
                    self.model.observe(pid, a_s, pr.s, dt, v.ts)
                new.anchor = (v.ts, pr.s)
        new.hist.append((v.ts, pr.s))
        while new.hist and v.ts - new.hist[0][0] > HISTORY_S:
            new.hist.popleft()
        self.states[v.id] = new
        self.by_pattern[pid].add(v.id)
