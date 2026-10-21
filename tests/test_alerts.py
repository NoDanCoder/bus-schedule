from busalert.alerts import AlertEngine
from busalert.model import Eta, Watch


def eta(vid, secs, status="approaching"):
    return Eta(vid, "R", "K10", "Tunal", "p", status, secs, secs * 5, 3, "default", 0, 0, None, 0)


W = Watch("w1", "c", "s", [], [10, 5, 2, 1])


def run(engine, series, vid="bus1"):
    fired = []
    for t, secs in series:
        ev, _ = engine.process(W, [eta(vid, secs)], "Stop", t)
        fired += [e.threshold for e in ev]
    return fired


def test_each_threshold_fires_once_in_order():
    series = [(i * 10, 11 * 60 - i * 10) for i in range(0, 67)]
    assert run(AlertEngine(), series) == [10, 5, 2, 1]


def test_first_sighting_close_in_announces_only_tightest_crossed():
    assert run(AlertEngine(), [(0, 3 * 60), (10, 170)]) == [5]


def test_skipped_ticks_announce_tightest_only():
    assert run(AlertEngine(), [(0, 11 * 60), (60, 90)]) == [2]


def test_eta_noise_does_not_refire():
    series = [(0, 590), (10, 610), (20, 595), (30, 615), (40, 580)]
    assert run(AlertEngine(), series) == [10]


def test_rearms_when_eta_jumps_back_up():
    fired = run(AlertEngine(), [(0, 590), (10, 1500), (20, 590)])
    assert fired == [10, 10]


def test_arrival_reports_actual_time_once_for_accuracy():
    eng = AlertEngine()
    ev, _ = eng.process(W, [eta("b", 590)], "Stop", 1000)
    eng.attach_alert_id("w1", "b", ev[0].threshold, 42)
    near = eta("b", 0)
    near.distance_m = 10
    _, arrivals = eng.process(W, [near], "Stop", 1600)
    assert arrivals == [(42, 600)]
    _, arrivals = eng.process(W, [near, ], "Stop", 1605)  # dwelling at the stop: no duplicate
    _, passes = eng.process(W, [eta("b", 0, "passed")], "Stop", 1640)
    assert arrivals == [] and passes == []
    # a passed bus does not re-trigger
    ev, _ = eng.process(W, [eta("b", 0, "passed")], "Stop", 1610)
    assert ev == []


def test_buses_are_independent():
    eng = AlertEngine()
    ev, _ = eng.process(W, [eta("a", 590), eta("b", 1200)], "Stop", 0)
    assert [(e.eta.vehicle_id, e.threshold) for e in ev] == [("a", 10)]
