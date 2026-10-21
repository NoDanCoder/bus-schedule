"""Fires 10/5/2/1-minute (configurable) arrival alerts, once per bus per threshold."""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Eta, Watch

REARM_FACTOR = 1.3  # a bus whose ETA jumps back above T*1.3+90s re-arms threshold T
REARM_SLACK_S = 90.0
FORGET_AFTER_S = 600.0
AT_STOP_M = 40.0  # within this distance the bus counts as having arrived


@dataclass
class AlertEvent:
    watch: Watch
    eta: Eta
    threshold: int  # minutes
    message: str
    title: str


@dataclass
class _VState:
    fired: dict[int, tuple[float, float, int | None]] = field(default_factory=dict)  # T -> (at, eta, alert_id)
    last_seen: float = 0.0
    arrived: bool = False


def describe(eta: Eta, stop_name: str, threshold: int) -> tuple[str, str]:
    mins = max(1, round(eta.eta_s / 60)) if eta.eta_s >= 30 else 0
    when = "arriving now" if mins == 0 else f"about {mins} min away"
    head = f" to {eta.headsign}" if eta.headsign else ""
    title = f"{eta.route_name}: {threshold} min" if threshold else f"{eta.route_name}: arriving"
    return title, f"{eta.route_name}{head} is {when} from {stop_name}"


class AlertEngine:
    def __init__(self):
        self._state: dict[tuple[str, str], _VState] = {}

    def process(self, watch: Watch, etas: list[Eta], stop_name: str, now: float):
        """Returns (events, arrivals). `arrivals` lists (alert_id, actual_s) for buses that have
        since reached the stop, so the caller can record prediction accuracy."""
        events: list[AlertEvent] = []
        arrivals: list[tuple[int, float]] = []
        thresholds = sorted(set(watch.thresholds), reverse=True)
        for e in etas:
            key = (watch.id, e.vehicle_id)
            if e.status == "passed":
                st = self._state.pop(key, None)
                if st and not st.arrived:
                    arrivals += self._arrival(st, now)
                continue
            st = self._state.setdefault(key, _VState())
            st.last_seen = now
            if e.distance_m <= AT_STOP_M and not st.arrived:
                st.arrived = True
                arrivals += self._arrival(st, now)
            for t in list(st.fired):
                if e.eta_s > t * 60 * REARM_FACTOR + REARM_SLACK_S:
                    del st.fired[t]
            crossed = [t for t in thresholds if e.eta_s <= t * 60 and t not in st.fired]
            if not crossed:
                continue
            tightest = min(crossed)
            for t in crossed:
                # Higher thresholds already crossed are marked, not announced.
                st.fired[t] = (now, e.eta_s, None)
            title, msg = describe(e, stop_name, tightest)
            events.append(AlertEvent(watch, e, tightest, msg, title))
        for key in [k for k, s in self._state.items() if now - s.last_seen > FORGET_AFTER_S]:
            del self._state[key]
        return events, arrivals

    @staticmethod
    def _arrival(st: _VState, now: float) -> list[tuple[int, float]]:
        return [(aid, now - at) for at, _, aid in st.fired.values() if aid is not None]

    def attach_alert_id(self, watch_id: str, vehicle_id: str, threshold: int, alert_id: int) -> None:
        st = self._state.get((watch_id, vehicle_id))
        if st and threshold in st.fired:
            at, eta, _ = st.fired[threshold]
            st.fired[threshold] = (at, eta, alert_id)

    def forget_watch(self, watch_id: str) -> None:
        for k in [k for k in self._state if k[0] == watch_id]:
            del self._state[k]
