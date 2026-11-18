"""Diagnostics: settings, learned values and recent controller samples.

Download from the integration page. The samples are what the step 4
auto-tuner will fit against, so this is also a quick way to share data.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    out: dict[str, Any] = {"data": dict(entry.data), "options": dict(entry.options)}
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("climate", DOMAIN, entry.entry_id)
    component = hass.data.get("entity_components", {}).get("climate")
    entity = component.get_entity(entity_id) if component and entity_id else None
    controller = getattr(entity, "controller", None)
    if controller is not None:
        out["learned"] = controller.learned.as_dict()
        out["integral"] = controller.integral
        out["history"] = list(controller.history)
    return out
