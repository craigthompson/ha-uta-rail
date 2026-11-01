"""Parse UTA's GTFS schedule and GTFS-realtime feeds. Standard library only.

UTA's realtime feed carries trip IDs but no route IDs, so rail vehicles are
identified by matching trip IDs against the static schedule.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import struct
from zoneinfo import ZoneInfo
import zipfile

# GTFS route_short_name: the label shown on the map marker.
ROUTES = {"701": "Blue", "703": "Red", "704": "Green", "750": "FR"}
TIMEZONE = "America/Denver"
MAX_POSITION_AGE = 15 * 60
LINE_TOLERANCE_DEGREES = 0.0002
_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
_STOPPED_AT = 1


@dataclass
class Static:
    """Schedule data for the tracked rail lines."""

    routes: dict
    trips: dict
    lines: list


def _rows(archive: zipfile.ZipFile, name: str):
    with archive.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def _seconds(value: str) -> int | None:
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])


def _station_name(value: str) -> str:
    """Normalize names, since one station can be both "River Trail" and "RIVER TRAIL STATION"."""
    name = value.strip()
    if name.upper().endswith(" STATION"):
        name = name[: -len(" STATION")].rstrip()
    return name.title() if name.isupper() else name


def _simplify(points: list, tolerance: float) -> list:
    """Reduce a polyline with Douglas-Peucker."""
    if len(points) < 3:
        return points
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        ay, ax = points[start]
        by, bx = points[end]
        dx, dy = bx - ax, by - ay
        length = (dx * dx + dy * dy) ** 0.5 or 1e-12
        worst, worst_index = 0.0, None
        for i in range(start + 1, end):
            y, x = points[i]
            distance = abs(dy * (x - ax) - dx * (y - ay)) / length
            if distance > worst:
                worst, worst_index = distance, i
        if worst_index is not None and worst > tolerance:
            keep.add(worst_index)
            stack += [(start, worst_index), (worst_index, end)]
    return [points[i] for i in sorted(keep)]


def parse_static(data: bytes, route_labels: dict | None = None) -> Static:
    """Read trips, stops and track shapes for the tracked routes from a GTFS zip."""
    labels = ROUTES if route_labels is None else route_labels
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        routes = {}
        for row in _rows(archive, "routes.txt"):
            label = labels.get(row["route_short_name"].strip())
            if label:
                routes[row["route_id"]] = {
                    "label": label,
                    "name": row["route_long_name"].strip() or label,
                    "color": f"#{row['route_color'].strip() or '555555'}",
                    "text_color": f"#{row['route_text_color'].strip() or 'FFFFFF'}",
                }
        if not routes:
            raise ValueError("None of the tracked routes are in the GTFS feed")

        trips = {
            t["trip_id"]: {"route_id": t["route_id"], "shape_id": t["shape_id"], "stops": []}
            for t in _rows(archive, "trips.txt")
            if t["route_id"] in routes
        }
        if not trips:
            raise ValueError("The tracked routes have no trips")

        stop_ids = set()
        for row in _rows(archive, "stop_times.txt"):
            trip = trips.get(row["trip_id"])
            if trip is None:
                continue
            # Relief points are crew-change locations, not passenger stops.
            if row.get("pickup_type") == "1" and row.get("drop_off_type") == "1":
                continue
            stop_ids.add(row["stop_id"])
            trip["stops"].append(
                {
                    "sequence": int(row["stop_sequence"]),
                    "stop_id": row["stop_id"],
                    "arrival": _seconds(row["arrival_time"]),
                    "departure": _seconds(row["departure_time"]),
                }
            )

        stops = {}
        for row in _rows(archive, "stops.txt"):
            if row["stop_id"] in stop_ids and "RELIEF" not in row["stop_name"].upper():
                stops[row["stop_id"]] = {
                    "name": _station_name(row["stop_name"]),
                    "lat": round(float(row["stop_lat"]), 5),
                    "lon": round(float(row["stop_lon"]), 5),
                }
        for trip in trips.values():
            trip["stops"] = sorted(
                (s for s in trip["stops"] if s["stop_id"] in stops),
                key=lambda s: s["sequence"],
            )
            for stop in trip["stops"]:
                stop.update(stops[stop["stop_id"]])

        shape_routes = {t["shape_id"]: t["route_id"] for t in trips.values()}
        shapes: dict[str, list] = {shape_id: [] for shape_id in shape_routes}
        for row in _rows(archive, "shapes.txt"):
            if row["shape_id"] in shapes:
                shapes[row["shape_id"]].append(
                    (int(row["shape_pt_sequence"]), float(row["shape_pt_lat"]), float(row["shape_pt_lon"]))
                )

    lines = []
    for route_id, route in routes.items():
        candidates = [points for shape_id, points in shapes.items() if shape_routes[shape_id] == route_id]
        longest = max(candidates, key=len, default=[])
        points = [[lat, lon] for _seq, lat, lon in sorted(longest)]
        # Each platform has its own stop ID, so one station can appear twice.
        stations = {
            stop["name"]: {"name": stop["name"], "lat": stop["lat"], "lon": stop["lon"]}
            for trip in trips.values()
            if trip["route_id"] == route_id
            for stop in trip["stops"]
        }
        lines.append(
            {
                "name": route["name"],
                "color": route["color"],
                "line": [[round(a, 5), round(b, 5)] for a, b in _simplify(points, LINE_TOLERANCE_DEGREES)],
                "stations": list(stations.values()),
            }
        )
    return Static(routes=routes, trips=trips, lines=lines)


def _varint(data: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, i


def _fields(data: bytes) -> dict[int, list]:
    """Decode one protobuf message into {field number: [raw values]}."""
    out: dict[int, list] = {}
    i = 0
    while i < len(data):
        key, i = _varint(data, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(data, i)
        elif wire == 1:
            value, i = data[i : i + 8], i + 8
        elif wire == 2:
            size, i = _varint(data, i)
            value, i = data[i : i + size], i + size
        elif wire == 5:
            value, i = data[i : i + 4], i + 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire}")
        out.setdefault(number, []).append(value)
    return out


def _first(fields: dict, number: int, default=None):
    values = fields.get(number)
    return values[0] if values else default


def _float(fields: dict, number: int) -> float | None:
    value = _first(fields, number)
    return struct.unpack("<f", value)[0] if value is not None else None


def _sub(fields: dict, number: int) -> dict:
    value = _first(fields, number)
    return _fields(value) if value is not None else {}


def parse_vehicles(data: bytes) -> dict[str, dict]:
    """Return the newest position per trip ID from a VehiclePositions feed."""
    vehicles: dict[str, dict] = {}
    for raw in _fields(data).get(2, []):
        vehicle = _sub(_fields(raw), 4)
        trip_id = _first(_sub(vehicle, 1), 1)
        position = _sub(vehicle, 2)
        lat, lon = _float(position, 1), _float(position, 2)
        if trip_id is None or lat is None or lon is None:
            continue
        trip_id = trip_id.decode()
        timestamp = _first(vehicle, 5, 0)
        if trip_id in vehicles and vehicles[trip_id]["timestamp"] >= timestamp:
            continue
        vehicles[trip_id] = {
            "lat": lat,
            "lon": lon,
            "bearing": _float(position, 3),
            "speed": _float(position, 5),
            "timestamp": timestamp,
            "sequence": _first(vehicle, 3),
            "status": _first(vehicle, 4),
        }
    return vehicles


def parse_trip_updates(data: bytes) -> dict[str, dict[int, dict]]:
    """Return {trip ID: {stop sequence: {arrival, departure}}} from a TripUpdates feed."""
    updates: dict[str, dict[int, dict]] = {}
    for raw in _fields(data).get(2, []):
        update = _sub(_fields(raw), 3)
        trip_id = _first(_sub(update, 1), 1)
        if trip_id is None:
            continue
        stops = {}
        for raw_stop in update.get(2, []):
            stop = _fields(raw_stop)
            sequence = _first(stop, 1)
            if sequence is not None:
                stops[sequence] = {
                    "arrival": _first(_sub(stop, 2), 2),
                    "departure": _first(_sub(stop, 3), 2),
                }
        updates[trip_id.decode()] = stops
    return updates


def _service_midnight(anchor: float, tz: ZoneInfo) -> datetime:
    """Return GTFS "noon minus 12 hours" for the anchor's local date, correct across DST."""
    local = datetime.fromtimestamp(anchor, tz)
    noon = datetime(local.year, local.month, local.day, 12, tzinfo=tz)
    return noon.astimezone(timezone.utc) - timedelta(hours=12)


def _iso(epoch: float | None, tz: ZoneInfo) -> str | None:
    return datetime.fromtimestamp(epoch, tz).isoformat() if epoch else None


def _next_sequence_by_position(stops: list, lat: float, lon: float) -> int:
    """Find the next stop from where the train is, since many trips have no predictions."""
    best, best_distance = stops[-1]["sequence"], float("inf")
    for start, end in zip(stops, stops[1:]):
        dx, dy = end["lon"] - start["lon"], end["lat"] - start["lat"]
        span = dx * dx + dy * dy or 1e-12
        along = ((lon - start["lon"]) * dx + (lat - start["lat"]) * dy) / span
        along = max(0.0, min(1.0, along))
        distance = (lon - start["lon"] - along * dx) ** 2 + (lat - start["lat"] - along * dy) ** 2
        if distance < best_distance:
            best, best_distance = end["sequence"], distance
    return best


def build_trains(static: Static, vehicles: dict, updates: dict, now: float) -> list[dict]:
    """Merge schedule and realtime data into the train shape the map card uses."""
    tz = ZoneInfo(TIMEZONE)
    trains = []
    for trip_id, vehicle in vehicles.items():
        trip = static.trips.get(trip_id)
        if trip is None or not trip["stops"]:
            continue
        if not vehicle["timestamp"] or now - vehicle["timestamp"] > MAX_POSITION_AGE:
            continue

        route = static.routes[trip["route_id"]]
        realtime = updates.get(trip_id, {})
        future = [t for u in realtime.values() for t in u.values() if t]
        anchor = min(future) if future else now
        midnight = _service_midnight(anchor, tz).timestamp()
        # A trip running past midnight belongs to the previous service day.
        first = trip["stops"][0]["departure"] or 0
        if midnight + first - anchor > 12 * 3600:
            midnight = _service_midnight(anchor - 86400, tz).timestamp()
        elif anchor - (midnight + trip["stops"][-1]["arrival"]) > 12 * 3600:
            midnight = _service_midnight(anchor + 86400, tz).timestamp()

        # UTA's predictions can skip ahead by many stops, so the first prediction
        # is not necessarily the next stop. Use the train's reported stop or position.
        next_sequence = vehicle["sequence"]
        if next_sequence is None:
            next_sequence = _next_sequence_by_position(trip["stops"], vehicle["lat"], vehicle["lon"])
        next_stop = next(
            (s for s in trip["stops"] if s["sequence"] >= next_sequence), trip["stops"][-1]
        )

        stations = []
        for stop in trip["stops"]:
            live = realtime.get(stop["sequence"], {})
            if stop["sequence"] < next_stop["sequence"]:
                status = "Departed"
            elif (
                stop is next_stop
                and vehicle["status"] == _STOPPED_AT
                and vehicle["sequence"] == stop["sequence"]
            ):
                status = "Station"
            else:
                status = "Enroute"
            stations.append(
                {
                    "name": stop["name"],
                    "code": "",
                    "tz": TIMEZONE,
                    "lat": stop["lat"],
                    "lon": stop["lon"],
                    "schArr": _iso(midnight + stop["arrival"], tz),
                    "schDep": _iso(midnight + stop["departure"], tz),
                    "arr": _iso(live.get("arrival"), tz),
                    "dep": _iso(live.get("departure"), tz),
                    "status": status,
                }
            )

        bearing = vehicle["bearing"]
        heading = _COMPASS[round(bearing / 45) % 8] if bearing is not None else ""
        speed = vehicle["speed"]
        origin, destination = trip["stops"][0]["name"], trip["stops"][-1]["name"]
        trains.append(
            {
                "provider": "UTA",
                "routeName": route["name"],
                "trainNum": route["label"],
                "trainID": f"uta-{trip_id}",
                "trainState": "Active",
                "lat": round(vehicle["lat"], 5),
                "lon": round(vehicle["lon"], 5),
                "heading": heading,
                "velocity": speed * 2.23694 if speed is not None else None,
                "iconColor": route["color"],
                "textColor": route["text_color"],
                "origName": origin,
                "origCode": origin,
                "destName": destination,
                "destCode": destination,
                "lastValTS": _iso(vehicle["timestamp"], tz),
                "alerts": [],
                "stations": stations,
            }
        )
    return trains
