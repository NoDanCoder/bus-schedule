"""End-to-end on the simulated city: tracking, ETA, alerts, learning, persistence."""
import asyncio
import sqlite3

import pytest

from busalert.model import Vehicle, Watch
from busalert.predict import Predictor
from busalert.speedmodel import SpeedModel
from busalert.tracker import Tracker

from sim import make_service, run


def test_eta_for_known_position(net):
    pat = net.patterns["K10-0"]
    poly, offs = net.polyline(pat), net.stop_offsets(pat)
    model = SpeedModel(None, default_speed=5.0)
    tracker = Tracker(net, model)
    # bus 2 km before stop #8
    lat, lon = poly.point_at(float(offs[8]) - 2000)
    tracker.update_all([Vehicle("b1", lat, lon, ts=1000.0, trip_id="K10-0-T0", route_id="K10")], now=1000.0)
    etas = Predictor(net, tracker, model, arrival_credit_s=0).etas_for(pat.stop_ids[8], None, now=1000.0)
    e = next(x for x in etas if x.vehicle_id == "b1")
    assert e.status == "approaching" and e.eta_s == pytest.approx(400, rel=0.05)
    assert e.distance_m == pytest.approx(2000, abs=15) and e.stops_away == 4 and e.basis == "default"
    # a stop well behind the bus, and a stop on the opposite direction, don't list it
    behind = Predictor(net, tracker, model).etas_for(pat.stop_ids[3], None, 1000.0)
    assert all(x.vehicle_id != "b1" for x in behind)
    other = Predictor(net, tracker, model).etas_for("K10-1-3", None, 1000.0)
    assert all(x.vehicle_id != "b1" for x in other)


def test_unknown_trip_is_matched_by_route_and_heading(net):
    pat = net.patterns["K10-1"]  # southbound lane, ~24 m from the northbound one
    poly = net.polyline(pat)
    lat, lon = poly.point_at(3000)
    model = SpeedModel(None)
    tracker = Tracker(net, model)
    tracker.update_all([Vehicle("x", lat, lon, 10.0, trip_id="NOT-IN-STATIC", route_id="K10",
                                bearing=180.0)], now=10.0)
    assert tracker.states["x"].pattern_id == "K10-1"
    tracker.update_all([Vehicle("n", lat, lon, 10.0, trip_id="NOT-IN-STATIC", route_id="K10",
                                bearing=0.0)], now=10.0)
    assert tracker.states["n"].pattern_id == "K10-0"
    # live route ids may be short names, and bus without bearing/trip still gets placed
    tracker.update_all([Vehicle("y", lat, lon, 10.0, trip_id=None, route_id="k10", direction_id=1)], now=10.0)
    assert tracker.states["y"].pattern_id == "K10-1"


def test_vehicle_far_from_route_is_not_tracked(net):
    tracker = Tracker(net, SpeedModel(None))
    tracker.update_all([Vehicle("far", 4.9, -74.3, 1.0, trip_id="K10-0-T0")], now=1.0)
    assert "far" not in tracker.states


def test_full_simulation_alerts_and_accuracy(tmp_path):
    async def go():
        svc = make_service(tmp_path)
        svc.store.add_watch(Watch("", "client-0001", "K10-0-14", [], [10, 5, 2, 1], "t"))
        q = svc.hub.subscribe("client-0001")
        await run(svc, 3 * 3600)
        return svc, q

    svc, q = asyncio.run(go())
    con = sqlite3.connect(tmp_path / "app.db")
    rows = con.execute("SELECT vehicle_id, threshold, eta_s, actual_s, fired_at FROM alerts").fetchall()
    assert len(rows) > 40
    assert len({(r[0], r[1], int(r[4] // 1200)) for r in rows}) == len(rows)  # never twice per pass
    assert {r[1] for r in rows} == {10, 5, 2, 1}
    acc = {a["threshold"]: a for a in svc.store.accuracy()}
    # near-term predictions must be tight, long-range ones within ~15% once learning has run
    assert acc[1]["mae"] < 30 and acc[2]["mae"] < 30
    assert acc[5]["mae"] < 60 and acc[10]["mae"] < 100
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert any(e == "alert" for e, _ in events) and any(e == "tick" for e, _ in events)


def test_learning_is_persisted_and_improves_cold_start(tmp_path):
    async def go():
        svc = make_service(tmp_path)
        svc.store.add_watch(Watch("", "client-0001", "K10-0-14", [], [10, 5, 2, 1], "t"))
        await run(svc, 2 * 3600)
        svc.model.flush()

    asyncio.run(go())
    reloaded = SpeedModel(tmp_path / "history.db", default_speed=5.0)
    slow, _ = reloaded.bin_speed("K10-0", 0, int(3500 // 250))   # congested stretch
    fast, _ = reloaded.bin_speed("K10-0", 0, int(7000 // 250))
    assert slow < fast * 0.8
