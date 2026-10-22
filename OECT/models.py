from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
from pathlib import Path
from typing import Any, Dict, Optional


class ExperimentMode(str, Enum):
    LINEAR = "linear"
    CYCLE = "cycle"


@dataclass
class InstrumentConfig:
    backend: str = "simulated"
    dmm_ip: str = "192.168.1.160"
    drain_smu_ip: str = "192.168.1.150"
    gate_smu_ip: str = "192.168.1.160"
    keithley_2634b_ip: str = "192.168.1.150"
    drain_channel: str = "smua"
    gate_channel: str = "smub"
    adcmt_6541_ip: str = "192.168.1.204"
    adcmt_drain_channel: int = 1
    adcmt_gate_channel: int = 2
    use_dmm_vref: bool = False
    timeout_ms: int = 10000
    retries: int = 3
    drain_compliance_a: float = 0.1
    gate_compliance_a: float = 0.001
    current_nplc: float = 1.0
    voltage_nplc: float = 1.0
    autozero: str = "ON"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LinearParams:
    run_name: str = "linear_test"
    output_dir: str = "experiment_data/oect"
    drain_voltage: float = -0.01
    gate_start: float = 0.0
    gate_step: float = -0.01
    step_duration_s: float = 4.0
    sample_interval_s: float = 1.0
    skip_points_after_voltage_change: int = 1
    gate_end: float = -0.8
    hold_duration_s: float = 60.0
    hold_forever: bool = False
    reference_control_enabled: bool = False
    vref_start: float = 0.0
    vref_end: float = -0.8
    vref_intermediate_steps: int = 7
    reference_tolerance_v: float = 0.005
    initial_gate_voltage: float = 0.0
    gate_step_voltage: float = 0.05
    gate_min_voltage: float = -1.0
    gate_max_voltage: float = 1.0
    reference_target_timeout_s: float = 300.0
    reference_check_interval_s: float = 1.0

    def reference_params(self) -> LinearReferenceParams:
        return LinearReferenceParams(
            run_name=self.run_name, output_dir=self.output_dir, drain_voltage=self.drain_voltage,
            vref_start=self.vref_start, vref_end=self.vref_end,
            vref_intermediate_steps=self.vref_intermediate_steps,
            reference_tolerance_v=self.reference_tolerance_v,
            initial_gate_voltage=self.initial_gate_voltage, gate_step_voltage=self.gate_step_voltage,
            gate_min_voltage=self.gate_min_voltage, gate_max_voltage=self.gate_max_voltage,
            reference_target_timeout_s=self.reference_target_timeout_s,
            reference_check_interval_s=self.reference_check_interval_s,
        )

    def validate(self) -> None:
        for name in ("drain_voltage", "hold_duration_s", "gate_min_voltage", "gate_max_voltage"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"Linear {name} must be finite.")
        if self.hold_duration_s < 0:
            raise ValueError("Hold duration cannot be negative.")
        if self.gate_min_voltage >= self.gate_max_voltage:
            raise ValueError("Gate minimum limit must be lower than Gate maximum limit.")
        if self.reference_control_enabled:
            self.reference_params().validate()
            return
        for name in ("gate_start", "gate_end", "gate_step", "step_duration_s", "sample_interval_s"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"Linear {name} must be finite.")
        if abs(self.gate_step) <= 1e-15:
            raise ValueError("Gate step cannot be 0 V.")
        if self.sample_interval_s < 0.05 or self.step_duration_s <= 0:
            raise ValueError("Sample interval must be at least 0.05 s and step duration must be positive.")
        if (
            isinstance(self.skip_points_after_voltage_change, bool)
            or not isinstance(self.skip_points_after_voltage_change, int)
            or self.skip_points_after_voltage_change < 0
        ):
            raise ValueError("Skip points must be a non-negative integer.")
        if not all(self.gate_min_voltage <= value <= self.gate_max_voltage for value in (self.gate_start, self.gate_end)):
            raise ValueError("Gate start and end must be inside the Gate safety limits.")

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CycleParams:
    run_name: str = "cycle_test"
    output_dir: str = "experiment_data/oect"
    drain_voltage: float = -0.01
    gate_start: float = 0.0
    gate_step: float = -0.01
    step_duration_s: float = 4.0
    sample_interval_s: float = 1.0
    skip_points_after_voltage_change: int = 1
    gate_max: float = -0.8
    hold_at_max_s: float = 30.0
    gate_min: float = 0.0
    hold_at_min_s: float = 30.0
    cycles: int = 3
    reference_control_enabled: bool = False
    vref_initial: float = 0.0
    vref_target: float = -0.8
    initial_gate_voltage: float = 0.0
    gate_step_voltage: float = 0.05
    step_wait_time_s: float = 30.0
    reference_sample_interval_s: float = 2.0
    tolerance_percent: float = 0.5
    threshold_percent: float = 90.0
    gate_min_voltage: float = -1.0
    gate_max_voltage: float = 1.0

    def reference_params(self) -> CycleReferenceParams:
        names = ("run_name", "output_dir", "drain_voltage", "vref_initial", "vref_target",
                 "initial_gate_voltage", "gate_step_voltage", "step_wait_time_s",
                 "tolerance_percent", "threshold_percent", "gate_min_voltage",
                 "gate_max_voltage", "cycles")
        return CycleReferenceParams(
            **{name: getattr(self, name) for name in names},
            sample_interval_s=self.reference_sample_interval_s,
        )

    def validate(self) -> None:
        if self.reference_control_enabled:
            self.reference_params().validate()
            return
        for name in ("drain_voltage", "gate_start", "gate_step", "gate_max", "gate_min",
                     "step_duration_s", "sample_interval_s", "hold_at_max_s", "hold_at_min_s",
                     "gate_min_voltage", "gate_max_voltage"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"Cycle {name} must be finite.")
        if abs(self.gate_step) <= 1e-15:
            raise ValueError("Gate step cannot be 0 V.")
        if self.step_duration_s <= 0 or self.sample_interval_s < 0.05:
            raise ValueError("Step duration must be positive and sample interval at least 0.05 s.")
        if self.hold_at_max_s < 0 or self.hold_at_min_s < 0:
            raise ValueError("Hold durations cannot be negative.")
        if isinstance(self.cycles, bool) or not isinstance(self.cycles, int) or self.cycles < 1:
            raise ValueError("Cycle count must be a positive integer.")
        if (isinstance(self.skip_points_after_voltage_change, bool)
                or not isinstance(self.skip_points_after_voltage_change, int)
                or self.skip_points_after_voltage_change < 0):
            raise ValueError("Skip points must be a non-negative integer.")
        if self.gate_min_voltage >= self.gate_max_voltage:
            raise ValueError("Gate minimum limit must be lower than Gate maximum limit.")
        if not all(self.gate_min_voltage <= value <= self.gate_max_voltage
                   for value in (self.gate_start, self.gate_min, self.gate_max)):
            raise ValueError("Cycle Gate start and both endpoints must be inside the Gate safety limits.")

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReferenceControlParams:
    run_name: str = "reference_test"
    output_dir: str = "experiment_data/oect"
    drain_voltage: float = -0.01
    initial_gate_voltage: float = 0.0
    gate_step_voltage: float = 0.05
    step_wait_time_s: float = 30.0
    sample_interval_s: float = 2.0
    tolerance_percent: float = 0.5
    threshold_percent: float = 90.0
    gate_min_voltage: float = -1.0
    gate_max_voltage: float = 1.0

    def _validate_common(self, mode_name: str) -> None:
        values = {
            "drain_voltage": self.drain_voltage,
            "initial_gate_voltage": self.initial_gate_voltage,
            "gate_step_voltage": self.gate_step_voltage,
            "step_wait_time_s": self.step_wait_time_s,
            "sample_interval_s": self.sample_interval_s,
            "tolerance_percent": self.tolerance_percent,
            "threshold_percent": self.threshold_percent,
            "gate_min_voltage": self.gate_min_voltage,
            "gate_max_voltage": self.gate_max_voltage,
        }
        for name, value in values.items():
            if not math.isfinite(value):
                raise ValueError(f"{mode_name} {name} must be finite.")
        if self.gate_step_voltage <= 0:
            raise ValueError("Gate voltage step must be greater than 0 V.")
        if self.step_wait_time_s <= 0:
            raise ValueError("Reference step wait time must be greater than 0 s.")
        if self.sample_interval_s < 0.05:
            raise ValueError("Reference sample interval must be at least 0.05 s.")
        if self.step_wait_time_s < self.sample_interval_s:
            raise ValueError("Reference step wait time cannot be shorter than Sample interval.")
        if self.tolerance_percent < 0:
            raise ValueError("Reference tolerance percent cannot be negative.")
        if not 0 < self.threshold_percent <= 100:
            raise ValueError("Reference threshold percent must be greater than 0 and at most 100.")
        if self.gate_min_voltage >= self.gate_max_voltage:
            raise ValueError("Gate minimum limit must be lower than Gate maximum limit.")
        if not self.gate_min_voltage <= self.initial_gate_voltage <= self.gate_max_voltage:
            raise ValueError("Initial Gate voltage must be inside the Gate safety limits.")

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CycleReferenceParams(ReferenceControlParams):
    """Reference-controlled forward/backward Gate step sweep."""

    vref_initial: float = 0.0
    vref_target: float = -0.8
    cycles: int = 3

    def validate(self) -> None:
        self._validate_common("cycle Reference feedback")
        if not math.isfinite(self.vref_initial) or not math.isfinite(self.vref_target):
            raise ValueError("Cycle Reference values must be finite.")
        if math.isclose(self.vref_initial, self.vref_target, abs_tol=1e-15):
            raise ValueError("Initial Reference and target Reference must be different.")
        if isinstance(self.cycles, bool) or not isinstance(self.cycles, int) or self.cycles < 1:
            raise ValueError("Cycle count must be a positive integer.")


@dataclass
class LinearReferenceParams(ReferenceControlParams):
    """Reference-controlled linear target sequence.

    ``vref_intermediate_steps`` counts evenly spaced targets strictly between
    ``vref_start`` and ``vref_end``. Zero selects a single target and requires
    equal endpoints. At each configured interval, measured Reference is compared
    with the active target. Gate voltage is adjusted by ``gate_step_voltage``
    until the Reference voltage is inside ``reference_tolerance_v``.
    """

    vref_start: float = 0.0
    vref_end: float = -0.8
    vref_intermediate_steps: int = 7
    reference_tolerance_v: float = 0.005
    reference_target_timeout_s: float = 300.0
    reference_check_interval_s: float = 1.0

    def validate(self) -> None:
        self._validate_common("linear Reference feedback")
        values = {
            "vref_start": self.vref_start,
            "vref_end": self.vref_end,
            "reference_tolerance_v": self.reference_tolerance_v,
            "reference_target_timeout_s": self.reference_target_timeout_s,
            "reference_check_interval_s": self.reference_check_interval_s,
        }
        for name, value in values.items():
            if not math.isfinite(value):
                raise ValueError(f"Linear Reference {name} must be finite.")
        if (
            isinstance(self.vref_intermediate_steps, bool)
            or not isinstance(self.vref_intermediate_steps, int)
            or self.vref_intermediate_steps < 0
        ):
            raise ValueError("Reference intermediate steps must be a non-negative integer.")
        if self.vref_intermediate_steps == 0:
            if self.vref_start != self.vref_end:
                raise ValueError("With 0 intermediate steps, Reference start V must equal Reference end V.")
        elif self.vref_start == self.vref_end:
            raise ValueError("For a single Reference target, set intermediate steps to 0.")
        if self.reference_tolerance_v < 0:
            raise ValueError("Reference tolerance must be non-negative.")
        if self.reference_target_timeout_s < 0:
            raise ValueError("Reference target timeout cannot be negative (0 disables it).")
        if self.reference_check_interval_s < 0.05:
            raise ValueError("Reference check interval must be at least 0.05 s.")
        if 0 < self.reference_target_timeout_s <= self.reference_check_interval_s:
            raise ValueError("Reference target timeout must be greater than the check interval (or 0 to disable it).")

    def reference_targets(self) -> list[float]:
        """Return one target, or both endpoints plus N evenly spaced targets."""
        self.validate()
        if self.vref_intermediate_steps == 0:
            return [self.vref_start]
        intervals = self.vref_intermediate_steps + 1
        span = self.vref_end - self.vref_start
        return [
            self.vref_start,
            # Match the rounding used by the other voltage target sequences.
            *(round(self.vref_start + span * (index / intervals), 12) for index in range(1, intervals)),
            self.vref_end,
        ]


# Backward-compatible import name for older code and saved scripts.
CycleRefParams = CycleReferenceParams
VrefSweepParams = CycleReferenceParams
LinerRefParams = LinearReferenceParams


@dataclass
class MeasurementSample:
    elapsed_s: float
    cycle: int
    phase: str
    gate_v: float
    drain_v: float
    drain_i: float
    gate_i: Optional[float]
    vref_v: Optional[float]
    reference_target_v: Optional[float] = None
    reference_error_v: Optional[float] = None
    gate_adjustment_v: Optional[float] = None
    vref_state: str = ""

    @staticmethod
    def _format_optional(value: Optional[float]) -> str:
        return f"{value:.9E}" if value is not None and math.isfinite(value) else "NaN"

    def as_txt_row(self, include_reference_v: bool = False, include_vref_mode: bool = False) -> str:
        values = [
            f"{self.elapsed_s:.6f}",
            self._format_optional(self.gate_i),
            f"{self.gate_v:.9E}",
            f"{self.drain_i:.9E}",
            f"{self.drain_v:.9E}",
        ]
        if include_reference_v:
            values.append(self._format_optional(self.vref_v))
        if include_vref_mode:
            values.extend(
                [
                    self._format_optional(self.reference_target_v),
                    self._format_optional(self.reference_error_v),
                    self._format_optional(self.gate_adjustment_v),
                    self.vref_state or "unknown",
                ]
            )
        return "\t".join(values)

    @staticmethod
    def txt_header(include_reference_v: bool = False, include_vref_mode: bool = False) -> str:
        columns = ["Time_s", "GateI_A", "GateV_V", "DrainI_A", "DrainV_V"]
        if include_reference_v:
            columns.append("ReferenceV_V")
        if include_vref_mode:
            columns.extend(
                [
                    "ReferenceTargetV_V",
                    "ReferenceErrorV_V",
                    "GateAdjustmentV_V",
                    "VrefState",
                ]
            )
        return "\t".join(columns)


def ensure_output_dir(path: str | Path) -> Path:
    output = Path(path).expanduser()
    if not output.exists():
        raise FileNotFoundError(f"Output directory does not exist: {output}")
    if not output.is_dir():
        raise NotADirectoryError(f"Output path is not a directory: {output}")
    return output
