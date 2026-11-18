"""Auto-tuner: a step test on the unit's demand, then SIMC PI gains.

No Home Assistant imports. The climate entity calls :meth:`AutoTuner.step`
every control cycle while a tune runs, and passes the returned demand (or
None) to :meth:`SetpointController.update` as ``forced_demand``.

The test, run separately for heating and cooling:

1. **Settle.** Normal control until the room has stayed within
   ``settle_band`` of target, with the compressor running, for
   ``settle_minutes``.
2. **Baseline.** Hold the demand the controller had settled on, open loop,
   for ``baseline_minutes``. The room's drift here is removed from the step.
3. **Step.** Add ``step_size`` °C of demand and hold it. Record the room,
   compressor frequency and power until the room has moved ``stop_rise`` °C
   or ``step_minutes`` pass.
4. **Fit.** Fit a first-order-plus-dead-time model (gain K, time constant
   tau, dead time theta) to the room's response. If the response is still a
   ramp at the end of the window, fit an integrating model instead (slope k
   and theta). Gains then follow the SIMC rules with closed-loop time
   constant tau_c = max(theta, min_tau_c) * aggressiveness:

   * FOPDT:       Kc = tau / (K (tau_c + theta)),  Ti = min(tau, 4 (tau_c + theta))
   * integrating: Kc = 1 / (k (tau_c + theta)),    Ti = 4 (tau_c + theta)

   ki = Kc / Ti (per hour). The compressor frequency response gives a sanity
   check (the unit must actually change speed) and the frequency gain.

It aborts, leaving the gains alone, if the compressor stops, a defrost
starts, the room strays more than ``max_deviation`` from target, or anything
outside the test changes (mode, target, manual override, stale sensors).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any

from .controller import HEAT, UnitState

IDLE = "idle"
SETTLING = "settling"
BASELINE = "baseline"
STEP = "step"
DONE = "done"
FAILED = "failed"
RUNNING = (SETTLING, BASELINE, STEP)


@dataclass
class TunerConfig:
    step_size: float = 1.5  # °C of demand added for the step
    settle_band: float = 0.3  # °C
    settle_minutes: float = 15.0
    settle_timeout_minutes: float = 120.0
    baseline_minutes: float = 10.0
    step_minutes: float = 75.0
    stop_rise: float = 1.0  # °C: end the step early once the room moved this far
    max_deviation: float = 1.5  # °C from target in the wrong direction: abort
    min_response: float = 0.2  # °C: less than this is too small to fit
    min_r2: float = 0.6
    min_tau_c_minutes: float = 10.0
    aggressiveness: float = 1.0  # >1 is gentler, <1 is faster
    min_hz_change: float = 3.0


@dataclass
class TuneResult:
    mode: str
    kp: float
    ki: float
    model: str  # "fopdt" or "integrating"
    gain: float | None  # °C room per °C demand (fopdt)
    slope: float | None  # °C/h per °C demand (integrating)
    tau_minutes: float | None
    theta_minutes: float
    r2: float
    hz_gain: float | None  # Hz per °C demand
    hz_delay_minutes: float | None
    baseline_demand: float
    samples: int

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


@dataclass
class _Sample:
    t: float  # seconds since phase start
    y: float  # room, signed so the step drives it up, relative to target
    hz: float | None
    power: float | None


@dataclass
class AutoTuner:
    config: TunerConfig = field(default_factory=TunerConfig)
    state: str = IDLE
    mode: str | None = None
    message: str = ""
    result: TuneResult | None = None
    _target: float | None = None
    _phase_t0: float = 0.0
    _settled_since: float | None = None
    _d0: float = 0.0
    _i0: float = 0.0
    _baseline: list[_Sample] = field(default_factory=list)
    _step: list[_Sample] = field(default_factory=list)

    # --------------------------------------------------------------- control

    @property
    def running(self) -> bool:
        return self.state in RUNNING

    def start(self, now: float, mode: str, target: float) -> None:
        self.state = SETTLING
        self.mode = mode
        self._target = target
        self._phase_t0 = now
        self._settled_since = None
        self._baseline, self._step = [], []
        self.result = None
        self.message = "waiting for the room to settle at target"

    def cancel(self, why: str) -> None:
        if self.running:
            self.state = FAILED
            self.message = f"cancelled: {why}"

    @property
    def restore_integral(self) -> float:
        """Integrator value to hand back to the controller after the test."""
        return self._i0

    def step(
        self,
        now: float,
        mode: str,
        target: float,
        room: float,
        unit: UnitState,
        demand_now: float | None,
        integral_now: float,
        defrosting: bool,
    ) -> float | None:
        """Advance the test. Returns a demand to hold, or None for normal control."""
        if not self.running:
            return None
        cfg = self.config
        if mode != self.mode:
            self.cancel("mode changed")
            return None
        if self._target is not None and abs(target - self._target) > 1e-6:
            self.cancel("target changed")
            return None
        s = 1.0 if mode == HEAT else -1.0
        y = s * (room - target)
        on = unit.compressor_on
        elapsed = now - self._phase_t0

        if self.state == SETTLING:
            steady = on is not False and abs(y) <= cfg.settle_band and not defrosting
            if not steady:
                self._settled_since = None
            elif self._settled_since is None:
                self._settled_since = now
            if self._settled_since is not None and now - self._settled_since >= cfg.settle_minutes * 60:
                if demand_now is None:
                    return None
                self._d0, self._i0 = demand_now, integral_now
                self._enter(BASELINE, now, "holding the settled demand to measure drift")
                return self._d0
            if elapsed > cfg.settle_timeout_minutes * 60:
                self._fail("the room didn't settle near target with the compressor running")
            return None

        # Open-loop phases.
        if on is False:
            self._fail("the compressor stopped during the test")
            return None
        if defrosting:
            self._fail("a defrost started during the test")
            return None
        if y < -cfg.max_deviation or y > cfg.max_deviation + cfg.stop_rise:
            self._fail("the room moved too far from target")
            return None
        sample = _Sample(elapsed, y, unit.hz, unit.power)

        if self.state == BASELINE:
            self._baseline.append(sample)
            if elapsed >= cfg.baseline_minutes * 60:
                self._enter(STEP, now, f"step test: +{cfg.step_size} °C demand")
                return self._d0 + cfg.step_size
            return self._d0

        # STEP
        self._step.append(sample)
        y0 = self._baseline[-1].y if self._baseline else self._step[0].y
        if elapsed >= cfg.step_minutes * 60 or y - y0 >= cfg.stop_rise:
            self._finish()
            return None
        return self._d0 + cfg.step_size

    def _enter(self, state: str, now: float, message: str) -> None:
        self.state = state
        self._phase_t0 = now
        self.message = message

    def _fail(self, why: str) -> None:
        self.state = FAILED
        self.message = why

    # -------------------------------------------------------------- analysis

    def _finish(self) -> None:
        try:
            self.result = analyse(self.mode or HEAT, self._baseline, self._step, self.config, self._d0)
        except TuneError as err:
            self._fail(str(err))
            return
        self.state = DONE
        r = self.result
        self.message = f"done: kp {r.kp:.2f}, ki {r.ki:.2f}/h ({r.model}, R² {r.r2:.2f})"


class TuneError(Exception):
    """The step response couldn't be turned into gains."""


def _linfit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least-squares (slope, intercept)."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    return slope, my - slope * mx


def _fit_through_origin(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Best k for y = k*x, and the residual sum of squares."""
    sxx = sum(x * x for x in xs)
    if sxx <= 0:
        return 0.0, sum(y * y for y in ys)
    k = sum(x * y for x, y in zip(xs, ys, strict=True)) / sxx
    return k, sum((y - k * x) ** 2 for x, y in zip(xs, ys, strict=True))


def analyse(
    mode: str,
    baseline: list[_Sample],
    step: list[_Sample],
    cfg: TunerConfig,
    d0: float,
) -> TuneResult:
    if len(step) < 10:
        raise TuneError("not enough samples in the step")

    # Remove the baseline drift (°C/h) and the starting offset.
    if len(baseline) >= 3:
        drift, _ = _linfit([b.t / 3600 for b in baseline], [b.y for b in baseline])
        y0 = baseline[-1].y
    else:
        drift, y0 = 0.0, step[0].y
    th = [p.t / 3600 for p in step]  # hours
    yc = [p.y - y0 - drift * t for p, t in zip(step, th, strict=True)]
    window = th[-1]
    response = max(yc)
    if response < cfg.min_response:
        raise TuneError(f"the room barely responded ({response:.2f} °C); try a bigger step")

    ss_tot = sum((y - sum(yc) / len(yc)) ** 2 for y in yc) or 1e-9
    du = cfg.step_size

    theta_grid = [m / 60 for m in range(0, int(min(30, window * 60 / 2)) + 1)]
    tau_grid = [math.exp(math.log(2 / 60) + i * (math.log(6.0) - math.log(2 / 60)) / 60) for i in range(61)]

    best_f = (math.inf, 0.0, 0.0, 0.0)  # sse, K, tau, theta
    for theta in theta_grid:
        for tau in tau_grid:
            xs = [1 - math.exp(-(t - theta) / tau) if t > theta else 0.0 for t in th]
            k, sse = _fit_through_origin(xs, yc)
            if k > 0 and sse < best_f[0]:
                best_f = (sse, k, tau, theta)

    best_i = (math.inf, 0.0, 0.0)  # sse, slope, theta
    for theta in theta_grid:
        xs = [max(0.0, t - theta) for t in th]
        k, sse = _fit_through_origin(xs, yc)
        if k > 0 and sse < best_i[0]:
            best_i = (sse, k, theta)

    # Prefer FOPDT when its time constant is resolved inside the window.
    use_fopdt = best_f[0] < math.inf and best_f[2] < 0.8 * window
    if use_fopdt:
        sse, k, tau, theta = best_f
        model = "fopdt"
    elif best_i[0] < math.inf:
        sse, slope, theta = best_i
        model = "integrating"
    else:
        raise TuneError("couldn't fit the response")
    r2 = 1 - sse / ss_tot
    if r2 < cfg.min_r2:
        raise TuneError(f"the response was too noisy to fit (R² {r2:.2f})")

    tau_c = max(theta, cfg.min_tau_c_minutes / 60) * cfg.aggressiveness
    if model == "fopdt":
        gain = k / du
        kc = tau / (gain * (tau_c + theta))
        ti = min(tau, 4 * (tau_c + theta))
        slope_out, gain_out, tau_out = None, gain, tau * 60
    else:
        per = slope / du
        kc = 1 / (per * (tau_c + theta))
        ti = 4 * (tau_c + theta)
        slope_out, gain_out, tau_out = per, None, None
    kp = max(0.2, min(6.0, kc))
    ki = max(0.05, min(6.0, kc / ti))

    # Compressor response: it has to have actually changed speed.
    hz_gain = hz_delay = None
    base_hz = [b.hz for b in baseline if b.hz is not None]
    step_hz = [(p.t, p.hz) for p in step if p.hz is not None]
    if base_hz and step_hz:
        h0 = sum(base_hz) / len(base_hz)
        tail = [h for _, h in step_hz[-max(3, len(step_hz) // 4) :]]
        dh = sum(tail) / len(tail) - h0
        if abs(dh) < cfg.min_hz_change:
            raise TuneError(
                "the compressor didn't change speed; it may be at its limit. "
                "Try when the weather is milder, or a bigger step"
            )
        hz_gain = dh / du
        hz_delay = next((t / 60 for t, h in step_hz if abs(h - h0) >= cfg.min_hz_change), None)

    return TuneResult(
        mode=mode,
        kp=round(kp, 3),
        ki=round(ki, 3),
        model=model,
        gain=gain_out,
        slope=slope_out,
        tau_minutes=tau_out,
        theta_minutes=theta * 60,
        r2=r2,
        hz_gain=hz_gain,
        hz_delay_minutes=hz_delay,
        baseline_demand=d0,
        samples=len(step),
    )
