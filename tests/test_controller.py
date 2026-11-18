"""Simulated-plant tests for the setpoint controller."""

from custom_components.heatpump_pid.controller import (
    COOL,
    HEAT,
    R_MIN_OFF,
    R_MIN_RUN,
    ControllerParams,
    SetpointController,
    UnitState,
)


class FakeInverter:
    """Crude TCL-like unit: modulates on (setpoint - own temp), with hysteresis."""

    def __init__(self, mode=HEAT, room=18.0, outdoor=5.0, stop=-1.0, start=1.0):
        self.mode, self.room, self.outdoor = mode, room, outdoor
        self.stop, self.start = stop, start
        self.setpoint = room
        self.on = False
        self.hz = 0.0
        self.offset = 1.0 if mode == HEAT else -1.0  # unit sensor reads warmer/cooler

    @property
    def unit_temp(self):
        return round(self.room + self.offset)  # whole degrees, like tclac

    def step(self, dt):
        s = 1 if self.mode == HEAT else -1
        demand = s * (self.setpoint - self.unit_temp)
        if self.on and demand < self.stop:
            self.on = False
        elif not self.on and demand >= self.start:
            self.on = True
        self.hz = max(20.0, min(90.0, 30 + 20 * demand)) if self.on else 0.0
        power = self.hz / 90.0 * 2.5 if self.on else 0.0  # °C/h of capacity at full
        loss = 0.15 * (self.room - self.outdoor)  # °C/h
        self.room += (s * power * 3.0 - loss if self.mode == HEAT else -power * 3.0 - loss) * dt / 3600

    def state(self):
        return UnitState(unit_temp=self.unit_temp, unit_setpoint=self.setpoint, hz=self.hz)


def run(ctrl, unit, target, hours, dt=60):
    starts = 0
    prev = unit.on
    temps = []
    for i in range(int(hours * 3600 / dt)):
        t = i * dt
        d = ctrl.update(t, unit.mode, target, unit.room, unit.state())
        unit.setpoint = d.setpoint
        unit.step(dt)
        if unit.on and not prev:
            starts += 1
        prev = unit.on
        temps.append(unit.room)
    return starts, temps


def test_heating_settles_near_target():
    ctrl = SetpointController()
    unit = FakeInverter(HEAT, room=17.0, outdoor=5.0)
    starts, temps = run(ctrl, unit, 21.0, 8)
    last_hour = temps[-60:]
    assert abs(sum(last_hour) / len(last_hour) - 21.0) < 0.4
    assert max(last_hour) - min(last_hour) < 1.0
    assert starts <= 8  # no short cycling


def test_cooling_settles_near_target():
    ctrl = SetpointController()
    unit = FakeInverter(COOL, room=28.0, outdoor=33.0, stop=-1.0, start=1.0)
    starts, temps = run(ctrl, unit, 24.0, 8)
    last_hour = temps[-60:]
    assert abs(sum(last_hour) / len(last_hour) - 24.0) < 0.5
    assert starts <= 8


def test_learns_stop_threshold():
    ctrl = SetpointController()
    # Light load: the unit has to cycle, which teaches the controller.
    unit = FakeInverter(HEAT, room=20.0, outdoor=18.0, stop=-2.0, start=0.0)
    run(ctrl, unit, 21.0, 12)
    assert ctrl.learned.heat.stops_seen > 0
    assert -3.0 < ctrl.learned.heat.stop_threshold < -0.5
    assert ctrl.learned.max_hz > 0


def test_min_run_holds_compressor_on():
    p = ControllerParams(min_run_s=600)
    ctrl = SetpointController(p)
    # Prime: compressor was off.
    ctrl.update(0, HEAT, 21.0, 20.0, UnitState(unit_temp=21, unit_setpoint=21, hz=0))
    # It just started, and the room is now well above target.
    d = ctrl.update(60, HEAT, 21.0, 23.0, UnitState(unit_temp=24, unit_setpoint=22, hz=40))
    assert d.reason == R_MIN_RUN
    assert d.demand >= ctrl.learned.heat.stop_threshold + p.idle_margin - 1e-9


def test_min_off_keeps_compressor_off():
    p = ControllerParams(min_off_s=300)
    ctrl = SetpointController(p)
    ctrl.update(0, HEAT, 21.0, 21.0, UnitState(unit_temp=22, unit_setpoint=22, hz=40))
    d = ctrl.update(60, HEAT, 21.0, 19.0, UnitState(unit_temp=20, unit_setpoint=19, hz=0))
    assert d.reason == R_MIN_OFF
    assert d.demand <= ctrl.learned.heat.stop_threshold - p.off_margin + 1e-9


def test_setpoint_quantised_clamped_and_rate_limited():
    p = ControllerParams(setpoint_step=1.0, max_change=1.0, min_setpoint=16, max_setpoint=31)
    ctrl = SetpointController(p)
    first = ctrl.update(0, HEAT, 30.0, 10.0, UnitState(unit_temp=30, unit_setpoint=20, hz=50))
    assert first.setpoint == 31.0
    second = ctrl.update(60, HEAT, 16.0, 30.0, UnitState(unit_temp=16, unit_setpoint=31, hz=50))
    assert second.setpoint == 30.0  # only one degree per update
    assert float(second.setpoint).is_integer()


def test_defrost_freezes_integrator_and_holds_setpoint():
    ctrl = SetpointController()
    ctrl.update(0, HEAT, 21.0, 20.0, UnitState(unit_temp=21, unit_setpoint=22, hz=50, indoor_coil=40))
    ctrl.update(600, HEAT, 21.0, 20.0, UnitState(unit_temp=21, unit_setpoint=22, hz=50, indoor_coil=40))
    i_before = ctrl.integral
    held = ctrl._last_cmd
    d = ctrl.update(660, HEAT, 21.0, 20.0, UnitState(unit_temp=21, unit_setpoint=22, hz=50, indoor_coil=5))
    assert d.reason == "defrost"
    assert ctrl.integral == i_before
    assert d.setpoint == held


def test_discharge_limit_blocks_more_demand():
    ctrl = SetpointController(ControllerParams(discharge_limit=90))
    first = ctrl.update(0, HEAT, 21.0, 20.0, UnitState(unit_temp=21, unit_setpoint=22, hz=50, discharge=70))
    d = ctrl.update(60, HEAT, 25.0, 18.0, UnitState(unit_temp=21, unit_setpoint=22, hz=50, discharge=95))
    assert d.reason == "discharge_limit"
    assert d.demand <= first.demand


def test_learning_round_trips():
    from custom_components.heatpump_pid.controller import Learned

    ctrl = SetpointController()
    ctrl.learned.heat.stop_threshold = -1.7
    ctrl.learned.max_hz = 88
    again = Learned.from_dict(ctrl.learned.as_dict())
    assert again.heat.stop_threshold == -1.7
    assert again.max_hz == 88


def test_half_degree_steps():
    ctrl = SetpointController(ControllerParams(setpoint_step=0.5))
    seen = set()
    room = 20.3
    for i in range(30):
        d = ctrl.update(i * 60, HEAT, 21.0, room, UnitState(unit_temp=room + 0.7, unit_setpoint=21, hz=30))
        seen.add(d.setpoint)
        room += 0.03
    assert all((sp * 2).is_integer() for sp in seen)
    assert any(not sp.is_integer() for sp in seen), "0.5 °C steps should be used"
