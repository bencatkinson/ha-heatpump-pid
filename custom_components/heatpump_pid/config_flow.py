"""Config and options flow for Heat Pump PID."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, selector
import voluptuous as vol

from .const import (
    CONF_COMPRESSOR_FREQ,
    CONF_COMPRESSOR_RUNNING,
    CONF_DISCHARGE,
    CONF_ENERGY,
    CONF_HEATPUMP,
    CONF_INDOOR_COIL,
    CONF_LINK,
    CONF_MIN_OFF,
    CONF_MIN_RUN,
    CONF_OUTDOOR_COIL,
    CONF_OUTDOOR_TEMP,
    CONF_OVERRIDE_MINUTES,
    CONF_POWER,
    CONF_ROOM_SENSOR,
    CONF_SENSOR_TIMEOUT,
    CONF_UNIT_TEMP,
    CONF_UPDATE_INTERVAL,
    DEFAULTS,
    DOMAIN,
    OPTIONAL_SENSORS,
    SUFFIXES,
)

_TEMP_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
)
_ANY_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_BINARY = selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor"))

_SENSOR_SELECTORS = {
    CONF_UNIT_TEMP: _TEMP_SENSOR,
    CONF_COMPRESSOR_FREQ: _ANY_SENSOR,
    CONF_COMPRESSOR_RUNNING: _BINARY,
    CONF_OUTDOOR_TEMP: _TEMP_SENSOR,
    CONF_INDOOR_COIL: _TEMP_SENSOR,
    CONF_OUTDOOR_COIL: _TEMP_SENSOR,
    CONF_DISCHARGE: _TEMP_SENSOR,
    CONF_LINK: _BINARY,
    CONF_POWER: _ANY_SENSOR,
    CONF_ENERGY: _ANY_SENSOR,
}


def _num(minimum: float, maximum: float, step: float, unit: str | None = None):
    config = selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX
    )
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


_TUNING_SELECTORS = {
    "kp": _num(0, 10, 0.05),
    "ki": _num(0, 10, 0.05, "/h"),
    "kd": _num(0, 10, 0.05, "h"),
    "kff": _num(0, 1, 0.01),
    "deadband": _num(0, 1, 0.05, "°C"),
    "restart_hysteresis": _num(0, 2, 0.05, "°C"),
    "idle_margin": _num(0, 3, 0.1, "°C"),
    "max_boost": _num(0.5, 10, 0.5, "°C"),
    "setpoint_step": selector.SelectSelector(selector.SelectSelectorConfig(options=["1.0", "0.5"])),
    "min_setpoint": _num(5, 30, 0.5, "°C"),
    "max_setpoint": _num(16, 35, 0.5, "°C"),
    "discharge_limit": _num(50, 120, 1, "°C"),
}

_BEHAVIOUR_SELECTORS = {
    CONF_UPDATE_INTERVAL: _num(15, 600, 5, "s"),
    CONF_SENSOR_TIMEOUT: _num(5, 240, 5, "min"),
    CONF_OVERRIDE_MINUTES: _num(0, 1440, 5, "min"),
    CONF_MIN_RUN: _num(0, 60, 1, "min"),
    CONF_MIN_OFF: _num(0, 60, 1, "min"),
}


def suggest_sensors(hass: HomeAssistant, climate_entity: str) -> dict[str, str]:
    """Find the decoder sensors on the same device as the climate entity."""
    registry = er.async_get(hass)
    entry = registry.async_get(climate_entity)
    if entry is None or entry.device_id is None:
        return {}
    on_device = [
        e.entity_id
        for e in er.async_entries_for_device(registry, entry.device_id)
        if e.entity_id != climate_entity
    ]
    found: dict[str, str] = {}
    for key, suffixes in SUFFIXES.items():
        domain = "binary_sensor" if _SENSOR_SELECTORS[key] is _BINARY else "sensor"
        for suffix in suffixes:
            match = next((e for e in on_device if e.startswith(f"{domain}.") and e.endswith(suffix)), None)
            if match:
                found[key] = match
                break
    return found


def _sensor_schema(current: dict[str, Any], suggested: dict[str, str]) -> vol.Schema:
    """Room sensor plus optional unit sensors."""
    fields: dict[Any, Any] = {
        vol.Required(CONF_ROOM_SENSOR, default=current.get(CONF_ROOM_SENSOR, vol.UNDEFINED)): _TEMP_SENSOR,
    }
    for key in OPTIONAL_SENSORS:
        value = current.get(key) or suggested.get(key)
        desc = {"suggested_value": value} if value else None
        fields[vol.Optional(key, description=desc)] = _SENSOR_SELECTORS[key]
    return vol.Schema(fields)


def _clean_sensors(user_input: dict[str, Any]) -> dict[str, Any]:
    out = {CONF_ROOM_SENSOR: user_input[CONF_ROOM_SENSOR]}
    for key in OPTIONAL_SENSORS:
        if user_input.get(key):
            out[key] = user_input[key]
    return out


class HeatPumpPidConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a PID wrapper around an existing heat pump climate entity."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HEATPUMP])
            self._abort_if_unique_id_configured()
            self._data = user_input
            return await self.async_step_sensors()

        default_hp = next(
            (e for e in self.hass.states.async_entity_ids("climate") if "officeheatpump" in e),
            vol.UNDEFINED,
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="Office heat pump PID"): selector.TextSelector(),
                vol.Required(CONF_HEATPUMP, default=default_hp): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate")
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_sensors(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_ROOM_SENSOR] == self._data[CONF_HEATPUMP]:
                errors[CONF_ROOM_SENSOR] = "same_entity"
            else:
                options = {**DEFAULTS, **_clean_sensors(user_input)}
                return self.async_create_entry(title=self._data[CONF_NAME], data=self._data, options=options)
        return self.async_show_form(
            step_id="sensors",
            data_schema=_sensor_schema(
                user_input or {}, suggest_sensors(self.hass, self._data[CONF_HEATPUMP])
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HeatPumpPidOptionsFlow()


class HeatPumpPidOptionsFlow(OptionsFlow):
    """Change sensors (including the room sensor) and tuning later."""

    def __init__(self) -> None:
        self._new: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._new = _clean_sensors(user_input)
            return await self.async_step_tuning()
        return self.async_show_form(
            step_id="init",
            data_schema=_sensor_schema(dict(self.config_entry.options), {}),
        )

    async def async_step_tuning(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = {**DEFAULTS, **self.config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            values = dict(user_input)
            values["setpoint_step"] = float(values.get("setpoint_step", 1.0))
            if values["min_setpoint"] >= values["max_setpoint"]:
                errors["max_setpoint"] = "range"
            else:
                return self.async_create_entry(data={**values, **self._new})

        fields: dict[Any, Any] = {}
        for key, sel in {**_BEHAVIOUR_SELECTORS, **_TUNING_SELECTORS}.items():
            default = current.get(key, DEFAULTS[key])
            if key == "setpoint_step":
                default = "0.5" if float(default) == 0.5 else "1.0"
            fields[vol.Required(key, default=default)] = sel
        return self.async_show_form(step_id="tuning", data_schema=vol.Schema(fields), errors=errors)
