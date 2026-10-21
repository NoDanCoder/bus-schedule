"""Learned effective bus speed per route-pattern segment and hour-of-week bucket.

"Effective" speed is distance / elapsed time as the bus actually progresses, so it already
includes station dwell and signal waits; no separate dwell model is needed. Observations come
from consecutive position fixes of every tracked vehicle, so the model improves the longer the
service runs. Each hour-of-week bucket is a prior-weighted estimate whose prior is the pooled
all-hours speed for that segment (itself weighted against the default speed), so a bucket that
has seen little traffic still starts from what the segment is known to do.
"""
from __future__ import annotations

import math
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import connect

BIN_M = 250.0
PRIOR_M = 500.0  # prior weight, in metres at the default speed
LEARNED_MIN_M = 250.0  # evidence needed before a bin counts as "learned"
DECAY = 0.995
POOL = -1  # pseudo-bucket holding all-hours statistics
MIN_V, MAX_V = 0.8, 14.0
BOGOTA = timezone(timedelta(hours=-5))  # no DST


def bucket_for(ts: float) -> int:
    dt = datetime.fromtimestamp(ts, BOGOTA)
    day = 0 if dt.weekday() < 5 else (1 if dt.weekday() == 5 else 2)
    return day * 24 + dt.hour


class SpeedModel:
    def __init__(self, db_path: Path | None, default_speed: float = 5.0):
        self.default = default_speed
        self._stats: dict[tuple[str, int, int], list[float]] = {}  # (pattern, bucket, bin) -> [m, s]
        self._dirty: set[tuple[str, int, int]] = set()
        self._lock = threading.Lock()
        self.db_path = db_path
        if db_path:
            con = connect(db_path)
            con.execute(
                "CREATE TABLE IF NOT EXISTS speed_bins(pattern_id TEXT, bucket INT, bin INT, "
                "dist REAL, time REAL, PRIMARY KEY(pattern_id, bucket, bin))"
            )
            for r in con.execute("SELECT * FROM speed_bins"):
                self._stats[(r["pattern_id"], r["bucket"], r["bin"])] = [r["dist"], r["time"]]
            con.close()

    def observe(self, pattern_id: str, s0: float, s1: float, dt: float, when: float) -> None:
        ds = s1 - s0
        if ds <= 0 or dt <= 0:
            return
        bucket = bucket_for(when)
        b0, b1 = int(s0 // BIN_M), int(s1 // BIN_M)
        with self._lock:
            for b in range(b0, b1 + 1):
                lo, hi = max(s0, b * BIN_M), min(s1, (b + 1) * BIN_M)
                if hi <= lo:
                    continue
                for bk in (bucket, POOL):
                    key = (pattern_id, bk, b)
                    st = self._stats.setdefault(key, [0.0, 0.0])
                    st[0] = st[0] * DECAY + (hi - lo)
                    st[1] = st[1] * DECAY + dt * (hi - lo) / ds
                    self._dirty.add(key)

    def _shrunk(self, key: tuple[str, int, int], prior_v: float) -> tuple[float, float]:
        d, t = self._stats.get(key, (0.0, 0.0))
        return (d + PRIOR_M) / (t + PRIOR_M / prior_v), d

    def bin_speed(self, pattern_id: str, bucket: int, b: int) -> tuple[float, bool]:
        pooled, d_pool = self._shrunk((pattern_id, POOL, b), self.default)
        v, d = self._shrunk((pattern_id, bucket, b), pooled)
        return min(max(v, MIN_V), MAX_V), max(d, d_pool) >= LEARNED_MIN_M

    def travel_time(self, pattern_id: str, s0: float, s1: float, when: float,
                    factor: float = 1.0, tau: float = 2000.0) -> tuple[float, float]:
        """Seconds to go from s0 to s1, and the fraction of that distance backed by data.

        `factor` rescales speeds (live traffic correction); its influence fades with distance
        ahead of the vehicle (length scale `tau`) because current conditions say little about
        far-away segments.
        """
        if s1 <= s0:
            return 0.0, 1.0
        bucket = bucket_for(when)
        total = learned = 0.0
        for b in range(int(s0 // BIN_M), int(s1 // BIN_M) + 1):
            lo, hi = max(s0, b * BIN_M), min(s1, (b + 1) * BIN_M)
            if hi <= lo:
                continue
            v, ok = self.bin_speed(pattern_id, bucket, b)
            v *= 1.0 + (factor - 1.0) * math.exp(-((lo + hi) / 2 - s0) / tau)
            total += (hi - lo) / max(v, MIN_V * 0.5)
            if ok:
                learned += hi - lo
        return total, learned / (s1 - s0)

    def flush(self) -> int:
        if not self.db_path:
            return 0
        with self._lock:
            keys, self._dirty = list(self._dirty), set()
            rows = [(k[0], k[1], k[2], *self._stats[k]) for k in keys]
        if rows:
            con = connect(self.db_path)
            con.executemany("INSERT OR REPLACE INTO speed_bins VALUES (?,?,?,?,?)", rows)
            con.commit()
            con.close()
        return len(rows)
