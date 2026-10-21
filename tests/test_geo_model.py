import pytest

from busalert.geo import Polyline, heading_diff
from busalert.speedmodel import BIN_M, SpeedModel, bucket_for


def test_polyline_roundtrip_and_window():
    poly = Polyline([4.60, 4.61, 4.62], [-74.08, -74.08, -74.07])
    lat, lon = poly.point_at(500)
    pr = poly.project(lat, lon)
    assert pr.off < 1 and pr.s == pytest.approx(500, abs=1)
    pr_w = poly.project(lat, lon, s_min=400, s_max=900)
    assert pr_w.s == pytest.approx(500, abs=1)


def test_heading_diff_wraps():
    assert heading_diff(350, 10) == 20


def test_speed_model_learns_slow_segment():
    m = SpeedModel(None, default_speed=5.0)
    t_default, frac = m.travel_time("p", 0, 1000, 1_700_000_000)
    assert t_default == pytest.approx(200, rel=0.01) and frac == 0
    for k in range(6):  # six slow passes (2 m/s) over the first 1000 m
        m.observe("p", 0, 1000, 500, 1_700_000_000)
    t_slow, frac = m.travel_time("p", 0, 1000, 1_700_000_000)
    assert t_slow > 2 * t_default and frac == 1
    # another hour of the week has no data of its own but inherits the pooled knowledge
    t_other_hour, frac_other = m.travel_time("p", 0, 1000, 1_700_000_000 + 6 * 3600)
    assert t_default < t_other_hour <= t_slow and frac_other == 1
    t_unseen_pattern, frac_unseen = m.travel_time("q", 0, 1000, 1_700_000_000)
    assert t_unseen_pattern == pytest.approx(t_default, rel=0.01) and frac_unseen == 0


def test_live_factor_fades_with_distance():
    m = SpeedModel(None, default_speed=5.0)
    near, _ = m.travel_time("p", 0, 500, 0, factor=0.5)
    far, _ = m.travel_time("p", 0, 500, 0, factor=1.0)
    assert near > far
    # the last 500 m of a 20 km trip are barely affected by the live factor
    def far(f):
        return m.travel_time("p", 0, 20_500, 0, factor=f)[0] - m.travel_time("p", 0, 20_000, 0, factor=f)[0]

    assert far(0.5) == pytest.approx(far(1.0), rel=0.01)


def test_bucket_is_bogota_local_time():
    # 2023-11-14 (Tue) 03:00 UTC == 22:00 Mon Bogotá
    assert bucket_for(1_699_930_800) == 22
    assert bucket_for(1_700_000_000 + 5 * 86400) // 24 in (1, 2)
