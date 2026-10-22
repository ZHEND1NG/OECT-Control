from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from .models import MeasurementSample, ensure_output_dir


def sanitize_run_name(run_name: str) -> str:
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in run_name).strip("_")
    return safe_name or "oect_run"


def finalize_run_directory(run_dir: str | Path, final_run_name: str) -> Path:
    """Rename a completed run folder using the final GUI Run name.

    Data is written continuously to the original folder during the experiment.
    After all files are closed, this function renames that folder and updates
    metadata paths. Existing folders are never overwritten.
    """
    source = Path(run_dir)
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"Run directory does not exist: {source}")

    metadata_path = source / "metadata.json"
    metadata: Dict[str, Any] = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}

    stamp = str(metadata.get("created_at") or source.name.split("_", 1)[0])
    safe_name = sanitize_run_name(final_run_name)
    target = source.parent / f"{stamp}_{safe_name}"

    if target != source:
        candidate = target
        suffix = 2
        while candidate.exists():
            candidate = source.parent / f"{stamp}_{safe_name}_{suffix}"
            suffix += 1
        source.rename(candidate)
        target = candidate

    metadata_path = target / "metadata.json"
    if metadata_path.exists() or metadata:
        params = metadata.get("params")
        if isinstance(params, dict):
            params["run_name"] = final_run_name
        metadata["final_run_name"] = final_run_name
        metadata["run_directory"] = str(target)
        metadata["data_file"] = str(target / "data.txt")
        metadata["log_file"] = str(target / "run.log")
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    return target


class DataLogger:
    def __init__(self, output_dir: str, run_name: str, metadata: Dict[str, Any]):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_name = sanitize_run_name(run_name)
        root = ensure_output_dir(output_dir)
        self.run_dir = root / f"{stamp}_{safe_name}"
        self.run_dir.mkdir(parents=True, exist_ok=False)

        instrument_config = dict(metadata.get("instrument_config") or {})
        mode = str(metadata.get("mode") or "")
        self.include_vref_mode = bool(metadata.get("reference_control_enabled")) or mode in {"cycle/ref", "liner/ref", "vref"}
        self.include_reference_v = bool(instrument_config.get("use_dmm_vref")) or self.include_vref_mode

        self.data_path = self.run_dir / "data.txt"
        self.metadata_path = self.run_dir / "metadata.json"
        self.log_path = self.run_dir / "run.log"
        self._file = open(self.data_path, "w", encoding="utf-8")
        self._file.write(
            MeasurementSample.txt_header(
                include_reference_v=self.include_reference_v,
                include_vref_mode=self.include_vref_mode,
            )
            + "\n"
        )
        self._file.flush()

        metadata = dict(metadata)
        metadata["created_at"] = stamp
        metadata["data_file"] = str(self.data_path)
        metadata["reference_v_column_included"] = self.include_reference_v
        metadata["vref_mode_columns_included"] = self.include_vref_mode
        metadata["time_basis"] = "active experiment seconds; paused intervals excluded"
        metadata["events"] = []
        self.metadata = metadata
        with open(self.metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

    def record_event(self, kind: str, elapsed_s: float, samples: int, **details) -> None:
        self.metadata["events"].append({
            "event": kind, "wall_time": datetime.now().astimezone().isoformat(),
            "elapsed_s": elapsed_s, "samples_written": samples, **details,
        })
        temporary = self.metadata_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.metadata_path)

    def write_sample(self, sample: MeasurementSample) -> None:
        self._file.write(
            sample.as_txt_row(
                include_reference_v=self.include_reference_v,
                include_vref_mode=self.include_vref_mode,
            )
            + "\n"
        )
        self._file.flush()

    def log(self, message: str) -> None:
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(message.rstrip() + "\n")

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()
