"""Ties feed -> tracker -> predictor -> alerts -> clients together."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

import httpx

from .alerts import AlertEngine, AlertEvent
from .config import Settings
from .demo import DemoSource, build_demo_gtfs
from .feed import HttpFeed, parse_service_alerts, parse_trip_updates, parse_vehicles
from .hub import Hub
from .model import Eta, Watch
from .predict import Predictor
from .speedmodel import SpeedModel
from .static import Network, build_static_db, ensure_static
from .store import Store
from .tracker import Tracker

log = logging.getLogger(__name__)

FLUSH_EVERY_S = 60.0
POSITION_LOG_EVERY_S = 15.0
STALE_FEED_S = 90.0


def eta_json(e: Eta) -> dict:
    return {
        "vehicle_id": e.vehicle_id, "route": e.route_name, "route_id": e.route_id, "headsign": e.headsign,
        "status": e.status, "eta_s": round(e.eta_s, 1), "distance_m": round(e.distance_m),
        "stops_away": e.stops_away, "basis": e.basis, "lat": e.lat, "lon": e.lon, "bearing": e.bearing,
        "vehicle_ts": e.vehicle_ts,
        "official_eta_s": None if e.official_eta_s is None else round(e.official_eta_s, 1),
    }


class Service:
    def __init__(self, settings: Settings):
        self.cfg = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(settings.data_dir / "app.db")
        self.hub = Hub()
        self.engine = AlertEngine()
        self.model = SpeedModel(settings.data_dir / "history.db", settings.default_speed)
        self.net: Network | None = None
        self.tracker: Tracker | None = None
        self.predictor: Predictor | None = None
        self.demo: DemoSource | None = None
        self.clock = time.time
        self.service_alerts: list[dict] = []
        self.feed_ts = 0.0
        self.vehicle_count = 0
        self.feed_errors = 0
        self.last_error = ""
        self._tasks: list[asyncio.Task] = []
        self._http: httpx.AsyncClient | None = None
        self._last_poslog: dict[str, float] = {}
        self._last_tick: dict[str, dict] = {}

    # -- lifecycle -------------------------------------------------------------------------
    async def start(self) -> None:
        self._http = httpx.AsyncClient(timeout=15, follow_redirects=True,
                                       headers={"User-Agent": "busalert/0.1"})
        if self.cfg.demo:
            self.demo = DemoSource(speed=self.cfg.demo_speed)
            self.clock = self.demo.clock
        self._tasks = [asyncio.create_task(self._static_loop()), asyncio.create_task(self._vehicle_loop()),
                       asyncio.create_task(self._maintenance_loop())]
        if not self.cfg.demo:
            self._tasks += [asyncio.create_task(self._aux_loop())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.to_thread(self.model.flush)
        if self._http:
            await self._http.aclose()

    # -- static data -----------------------------------------------------------------------
    def _load_network(self) -> Network:
        d = self.cfg.data_dir
        if self.cfg.demo:
            db = d / "static-demo.db"
            if not db.exists():
                build_demo_gtfs(d / "GTFS-demo.zip")
                build_static_db(d / "GTFS-demo.zip", db)
        else:
            db = ensure_static(self.cfg.static_url, d)
        return Network(db)

    async def _static_loop(self) -> None:
        while True:
            try:
                net = await asyncio.to_thread(self._load_network)
                self.net = net
                self.tracker = Tracker(net, self.model)
                self.predictor = Predictor(net, self.tracker, self.model, self.cfg.arrival_credit_s)
                await asyncio.sleep(6 * 3600)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"static GTFS: {exc}"
                log.exception("static GTFS load failed; retrying in 60s")
                await asyncio.sleep(60)

    # -- realtime --------------------------------------------------------------------------
    async def _vehicle_loop(self) -> None:
        feed = None if self.demo else HttpFeed(self._http, self.cfg.vehicle_url)
        interval = 2.0 if self.demo else self.cfg.poll_interval
        while True:
            t0 = time.monotonic()
            try:
                if self.demo:
                    vehicles, ts = await self.demo.fetch()
                else:
                    msg = await feed.fetch()
                    vehicles, ts = parse_vehicles(msg) if msg is not None else (None, 0.0)
                if vehicles is not None and self.tracker is not None:
                    self.feed_ts, self.vehicle_count = ts, len(vehicles)
                    await self._on_vehicles(vehicles)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.feed_errors += 1
                self.last_error = f"vehicle feed: {exc!r}"
                log.warning("vehicle poll failed: %r", exc)
            await asyncio.sleep(max(0.5, interval - (time.monotonic() - t0)))

    async def _aux_loop(self) -> None:
        """Official trip updates (15 s) and service alerts (60 s); both optional."""
        tu, al = HttpFeed(self._http, self.cfg.tripupdate_url), HttpFeed(self._http, self.cfg.alerts_url)
        n = 0
        while True:
            for feed, parse, every in ((tu, parse_trip_updates, 1), (al, parse_service_alerts, 4)):
                if n % every:
                    continue
                try:
                    msg = await feed.fetch()
                    if msg is None:
                        continue
                    if parse is parse_trip_updates and self.predictor is not None:
                        self.predictor.trip_updates = parse(msg)
                    elif parse is parse_service_alerts:
                        self.service_alerts = parse(msg)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("aux feed %s failed: %r", feed.url, exc)
            n += 1
            await asyncio.sleep(15)

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_EVERY_S)
            n = await asyncio.to_thread(self.model.flush)
            await asyncio.to_thread(self.store.prune, time.time() - self.cfg.retention_days * 86400)
            log.info("flushed %d speed bins; tracking %d vehicles", n,
                     len(self.tracker.states) if self.tracker else 0)

    async def _on_vehicles(self, vehicles) -> None:
        now = self.clock()
        await asyncio.to_thread(self.tracker.update_all, vehicles, now)
        by_client: dict[str, list[Watch]] = defaultdict(list)
        for w in self.store.list_watches():
            by_client[w.client_id].append(w)
        poslog: set[str] = set()
        for client, watches in by_client.items():
            payload = []
            for w in watches:
                etas = self.predictor.etas_for(w.stop_id, set(w.route_ids) or None, now)
                poslog.update(e.vehicle_id for e in etas)
                stop = self.net.stops[w.stop_id]
                events, arrivals = self.engine.process(w, etas, stop.name, now)
                for alert_id, actual in arrivals:
                    self.store.set_actual(alert_id, actual)
                for ev in events:
                    self._dispatch(ev, stop.name, now)
                payload.append({"watch_id": w.id, "etas": [eta_json(e) for e in etas]})
            msg = {"server_ts": now, "feed_ts": self.feed_ts, "watches": payload}
            self._last_tick[client] = msg
            self.hub.publish(client, "tick", msg)
        await asyncio.to_thread(self._log_positions, vehicles, poslog, now)

    def _log_positions(self, vehicles, watched_ids: set[str], now: float) -> None:
        mode = self.cfg.log_positions
        if mode == "off":
            return
        rows = []
        for v in vehicles:
            if mode == "watched" and v.id not in watched_ids:
                continue
            if now - self._last_poslog.get(v.id, 0) < POSITION_LOG_EVERY_S:
                continue
            self._last_poslog[v.id] = now
            rows.append((v.ts, v.id, v.trip_id, v.route_id, v.lat, v.lon, v.speed))
        self.store.log_positions(rows)

    # -- alert dispatch ---------------------------------------------------------------------
    def _dispatch(self, ev: AlertEvent, stop_name: str, now: float) -> None:
        w, e = ev.watch, ev.eta
        alert_id = self.store.log_alert(w, e.vehicle_id, e.route_name, stop_name, ev.threshold,
                                        e.eta_s, ev.message, now)
        self.engine.attach_alert_id(w.id, e.vehicle_id, ev.threshold, alert_id)
        self.hub.publish(w.client_id, "alert", {
            "id": alert_id, "watch_id": w.id, "title": ev.title, "message": ev.message,
            "threshold": ev.threshold, "route": e.route_name, "vehicle_id": e.vehicle_id, "ts": now,
        })
        if w.ntfy_topic:
            asyncio.create_task(self._ntfy(w.ntfy_topic, ev))

    async def _ntfy(self, topic: str, ev: AlertEvent) -> None:
        try:
            r = await self._http.post(self.cfg.ntfy_server, json={
                "topic": topic, "title": ev.title, "message": ev.message,
                "priority": 5 if ev.threshold <= 2 else 3, "tags": ["bus"],
            })
            r.raise_for_status()
        except Exception as exc:
            log.warning("ntfy push failed: %r", exc)

    # -- queries for the API ----------------------------------------------------------------
    def health(self) -> dict:
        now = self.clock()
        age = now - self.feed_ts if self.feed_ts else None
        return {
            "demo": self.cfg.demo, "network_loaded": self.net is not None,
            "feed_age_s": None if age is None else round(age, 1),
            "feed_stale": age is None or age > STALE_FEED_S,
            "vehicles": self.vehicle_count, "tracked": len(self.tracker.states) if self.tracker else 0,
            "feed_errors": self.feed_errors, "last_error": self.last_error, "server_ts": now,
        }

    def geometry(self, w: Watch) -> list[dict]:
        out = []
        for pid in self.net.stop_patterns.get(w.stop_id, {}):
            pat = self.net.patterns[pid]
            if w.route_ids and pat.route_id not in w.route_ids:
                continue
            poly = self.net.polyline(pat)
            if poly is not None:
                out.append({"pattern_id": pid, "route": self.net.route_name(pat.route_id),
                            "headsign": pat.headsign, "points": poly.latlons()})
        return out
