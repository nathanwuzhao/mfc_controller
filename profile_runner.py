"""
profile_runner.py

Profile execution layer for Alicat MFC flow control.

This module assumes you already have alicat_mfc.py containing:
    - AlicatMFC
    - MFCReading

Main concept:
    ProfileRunner repeatedly computes q_cmd = profile(t),
    clamps it to safe flow limits, sends it to the MFC, polls the MFC,
    and logs the result.

The profile output should be in the same engineering units currently
configured on the Alicat, usually SLPM.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional, List, Tuple, Dict, Any
import csv
import math
import time
import traceback

from alicat_mfc import AlicatMFC, MFCReading, AlicatError


FlowProfile = Callable[[float], float]


def constant_profile(value: float) -> FlowProfile:
    def profile(t: float) -> float:
        return value
    return profile


def ramp_profile(
    start: float,
    stop: float,
    duration_s: float,
    hold_after: bool = True,
) -> FlowProfile:
    """
    linear ramp from start to stop over duration_s

    if hold_after=True, profile stays at stop after duration_s.
    if hold_after=False, profile returns 0 after duration_s.
    """
    if duration_s <= 0:
        raise ValueError("duration_s must be positive.")

    def profile(t: float) -> float:
        if t < 0:
            return start
        if t <= duration_s:
            alpha = t / duration_s
            return start + alpha * (stop - start)
        return stop if hold_after else 0.0

    return profile


def gaussian_profile(
    baseline: float,
    amplitude: float,
    center_s: float,
    sigma_s: float,
) -> FlowProfile:
    """
    gaussian flow pulse.

    q(t) = baseline + amplitude * exp(-0.5 * ((t - center) / sigma)^2)
    """
    if sigma_s <= 0:
        raise ValueError("sigma_s must be positive")

    def profile(t: float) -> float:
        z = (t - center_s) / sigma_s
        return baseline + amplitude * math.exp(-0.5 * z * z)

    return profile


def exponential_decay_profile(
    start: float,
    tau_s: float,
    baseline: float = 0.0,
) -> FlowProfile:
    """
    exponential decay flow profile.

    q(t) = baseline + start * exp(-t / tau)
    """
    if tau_s <= 0:
        raise ValueError("tau_s must be positive")

    def profile(t: float) -> float:
        if t < 0:
            return baseline + start
        return baseline + start * math.exp(-t / tau_s)

    return profile


def exponential_rise_profile(
    final: float,
    tau_s: float,
    initial: float = 0.0,
) -> FlowProfile:
    """
    exponential rise profile.

    q(t) = final - (final - initial) * exp(-t / tau)
    """
    if tau_s <= 0:
        raise ValueError("tau_s must be positive")

    def profile(t: float) -> float:
        if t < 0:
            return initial
        return final - (final - initial) * math.exp(-t / tau_s)

    return profile


def sine_profile(
    baseline: float,
    amplitude: float,
    frequency_hz: float,
    phase_rad: float = 0.0,
) -> FlowProfile:
    """
    sinusoidal flow profile

    q(t) = baseline + amplitude * sin(2*pi*f*t + phase)
    """
    if frequency_hz < 0:
        raise ValueError("frequency_hz must be nonnegative")

    def profile(t: float) -> float:
        return baseline + amplitude * math.sin(
            2.0 * math.pi * frequency_hz * t + phase_rad
        )

    return profile


def piecewise_linear_profile(
    points: List[Tuple[float, float]],
    hold_before: bool = True,
    hold_after: bool = True,
) -> FlowProfile:
    """
    piecewise-linear profile from time-flow points

    Example:
        points = [
            (0.0, 0.0),
            (5.0, 0.2),
            (10.0, 0.2),
            (15.0, 0.0),
        ]

    times must be increasing
    """
    if len(points) < 2:
        raise ValueError("at least two points are required.")

    points = sorted(points, key=lambda x: x[0])

    for i in range(1, len(points)):
        if points[i][0] <= points[i - 1][0]:
            raise ValueError("point times must be strictly increasing.")

    def profile(t: float) -> float:
        if t <= points[0][0]:
            return points[0][1] if hold_before else 0.0

        if t >= points[-1][0]:
            return points[-1][1] if hold_after else 0.0

        for i in range(1, len(points)):
            t0, q0 = points[i - 1]
            t1, q1 = points[i]

            if t0 <= t <= t1:
                alpha = (t - t0) / (t1 - t0)
                return q0 + alpha * (q1 - q0)

        # should never reach this if points are valid
        return 0.0

    return profile


@dataclass
class ProfileRunnerConfig:
    """
    config for running a flow profile

    duration_s:
        total experiment duration

    control_period_s:
        how often to send a new setpoint

    poll_period_s:
        how often to poll MFC data, can be same as control_period_s

    min_flow:
        minimum allowed commanded flow

    max_flow:
        maximum allowed commanded flow

    send_if_change_greater_than:
        avoids sending redundant setpoint commands if change is tiny

    zero_on_start:
        send zero-flow before starting profile

    zero_on_finish:
        send zero-flow after profile completes or errors

    settle_s:
        optional wait after zero_on_start before beginning profile.

    log_path:
        csv output path. if None, does not write CSV
    """
    duration_s: float
    control_period_s: float = 0.2
    poll_period_s: float = 0.2

    min_flow: float = 0.0
    max_flow: float = 1.0

    send_if_change_greater_than: float = 1e-5

    zero_on_start: bool = True
    zero_on_finish: bool = True
    settle_s: float = 1.0

    log_path: Optional[Path] = Path("profile_log.csv")


@dataclass
class ProfileLogRow:
    t_s: float
    q_target: float
    q_commanded: float
    command_sent: bool

    raw: Optional[str] = None
    unit_id: Optional[str] = None
    abs_pressure: Optional[float] = None
    temperature: Optional[float] = None
    volumetric_flow: Optional[float] = None
    mass_flow: Optional[float] = None
    setpoint: Optional[float] = None
    totalizer: Optional[float] = None
    gas: Optional[str] = None

    error: Optional[str] = None

class ProfileRunner:
    def __init__(
        self,
        mfc: AlicatMFC,
        profile: FlowProfile,
        config: ProfileRunnerConfig,
    ) -> None:
        self.mfc = mfc
        self.profile = profile
        self.config = config

        self.rows: List[ProfileLogRow] = []
        self._stop_requested = False

        self._validate_config()

    def _validate_config(self) -> None:
        cfg = self.config

        if cfg.duration_s <= 0:
            raise ValueError("duration_s must be positive.")
        if cfg.control_period_s <= 0:
            raise ValueError("control_period_s must be positive.")
        if cfg.poll_period_s <= 0:
            raise ValueError("poll_period_s must be positive.")
        if cfg.min_flow < 0:
            raise ValueError("min_flow should usually be nonnegative.")
        if cfg.max_flow <= cfg.min_flow:
            raise ValueError("max_flow must be greater than min_flow.")
        if cfg.send_if_change_greater_than < 0:
            raise ValueError("send_if_change_greater_than must be nonnegative.")

    def request_stop(self) -> None:
        self._stop_requested = True

    def clamp_flow(self, q: float) -> float:
        return max(self.config.min_flow, min(self.config.max_flow, q))

    def run(self) -> List[ProfileLogRow]:
        """
        Execute the profile.

        Returns
        -------
        rows:
            List of logged rows.
        """
        cfg = self.config

        if not self.mfc.is_connected:
            self.mfc.connect()

        if cfg.zero_on_start:
            self.mfc.zero_flow()
            time.sleep(cfg.settle_s)

        self.rows = []
        self._stop_requested = False

        last_commanded: Optional[float] = None
        next_control_t = 0.0
        next_poll_t = 0.0

        start_monotonic = time.monotonic()

        try:
            while True:
                now = time.monotonic()
                t_s = now - start_monotonic

                if t_s >= cfg.duration_s:
                    break

                if self._stop_requested:
                    break

                should_control = t_s >= next_control_t
                should_poll = t_s >= next_poll_t

                q_target = self.profile(t_s)
                q_commanded = self.clamp_flow(q_target)

                command_sent = False
                error: Optional[str] = None

                if should_control:
                    should_send = (
                        last_commanded is None
                        or abs(q_commanded - last_commanded)
                        >= cfg.send_if_change_greater_than
                    )

                    if should_send:
                        try:
                            self.mfc.set_setpoint(q_commanded)
                            last_commanded = q_commanded
                            command_sent = True
                        except Exception as exc:
                            error = f"setpoint_error: {type(exc).__name__}: {exc}"
                            raise

                    next_control_t += cfg.control_period_s

                    # If the loop fell behind, resynchronize instead of trying
                    # to spam many old commands.
                    if next_control_t < t_s - cfg.control_period_s:
                        next_control_t = t_s + cfg.control_period_s

                reading: Optional[MFCReading] = None

                if should_poll:
                    try:
                        reading = self.mfc.poll()
                    except Exception as exc:
                        error = f"poll_error: {type(exc).__name__}: {exc}"
                        # Poll failure should be logged, but not necessarily
                        # fatal. You can change this behavior if desired.
                        reading = None

                    next_poll_t += cfg.poll_period_s

                    if next_poll_t < t_s - cfg.poll_period_s:
                        next_poll_t = t_s + cfg.poll_period_s

                    self.rows.append(
                        self._make_log_row(
                            t_s=t_s,
                            q_target=q_target,
                            q_commanded=q_commanded,
                            command_sent=command_sent,
                            reading=reading,
                            error=error,
                        )
                    )

                sleep_s = min(next_control_t, next_poll_t) - (
                    time.monotonic() - start_monotonic
                )
                if sleep_s > 0:
                    time.sleep(min(sleep_s, 0.05))

        except KeyboardInterrupt:
            self.rows.append(
                ProfileLogRow(
                    t_s=time.monotonic() - start_monotonic,
                    q_target=0.0,
                    q_commanded=0.0,
                    command_sent=False,
                    error="KeyboardInterrupt",
                )
            )
            raise

        except Exception as exc:
            self.rows.append(
                ProfileLogRow(
                    t_s=time.monotonic() - start_monotonic,
                    q_target=0.0,
                    q_commanded=0.0,
                    command_sent=False,
                    error=f"fatal_error: {type(exc).__name__}: {exc}\n"
                          f"{traceback.format_exc()}",
                )
            )
            raise

        finally:
            if cfg.zero_on_finish:
                try:
                    self.mfc.zero_flow()
                except Exception:
                    pass

            if cfg.log_path is not None:
                self.write_csv(cfg.log_path)

        return self.rows

    def _make_log_row(
        self,
        t_s: float,
        q_target: float,
        q_commanded: float,
        command_sent: bool,
        reading: Optional[MFCReading],
        error: Optional[str] = None,
    ) -> ProfileLogRow:
        if reading is None:
            return ProfileLogRow(
                t_s=t_s,
                q_target=q_target,
                q_commanded=q_commanded,
                command_sent=command_sent,
                error=error,
            )

        return ProfileLogRow(
            t_s=t_s,
            q_target=q_target,
            q_commanded=q_commanded,
            command_sent=command_sent,
            raw=reading.raw,
            unit_id=reading.unit_id,
            abs_pressure=reading.abs_pressure,
            temperature=reading.temperature,
            volumetric_flow=reading.volumetric_flow,
            mass_flow=reading.mass_flow,
            setpoint=reading.setpoint,
            totalizer=reading.totalizer,
            gas=reading.gas,
            error=error,
        )

    def write_csv(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if not self.rows:
            return

        fieldnames = list(asdict(self.rows[0]).keys())

        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for row in self.rows:
                writer.writerow(asdict(row))