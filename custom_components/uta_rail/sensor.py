"""Sensor reporting how many UTA rail trains are running."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ATTRIBUTION, DOMAIN, UtaRailCoordinator


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Add the sensor."""
    async_add_entities([UtaRailTrainsSensor(entry.runtime_data)])


class UtaRailTrainsSensor(CoordinatorEntity[UtaRailCoordinator], SensorEntity):
    """Count of FrontRunner and TRAX trains reporting a position."""

    _attr_name = "UTA rail trains"
    _attr_unique_id = f"{DOMAIN}_trains"
    _attr_icon = "mdi:train"
    _attr_native_unit_of_measurement = "trains"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = ATTRIBUTION

    @property
    def native_value(self) -> int:
        """Return the number of trains running."""
        return len(self.coordinator.data)
