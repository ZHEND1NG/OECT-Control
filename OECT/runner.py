from __future__ import annotations

import copy
import math
import threading
import time
from typing import Callable, Iterable, Optional

from .data_logger import DataLogger
from .instruments import create_rig
from .models import (
    CycleParams,
    CycleReferenceParams,
    ExperimentMode,
    InstrumentConfig,
    LinearParams,
    LinearReferenceParams,
    MeasurementSample,
)


StatusCallback = Callable[[str], None]
SampleCallback = Callable[[MeasurementSample], None]


class _RestartSequence(Exception):
    """A paused user changed the sweep endpoints; start a new logged segment."""


class ExperimentRunner:
    def __init__(
        self,
        config: InstrumentConfig,
        status_callback: Optional[StatusCallback] = None,
        sample_callback: Optional[SampleCallback] = None,
        pause_callback: Optional[Callable[[bool], None]] = None,
    ):
        self.config = config
        self.status_callback = status_callback
        self.sample_callback = sample_callback
        self.stop_event = threading.Event()
        self.rig = None
        self.logger: Optional[DataLogger] = None
        self.start_time = 0.0
        self.samples = 0
        self.last_gate_voltage: Optional[float] = None
        self.current_drain_voltage = 0.0
        self.pause_callback = pause_callback
        self.pause_requested = threading.Event()
        self.paused_event = threading.Event()
        self._pause_condition = threading.Condition()
        self._pending_params = None
        self._active_params = None
        self._paused_at = None
        self._paused_seconds = 0.0
        self._params_revision = 0

    def stop(self) -> None:
        self.stop_event.set()
        with self._pause_condition:
            self._pause_condition.notify_all()

    def pause(self) -> None:
        self.pause_requested.set()

    def resume(self, params=None) -> None:
        """Validate in the caller; instrument commands remain in the run thread."""
        with self._pause_condition:
            if not self.paused_event.is_set() or self.stop_event.is_set():
                raise ValueError("The experiment must be paused before continuing.")
            candidate = copy.deepcopy(self._active_params if params is None else params)
            if type(candidate) is not type(self._active_params):
                raise ValueError("Experiment mode cannot change during a run.")
            for name in ("reference_control_enabled", "initial_gate_voltage"):
                if getattr(candidate, name, None) != getattr(self._active_params, name, None):
                    raise ValueError(f"{name} cannot change during a run.")
            # The GUI permits a final run name, but the open output file stays put.
            candidate.run_name = self._active_params.run_name
            candidate.output_dir = self._active_params.output_dir
            candidate.validate()
            if hasattr(candidate, "gate_min_voltage") and self.last_gate_voltage is not None:
                if not candidate.gate_min_voltage <= self.last_gate_voltage <= candidate.gate_max_voltage:
                    raise ValueError("Current Gate voltage must be inside the new Gate limits.")
            self._pending_params = candidate
            self._pause_condition.notify_all()

    def _clock(self) -> float:
        raw = time.monotonic() if self._paused_at is None else self._paused_at
        return raw - self._paused_seconds

    def _event(self, kind: str, **details) -> None:
        if self.logger is not None:
            self.logger.record_event(kind, max(0.0, self._clock() - self.start_time), self.samples, **details)

    def _checkpoint(self) -> bool:
        """Acknowledge pause only between I/O operations; return whether it paused."""
        if not self.pause_requested.is_set() or self.stop_event.is_set():
            return False
        restart = False
        with self._pause_condition:
            self._paused_at = time.monotonic()
            self._event("paused")
            self._status("Paused. Sampling and feedback stopped; instrument outputs are held.")
            self.paused_event.set()
            if self.pause_callback:
                self.pause_callback(True)
            while self._pending_params is None and not self.stop_event.is_set():
                self._pause_condition.wait(timeout=0.1)
            if not self.stop_event.is_set():
                candidate = self._pending_params
                before, after = self._active_params.as_dict(), candidate.as_dict()
                changed = {name: {"old": before[name], "new": value}
                           for name, value in after.items() if before[name] != value}
                if isinstance(candidate, LinearParams):
                    shape = (("vref_start", "vref_end", "vref_intermediate_steps")
                             if candidate.reference_control_enabled else ("gate_start", "gate_end", "gate_step"))
                else:
                    shape = (("vref_initial", "vref_target") if candidate.reference_control_enabled
                             else ("gate_start", "gate_min", "gate_max", "gate_step"))
                restart = any(name in changed for name in shape)
                if changed:
                    self._event("parameters_changed", changes=changed, params=after,
                                restart_sequence=restart)
                    vars(self._active_params).update(vars(candidate))
                    self._params_revision += 1
                self._event("resumed", pause_duration_s=time.monotonic() - self._paused_at)
            else:
                self._event("stopped_while_paused")
            self._paused_seconds += time.monotonic() - self._paused_at
            self._paused_at = None
            self._pending_params = None
            self.pause_requested.clear()
            self.paused_event.clear()
        if self.stop_event.is_set():
            return True
        if self.current_drain_voltage != self._active_params.drain_voltage:
            self.rig.set_drain_voltage(self._active_params.drain_voltage)
            self.current_drain_voltage = self._active_params.drain_voltage
        self._status("Resumed with a new sweep segment." if restart else "Resumed from the paused position.")
        if self.pause_callback:
            self.pause_callback(False)
        if restart:
            raise _RestartSequence()
        return True

    def _run_sequence(self, function, params) -> None:
        while not self.stop_event.is_set():
            try:
                self._checkpoint()
                if not self.stop_event.is_set():
                    function(params)
                return
            except _RestartSequence:
                continue

    def run_linear(self, params: LinearParams) -> str:
        return self._run_experiment(ExperimentMode.LINEAR, params,
                                    self._linear_sequence, self._linear_reference_sequence)

    def run_cycle(self, params: CycleParams) -> str:
        return self._run_experiment(ExperimentMode.CYCLE, params,
                                    self._cycle_sequence, self._cycle_reference_sequence)

    def _run_experiment(self, mode: ExperimentMode, params, direct_sequence, reference_sequence) -> str:
        params = copy.deepcopy(params)
        params.validate()
        if bool(params.reference_control_enabled) != bool(self.config.use_dmm_vref):
            raise ValueError(f"{mode.value} Reference feedback and DMM Reference measurement must be enabled or disabled together.")
        self._active_params = params
        metadata = {
            "mode": mode.value,
            "control_variable": "reference" if params.reference_control_enabled else "gate",
            "reference_control_enabled": bool(params.reference_control_enabled),
            "instrument_config": self.config.as_dict(),
            "params": params.as_dict(),
        }
        self.logger = DataLogger(params.output_dir, params.run_name, metadata)
        try:
            if not self.stop_event.is_set():
                self._prepare(params.drain_voltage)
                sequence = reference_sequence if params.reference_control_enabled else direct_sequence
                self._run_sequence(sequence, params)
            return str(self.logger.run_dir)
        finally:
            self._cleanup()

    def run_cycle_ref(self, params: CycleReferenceParams) -> str:
        """Legacy adapter; the cycle mode now owns both control branches."""
        params.validate()
        values = params.as_dict()
        values["reference_sample_interval_s"] = values.pop("sample_interval_s")
        return self.run_cycle(CycleParams(**values, reference_control_enabled=True))

    def run_liner_ref(self, params: LinearReferenceParams) -> str:
        """Compatibility entry point; all new runs use the unified linear mode."""
        names = (
            "run_name", "output_dir", "drain_voltage", "vref_start", "vref_end",
            "vref_intermediate_steps", "reference_tolerance_v", "initial_gate_voltage",
            "gate_step_voltage", "gate_min_voltage", "gate_max_voltage", "reference_target_timeout_s",
            "reference_check_interval_s",
        )
        return self.run_linear(LinearParams(
            **{name: getattr(params, name) for name in names},
            reference_control_enabled=True, hold_duration_s=0.0,
        ))

    # Compatibility for callers using the previous method name.
    def run_vref(self, params: CycleReferenceParams) -> str:
        return self.run_cycle_ref(params)

    def _prepare(self, drain_voltage: float) -> None:
        self._status("Connecting instruments...")
        self.rig = create_rig(self.config)
        self.rig.connect()
        if self.stop_event.is_set():
            return
        self.rig.configure()
        if self.stop_event.is_set():
            return
        self.rig.set_drain_voltage(drain_voltage)
        self.current_drain_voltage = drain_voltage
        self.start_time = self._clock()
        self.samples = 0
        self.last_gate_voltage = None
        self._status(f"Ready. Drain V = {drain_voltage:+.1f} V")

    def _cleanup(self) -> None:
        try:
            self._status("Cleaning up instruments...")
        finally:
            # A full disk or failing UI callback must never skip instrument shutdown.
            try:
                if self.rig is not None:
                    self.rig.close()
            finally:
                if self.logger is not None:
                    self.logger.close()
        self._status("Instrument outputs disabled safely.")

    def _linear_sequence(self, params: LinearParams) -> None:
        self._status("Starting linear Gate-voltage mode.")
        for gate_v in self._gate_targets(params.gate_start, params.gate_end, params.gate_step):
            if self.stop_event.is_set():
                return
            self._hold_gate(gate_v, 1, "linear_step")
        if not self.stop_event.is_set() and (params.hold_forever or params.hold_duration_s > 0):
            self._hold_gate(params.gate_end, 1, "linear_hold_end")
        if not self.stop_event.is_set():
            self._status("Linear task completed.")

    @staticmethod
    def _gate_targets(start: float, end: float, step: float, *, include_start: bool = True) -> Iterable[float]:
        """Stream exact endpoints, correcting direction without overshooting."""
        if abs(step) <= 1e-15:
            raise ValueError("Voltage step cannot be 0.")
        if include_start:
            yield start
        if start == end:
            return
        signed_step = math.copysign(abs(step), end - start)
        lower, upper = sorted((start, end))
        previous = start
        index = 1
        while True:
            raw = start + index * signed_step
            if (signed_step > 0 and raw >= end) or (signed_step < 0 and raw <= end):
                break
            value = min(upper, max(lower, round(raw, 12)))
            if value != previous:
                yield value
                previous = value
            if value == end:
                return
            index += 1
        yield end

    def _cycle_sequence(self, params: CycleParams) -> None:
        self._status("Starting cycle Gate-voltage mode.")
        cycle = 1
        while cycle <= params.cycles and not self.stop_event.is_set():
            self._status(f"Cycle {cycle}/{params.cycles}: forward.")
            for gate_v in self._gate_targets(
                params.gate_start if cycle == 1 else params.gate_min, params.gate_max, params.gate_step
            ):
                if self.stop_event.is_set():
                    return
                self._hold_gate(gate_v, cycle, "cycle_rise")
            if params.hold_at_max_s > 0 and not self.stop_event.is_set():
                self._hold_gate(params.gate_max, cycle, "cycle_hold_max")
            for gate_v in self._gate_targets(params.gate_max, params.gate_min, params.gate_step, include_start=False):
                if self.stop_event.is_set():
                    return
                self._hold_gate(gate_v, cycle, "cycle_fall")
            if params.hold_at_min_s > 0 and not self.stop_event.is_set():
                self._hold_gate(params.gate_min, cycle, "cycle_hold_min")
            cycle += 1
        if not self.stop_event.is_set():
            self._status(f"Cycle task completed: {params.cycles} cycle(s).")

    def _direct_stage_timing(self, phase: str) -> tuple[Optional[float], float, int, str]:
        params = self._active_params
        if phase.startswith("linear_hold_end"):
            duration = None if params.hold_forever else params.hold_duration_s
            phase = "linear_hold_end_continuous" if params.hold_forever else "linear_hold_end"
        elif phase == "cycle_hold_max":
            duration = params.hold_at_max_s
        elif phase == "cycle_hold_min":
            duration = params.hold_at_min_s
        else:
            duration = params.step_duration_s
        return duration, params.sample_interval_s, params.skip_points_after_voltage_change, phase

    def _hold_gate(self, gate_v: float, cycle: int, phase: str) -> None:
        if self.rig is None:
            raise RuntimeError("Rig is not prepared.")
        self._checkpoint()
        if self.stop_event.is_set():
            return
        params = self._active_params
        if not params.gate_min_voltage <= gate_v <= params.gate_max_voltage:
            raise RuntimeError("Gate target would exceed the configured safety limits.")
        duration_s, sample_interval_s, skip_points, phase = self._direct_stage_timing(phase)
        changed = self.last_gate_voltage is None or not math.isclose(gate_v, self.last_gate_voltage, abs_tol=1e-12)
        self.rig.set_gate_voltage(gate_v)
        self.last_gate_voltage = gate_v
        duration_text = "until Stop" if duration_s is None else f"for {duration_s:g}s"
        self._status(f"{phase}: Gate V = {gate_v:+.6f} V {duration_text}")

        start = self._clock()
        next_sample = start + (skip_points * sample_interval_s if changed else 0.0)
        while not self.stop_event.is_set():
            if self._checkpoint():
                if self.stop_event.is_set():
                    return
                duration_s, sample_interval_s, _, phase = self._direct_stage_timing(phase)
                next_sample = self._clock() + sample_interval_s
            now = self._clock()
            if duration_s is not None and now - start > duration_s:
                break
            if now >= next_sample:
                drain_i, gate_i, vref = self.rig.read()
                if self.stop_event.is_set():
                    return
                if self.pause_requested.is_set():
                    continue  # Discard in-flight readings and resume with a fresh one.
                self._record_sample(MeasurementSample(
                    elapsed_s=now - self.start_time, cycle=cycle, phase=phase,
                    gate_v=self.last_gate_voltage, drain_v=self.current_drain_voltage,
                    drain_i=drain_i, gate_i=gate_i, vref_v=vref, vref_state="direct_gate",
                ))
                next_sample = self._advance_sample_time(next_sample, sample_interval_s)
            time.sleep(0.01)

    def _cycle_reference_sequence(self, params: CycleParams) -> None:
        if self.rig is None:
            raise RuntimeError("Rig is not prepared.")
        if self.stop_event.is_set():
            return

        if self.last_gate_voltage is None:
            self.rig.set_gate_voltage(params.initial_gate_voltage)
            self.last_gate_voltage = params.initial_gate_voltage
        forward_gate_step = (
            abs(params.gate_step_voltage)
            if params.vref_target > params.vref_initial
            else -abs(params.gate_step_voltage)
        )
        current_gate = self.last_gate_voltage

        self._status(
            f"Starting cycle Reference feedback for {params.cycles} cycle(s). Reference "
            f"{params.vref_initial:+.3f} V -> {params.vref_target:+.3f} V -> "
            f"{params.vref_initial:+.3f} V; Gate step {abs(params.gate_step_voltage):.3f} V."
        )

        cycle = 1
        while cycle <= params.cycles:
            if self.stop_event.is_set():
                return
            self._status(f"Cycle Reference {cycle}/{params.cycles}: forward sweep started.")
            current_gate, forward_average = self._reference_sweep_leg(
                index=cycle,
                phase="cycle_ref_forward",
                initial_vref=params.vref_initial,
                target_vref=params.vref_target,
                initial_gate=current_gate,
                gate_step=forward_gate_step,
                params=params,
            )
            if self.stop_event.is_set():
                return
            self._status(
                f"Cycle Reference {cycle}: forward target reached. Average Reference "
                f"{forward_average:+.3f} V at Gate {current_gate:+.3f} V."
            )

            self._status(f"Cycle Reference {cycle}/{params.cycles}: backward sweep started.")
            current_gate, backward_average = self._reference_sweep_leg(
                index=cycle,
                phase="cycle_ref_backward",
                initial_vref=params.vref_target,
                target_vref=params.vref_initial,
                initial_gate=current_gate,
                gate_step=-forward_gate_step,
                params=params,
            )
            if self.stop_event.is_set():
                return
            self._status(
                f"Cycle Reference {cycle}: backward target reached. Average Reference "
                f"{backward_average:+.3f} V at Gate {current_gate:+.3f} V."
            )

            cycle += 1
        if not self.stop_event.is_set():
            self._status(f"Cycle Reference task completed: {params.cycles} cycle(s).")

    def _linear_reference_sequence(self, linear_params: LinearParams) -> None:
        if self.rig is None:
            raise RuntimeError("Rig is not prepared.")
        if self.stop_event.is_set():
            return

        params = linear_params.reference_params()
        targets = params.reference_targets()
        if self.last_gate_voltage is None:
            self.rig.set_gate_voltage(params.initial_gate_voltage)
            self.last_gate_voltage = params.initial_gate_voltage
        current_gate = self.last_gate_voltage

        self._status(
            f"Starting linear Reference feedback with {len(targets)} Reference point(s): "
            f"{params.vref_start:+.6f} V to {params.vref_end:+.6f} V; "
            f"{params.vref_intermediate_steps} intermediate step(s). "
            f"Ref tolerance ±{params.reference_tolerance_v:.6f} V; "
            f"Gate adjustment {abs(params.gate_step_voltage):.6f} V every {params.reference_check_interval_s:g} s when needed."
        )

        for step_index, target in enumerate(targets, start=1):
            if self.stop_event.is_set():
                return
            self._status(
                f"Linear Reference target {step_index}/{len(targets)} started: {target:+.6f} V."
            )
            current_gate, measured_vref = self._track_linear_reference_target(
                index=step_index,
                target_vref=target,
                initial_gate=current_gate,
                params=linear_params.reference_params(),
                hold_duration_s=linear_params.hold_duration_s if step_index == len(targets) else 0.0,
                hold_forever=linear_params.hold_forever and step_index == len(targets),
                final_target=step_index == len(targets),
            )
            if self.stop_event.is_set():
                return
            self._status(
                f"Linear Reference target {step_index} finished: Reference {measured_vref:+.6f} V "
                f"at Gate {current_gate:+.6f} V."
            )

        if not self.stop_event.is_set():
            self._status(f"Linear Reference task completed: {len(targets)} Reference point(s).")

    def _track_linear_reference_target(
        self,
        *,
        index: int,
        target_vref: float,
        initial_gate: float,
        params: LinearReferenceParams,
        hold_duration_s: float = 0.0,
        hold_forever: bool = False,
        final_target: bool = False,
    ) -> tuple[float, float]:
        """Track one Reference target at the configured checking interval.

        If measured Reference is below target, Gate is increased by one configured
        step. If Reference is above target, Gate is decreased by one step. Once the
        measurement is inside target ± tolerance, the target is reached. Optional
        final holding continues feedback for an active experiment duration or until Stop.
        """
        if self.rig is None:
            raise RuntimeError("Reference feedback was not initialized.")

        check_interval_s = params.reference_check_interval_s
        current_gate = initial_gate
        outside_since = self._clock()
        next_check = outside_since + check_interval_s
        hold_started = False
        hold_started_at = None
        hold_deadline = None
        last_vref = float("nan")

        while not self.stop_event.is_set():
            if self._checkpoint():
                if self.stop_event.is_set():
                    return current_gate, last_vref
                params = self._active_params.reference_params()
                check_interval_s = params.reference_check_interval_s
                if final_target:
                    hold_duration_s = self._active_params.hold_duration_s
                    hold_forever = self._active_params.hold_forever
                    if hold_started:
                        hold_deadline = None if hold_forever else hold_started_at + hold_duration_s
                next_check = self._clock() + check_interval_s
            now = self._clock()
            if hold_deadline is not None and now >= hold_deadline:
                return current_gate, last_vref
            if params.reference_target_timeout_s > 0 and outside_since is not None and now - outside_since >= params.reference_target_timeout_s:
                raise RuntimeError(
                    f"Reference {target_vref:+.6f} V stayed outside tolerance for "
                    f"{params.reference_target_timeout_s:g} s. Check Gate step, tolerance and device response."
                )
            if now < next_check:
                time.sleep(min(0.01, next_check - now))
                continue

            measured_gate = current_gate
            drain_i, gate_i, vref = self.rig.read()
            if self.stop_event.is_set():
                return current_gate, float("nan")
            if self.pause_requested.is_set():
                continue
            if vref is None or not math.isfinite(vref):
                raise RuntimeError("Linear Reference feedback is active, but no valid Reference value was received.")
            measured_at = self._clock()
            if hold_deadline is not None and measured_at >= hold_deadline:
                return current_gate, vref
            if params.reference_target_timeout_s > 0 and outside_since is not None and measured_at - outside_since >= params.reference_target_timeout_s:
                raise RuntimeError("Reference target timeout expired during instrument reading.")
            last_vref = vref

            error_v = target_vref - vref
            in_tolerance = abs(error_v) <= params.reference_tolerance_v
            gate_adjustment = 0.0
            state = "linear_ref_in_tolerance"

            if in_tolerance:
                outside_since = None
                if not hold_started and (hold_forever or hold_duration_s > 0):
                    hold_started = True
                    hold_started_at = measured_at
                    hold_deadline = None if hold_forever else measured_at + hold_duration_s
                    duration_text = "until Stop" if hold_forever else f"for {hold_duration_s:g} s"
                    self._status(f"Final Reference reached. Maintaining {target_vref:+.6f} V {duration_text} with feedback.")
            elif outside_since is None:
                outside_since = measured_at

            if not in_tolerance:
                gate_adjustment = (
                    abs(params.gate_step_voltage)
                    if vref < target_vref
                    else -abs(params.gate_step_voltage)
                )
                next_gate = round(current_gate + gate_adjustment, 12)
                if not params.gate_min_voltage <= next_gate <= params.gate_max_voltage:
                    raise RuntimeError(
                        f"Reference target {target_vref:+.3f} V was not reached before the next Gate adjustment "
                        f"would exceed the configured limits [{params.gate_min_voltage:+.3f}, "
                        f"{params.gate_max_voltage:+.3f}] V. Last Reference was {vref:+.3f} V "
                        f"at Gate {current_gate:+.3f} V."
                    )
                if self.stop_event.is_set():
                    return current_gate, vref
                self.rig.set_gate_voltage(next_gate)
                current_gate = next_gate
                self.last_gate_voltage = current_gate
                state = "linear_ref_adjust_up" if gate_adjustment > 0 else "linear_ref_adjust_down"

            self._record_sample(
                MeasurementSample(
                    elapsed_s=measured_at - self.start_time,
                    cycle=index,
                    phase="linear_ref_hold" if hold_started else "linear_ref_feedback",
                    gate_v=measured_gate,
                    drain_v=self.current_drain_voltage,
                    drain_i=drain_i,
                    gate_i=gate_i,
                    vref_v=vref,
                    reference_target_v=target_vref,
                    reference_error_v=error_v,
                    gate_adjustment_v=gate_adjustment,
                    vref_state=state,
                )
            )

            if in_tolerance and not hold_started:
                return current_gate, vref

            self._status(
                f"Linear Reference feedback: Ref {vref:+.6f} V vs target {target_vref:+.6f} V; "
                f"Gate {measured_gate:+.6f} V -> {current_gate:+.6f} V "
                f"({gate_adjustment:+.6f} V)."
            )
            next_check = self._advance_sample_time(next_check, check_interval_s)

        return current_gate, float("nan")

    def _reference_sweep_leg(
        self,
        *,
        index: int,
        phase: str,
        initial_vref: float,
        target_vref: float,
        initial_gate: float,
        gate_step: float,
        params: CycleParams,
    ) -> tuple[float, float]:
        if self.rig is None:
            raise RuntimeError("Reference sweep was not initialized.")

        current_gate = initial_gate
        gate_point = 0

        while not self.stop_event.is_set():
            paused = self._checkpoint()
            if self.stop_event.is_set():
                return current_gate, float("nan")
            if paused and gate_point > 0 and current_gate != self.last_gate_voltage:
                gate_step = math.copysign(params.gate_step_voltage, gate_step)
                current_gate = round(self.last_gate_voltage + gate_step, 12)
            gate_point += 1
            if not params.gate_min_voltage <= current_gate <= params.gate_max_voltage:
                raise RuntimeError(
                    f"{phase} requested Gate {current_gate:+.3f} V outside the configured "
                    f"limits [{params.gate_min_voltage:+.3f}, {params.gate_max_voltage:+.3f}] V."
                )

            previous_gate = self.last_gate_voltage
            gate_adjustment = 0.0 if previous_gate is None else current_gate - previous_gate
            self.rig.set_gate_voltage(current_gate)
            self.last_gate_voltage = current_gate

            self._status(
                f"{phase} Gate point {gate_point}: Gate {current_gate:+.3f} V; collecting Reference for "
                f"{params.step_wait_time_s:.1f} s."
            )
            samples = self._collect_reference_step_samples(
                index=index,
                phase=phase,
                target_vref=target_vref,
                gate_voltage=current_gate,
                gate_adjustment=gate_adjustment,
                wait_time_s=params.step_wait_time_s,
                sample_interval_s=params.reference_sample_interval_s,
            )
            if self.stop_event.is_set():
                return current_gate, float("nan")

            reached, average_vref, satisfied_percent = self._evaluate_reference_samples(
                samples=samples,
                initial_vref=initial_vref,
                target_vref=target_vref,
                tolerance_percent=params.tolerance_percent,
                threshold_percent=params.threshold_percent,
            )
            self._status(
                f"{phase} Gate point {gate_point}: average Reference {average_vref:+.3f} V; "
                f"condition satisfied by {satisfied_percent:.1f}% of samples."
            )
            if reached:
                return current_gate, average_vref

            gate_step = math.copysign(params.gate_step_voltage, gate_step)
            next_gate = round(current_gate + gate_step, 12)
            if not params.gate_min_voltage <= next_gate <= params.gate_max_voltage:
                raise RuntimeError(
                    f"Reference target {target_vref:+.3f} V was not reached before the next Gate step "
                    f"would exceed the configured limits. Last average Reference was {average_vref:+.3f} V "
                    f"at Gate {current_gate:+.3f} V."
                )
            current_gate = next_gate

        return current_gate, float("nan")

    def _collect_reference_step_samples(
        self,
        *,
        index: int,
        phase: str,
        target_vref: float,
        gate_voltage: float,
        gate_adjustment: float,
        wait_time_s: float,
        sample_interval_s: float,
    ) -> list[float]:
        if self.rig is None:
            raise RuntimeError("Reference sampling was not initialized.")

        sample_count = self._reference_sample_count(wait_time_s, sample_interval_s)
        samples: list[float] = []
        next_sample = self._clock() + sample_interval_s

        while len(samples) < sample_count and not self.stop_event.is_set():
            revision = self._params_revision
            if self._checkpoint():
                if self.stop_event.is_set():
                    return samples
                if revision != self._params_revision:
                    # A new analysis window must contain only samples at new settings.
                    samples = []
                    gate_adjustment = 0.0
                    sample_interval_s = self._active_params.reference_sample_interval_s
                    sample_count = self._reference_sample_count(self._active_params.step_wait_time_s, sample_interval_s)
                next_sample = self._clock() + sample_interval_s
            now = self._clock()
            if now < next_sample:
                time.sleep(min(0.01, next_sample - now))
                continue

            drain_i, gate_i, vref = self.rig.read()
            if self.stop_event.is_set():
                return samples
            if self.pause_requested.is_set():
                continue
            if vref is None or not math.isfinite(vref):
                raise RuntimeError("Reference mode is active, but no valid Reference value was received.")

            samples.append(vref)
            self._record_sample(
                MeasurementSample(
                    elapsed_s=now - self.start_time,
                    cycle=index,
                    phase=phase,
                    gate_v=gate_voltage,
                    drain_v=self.current_drain_voltage,
                    drain_i=drain_i,
                    gate_i=gate_i,
                    vref_v=vref,
                    reference_target_v=target_vref,
                    reference_error_v=target_vref - vref,
                    gate_adjustment_v=gate_adjustment if len(samples) == 1 else 0.0,
                    vref_state=f"{phase}_sampling",
                )
            )
            next_sample = self._advance_sample_time(next_sample, sample_interval_s)

        if not samples and not self.stop_event.is_set():
            raise RuntimeError("No Reference samples were collected at the current Gate voltage.")
        return samples

    @staticmethod
    def _reference_sample_count(wait_time_s: float, sample_interval_s: float) -> int:
        # Preserve whole-interval sampling; 0.6 / 0.2 must produce 3, not 2.
        return max(int(round(wait_time_s / sample_interval_s, 12)), 1)

    @staticmethod
    def _evaluate_reference_samples(
        *,
        samples: list[float],
        initial_vref: float,
        target_vref: float,
        tolerance_percent: float,
        threshold_percent: float,
    ) -> tuple[bool, float, float]:
        if not samples:
            raise ValueError("At least one Reference sample is required for evaluation.")

        tolerance = abs(target_vref * tolerance_percent / 100.0)
        # A zero target would otherwise have exactly zero tolerance. Keep a
        # small absolute band so returning to 0 V is physically achievable.
        tolerance = max(tolerance, 1e-6)
        lower = target_vref - tolerance
        upper = target_vref + tolerance
        direction = 1 if target_vref > initial_vref else -1 if target_vref < initial_vref else 0

        def condition_satisfied(value: float) -> bool:
            in_tolerance = lower <= value <= upper
            if direction > 0:
                return value >= target_vref or in_tolerance
            if direction < 0:
                return value <= target_vref or in_tolerance
            return in_tolerance

        condition_count = sum(1 for value in samples if condition_satisfied(value))
        average_vref = sum(samples) / len(samples)
        satisfied_percent = condition_count / len(samples) * 100.0
        reached = satisfied_percent >= threshold_percent or condition_satisfied(average_vref)
        return reached, average_vref, satisfied_percent

    # Compatibility name retained for older tests/callers.
    _evaluate_vref_samples = _evaluate_reference_samples

    def _advance_sample_time(self, previous: float, interval: float) -> float:
        next_sample = previous + interval
        now = self._clock()
        return next_sample if next_sample > now else now + interval

    def _record_sample(self, sample: MeasurementSample) -> None:
        self.samples += 1
        if self.logger is not None:
            self.logger.write_sample(sample)
        if self.sample_callback is not None:
            self.sample_callback(sample)

    def _status(self, message: str) -> None:
        if self.logger is not None:
            self.logger.log(message)
        if self.status_callback is not None:
            self.status_callback(message)
