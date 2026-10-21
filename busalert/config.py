from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    static_url: str
    vehicle_url: str
    tripupdate_url: str
    alerts_url: str
    poll_interval: float  # seconds between vehicle-position polls
    default_speed: float  # m/s effective speed (incl. dwell) when nothing is learned
    arrival_credit_s: float  # dwell at the target stop that the learned speeds over-count
    ntfy_server: str
    log_positions: str  # off | watched | all
    retention_days: float
    demo: bool
    demo_speed: float  # simulation time multiplier (demo only)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("BUSALERT_DATA_DIR", "data")),
            static_url=os.environ.get(
                "BUSALERT_STATIC_URL", "https://gtfs.transmilenio.gov.co/GTFS.zip"
            ),
            vehicle_url=os.environ.get(
                "BUSALERT_VEHICLE_URL", "https://gis.transmilenio.gov.co/gtfs/vehiclepos.pb"
            ),
            tripupdate_url=os.environ.get(
                "BUSALERT_TRIPUPDATE_URL", "https://gis.transmilenio.gov.co/gtfs/tripupdate.pb"
            ),
            alerts_url=os.environ.get(
                "BUSALERT_ALERTS_URL", "https://gis.transmilenio.gov.co/gtfs/serviceAlerts.pb"
            ),
            poll_interval=_f("BUSALERT_POLL_INTERVAL", 3.0),
            default_speed=_f("BUSALERT_DEFAULT_SPEED", 5.0),
            arrival_credit_s=_f("BUSALERT_ARRIVAL_CREDIT", 15.0),
            ntfy_server=os.environ.get("BUSALERT_NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            log_positions=os.environ.get("BUSALERT_LOG_POSITIONS", "watched"),
            retention_days=_f("BUSALERT_RETENTION_DAYS", 7),
            demo=os.environ.get("BUSALERT_DEMO", "") not in ("", "0", "false"),
            demo_speed=_f("BUSALERT_DEMO_SPEED", 1.0),
        )
