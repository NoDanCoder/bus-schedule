from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Vehicle:
    id: str
    lat: float
    lon: float
    ts: float  # epoch seconds of the position fix
    trip_id: str | None = None
    route_id: str | None = None
    direction_id: int | None = None
    bearing: float | None = None
    speed: float | None = None  # m/s
    stop_id: str | None = None
    status: str | None = None  # INCOMING_AT | STOPPED_AT | IN_TRANSIT_TO
    label: str | None = None


@dataclass(slots=True)
class Watch:
    id: str
    client_id: str
    stop_id: str
    route_ids: list[str]  # empty = every route serving the stop
    thresholds: list[int]  # minutes before arrival
    label: str = ""
    ntfy_topic: str = ""


@dataclass(slots=True)
class Eta:
    vehicle_id: str
    route_id: str
    route_name: str
    headsign: str
    pattern_id: str
    status: str  # approaching | passed
    eta_s: float
    distance_m: float
    stops_away: int
    basis: str  # learned | mixed | default
    lat: float
    lon: float
    bearing: float | None
    vehicle_ts: float
    official_eta_s: float | None = None
