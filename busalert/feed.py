"""GTFS-Realtime decoding and fetching."""
from __future__ import annotations

import logging
import time

import httpx
from google.transit import gtfs_realtime_pb2 as pb

from .model import Vehicle

log = logging.getLogger(__name__)

_STATUS = {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}


def decode(raw: bytes) -> pb.FeedMessage:
    msg = pb.FeedMessage()
    msg.ParseFromString(raw)
    return msg


def parse_vehicles(msg: pb.FeedMessage) -> tuple[list[Vehicle], float]:
    header_ts = float(msg.header.timestamp) if msg.header.timestamp else time.time()
    out: list[Vehicle] = []
    for ent in msg.entity:
        if not ent.HasField("vehicle"):
            continue
        v = ent.vehicle
        if not v.HasField("position"):
            continue
        lat, lon = v.position.latitude, v.position.longitude
        if abs(lat) < 1e-6 and abs(lon) < 1e-6:
            continue
        vid = (v.vehicle.id if v.HasField("vehicle") else "") or (v.vehicle.label if v.HasField("vehicle") else "") or ent.id
        trip = v.trip if v.HasField("trip") else None
        out.append(
            Vehicle(
                id=vid, lat=lat, lon=lon, ts=float(v.timestamp) if v.timestamp else header_ts,
                trip_id=(trip.trip_id or None) if trip else None,
                route_id=(trip.route_id or None) if trip else None,
                direction_id=trip.direction_id if trip is not None and trip.HasField("direction_id") else None,
                bearing=v.position.bearing if v.position.HasField("bearing") else None,
                speed=v.position.speed if v.position.HasField("speed") else None,
                stop_id=v.stop_id or None,
                status=_STATUS.get(v.current_status) if v.HasField("current_status") else None,
                label=(v.vehicle.label or None) if v.HasField("vehicle") else None,
            )
        )
    return out, header_ts


def parse_trip_updates(msg: pb.FeedMessage) -> dict[str, dict[str, float]]:
    """trip_id -> stop_id -> official predicted arrival (epoch). Only absolute times are used."""
    out: dict[str, dict[str, float]] = {}
    for ent in msg.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        for stu in tu.stop_time_update:
            t = stu.arrival.time or stu.departure.time
            if stu.stop_id and t:
                out.setdefault(tu.trip.trip_id, {})[stu.stop_id] = float(t)
    return out


def parse_service_alerts(msg: pb.FeedMessage) -> list[dict]:
    def text(ts) -> str:
        for tr in ts.translation:
            if tr.language.startswith("es") or not tr.language:
                return tr.text
        return ts.translation[0].text if ts.translation else ""

    out = []
    for ent in msg.entity:
        if not ent.HasField("alert"):
            continue
        a = ent.alert
        out.append(
            {
                "id": ent.id,
                "header": text(a.header_text),
                "description": text(a.description_text),
                "routes": sorted({e.route_id for e in a.informed_entity if e.route_id}),
                "stops": sorted({e.stop_id for e in a.informed_entity if e.stop_id}),
            }
        )
    return out


class HttpFeed:
    """Fetches one GTFS-RT URL, skipping bodies that haven't changed (by ETag or header time)."""

    def __init__(self, client: httpx.AsyncClient, url: str):
        self.client, self.url = client, url
        self._etag: str | None = None
        self.last_header_ts: float = 0.0

    async def fetch(self) -> pb.FeedMessage | None:
        headers = {"If-None-Match": self._etag} if self._etag else {}
        r = await self.client.get(self.url, headers=headers)
        if r.status_code == 304:
            return None
        r.raise_for_status()
        self._etag = r.headers.get("etag")
        msg = decode(r.content)
        if msg.header.timestamp and msg.header.timestamp == self.last_header_ts:
            return None
        self.last_header_ts = float(msg.header.timestamp)
        return msg
