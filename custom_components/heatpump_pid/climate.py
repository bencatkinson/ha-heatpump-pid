"""Virtual climate entity that steers a heat pump's setpoint with a PID."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_FAN_MODE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_NAME,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
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
    SERVICE_RESET_LEARNING,
    SERVICE_RESUME,
    SERVICE_SET_TUNING,
    STORE_VERSION,
    TUNING_FIELDS,
)
from .controller import (
    COOL,
    HEAT,
    R_FALLBACK,
    ControllerParams,
    Decision,
    Learned,
    SetpointController,
    UnitState,
)

_LOGGER = logging.getLogger(__name__)

REGULATED = {HVACMode.HEAT: HEAT, HVACMode.COOL: COOL}
COMMAND_GRACE_S = 120  # ignore unit setpoint changes this soon after we sent one


def _float(state: State | None, attr: str | None = None) -> float | None:
    if state is None:
        return None
    raw = state.attributes.get(attr) if attr else state.state
    if raw in (None, STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def build_params(options: dict[str, Any]) -> ControllerParams:
    opts = {**DEFAULTS, **options}
    values = {k: float(opts[k]) for k in TUNING_FIELDS}
    values["min_run_s"] = float(opts[CONF_MIN_RUN]) * 60
    values["min_off_s"] = float(opts[CONF_MIN_OFF]) * 60
    values["max_change"] = max(values["setpoint_step"], 1.0)
    return ControllerParams.from_dict(values)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
    stored = await store.async_load() or {}
    entity = HeatPumpPidClimate(hass, entry, store, stored)
    async_add_entities([entity])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(SERVICE_RESET_LEARNING, {}, "async_reset_learning")
    platform.async_register_entity_service(SERVICE_RESUME, {}, "async_resume")
    platform.async_register_entity_service(
        SERVICE_SET_TUNING,
        {vol.Optional(k): vol.Coerce(float) for k in ("kp", "ki", "kd", "kff")},
        "async_set_tuning",
    )


class HeatPumpPidClimate(ClimateEntity, RestoreEntity):
    """Climate entity wrapping the real heat pump."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.1
    _attr_precision = 0.1
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, store: Store, stored: dict[str, Any]) -> None:
        self.hass = hass
        self._entry = entry
        self._store = store
        self._opts = {**DEFAULTS, **entry.options}
        self._heatpump: str = entry.data[CONF_HEATPUMP]
        self._room: str = self._opts[CONF_ROOM_SENSOR]
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.data.get(CONF_NAME, "Heat pump PID"),
            "manufacturer": "Heat Pump PID",
            "model": "Setpoint PID controller",
        }
        self.controller = SetpointController(
            build_params(self._opts), Learned.from_dict(stored.get("learned"))
        )
        self.controller.restore(stored.get("integral"), stored.get("mode"))

        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = 21.0
        self._attr_current_temperature = None
        self._decision: Decision | None = None
        self._fallback_reason: str | None = None
        self._last_sent: float | None = None
        self._last_sent_at = 0.0
        self._override_until: datetime | None = None
        self._unsubs: list[Any] = []

    # -------------------------------------------------------------- lifecycle

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        hp = self.hass.states.get(self._heatpump)
        last = await self.async_get_last_state()
        if last is not None:
            if last.attributes.get(ATTR_TEMPERATURE) is not None:
                self._attr_target_temperature = float(last.attributes[ATTR_TEMPERATURE])
            if last.state in [m.value for m in self.hvac_modes]:
                self._attr_hvac_mode = HVACMode(last.state)
        elif hp is not None:
            if hp.state in [m.value for m in self.hvac_modes]:
                self._attr_hvac_mode = HVACMode(hp.state)
            if (t := _float(hp, ATTR_TEMPERATURE)) is not None:
                self._attr_target_temperature = t

        watched = [self._heatpump, self._room] + [
            self._opts[k] for k in OPTIONAL_SENSORS if self._opts.get(k)
        ]
        self._unsubs.append(async_track_state_change_event(self.hass, watched, self._async_input_changed))
        interval = timedelta(seconds=float(self._opts[CONF_UPDATE_INTERVAL]))
        self._unsubs.append(async_track_time_interval(self.hass, self._async_tick, interval))
        self._update_room()
        self.hass.async_create_task(self._async_control())

    async def async_will_remove_from_hass(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._async_save(now=True)

    # ------------------------------------------------------------- properties

    @property
    def _hp_state(self) -> State | None:
        return self.hass.states.get(self._heatpump)

    @property
    def available(self) -> bool:
        hp = self._hp_state
        return hp is not None and hp.state != STATE_UNAVAILABLE

    @property
    def hvac_modes(self) -> list[HVACMode]:
        hp = self._hp_state
        modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
        if hp is not None:
            for mode in hp.attributes.get("hvac_modes", []):
                try:
                    m = HVACMode(mode)
                except ValueError:
                    continue
                if m not in modes:
                    modes.append(m)
        return modes

    @property
    def supported_features(self) -> ClimateEntityFeature:
        feats = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )
        if self.fan_modes:
            feats |= ClimateEntityFeature.FAN_MODE
        return feats

    @property
    def fan_modes(self) -> list[str] | None:
        hp = self._hp_state
        return hp.attributes.get(ATTR_FAN_MODES) if hp else None

    @property
    def fan_mode(self) -> str | None:
        hp = self._hp_state
        return hp.attributes.get(ATTR_FAN_MODE) if hp else None

    @property
    def min_temp(self) -> float:
        return float(self._opts["min_setpoint"])

    @property
    def max_temp(self) -> float:
        return float(self._opts["max_setpoint"])

    @property
    def hvac_action(self) -> HVACAction | None:
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        unit = self._unit_state()
        if self.controller.defrosting:
            return HVACAction.DEFROSTING
        on = unit.compressor_on
        if on is None:
            hp = self._hp_state
            action = hp.attributes.get(ATTR_HVAC_ACTION) if hp else None
            return HVACAction(action) if action in HVACAction._value2member_map_ else None
        if not on:
            return HVACAction.IDLE
        if self.hvac_mode == HVACMode.COOL:
            return HVACAction.COOLING
        if self.hvac_mode == HVACMode.HEAT:
            return HVACAction.HEATING
        if self.hvac_mode == HVACMode.DRY:
            return HVACAction.DRYING
        return HVACAction.FAN if self.hvac_mode == HVACMode.FAN_ONLY else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.controller
        now = time.monotonic()
        run_s, off_s = c.compressor_seconds(now)
        attrs: dict[str, Any] = {
            "room_sensor": self._room,
            "serial_link_ok": self._link_ok(),
            "heatpump_entity": self._heatpump,
            "unit_setpoint_sent": self._last_sent,
            "control_state": self._control_state(),
            "override_until": self._override_until.isoformat() if self._override_until else None,
            "compressor_run_minutes": round(run_s / 60, 1) if run_s is not None else None,
            "compressor_off_minutes": round(off_s / 60, 1) if off_s is not None else None,
            "learned_heat_stop_demand": round(c.learned.heat.stop_threshold, 2),
            "learned_heat_start_demand": round(c.learned.heat.start_threshold, 2),
            "learned_cool_stop_demand": round(c.learned.cool.stop_threshold, 2),
            "learned_cool_start_demand": round(c.learned.cool.start_threshold, 2),
            "learned_max_hz": c.learned.max_hz,
            "kp": c.params.kp,
            "ki": c.params.ki,
            "kd": c.params.kd,
        }
        if (d := self._decision) is not None:
            attrs.update(
                error=d.error,
                demand=d.demand,
                idle_demand=d.idle,
                p_term=d.p,
                i_term=d.i,
                d_term=d.d,
                ff_term=d.ff,
            )
        return attrs

    def _control_state(self) -> str:
        if self.hvac_mode not in REGULATED:
            return "passthrough" if self.hvac_mode != HVACMode.OFF else "off"
        if self._override_until and dt_util.utcnow() < self._override_until:
            return "override"
        if self._fallback_reason:
            return f"{R_FALLBACK}: {self._fallback_reason}"
        return self._decision.reason if self._decision else "starting"

    # ------------------------------------------------------------- user input

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        self._attr_target_temperature = float(temp)
        if (mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(HVACMode(mode))
            return
        self._override_until = None
        await self._async_control()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        hvac_mode = HVACMode(hvac_mode)
        self._attr_hvac_mode = hvac_mode
        self._override_until = None
        self.controller.reset()
        hp = self._hp_state
        if hp is None or hp.state != hvac_mode.value:
            await self._call(SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: hvac_mode.value})
        await self._async_control()

    async def async_turn_on(self) -> None:
        hp = self._hp_state
        mode = HVACMode.HEAT
        if hp is not None and hp.state in (HVACMode.COOL, HVACMode.HEAT):
            mode = HVACMode(hp.state)
        await self.async_set_hvac_mode(mode)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._call(SERVICE_SET_FAN_MODE, {ATTR_FAN_MODE: fan_mode})

    # ---------------------------------------------------------------- services

    async def async_reset_learning(self) -> None:
        self.controller.learned = Learned()
        self.controller.reset()
        await self._async_save(now=True)
        self.async_write_ha_state()

    async def async_resume(self) -> None:
        self._override_until = None
        await self._async_control()

    async def async_set_tuning(self, **gains: float) -> None:
        """Hook for the auto-tuner: store new gains in the entry options."""
        options = {**self._entry.options, **{k: float(v) for k, v in gains.items()}}
        self.hass.config_entries.async_update_entry(self._entry, options=options)

    # ----------------------------------------------------------------- control

    @callback
    def _async_input_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id == self._room:
            self._update_room()
        elif entity_id == self._heatpump:
            self._handle_unit_change(event.data.get("old_state"), event.data.get("new_state"))
        self.async_write_ha_state()

    async def _async_tick(self, _now: datetime) -> None:
        await self._async_control()

    def _update_room(self) -> None:
        self._attr_current_temperature = _float(self.hass.states.get(self._room))

    def _handle_unit_change(self, old: State | None, new: State | None) -> None:
        """Mirror mode changes made at the unit, and spot manual setpoint overrides."""
        if new is None or new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        if old is not None and old.state != new.state and new.state != self.hvac_mode.value:
            try:
                self._attr_hvac_mode = HVACMode(new.state)
                self.controller.reset()
                _LOGGER.info("%s: unit mode changed at the unit to %s", self.entity_id, new.state)
            except ValueError:
                pass
            return
        if self.hvac_mode not in REGULATED or self._last_sent is None:
            return
        new_sp = _float(new, ATTR_TEMPERATURE)
        old_sp = _float(old, ATTR_TEMPERATURE) if old else None
        recent = time.monotonic() - self._last_sent_at < COMMAND_GRACE_S
        if new_sp is None or new_sp == old_sp or recent or new_sp == self._last_sent:
            return
        minutes = float(self._opts[CONF_OVERRIDE_MINUTES])
        if minutes <= 0:
            return
        self._override_until = dt_util.utcnow() + timedelta(minutes=minutes)
        _LOGGER.info(
            "%s: unit setpoint changed to %s outside this controller, pausing until %s",
            self.entity_id,
            new_sp,
            self._override_until,
        )

    def _link_ok(self) -> bool:
        """False when the decoder says its serial data is stale."""
        ent = self._opts.get(CONF_LINK)
        if not ent:
            return True
        st = self.hass.states.get(ent)
        return st is not None and st.state == STATE_ON

    def _unit_state(self) -> UnitState:
        hp = self._hp_state
        get = self.hass.states.get
        link = self._link_ok()

        def sensor(key: str) -> float | None:
            ent = self._opts.get(key)
            return _float(get(ent)) if ent and link else None

        running = None
        if link and (ent := self._opts.get(CONF_COMPRESSOR_RUNNING)):
            st = get(ent)
            if st is not None and st.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                running = st.state == STATE_ON
        unit_temp = sensor(CONF_UNIT_TEMP)
        if unit_temp is None:
            unit_temp = _float(hp, ATTR_CURRENT_TEMPERATURE)
        return UnitState(
            unit_temp=unit_temp,
            unit_setpoint=_float(hp, ATTR_TEMPERATURE),
            hz=sensor(CONF_COMPRESSOR_FREQ),
            running=running,
            indoor_coil=sensor(CONF_INDOOR_COIL),
            outdoor_coil=sensor(CONF_OUTDOOR_COIL),
            outdoor_temp=sensor(CONF_OUTDOOR_TEMP),
            discharge=sensor(CONF_DISCHARGE),
            power=sensor(CONF_POWER),
            energy=sensor(CONF_ENERGY),
        )

    def _room_problem(self) -> str | None:
        st = self.hass.states.get(self._room)
        if st is None or _float(st) is None:
            return "room sensor unavailable"
        age = dt_util.utcnow() - st.last_reported
        if age > timedelta(minutes=float(self._opts[CONF_SENSOR_TIMEOUT])):
            return "room sensor stale"
        return None

    async def _async_control(self) -> None:
        hp = self._hp_state
        if hp is None or hp.state == STATE_UNAVAILABLE:
            self.async_write_ha_state()
            return
        mode = self.hvac_mode
        target = self.target_temperature
        if mode == HVACMode.OFF or target is None:
            self._decision = None
            self.async_write_ha_state()
            return
        if mode not in REGULATED:
            # Dry, fan and auto: hand the target to the unit as is.
            if hp.attributes.get(ATTR_TEMPERATURE) is not None and mode != HVACMode.FAN_ONLY:
                await self._send(self.controller.fallback(target))
            self.async_write_ha_state()
            return
        if self._override_until:
            if dt_util.utcnow() < self._override_until:
                self.async_write_ha_state()
                return
            self._override_until = None
            self.controller.reset()

        self._update_room()
        self._fallback_reason = self._room_problem()
        if self._fallback_reason:
            await self._send(self.controller.fallback(target))
            self.async_write_ha_state()
            return

        decision = self.controller.update(
            time.monotonic(), REGULATED[mode], target, self.current_temperature, self._unit_state()
        )
        self._decision = decision
        await self._send(decision.setpoint)
        await self._async_save()
        self.async_write_ha_state()

    async def _send(self, setpoint: float) -> None:
        current = _float(self._hp_state, ATTR_TEMPERATURE)
        if current is not None and abs(current - setpoint) < 0.05:
            self._last_sent = setpoint
            return
        self._last_sent = setpoint
        self._last_sent_at = time.monotonic()
        await self._call(SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: setpoint})

    async def _call(self, service: str, data: dict[str, Any]) -> None:
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            service,
            {ATTR_ENTITY_ID: self._heatpump, **data},
            blocking=True,
            context=self._context,
        )

    async def _async_save(self, now: bool = False) -> None:
        def data() -> dict[str, Any]:
            return {
                "learned": self.controller.learned.as_dict(),
                "integral": self.controller.integral,
                "mode": self.controller.mode,
            }

        if now:
            await self._store.async_save(data())
        else:
            self._store.async_delay_save(data, 300)


__all__ = ["HeatPumpPidClimate", "build_params"]
