"""Local planar geometry. Everything is projected to metres around Bogotá, which is
accurate to well under 0.1% at city scale and lets us use plain numpy."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

EARTH_R = 6_371_008.8


class Projector:
    def __init__(self, lat0: float = 4.65, lon0: float = -74.09):
        self.lat0, self.lon0 = lat0, lon0
        self._ky = EARTH_R * math.pi / 180
        self._kx = self._ky * math.cos(math.radians(lat0))

    def to_xy(self, lat, lon):
        return (np.asarray(lon, dtype=float) - self.lon0) * self._kx, (
            np.asarray(lat, dtype=float) - self.lat0
        ) * self._ky

    def to_latlon(self, x, y):
        return self.lat0 + np.asarray(y) / self._ky, self.lon0 + np.asarray(x) / self._kx


PROJ = Projector()


def heading_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    x1, y1 = PROJ.to_xy(lat1, lon1)
    x2, y2 = PROJ.to_xy(lat2, lon2)
    return float(math.hypot(x2 - x1, y2 - y1))


@dataclass(slots=True)
class Projection:
    s: float  # metres along the polyline
    off: float  # metres from the polyline
    heading: float  # degrees clockwise from north of the matched segment


class Polyline:
    def __init__(self, lats, lons):
        x, y = PROJ.to_xy(lats, lons)
        if len(x) < 2:
            raise ValueError("polyline needs at least 2 points")
        keep = np.r_[True, np.hypot(np.diff(x), np.diff(y)) > 0.5]
        x, y = x[keep], y[keep]
        if len(x) < 2:
            raise ValueError("degenerate polyline")
        self.x, self.y = x, y
        self._dx, self._dy = np.diff(x), np.diff(y)
        self._len = np.hypot(self._dx, self._dy)
        self._len2 = self._len**2
        self.cum = np.r_[0.0, np.cumsum(self._len)]
        self.length = float(self.cum[-1])

    def project(self, lat: float, lon: float, s_min: float | None = None,
                s_max: float | None = None) -> Projection:
        px, py = PROJ.to_xy(lat, lon)
        n = len(self._len)
        lo, hi = 0, n
        if s_min is not None:
            lo = max(0, int(np.searchsorted(self.cum, s_min, "right")) - 1)
        if s_max is not None:
            hi = min(n, int(np.searchsorted(self.cum, s_max, "left")))
        lo = min(lo, n - 1)
        if hi <= lo:
            hi = lo + 1
        ax, ay = self.x[lo:hi], self.y[lo:hi]
        dx, dy = self._dx[lo:hi], self._dy[lo:hi]
        t = np.clip(((px - ax) * dx + (py - ay) * dy) / self._len2[lo:hi], 0.0, 1.0)
        d2 = (px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2
        k = int(np.argmin(d2))
        i = lo + k
        s = float(self.cum[i] + t[k] * self._len[i])
        heading = (math.degrees(math.atan2(dx[k], dy[k])) + 360.0) % 360.0
        return Projection(s, math.sqrt(float(d2[k])), heading)

    def point_at(self, s: float) -> tuple[float, float]:
        s = min(max(s, 0.0), self.length)
        lat, lon = PROJ.to_latlon(np.interp(s, self.cum, self.x), np.interp(s, self.cum, self.y))
        return float(lat), float(lon)

    def latlons(self, step_m: float = 0.0) -> list[list[float]]:
        lat, lon = PROJ.to_latlon(self.x, self.y)
        return [[round(float(a), 6), round(float(b), 6)] for a, b in zip(lat, lon)]
