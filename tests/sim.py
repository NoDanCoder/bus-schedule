"""Drives the whole Service on a virtual clock against the simulated city."""
import asyncio
from pathlib import Path

from busalert.config import Settings
from busalert.model import Watch
from busalert.predict import Predictor
from busalert.service import Service
from busalert.tracker import Tracker


def make_service(data_dir: Path, **over) -> Service:
    cfg = Settings.from_env()
    cfg = Settings(**{**cfg.__dict__, "data_dir": data_dir, "demo": True, "log_positions": "off", **over})
    svc = Service(cfg)
    svc.net = svc._load_network()
    svc.tracker = Tracker(svc.net, svc.model)
    svc.predictor = Predictor(svc.net, svc.tracker, svc.model, svc.cfg.arrival_credit_s)
    from busalert.demo import DemoSource

    svc.demo = DemoSource(speed=1.0)
    return svc


async def run(svc: Service, seconds: int, step: int = 3, on_step=None):
    t0 = svc.demo._t0
    for i in range(0, seconds, step):
        now = t0 + i
        svc.demo.clock = svc.clock = lambda now=now: now
        vehicles, ts = await svc.demo.fetch()
        svc.feed_ts, svc.vehicle_count = ts, len(vehicles)
        await svc._on_vehicles(vehicles)
        if on_step:
            on_step(i, now)
