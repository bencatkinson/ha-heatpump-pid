# Heat Pump PID

A Home Assistant custom integration (HACS) that makes an inverter heat pump hold
the temperature measured by **a sensor you choose** (Mysa, Zigbee, anything),
even though the only thing you can control is the unit's setpoint.

It creates a new thermostat entity that wraps your existing heat pump climate
entity and steers its setpoint with a PID. It is written for a TCL-built unit
(Rheem RC2IV12HP3BA) running ESPHome with
[sorz2122/tclac](https://github.com/sorz2122/tclac) and the extra serial-data
sensors from
[Bencatkinson/esphome-tcl-heatpump](https://github.com/Bencatkinson/esphome-tcl-heatpump),
but works with any climate entity. The extra sensors are optional and make it
smarter.

## Goals

- **Steady room temperature** at the sensor you care about, not at the unit's
  return-air sensor.
- **Less energy and less short cycling.** The compressor is kept idling at low
  frequency instead of stopping and restarting.
- **Heating and cooling.**

## How it works

The unit modulates its compressor on its own error: the setpoint you send minus
the temperature it measures itself. The integration calls that the *demand*.

1. **Learns where your compressor stops and starts.** Each time compressor
   frequency drops to 0 (or rises from 0), it records the demand at that moment,
   separately for heating and cooling. If it asks for a stop and the unit keeps
   running, it walks its estimate down until the unit agrees. (Idea from
   [E-t0m's setpoint guidance](https://github.com/E-t0m/esphome-local-ac-control).)
2. **Idles instead of cycling.** With the room at target, it holds demand just
   above the learned stop point (the *idle margin*), so the compressor runs at
   minimum frequency.
3. **PID on the room sensor** adds or removes demand from there.
4. **Guard rails:**
   - Minimum compressor run and off times.
   - After a stop, no restart until the room drifts past the restart hysteresis.
   - Integrator is frozen at maximum frequency, during defrost (heating, with the
     indoor coil colder than the room), and at the discharge temperature limit.
   - Setpoints are clamped, quantised to the unit's step (1 °C for stock tclac)
     and moved at most 1 °C per control cycle.
5. **Safe fallbacks.** If the room sensor goes unavailable or stale, the unit is
   simply set to your target. If the ESPHome serial link sensor is off, unit
   sensors are ignored.
6. **Manual overrides.** Change the setpoint on the unit or remote and control
   pauses (60 minutes by default; `heatpump_pid.resume` ends it). Mode changes
   at the unit (off, heat, cool) are mirrored.

Dry, fan and auto modes are passed straight through without regulation.

## Install

1. In HACS, open the menu, choose **Custom repositories**, add
   `https://github.com/Bencatkinson/ha-heatpump-pid` as an **Integration**.
2. Install **Heat Pump PID** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Heat Pump PID.**
4. Pick the heat pump's climate entity, then your room sensor. Sensors from the
   ESPHome decoder on the same device are filled in for you.

Everything is changeable later from the integration's **Configure** button,
including the room sensor.

## Inputs

| Input | Required | Used for |
|---|---|---|
| Heat pump climate entity | yes | Setpoint and mode control, fan mode passthrough |
| Room temperature sensor | yes | The temperature that is regulated |
| Unit return-air temperature (0.1 °C) | no | Better demand calculation than tclac's rounded value |
| Compressor frequency | no | Learning stop/start points, max-frequency anti-windup |
| Compressor running | no | Run state (otherwise frequency > 0) |
| Outdoor temperature | no | Optional feed-forward (`kff`) |
| Indoor coil temperature | no | Defrost detection |
| Outdoor coil, discharge temperature | no | Discharge limit; logged for auto-tune |
| Serial link | no | Ignore stale unit data |
| Power, energy | no | Logged for auto-tune |

## Tuning

Defaults are a reasonable start for a single room: `kp 1.5`, `ki 1.0 /h`,
`kd 0`, deadband 0.1 °C, restart hysteresis 0.3 °C, idle margin 0.5 °C,
minimum run 10 min, minimum off 5 min.

The thermostat's attributes show what the controller is doing:
`control_state` (`pid`, `idle`, `min_run`, `min_off`, `waiting_restart`,
`defrost`, `discharge_limit`, `override`, `fallback: …`), `error`, `demand`,
the P/I/D terms and the learned stop/start points.

Services:

- `heatpump_pid.set_tuning`: set `kp`, `ki`, `kd`, `kff` (used by auto-tune).
- `heatpump_pid.reset_learning`: forget the learned stop and start points.
- `heatpump_pid.resume`: end a manual override pause.

**Diagnostics** (on the integration page) downloads the settings, learned
values and the last 48 hours of controller samples (room, unit, compressor
frequency, coil temperatures, power, setpoint sent). That's the data the
upcoming auto-tune fits against.

## Development

```bash
pip install -r requirements_test.txt
pytest
ruff check .
```

`custom_components/heatpump_pid/controller.py` has no Home Assistant imports,
and `tests/test_controller.py` runs it against a simulated inverter unit.

## License

MIT
