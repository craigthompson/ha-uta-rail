"""Live UTA FrontRunner and TRAX train positions for Home Assistant."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
import logging
import time

import aiohttp

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import gtfs

DOMAIN = "uta_rail"
ATTRIBUTION = "Route and departure data provided by permission of UTA"

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.SENSOR]
_SCHEDULE_URL = "https://gtfsfeed.rideuta.com/GTFS.zip"
_VEHICLES_URL = "https://apps.rideuta.com/tms/gtfs/Vehicle"
_TRIP_UPDATES_URL = "https://apps.rideuta.com/tms/gtfs/TripUpdate"
_POLL_INTERVAL = timedelta(seconds=30)
_SCHEDULE_MAX_AGE = 24 * 3600
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
_HEADERS = {"User-Agent": "HomeAssistant-uta_rail/1.0"}


class UtaRailCoordinator(DataUpdateCoordinator[list[dict]]):
    """Poll UTA's realtime feeds and refresh the schedule daily."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=_POLL_INTERVAL)
        self.static = None
        self._static_time = 0.0

    async def _get(self, url: str) -> bytes:
        session = async_get_clientsession(self.hass)
        async with session.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT) as response:
            response.raise_for_status()
            return await response.read()

    async def _refresh_schedule(self) -> None:
        if self.static is not None and time.time() - self._static_time < _SCHEDULE_MAX_AGE:
            return
        try:
            data = await self._get(_SCHEDULE_URL)
            self.static = await self.hass.async_add_executor_job(gtfs.parse_static, data)
            self._static_time = time.time()
        except Exception as err:  # noqa: BLE001 - a stale schedule is better than no trains
            if self.static is None:
                raise UpdateFailed(f"Could not load the UTA schedule: {err}") from err
            _LOGGER.warning("Keeping the existing UTA schedule: %s", err)
            self._static_time = time.time() - _SCHEDULE_MAX_AGE + 3600

    async def _async_update_data(self) -> list[dict]:
        await self._refresh_schedule()
        try:
            vehicles = gtfs.parse_vehicles(await self._get(_VEHICLES_URL))
        except Exception as err:
            raise UpdateFailed(f"Could not read UTA vehicle positions: {err}") from err
        try:
            updates = gtfs.parse_trip_updates(await self._get(_TRIP_UPDATES_URL))
        except Exception as err:  # noqa: BLE001 - positions still work without arrival estimates
            _LOGGER.debug("No UTA trip updates this cycle: %s", err)
            updates = {}
        return gtfs.build_trains(self.static, vehicles, updates, time.time())


class UtaRailDataView(HomeAssistantView):
    """Serve train data to dashboards on request.

    This is an API rather than sensor attributes because attributes would be
    pushed to every connected dashboard each time the trains move.
    """

    url = "/api/uta_rail/data"
    name = "api:uta_rail:data"
    requires_auth = True

    async def get(self, request):
        """Return trains and rail lines."""
        hass = request.app["hass"]
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            return self.json_message("UTA Rail is not loaded", HTTPStatus.SERVICE_UNAVAILABLE)
        coordinator = entries[0].runtime_data
        if not coordinator.last_update_success:
            return self.json_message("UTA feeds are unavailable", HTTPStatus.SERVICE_UNAVAILABLE)
        return self.json({"trains": coordinator.data, "lines": coordinator.static.lines})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the API once."""
    hass.http.register_view(UtaRailDataView)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration."""
    coordinator = UtaRailCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the integration."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
