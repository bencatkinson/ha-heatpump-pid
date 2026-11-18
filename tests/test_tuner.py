"""Auto-tune against the simulated inverter."""

from custom_components.heatpump_pid.controller import (
    COOL,
    HEAT,
    ControllerParams,
    SetpointController,
    UnitState,
)
from custom_components.heatpump_pid.tuner import DONE, FAILED, AutoTuner, TunerConfig

from .test_controller import FakeInverter


class FineInverter(FakeInverter):
    """Unit sensor at 0.1 °C, like the decoder's return-air sensor."""

    @property
    def unit_temp(self):
        return round((self.room + self.offset) * 10) / 10

    def state(self):
        return UnitState(
            unit_temp=self.unit_temp, unit_setpoint=self.setpoint, hz=self.hz, power=self.hz * 12
        )


def run_tune(unit, target, params=None, cfg=None, hours=6, dt=60):
    ctrl = SetpointController(params or ControllerParams(setpoint_step=0.5))
    tuner = AutoTuner(cfg or TunerConfig())
    started = False
    for i in range(int(hours * 3600 / dt)):
        t = i * dt
        if i == 60 and not started:  # let it warm up first
            tuner.start(t, unit.mode, target)
            started = True
        room = round(unit.room, 1)
        forced = tuner.step(
            t, unit.mode, target, room, unit.state(), ctrl._last_demand, ctrl.integral, ctrl.defrosting
        )
        d = ctrl.update(t, unit.mode, target, room, unit.state(), forced_demand=forced)
        unit.setpoint = d.setpoint
        unit.step(dt)
        if started and not tuner.running:
            break
    return tuner, ctrl


def closed_loop_iae(params, unit, target, hours=6, dt=60, disturb_at=3):
    """Integrated absolute error after a 2 °C outdoor drop part way through."""
    ctrl = SetpointController(params)
    iae = 0.0
    for i in range(int(hours * 3600 / dt)):
        t = i * dt
        if t == disturb_at * 3600:
            unit.outdoor -= 2.0
        d = ctrl.update(t, unit.mode, target, round(unit.room, 1), unit.state())
        unit.setpoint = d.setpoint
        unit.step(dt)
        if t >= 2 * 3600:
            iae += abs(unit.room - target) * dt / 3600
    return iae


def test_heating_tune_finishes_with_sane_gains():
    tuner, ctrl = run_tune(FineInverter(HEAT, room=19.0, outdoor=5.0), 21.0)
    assert tuner.state == DONE, tuner.message
    r = tuner.result
    assert 0.2 <= r.kp <= 6 and 0.05 <= r.ki <= 6
    assert r.hz_gain and r.hz_gain > 0
    assert r.r2 > 0.8


def test_cooling_tune_finishes():
    tuner, _ = run_tune(FineInverter(COOL, room=26.0, outdoor=40.0), 24.0)
    assert tuner.state == DONE, tuner.message
    assert tuner.result.mode == COOL


def test_tuned_gains_control_at_least_as_well():
    tuner, _ = run_tune(FineInverter(HEAT, room=19.0, outdoor=5.0), 21.0)
    r = tuner.result
    base = ControllerParams(setpoint_step=0.5)
    tuned = ControllerParams(setpoint_step=0.5, heat_kp=r.kp, heat_ki=r.ki)
    iae_base = closed_loop_iae(base, FineInverter(HEAT, room=21.0, outdoor=5.0), 21.0)
    iae_tuned = closed_loop_iae(tuned, FineInverter(HEAT, room=21.0, outdoor=5.0), 21.0)
    assert iae_tuned <= iae_base * 1.1


def test_aborts_if_compressor_stops():
    unit = FineInverter(HEAT, room=19.0, outdoor=5.0)
    tuner, _ = run_tune(unit, 21.0, cfg=TunerConfig(step_size=-6.0))  # a "step" that stops it
    assert tuner.state == FAILED


def test_cancel_on_target_change():
    tuner = AutoTuner()
    tuner.start(0, HEAT, 21.0)
    out = tuner.step(60, HEAT, 22.0, 21.0, UnitState(hz=30), 0.5, 0.0, False)
    assert out is None and tuner.state == FAILED and "target" in tuner.message


def test_per_mode_gains_are_used():
    p = ControllerParams(kp=1.0, heat_kp=3.0)
    assert p.gains(HEAT)[0] == 3.0
    assert p.gains(COOL)[0] == 1.0


class LaggyInverter(FineInverter):
    """Adds ~8 minutes of transport lag between compressor and room."""

    def __init__(self, *a, lag_steps=8, **kw):
        super().__init__(*a, **kw)
        self._hz_hist = [0.0] * lag_steps

    def step(self, dt):
        s = 1 if self.mode == HEAT else -1
        demand = s * (self.setpoint - self.unit_temp)
        if self.on and demand < self.stop:
            self.on = False
        elif not self.on and demand >= self.start:
            self.on = True
        self.hz = max(20.0, min(90.0, 30 + 20 * demand)) if self.on else 0.0
        self._hz_hist.append(self.hz)
        hz_late = self._hz_hist.pop(0)
        power = hz_late / 90.0 * 2.5
        loss = 0.15 * (self.room - self.outdoor)
        self.room += (s * power * 3.0 - loss) * dt / 3600


def test_dead_time_is_measured():
    tuner, _ = run_tune(LaggyInverter(HEAT, room=19.0, outdoor=5.0), 21.0)
    assert tuner.state == DONE, tuner.message
    assert 4 <= tuner.result.theta_minutes <= 12
