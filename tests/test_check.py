import asyncio
import http.server
import threading
import time

from google.transit import gtfs_realtime_pb2 as pb

from busalert.check import run_check
from busalert.config import Settings
from busalert.demo import DemoSource, build_demo_gtfs


def _feed_bytes() -> tuple[bytes, int]:
    vehicles, _ = asyncio.run(DemoSource().fetch())
    known = sum(not v.trip_id.startswith("UNKNOWN") for v in vehicles)
    m = pb.FeedMessage()
    m.header.gtfs_realtime_version = "2.0"
    m.header.timestamp = int(time.time())
    for v in vehicles:
        e = m.entity.add()
        e.id = v.id
        e.vehicle.vehicle.id = v.id
        e.vehicle.trip.trip_id = v.trip_id
        e.vehicle.trip.route_id = v.route_id
        e.vehicle.position.latitude, e.vehicle.position.longitude = v.lat, v.lon
        e.vehicle.timestamp = int(time.time())
    return m.SerializeToString(), round(100 * known / len(vehicles))


def test_check_reports_on_a_local_feed(tmp_path):
    body, known_pct = _feed_bytes()
    build_demo_gtfs(tmp_path / "GTFS.zip")
    zbytes = (tmp_path / "GTFS.zip").read_bytes()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path in ("/v.pb", "/g.zip")
            self.send_response(200 if ok else 404)
            self.end_headers()
            if ok:
                self.wfile.write(body if self.path == "/v.pb" else zbytes)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    cfg = Settings.from_env()
    cfg = Settings(**{**cfg.__dict__, "data_dir": tmp_path / "d", "vehicle_url": base + "/v.pb",
                      "static_url": base + "/g.zip", "tripupdate_url": base + "/none",
                      "alerts_url": base + "/none"})
    lines: list[str] = []
    ok = run_check(cfg, samples=2, gap_s=0.1, out=lines.append)
    srv.shutdown()
    text = "\n".join(lines)
    assert ok, text
    assert "20 vehicles" in text
    assert "live route_id found in static: 100%" in text
    assert f"live trip_id found in static:  {known_pct}%" in text  # some demo trips are deliberately unknown
    assert "optional" in text
