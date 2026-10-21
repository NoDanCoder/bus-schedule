from __future__ import annotations

import json
import time
import uuid
from contextlib import closing
from pathlib import Path

from .db import connect
from .model import Watch

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches(id TEXT PRIMARY KEY, client_id TEXT, stop_id TEXT, route_ids TEXT,
  thresholds TEXT, label TEXT, ntfy_topic TEXT, created REAL);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, watch_id TEXT, client_id TEXT,
  vehicle_id TEXT, route TEXT, stop_name TEXT, threshold INT, eta_s REAL, fired_at REAL,
  actual_s REAL, message TEXT);
CREATE INDEX IF NOT EXISTS alerts_client ON alerts(client_id, fired_at);
CREATE TABLE IF NOT EXISTS positions(ts REAL, vehicle_id TEXT, trip_id TEXT, route_id TEXT,
  lat REAL, lon REAL, speed REAL);
CREATE INDEX IF NOT EXISTS positions_ts ON positions(ts);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        with closing(connect(path)) as con:
            con.executescript(SCHEMA)

    # watches
    def add_watch(self, w: Watch) -> Watch:
        w.id = w.id or uuid.uuid4().hex[:12]
        with closing(connect(self.path)) as con:
            con.execute(
                "INSERT INTO watches VALUES (?,?,?,?,?,?,?,?)",
                (w.id, w.client_id, w.stop_id, json.dumps(w.route_ids), json.dumps(w.thresholds),
                 w.label, w.ntfy_topic, time.time()),
            )
            con.commit()
        return w

    def delete_watch(self, watch_id: str, client_id: str) -> bool:
        with closing(connect(self.path)) as con:
            n = con.execute("DELETE FROM watches WHERE id=? AND client_id=?", (watch_id, client_id)).rowcount
            con.commit()
        return n > 0

    def list_watches(self, client_id: str | None = None) -> list[Watch]:
        q, args = "SELECT * FROM watches", ()
        if client_id:
            q, args = q + " WHERE client_id=?", (client_id,)
        with closing(connect(self.path)) as con:
            rows = con.execute(q + " ORDER BY created", args).fetchall()
        return [
            Watch(r["id"], r["client_id"], r["stop_id"], json.loads(r["route_ids"]),
                  json.loads(r["thresholds"]), r["label"] or "", r["ntfy_topic"] or "")
            for r in rows
        ]

    # alerts
    def log_alert(self, watch: Watch, vehicle_id: str, route: str, stop_name: str, threshold: int,
                  eta_s: float, message: str, fired_at: float) -> int:
        with closing(connect(self.path)) as con:
            cur = con.execute(
                "INSERT INTO alerts(watch_id, client_id, vehicle_id, route, stop_name, threshold, eta_s,"
                " fired_at, message) VALUES (?,?,?,?,?,?,?,?,?)",
                (watch.id, watch.client_id, vehicle_id, route, stop_name, threshold, eta_s, fired_at, message),
            )
            con.commit()
            return int(cur.lastrowid)

    def set_actual(self, alert_id: int, actual_s: float) -> None:
        with closing(connect(self.path)) as con:
            con.execute("UPDATE alerts SET actual_s=? WHERE id=?", (actual_s, alert_id))
            con.commit()

    def recent_alerts(self, client_id: str, limit: int = 30) -> list[dict]:
        with closing(connect(self.path)) as con:
            rows = con.execute(
                "SELECT * FROM alerts WHERE client_id=? ORDER BY fired_at DESC LIMIT ?", (client_id, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def accuracy(self) -> list[dict]:
        """How far off was each alert threshold? error = predicted - actual seconds to arrival."""
        with closing(connect(self.path)) as con:
            rows = con.execute(
                "SELECT threshold, COUNT(*) n, AVG(eta_s - actual_s) bias, AVG(ABS(eta_s - actual_s)) mae "
                "FROM alerts WHERE actual_s IS NOT NULL GROUP BY threshold ORDER BY threshold DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # raw positions
    def log_positions(self, rows: list[tuple]) -> None:
        if not rows:
            return
        with closing(connect(self.path)) as con:
            con.executemany("INSERT INTO positions VALUES (?,?,?,?,?,?,?)", rows)
            con.commit()

    def prune(self, older_than: float) -> None:
        with closing(connect(self.path)) as con:
            con.execute("DELETE FROM positions WHERE ts < ?", (older_than,))
            con.commit()
