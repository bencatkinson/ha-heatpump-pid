"""Setpoint controller for an inverter heat pump that only accepts a setpoint.

This module has no Home Assistant imports so it can be unit tested and reused
by the auto-tuner.

How it works
------------
The indoor unit modulates its compressor on its own idea of the error: the
difference between the setpoint we send and the temperature it measures at
its return-air sensor (``unit_temp``). We call that difference the *demand*:

    demand = s * (unit_setpoint - unit_temp)        s = +1 heat, -1 cool

The unit stops the compressor when demand falls below some threshold, and
starts it again when demand rises above another. Both thresholds are learned
from compressor frequency transitions (the idea comes from E-t0m's
"setpoint guidance"). Holding demand just above the stop threshold keeps the
compressor idling at low frequency instead of cycling on and off.

A PID on the *room* error (from the selectable external sensor) adds to that
idle demand:

    demand = idle + kp*e + I + kd*de/dt + kff*(outdoor feed-forward)

The result is turned back into a setpoint, quantised to the unit's step,
clamped and rate limited. A guard enforces minimum compressor run and off
times, freezes the integrator during defrost, at maximum frequency and at the
discharge temperature limit, and only lets the compressor restart once the
room has drifted past a restart hysteresis.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
import math
from typing import Any

HEAT = "heat"
COOL = "cool"
MODES = (HEAT, COOL)

# Reasons reported with each decision (shown as an entity attribute).
R_PID = "pid"
R_IDLE = "idle"
R_MIN_RUN = "min_run"
R_MIN_OFF = "min_off"
R_WAIT_RESTART = "waiting_restart"
R_DEFROST = "defrost"
R_DISCHARGE = "discharge_limit"
R_FALLBACK = "fallback"
R_AUTOTUNE = "autotune"


@dataclass
class ControllerParams:
    """Tunable parameters. Step 4 (auto-tune) writes kp/ki/kd/kff."""

    kp: float = 1.5  # demand °C per °C of room error
    ki: float = 1.0  # demand °C per °C·hour of room error
    kd: float = 0.0  # demand °C per (°C/hour) of room error change
    kff: float = 0.0  # demand °C per °C of (target - outdoor), heat sense
    deadband: float = 0.1  # °C of room error treated as zero
    restart_hysteresis: float = 0.3  # room must be this far off target to restart
    idle_margin: float = 0.5  # hold demand this far above the learned stop point
    off_margin: float = 1.5  # demand this far below stop point to stop the compressor
    max_boost: float = 4.0  # demand cap above idle
    min_run_s: float = 600.0
    min_off_s: float = 300.0
    setpoint_step: float = 1.0  # unit accepts whole degrees (0.5 if supported)
    min_setpoint: float = 16.0
    max_setpoint: float = 31.0
    max_change: float = 1.0  # largest setpoint move per update, °C
    discharge_limit: float = 90.0  # °C; no more demand above this
    defrost_coil_margin: float = 0.0  # heat: indoor coil below room+margin = defrost
    probe_rate: float = 1.0  # °C/hour to move a threshold the unit is ignoring
    # Per-mode gains written by the auto-tuner. None = use kp/ki/kff above.
    heat_kp: float | None = None
    heat_ki: float | None = None
    heat_kff: float | None = None
    cool_kp: float | None = None
    cool_ki: float | None = None
    cool_kff: float | None = None

    def gains(self, mode: str) -> tuple[float, float, float]:
        """(kp, ki, kff) for a mode, preferring auto-tuned values."""

        def pick(name: str, default: float) -> float:
            value = getattr(self, f"{mode}_{name}", None)
            return default if value is None else value

        return pick("kp", self.kp), pick("ki", self.ki), pick("kff", self.kff)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ControllerParams:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class ModeLearning:
    """What we've learned about the unit in one mode."""

    stop_threshold: float = 0.0  # demand at which the compressor stops
    start_threshold: float = 1.0  # demand at which it starts again
    stops_seen: int = 0
    starts_seen: int = 0


@dataclass
class Learned:
    heat: ModeLearning = field(default_factory=ModeLearning)
    cool: ModeLearning = field(default_factory=ModeLearning)
    max_hz: float = 0.0

    def mode(self, mode: str) -> ModeLearning:
        return self.heat if mode == HEAT else self.cool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Learned:
        if not data:
            return cls()
        out = cls(max_hz=float(data.get("max_hz", 0.0)))
        for name in MODES:
            raw = data.get(name) or {}
            known = {k: v for k, v in raw.items() if k in ModeLearning.__dataclass_fields__}
            setattr(out, name, ModeLearning(**known))
        return out


@dataclass
class UnitState:
    """A snapshot of the heat pump. Every field may be unknown (None)."""

    unit_temp: float | None = None  # the unit's own return-air reading
    unit_setpoint: float | None = None  # what the unit is set to now
    hz: float | None = None  # compressor frequency
    running: bool | None = None  # compressor running (falls back to hz > 0)
    indoor_coil: float | None = None
    outdoor_coil: float | None = None
    outdoor_temp: float | None = None
    discharge: float | None = None
    power: float | None = None  # W, logged for auto-tune
    energy: float | None = None  # kWh counter, logged for auto-tune

    @property
    def compressor_on(self) -> bool | None:
        if self.running is not None:
            return self.running
        if self.hz is not None:
            return self.hz > 0
        return None


@dataclass
class Decision:
    setpoint: float
    demand: float
    reason: str
    error: float
    p: float = 0.0
    i: float = 0.0
    d: float = 0.0
    ff: float = 0.0
    idle: float = 0.0


def _ema(old: float, new: float, count: int, alpha: float = 0.3) -> float:
    if count == 0:
        return new
    return old + alpha * (new - old)


class SetpointController:
    """Turns a room target into a setpoint for a setpoint-only heat pump."""

    def __init__(
        self,
        params: ControllerParams | None = None,
        learned: Learned | None = None,
        history_len: int = 2880,
    ) -> None:
        self.params = params or ControllerParams()
        self.learned = learned or Learned()
        self.integral = 0.0
        self.mode: str | None = None
        self._last_t: float | None = None
        self._last_error: float | None = None
        self._last_cmd: float | None = None
        self._last_demand: float | None = None
        self._prev_on: bool | None = None
        self._on_since: float | None = None
        self._off_since: float | None = None
        self._defrosting = False
        # Rolling samples for diagnostics and the step 4 auto-tuner.
        self.history: deque[dict[str, Any]] = deque(maxlen=history_len)

    # ----------------------------------------------------------------- state

    def reset(self) -> None:
        """Forget the integrator and timers (mode change, turned off)."""
        self.integral = 0.0
        self._last_t = None
        self._last_error = None
        self._last_cmd = None
        self._last_demand = None
        self._defrosting = False

    def restore(self, integral: float | None, mode: str | None) -> None:
        if integral is not None and math.isfinite(integral):
            self.integral = float(integral)
        self.mode = mode

    @property
    def last_demand(self) -> float | None:
        return self._last_demand

    @property
    def defrosting(self) -> bool:
        return self._defrosting

    def compressor_seconds(self, now: float) -> tuple[float | None, float | None]:
        """(seconds running, seconds stopped); one of them is None."""
        run = now - self._on_since if self._on_since is not None else None
        off = now - self._off_since if self._off_since is not None else None
        return run, off

    # -------------------------------------------------------------- learning

    def _track_compressor(self, now: float, unit: UnitState, mode: str) -> None:
        on = unit.compressor_on
        if unit.hz is not None and unit.hz > self.learned.max_hz:
            self.learned.max_hz = float(unit.hz)
        if on is None:
            return
        if self._prev_on is None:
            # First sight: assume the state has lasted long enough already.
            if on:
                self._on_since = now - self.params.min_run_s
            else:
                self._off_since = now - self.params.min_off_s
        elif on != self._prev_on:
            demand_now = self._actual_demand(unit, mode)
            ml = self.learned.mode(mode)
            if on:
                self._on_since, self._off_since = now, None
                last = self._last_demand
                if demand_now is not None and last is not None and demand_now - last <= 1.5:
                    # It started somewhere between the last demand and now.
                    seen = (last + demand_now) / 2 if last < demand_now else demand_now
                    ml.start_threshold = _ema(ml.start_threshold, seen, ml.starts_seen)
                    ml.starts_seen += 1
            else:
                self._off_since, self._on_since = now, None
                if demand_now is not None and not self._defrosting:
                    # The unit stopped somewhere between the last demand and now.
                    seen = demand_now
                    if self._last_demand is not None and self._last_demand > demand_now:
                        seen = (self._last_demand + demand_now) / 2
                    ml.stop_threshold = _ema(ml.stop_threshold, seen, ml.stops_seen)
                    ml.stops_seen += 1
            ml.stop_threshold = max(-4.0, min(3.0, ml.stop_threshold))
            ml.start_threshold = max(ml.stop_threshold + 0.5, min(5.0, ml.start_threshold))
        self._prev_on = on

    @staticmethod
    def _sign(mode: str) -> float:
        return 1.0 if mode == HEAT else -1.0

    def _actual_demand(self, unit: UnitState, mode: str) -> float | None:
        if unit.unit_setpoint is None or unit.unit_temp is None:
            return None
        return self._sign(mode) * (unit.unit_setpoint - unit.unit_temp)

    def _detect_defrost(self, unit: UnitState, room: float, mode: str, run_s: float | None) -> bool:
        if mode != HEAT or not unit.compressor_on or unit.indoor_coil is None:
            return False
        if run_s is None or run_s < 300:
            return False  # coil is still warming up after a start
        return unit.indoor_coil < room + self.params.defrost_coil_margin

    # --------------------------------------------------------------- control

    def fallback(self, target: float) -> float:
        """Setpoint to use when the room sensor can't be trusted."""
        self.reset()
        return self._quantise(target)

    def _quantise(self, value: float) -> float:
        p = self.params
        step = p.setpoint_step if p.setpoint_step > 0 else 1.0
        value = max(p.min_setpoint, min(p.max_setpoint, value))
        return round(round(value / step) * step, 2)

    def update(
        self,
        now: float,
        mode: str,
        target: float,
        room: float,
        unit: UnitState,
        forced_demand: float | None = None,
    ) -> Decision:
        """Compute the setpoint to send. ``now`` is a monotonic time in seconds.

        ``forced_demand`` bypasses the PID and short-cycle guard and holds the
        given demand (open loop). The auto-tuner uses it for its step test.
        """
        p = self.params
        kp, ki, kff = p.gains(mode)
        if mode not in MODES:
            raise ValueError(f"unsupported mode {mode}")
        if mode != self.mode:
            self.reset()
            self.mode = mode

        s = self._sign(mode)
        self._track_compressor(now, unit, mode)
        run_s, off_s = self.compressor_seconds(now)
        on = unit.compressor_on
        ml = self.learned.mode(mode)

        error = s * (target - room)
        if abs(error) < p.deadband:
            eff_error = 0.0
        else:
            eff_error = error - math.copysign(p.deadband, error)

        dt_h = 0.0
        if self._last_t is not None:
            dt_h = max(0.0, min(now - self._last_t, 1800.0)) / 3600.0
        de_dt = 0.0
        if self._last_error is not None and dt_h > 0:
            de_dt = (error - self._last_error) / dt_h

        idle = ml.stop_threshold + p.idle_margin
        floor = ml.stop_threshold - p.off_margin
        ceiling = idle + p.max_boost

        ff = 0.0
        if kff and unit.outdoor_temp is not None:
            ff = kff * s * (target - unit.outdoor_temp)

        p_term = kp * eff_error
        d_term = p.kd * de_dt
        raw = idle + p_term + self.integral + d_term + ff

        reason = R_PID
        freeze = False

        self._defrosting = self._detect_defrost(unit, room, mode, run_s)
        if self._defrosting:
            freeze = True
            reason = R_DEFROST

        at_max_hz = (
            on and unit.hz is not None and self.learned.max_hz > 0 and unit.hz >= 0.95 * self.learned.max_hz
        )
        hot_discharge = unit.discharge is not None and unit.discharge >= p.discharge_limit

        # Anti-windup: don't integrate into a limit we're already pushing on.
        if raw >= ceiling and eff_error > 0:
            freeze = True
        if raw <= floor and eff_error < 0:
            freeze = True
        if at_max_hz and eff_error > 0:
            freeze = True

        demand = max(floor, min(ceiling, raw))

        # Probe: if we're asking for a stop (or start) and the unit ignores it,
        # our threshold estimate is off. Walk it toward the unit's real one.
        probe = p.probe_rate * dt_h
        if (
            forced_demand is None
            and on
            and run_s is not None
            and run_s >= p.min_run_s
            and raw <= floor
            and error <= -p.restart_hysteresis
            and (actual := self._actual_demand(unit, mode)) is not None
            and actual <= floor + 0.25
            and not self._defrosting
        ):
            ml.stop_threshold = max(-4.0, ml.stop_threshold - probe)
        if (
            forced_demand is None
            and on is False
            and off_s is not None
            and off_s >= p.min_off_s + 600
            and self._last_demand is not None
            and self._last_demand >= ml.start_threshold
        ):
            ml.start_threshold = min(5.0, ml.start_threshold + probe)

        if hot_discharge and self._last_demand is not None:
            if demand > self._last_demand:
                demand = self._last_demand
                reason = R_DISCHARGE
                if eff_error > 0:
                    freeze = True

        if forced_demand is not None:
            demand = max(floor, min(ceiling, forced_demand))
            if not (hot_discharge and self._last_demand is not None and demand > self._last_demand):
                reason = R_AUTOTUNE
            else:
                demand = self._last_demand
            freeze = True
        # Short-cycle guard.
        elif on and run_s is not None and run_s < p.min_run_s and demand < idle:
            demand = idle
            reason = R_MIN_RUN
            freeze = freeze or eff_error < 0
        elif on is False:
            want_start = demand > ml.stop_threshold and error >= p.restart_hysteresis
            if off_s is not None and off_s < p.min_off_s:
                demand = min(demand, floor)
                reason = R_MIN_OFF
                freeze = True
            elif not want_start:
                demand = min(demand, floor)
                reason = R_WAIT_RESTART
                freeze = freeze or eff_error > 0
            else:
                # Make sure the unit actually sees enough demand to start.
                demand = max(demand, ml.start_threshold + 0.25)
        elif on and reason == R_PID and abs(demand - idle) < 0.25:
            reason = R_IDLE

        if self._defrosting and self._last_cmd is not None:
            setpoint = self._last_cmd
        else:
            base = unit.unit_temp if unit.unit_temp is not None else room
            # When asking for a stop, round away from demand so the unit
            # really sees it; otherwise round to nearest.
            toward = -s if demand <= floor + 1e-9 else None
            setpoint = self._limit(base + s * demand, toward)

        if not freeze and dt_h > 0:
            self.integral += ki * eff_error * dt_h
            lo, hi = -(p.max_boost + p.off_margin + p.idle_margin), p.max_boost
            self.integral = max(lo, min(hi, self.integral))

        self._last_t = now
        self._last_error = error
        self._last_cmd = setpoint
        self._last_demand = demand

        decision = Decision(
            setpoint=setpoint,
            demand=round(demand, 3),
            reason=reason,
            error=round(error, 3),
            p=round(p_term, 3),
            i=round(self.integral, 3),
            d=round(d_term, 3),
            ff=round(ff, 3),
            idle=round(idle, 3),
        )
        self.history.append(
            {
                "t": round(now, 1),
                "mode": mode,
                "target": target,
                "room": room,
                **{k: v for k, v in asdict(unit).items()},
                "setpoint": setpoint,
                "demand": decision.demand,
                "reason": reason,
                "i": decision.i,
            }
        )
        return decision

    def _limit(self, wanted: float, toward: float | None = None) -> float:
        """Clamp, rate limit and quantise with a little hysteresis.

        ``toward`` < 0 rounds down, > 0 rounds up, None rounds to nearest.
        """
        p = self.params
        step = p.setpoint_step if p.setpoint_step > 0 else 1.0
        if self._last_cmd is not None:
            delta = wanted - self._last_cmd
            if toward is None and abs(delta) < 0.6 * step:
                return self._last_cmd
            delta = max(-p.max_change, min(p.max_change, delta))
            wanted = self._last_cmd + delta
        if toward is None:
            return self._quantise(wanted)
        q = wanted / step
        q = math.floor(q + 1e-6) if toward < 0 else math.ceil(q - 1e-6)
        return self._quantise(q * step)
