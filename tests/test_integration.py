"""Config flow and climate entity tests against a real Home Assistant core."""

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.heatpump_pid.const import DEFAULTS, DOMAIN

HP = "climate.office_heat_pump_officeheatpump_climate"
ROOM = "sensor.mysa_office_temperature"
HZ = "sensor.office_heat_pump_compressor_frequency"
UNIT_T = "sensor.office_heat_pump_room_temperature_unit"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _hp_state(hass, mode="heat", current=20, setpoint=20):
    hass.states.async_set(
        HP,
        mode,
        {
            "hvac_modes": ["off", "heat", "cool", "dry", "fan_only", "auto"],
            "current_temperature": current,
            "temperature": setpoint,
            "fan_modes": ["auto", "low", "high"],
            "fan_mode": "auto",
        },
    )


def _mock_services(hass: HomeAssistant):
    """Record calls to the heat pump. Must run after setup, because loading the
    climate component registers the real climate.* services over any mocks."""
    return {
        "set_temperature": async_mock_service(hass, "climate", "set_temperature"),
        "set_hvac_mode": async_mock_service(hass, "climate", "set_hvac_mode"),
    }


async def test_config_flow_suggests_device_sensors(hass: HomeAssistant) -> None:
    src = MockConfigEntry(domain="esphome")
    src.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=src.entry_id, identifiers={("esphome", "officeheatpump")}
    )
    reg = er.async_get(hass)
    for domain, obj in (
        ("climate", "officeheatpump_climate"),
        ("sensor", "compressor_frequency"),
        ("binary_sensor", "compressor_running"),
        ("sensor", "room_temperature_unit"),
        ("binary_sensor", "heat_pump_link"),
    ):
        reg.async_get_or_create(
            domain, "esphome", obj, device_id=dev.id, suggested_object_id=f"office_heat_pump_{obj}"
        )
    _hp_state(hass)
    hass.states.async_set(ROOM, "19.5", {"device_class": "temperature"})

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Office", "heatpump_entity": HP}
    )
    assert result["step_id"] == "sensors"
    schema = {str(k): k.description for k in result["data_schema"].schema}
    assert schema["compressor_freq_sensor"]["suggested_value"] == HZ
    assert schema["unit_temp_sensor"]["suggested_value"] == UNIT_T
    assert schema["link_sensor"]["suggested_value"] == "binary_sensor.office_heat_pump_heat_pump_link"

    with patch("custom_components.heatpump_pid.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"room_sensor": ROOM, "compressor_freq_sensor": HZ}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["heatpump_entity"] == HP
    assert result["options"]["room_sensor"] == ROOM
    assert result["options"]["compressor_freq_sensor"] == HZ
    assert result["options"]["kp"] == DEFAULTS["kp"]


async def _setup(hass, **options):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"name": "Office", "heatpump_entity": HP},
        options={**DEFAULTS, "room_sensor": ROOM, "compressor_freq_sensor": HZ, **options},
        unique_id=HP,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _our_entity(hass):
    return next(e for e in hass.states.async_entity_ids("climate") if e != HP)


def _obj(hass, entity_id):
    """Our entity object. climate.* services are mocked to record calls to the
    real heat pump, so tests drive our entity through its methods instead."""
    return hass.data["entity_components"]["climate"].get_entity(entity_id)


async def test_heating_raises_unit_setpoint(hass: HomeAssistant) -> None:
    _hp_state(hass, "heat", current=20, setpoint=20)
    hass.states.async_set(ROOM, "18.0")
    hass.states.async_set(HZ, "0")
    await _setup(hass)
    services = _mock_services(hass)
    ent = _our_entity(hass)

    await _obj(hass, ent).async_set_temperature(temperature=21)
    calls = [c for c in services["set_temperature"] if c.data["entity_id"] == HP]
    assert calls, "controller should have sent a setpoint"
    assert calls[-1].data["temperature"] > 20
    st = hass.states.get(ent)
    assert st.attributes["current_temperature"] == 18.0
    assert st.attributes["control_state"] in ("pid", "idle")
    assert st.attributes["room_sensor"] == ROOM


async def test_stale_room_sensor_falls_back_to_target(hass: HomeAssistant) -> None:
    _hp_state(hass, "heat", current=20, setpoint=20)
    hass.states.async_set(ROOM, "unavailable")
    await _setup(hass)
    services = _mock_services(hass)
    ent = _our_entity(hass)
    await _obj(hass, ent).async_set_temperature(temperature=22)
    assert services["set_temperature"][-1].data["temperature"] == 22
    assert hass.states.get(ent).attributes["control_state"].startswith("fallback")


async def test_manual_change_at_unit_pauses_control(hass: HomeAssistant) -> None:
    _hp_state(hass, "heat", current=20, setpoint=20)
    hass.states.async_set(ROOM, "19.0")
    hass.states.async_set(HZ, "30")
    with patch("custom_components.heatpump_pid.climate.COMMAND_GRACE_S", 0):
        await _setup(hass)
        _mock_services(hass)
        ent = _our_entity(hass)
        await _obj(hass, ent).async_set_temperature(temperature=21)
        _hp_state(hass, "heat", current=20, setpoint=27)  # someone used the remote
        await hass.async_block_till_done()
    assert hass.states.get(ent).attributes["control_state"] == "override"
    await hass.services.async_call(DOMAIN, "resume", {"entity_id": ent}, blocking=True)
    assert hass.states.get(ent).attributes["control_state"] != "override"


async def test_mode_change_at_unit_is_mirrored(hass: HomeAssistant) -> None:
    _hp_state(hass, "heat")
    hass.states.async_set(ROOM, "20.0")
    await _setup(hass)
    _mock_services(hass)
    ent = _our_entity(hass)
    await _obj(hass, ent).async_set_hvac_mode("heat")
    _hp_state(hass, "off")
    await hass.async_block_till_done()
    assert hass.states.get(ent).state == "off"


async def test_options_flow_changes_room_sensor(hass: HomeAssistant) -> None:
    _hp_state(hass)
    hass.states.async_set(ROOM, "20.0")
    hass.states.async_set("sensor.zigbee_office", "20.4", {"device_class": "temperature"})
    entry = await _setup(hass)
    _mock_services(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"room_sensor": "sensor.zigbee_office"}
    )
    assert result["step_id"] == "tuning"
    values = {k: DEFAULTS[k] for k in DEFAULTS}
    values["setpoint_step"] = "1.0"
    values["kp"] = 2.0
    result = await hass.config_entries.options.async_configure(result["flow_id"], values)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["room_sensor"] == "sensor.zigbee_office"
    assert entry.options["kp"] == 2.0
    assert hass.states.get(_our_entity(hass)).attributes["room_sensor"] == "sensor.zigbee_office"


async def test_set_tuning_service(hass: HomeAssistant) -> None:
    _hp_state(hass)
    hass.states.async_set(ROOM, "20.0")
    entry = await _setup(hass)
    _mock_services(hass)
    await hass.services.async_call(
        DOMAIN, "set_tuning", {"entity_id": _our_entity(hass), "kp": 0.8, "ki": 0.4}, blocking=True
    )
    await hass.async_block_till_done()
    assert entry.options["kp"] == 0.8
    assert hass.states.get(_our_entity(hass)).attributes["ki"] == 0.4


async def test_autotune_start_cancel_and_apply(hass: HomeAssistant) -> None:
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.heatpump_pid.tuner import DONE, TuneResult

    _hp_state(hass, "heat", current=21, setpoint=21)
    hass.states.async_set(ROOM, "21.0")
    hass.states.async_set(HZ, "30")
    entry = await _setup(hass)
    _mock_services(hass)
    ent = _our_entity(hass)
    obj = _obj(hass, ent)

    await obj.async_set_hvac_mode("off")
    with pytest.raises(HomeAssistantError):
        await obj.async_start_autotune()

    await obj.async_set_hvac_mode("heat")
    await obj.async_set_temperature(temperature=21)
    buttons = [e for e in hass.states.async_entity_ids("button")]
    start = next(b for b in buttons if "start" in b)
    cancel = next(b for b in buttons if "cancel" in b)
    await hass.services.async_call("button", "press", {"entity_id": start}, blocking=True)
    assert hass.states.get(ent).attributes["control_state"] == "autotune_settling"
    await hass.services.async_call("button", "press", {"entity_id": cancel}, blocking=True)
    assert hass.states.get(ent).attributes["autotune_state"] == "failed"

    # A finished tune writes that mode's gains and reloads.
    await obj.async_start_autotune()
    obj.tuner.state = DONE
    obj.tuner.mode = "heat"
    obj.tuner.result = TuneResult("heat", 2.2, 0.7, "integrating", None, 1.8, None, 6.0, 0.97, 20.0, 1.0, 0.4, 40)
    await obj._async_control()
    await hass.async_block_till_done()
    assert entry.options["heat_kp"] == 2.2
    assert entry.options["heat_ki"] == 0.7
    attrs = hass.states.get(_our_entity(hass)).attributes
    assert attrs["heat_kp"] == 2.2
    assert attrs["cool_kp"] == DEFAULTS["kp"]
    assert attrs["autotune_last_heat"]["theta_minutes"] == 6.0
