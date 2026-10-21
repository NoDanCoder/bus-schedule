import asyncio

import httpx
from google.transit import gtfs_realtime_pb2 as pb

from busalert.feed import HttpFeed, decode, parse_service_alerts, parse_trip_updates, parse_vehicles


def make_feed(ts=1_700_000_000):
    m = pb.FeedMessage()
    m.header.gtfs_realtime_version = "2.0"
    m.header.timestamp = ts
    e = m.entity.add()
    e.id = "1"
    v = e.vehicle
    v.trip.trip_id, v.trip.route_id, v.trip.direction_id = "T1", "K10", 1
    v.vehicle.id, v.vehicle.label = "BUS-9", "Z70-1234"
    v.position.latitude, v.position.longitude, v.position.bearing, v.position.speed = 4.65, -74.1, 90, 6.5
    v.timestamp = ts - 4
    v.current_status = pb.VehiclePosition.STOPPED_AT
    z = m.entity.add()
    z.id = "2"
    z.vehicle.position.latitude = 0.0  # bogus fix, must be dropped
    z.vehicle.position.longitude = 0.0
    tu = m.entity.add()
    tu.id = "3"
    tu.trip_update.trip.trip_id = "T1"
    s = tu.trip_update.stop_time_update.add()
    s.stop_id = "S1"
    s.arrival.time = ts + 120
    a = m.entity.add()
    a.id = "4"
    tr = a.alert.header_text.translation.add()
    tr.text, tr.language = "Desvío", "es"
    a.alert.informed_entity.add().route_id = "K10"
    return m


def test_parse_all_feeds():
    msg = decode(make_feed().SerializeToString())
    vehicles, hdr = parse_vehicles(msg)
    assert hdr == 1_700_000_000 and len(vehicles) == 1
    v = vehicles[0]
    assert (v.id, v.trip_id, v.route_id, v.direction_id, v.status) == ("BUS-9", "T1", "K10", 1, "STOPPED_AT")
    assert v.ts == 1_700_000_000 - 4 and v.bearing == 90 and v.speed == 6.5
    assert parse_trip_updates(msg) == {"T1": {"S1": 1_700_000_120.0}}
    assert parse_service_alerts(msg)[0]["header"] == "Desvío"


def test_http_feed_skips_unchanged_bodies():
    bodies = [make_feed(100), make_feed(100), make_feed(103)]
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, content=bodies[min(len(calls) - 1, 2)].SerializeToString())

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            feed = HttpFeed(c, "http://x/vehiclepos.pb")
            return [await feed.fetch() for _ in range(3)]

    got = asyncio.run(go())
    assert got[0] is not None and got[1] is None and got[2] is not None
