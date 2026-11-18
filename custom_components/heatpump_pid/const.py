"""Constants for Heat Pump PID."""

from __future__ import annotations

DOMAIN = "heatpump_pid"

# Entities (config + options)
CONF_HEATPUMP = "heatpump_entity"
CONF_ROOM_SENSOR = "room_sensor"
CONF_COMPRESSOR_FREQ = "compressor_freq_sensor"
CONF_COMPRESSOR_RUNNING = "compressor_running_sensor"
CONF_OUTDOOR_TEMP = "outdoor_temp_sensor"
CONF_INDOOR_COIL = "indoor_coil_sensor"
CONF_OUTDOOR_COIL = "outdoor_coil_sensor"
CONF_DISCHARGE = "discharge_temp_sensor"
CONF_UNIT_TEMP = "unit_temp_sensor"
CONF_LINK = "link_sensor"
CONF_POWER = "power_sensor"
CONF_ENERGY = "energy_sensor"

OPTIONAL_SENSORS = (
    CONF_UNIT_TEMP,
    CONF_COMPRESSOR_FREQ,
    CONF_COMPRESSOR_RUNNING,
    CONF_OUTDOOR_TEMP,
    CONF_INDOOR_COIL,
    CONF_OUTDOOR_COIL,
    CONF_DISCHARGE,
    CONF_LINK,
    CONF_POWER,
    CONF_ENERGY,
)

# Behaviour options
CONF_UPDATE_INTERVAL = "update_interval"
CONF_SENSOR_TIMEOUT = "sensor_timeout"
CONF_OVERRIDE_MINUTES = "override_minutes"
CONF_MIN_RUN = "min_run_minutes"
CONF_MIN_OFF = "min_off_minutes"

# Tuning options (map 1:1 onto ControllerParams fields)
TUNING_FIELDS = {
    "kp": 1.5,
    "ki": 1.0,
    "kd": 0.0,
    "kff": 0.0,
    "deadband": 0.1,
    "restart_hysteresis": 0.3,
    "idle_margin": 0.5,
    "max_boost": 4.0,
    "setpoint_step": 1.0,
    "min_setpoint": 16.0,
    "max_setpoint": 31.0,
    "discharge_limit": 90.0,
}

DEFAULTS = {
    CONF_UPDATE_INTERVAL: 60,
    CONF_SENSOR_TIMEOUT: 30,
    CONF_OVERRIDE_MINUTES: 60,
    CONF_MIN_RUN: 10,
    CONF_MIN_OFF: 5,
    **TUNING_FIELDS,
}

STORE_VERSION = 1
SERVICE_RESET_LEARNING = "reset_learning"
SERVICE_SET_TUNING = "set_tuning"
SERVICE_RESUME = "resume"
SERVICE_START_AUTOTUNE = "start_autotune"
SERVICE_CANCEL_AUTOTUNE = "cancel_autotune"

# Per-mode gains written by auto-tune (blank = use kp/ki/kff).
MODE_GAIN_FIELDS = ("heat_kp", "heat_ki", "heat_kff", "cool_kp", "cool_ki", "cool_kff")

# Entity-id suffixes the ESPHome decoder package uses (docs/entities.md in
# Bencatkinson/esphome-tcl-heatpump). Sensors on the same device as the chosen
# climate entity are suggested in the config flow by matching these, so the
# device name prefix doesn't matter. First match wins.
SUFFIXES: dict[str, tuple[str, ...]] = {
    CONF_UNIT_TEMP: ("_room_temperature_unit",),
    CONF_COMPRESSOR_FREQ: ("_compressor_frequency",),
    CONF_COMPRESSOR_RUNNING: ("_compressor_running",),
    CONF_OUTDOOR_TEMP: ("_outdoor_temperature",),
    CONF_INDOOR_COIL: ("_indoor_coil_temperature_a", "_indoor_coil_temperature_estimated"),
    CONF_OUTDOOR_COIL: ("_outdoor_coil_temperature",),
    CONF_DISCHARGE: ("_compressor_discharge_temperature",),
    CONF_LINK: ("_heat_pump_link",),
    CONF_POWER: ("_power",),
    CONF_ENERGY: ("_energy",),
}
