from __future__ import annotations

import numpy as np

from .model import Eta
from .static import Network
from .tracker import Tracker
from .speedmodel import SpeedModel

STALE_S = 120.0  # ignore vehicles whose last fix is older than this
AT_STOP_M = 40.0
PASSED_KEEP_M = 300.0
MAX_ETA_S = 90 * 60
MAX_RESULTS = 10
DEFAULT_ARRIVAL_CREDIT_S = 15.0


class Predictor:
    def __init__(self, net: Network, tracker: Tracker, model: SpeedModel,
                 arrival_credit_s: float = DEFAULT_ARRIVAL_CREDIT_S):
        self.net, self.tracker, self.model = net, tracker, model
        # Learned speeds include dwell at every stop, but a bus *arrives* before dwelling at the
        # target stop, so up to one dwell is credited back (never more than half the trip time).
        self.arrival_credit_s = arrival_credit_s
        self.trip_updates: dict[str, dict[str, float]] = {}  # official: trip -> stop -> epoch

    def etas_for(self, stop_id: str, route_ids: set[str] | None, now: float) -> list[Eta]:
        approaching: list[Eta] = []
        passed: list[Eta] = []
        for pid, idxs in self.net.stop_patterns.get(stop_id, {}).items():
            pat = self.net.patterns[pid]
            if route_ids and pat.route_id not in route_ids:
                continue
            offsets = self.net.stop_offsets(pat)
            if offsets is None:
                continue
            stop_s = [float(offsets[i]) for i in idxs]
            for vid in self.tracker.by_pattern.get(pid, ()):
                st = self.tracker.states[vid]
                v = st.vehicle
                age = max(0.0, now - v.ts)
                if age > STALE_S:
                    continue
                ahead = [d for d in (x - st.s for x in stop_s) if d >= -AT_STOP_M]
                d = min(ahead) if ahead else max(x - st.s for x in stop_s)
                status = "approaching" if ahead else "passed"
                if status == "passed" and d < -PASSED_KEEP_M:
                    continue
                if status == "approaching" and d <= AT_STOP_M:
                    eta, frac = 0.0, 1.0
                elif status == "approaching":
                    eta, frac = self.model.travel_time(
                        pid, st.s, st.s + d, v.ts, self.tracker.live_factor(st, v.ts)
                    )
                    eta = max(0.0, eta - min(self.arrival_credit_s, eta / 2) - age)
                else:
                    eta, frac = 0.0, 1.0
                if eta > MAX_ETA_S:
                    continue
                target = stop_s[0]
                for x in stop_s:
                    if x - st.s >= -AT_STOP_M:
                        target = x
                        break
                away = int(
                    np.searchsorted(offsets, target - 1) - np.searchsorted(offsets, st.s - AT_STOP_M)
                )
                off = self.trip_updates.get(v.trip_id or "", {}).get(stop_id)
                e = Eta(
                    vehicle_id=vid, route_id=pat.route_id, route_name=self.net.route_name(pat.route_id),
                    headsign=pat.headsign, pattern_id=pid, status=status, eta_s=eta,
                    distance_m=max(d, 0.0), stops_away=max(away, 0),
                    basis="learned" if frac >= 0.8 else ("mixed" if frac > 0.1 else "default"),
                    lat=v.lat, lon=v.lon, bearing=v.bearing, vehicle_ts=v.ts,
                    official_eta_s=max(0.0, off - now) if off else None,
                )
                (approaching if status == "approaching" else passed).append(e)
        approaching.sort(key=lambda e: e.eta_s)
        return approaching[:MAX_RESULTS] + passed
