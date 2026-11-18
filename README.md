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
   - Setpoints are clamped, quantised to the unit's step and moved at most
     1 °C per control cycle. The step is 1 °C for stock tclac. Set **Unit
     setpoint step** to 0.5 °C under Configure once the ESPHome device runs the
     half-degree tclac patch, for twice the resolution.
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

- `heatpump_pid.set_tuning`: set `kp`, `ki`, `kd`, `kff`, optionally for one `mode`.
- `heatpump_pid.start_autotune` / `heatpump_pid.cancel_autotune`: see below.
- `heatpump_pid.reset_learning`: forget the learned stop and start points.
- `heatpump_pid.resume`: end a manual override pause.

**Diagnostics** (on the integration page) downloads the settings, learned
values and the last 48 hours of controller samples (room, unit, compressor
frequency, coil temperatures, power, setpoint sent).

## Auto-tune

Auto-tune measures how your room and unit respond and sets the PID gains for
the current mode. Heating and cooling are tuned separately, so run it once in
each season.

**To run it:** set the thermostat to heat (or cool) at a normal target, then
press **Start auto-tune** on the device page (or call
`heatpump_pid.start_autotune`). It takes about 1.5 to 3 hours:

1. **Settle.** Normal control until the room has held within 0.3 °C of
   target, with the compressor running, for 15 minutes.
2. **Baseline.** Holds the unit's demand steady for 10 minutes to measure
   drift.
3. **Step.** Adds 1.5 °C of demand and holds it, recording room temperature,
   compressor frequency and power, until the room has moved 1 °C or 75 minutes
   pass. Expect the room to overshoot target by up to about 1 °C here.
4. **Fit and apply.** Fits a dead-time model to the room's response, checks
   the compressor really changed speed, and computes gains with the SIMC
   rules. The results are written as that mode's **Kp/Ki (auto-tuned)**
   options, and normal control resumes.

Follow it in the `autotune_state` and `autotune_message` attributes. The
measured model (dead time, slope or time constant, compressor Hz per °C of
demand, fit quality) is kept in `autotune_last_heat` / `autotune_last_cool`.

It stops without changing anything if the compressor stops, a defrost starts,
the room strays more than 1.5 °C the wrong way, or you change the mode, target
or the unit's setpoint. **Cancel auto-tune** stops it at any time.

It needs enough load that the compressor runs continuously at target. On a
mild day the unit may cycle even at minimum speed, so tune on a colder (or
hotter) day. The service takes `step_size` (bigger for a clearer response)
and `aggressiveness` (above 1 is gentler).

Per-mode gains can be edited or cleared under **Configure → Control and
tuning**. Empty means the general Kp/Ki apply.

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
