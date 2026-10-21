from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .model import Watch
from .service import Service

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DEFAULT_THRESHOLDS = [10, 5, 2, 1]
CLIENT_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{0,64}$")


class WatchIn(BaseModel):
    stop_id: str
    route_ids: list[str] = Field(default_factory=list)
    thresholds: list[int] = Field(default_factory=lambda: list(DEFAULT_THRESHOLDS))
    label: str = Field(default="", max_length=80)
    ntfy_topic: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    svc = Service(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        await svc.start()
        yield
        await svc.stop()

    app = FastAPI(title="busalert", lifespan=lifespan)
    app.state.service = svc

    def client(x_client_id: str | None) -> str:
        if not x_client_id or not CLIENT_RE.match(x_client_id):
            raise HTTPException(400, "missing or invalid X-Client-Id")
        return x_client_id

    def net():
        if svc.net is None:
            raise HTTPException(503, "static GTFS still loading")
        return svc.net

    def stop_json(s, routes=True, dist=None):
        d = {"id": s.id, "code": s.code, "name": s.name, "lat": s.lat, "lon": s.lon}
        if routes:
            n = net()
            d["routes"] = [{"id": r, "name": n.route_name(r)} for r in n.routes_at_stop(s.id)]
        if dist is not None:
            d["distance_m"] = round(dist)
        return d

    @app.get("/api/health")
    def health():
        return svc.health()

    @app.get("/api/stops")
    def stops(q: str = "", lat: float | None = None, lon: float | None = None):
        n = net()
        if lat is not None and lon is not None:
            return [stop_json(s, dist=d) for s, d in n.nearest_stops(lat, lon, 10)]
        return [stop_json(s) for s in n.search_stops(q)]

    @app.get("/api/watches")
    def list_watches(x_client_id: str | None = Header(None)):
        cid = client(x_client_id)
        n = net()
        return [
            {"id": w.id, "stop": stop_json(n.stops[w.stop_id], routes=False), "route_ids": w.route_ids,
             "thresholds": w.thresholds, "label": w.label, "ntfy_topic": w.ntfy_topic,
             "geometry": svc.geometry(w)}
            for w in svc.store.list_watches(cid) if w.stop_id in n.stops
        ]

    @app.post("/api/watches", status_code=201)
    def add_watch(body: WatchIn, x_client_id: str | None = Header(None)):
        cid = client(x_client_id)
        n = net()
        if body.stop_id not in n.stops or body.stop_id not in n.stop_patterns:
            raise HTTPException(404, "unknown stop")
        served = set(n.routes_at_stop(body.stop_id))
        if any(r not in served for r in body.route_ids):
            raise HTTPException(422, "route does not serve this stop")
        thresholds = sorted({t for t in body.thresholds if 0 < t <= 60}, reverse=True)
        if not thresholds:
            raise HTTPException(422, "thresholds must be minutes between 1 and 60")
        if not TOPIC_RE.match(body.ntfy_topic):
            raise HTTPException(422, "invalid ntfy topic")
        if len(svc.store.list_watches(cid)) >= 10:
            raise HTTPException(429, "watch limit reached")
        w = svc.store.add_watch(Watch("", cid, body.stop_id, body.route_ids, thresholds,
                                      body.label or n.stops[body.stop_id].name, body.ntfy_topic))
        return {"id": w.id}

    @app.delete("/api/watches/{watch_id}", status_code=204)
    def delete_watch(watch_id: str, x_client_id: str | None = Header(None)):
        if not svc.store.delete_watch(watch_id, client(x_client_id)):
            raise HTTPException(404)
        svc.engine.forget_watch(watch_id)

    @app.get("/api/alerts")
    def alerts(x_client_id: str | None = Header(None)):
        return svc.store.recent_alerts(client(x_client_id))

    @app.get("/api/service-alerts")
    def service_alerts():
        return svc.service_alerts

    @app.get("/api/accuracy")
    def accuracy():
        return svc.store.accuracy()

    @app.get("/api/stream")
    async def stream(client_id: str = Query(..., alias="client_id")):
        cid = client(client_id)

        async def gen():
            q = svc.hub.subscribe(cid)
            try:
                yield "retry: 2000\n\n"
                if cid in svc._last_tick:
                    yield f"event: tick\ndata: {json.dumps(svc._last_tick[cid])}\n\n"
                while True:
                    try:
                        event, data = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                svc.hub.unsubscribe(cid, q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
