"""Buttons to start and cancel auto-tune from the device page."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([AutoTuneButton(hass, entry, start=True), AutoTuneButton(hass, entry, start=False)])


class AutoTuneButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, start: bool) -> None:
        self.hass = hass
        self._entry_id = entry.entry_id
        self._start = start
        self._attr_translation_key = "start_autotune" if start else "cancel_autotune"
        self._attr_unique_id = f"{entry.entry_id}_{self._attr_translation_key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    async def async_press(self) -> None:
        climate = self.hass.data[DOMAIN][self._entry_id]
        if self._start:
            await climate.async_start_autotune()
        else:
            await climate.async_cancel_autotune()
