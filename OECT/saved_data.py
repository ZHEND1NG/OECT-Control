from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SavedOECTData:
    time_s: list[float]
    drain_i_a: list[float]
    gate_v_v: list[float]
    gate_i_a: list[float]


def _normalize_header(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


_COLUMN_ALIASES = {
    "time": {
        "times", "time", "elapseds", "elapsedtime", "relativetime", "seconds", "timesec",
    },
    "drain_i": {
        "drainia", "draini", "draincurrenta", "draincurrent", "idsa", "ids",
    },
    "gate_v": {
        "gatevv", "gatev", "gatevoltagev", "gatevoltage", "vgv", "vg",
    },
    "gate_i": {
        "gateia", "gatei", "gatecurrenta", "gatecurrent", "iga", "ig",
    },
}


def _split_line(line: str, delimiter: str | None) -> list[str]:
    if delimiter is None:
        return line.split()
    return [item.strip() for item in line.split(delimiter)]


def _find_column(headers: Iterable[str], aliases: set[str]) -> int | None:
    for index, header in enumerate(headers):
        if _normalize_header(header) in aliases:
            return index
    return None


def read_saved_oect_data(path: str | Path) -> SavedOECTData:
    data_path = Path(path)
    if not data_path.exists() or not data_path.is_file():
        raise FileNotFoundError(f"Saved data file does not exist: {data_path}")

    lines = [line.strip() for line in data_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("Saved data file does not contain a header and data rows.")

    header_line = lines[0]
    delimiter = "\t" if "\t" in header_line else "," if "," in header_line else None
    headers = _split_line(header_line, delimiter)
    indexes = {
        name: _find_column(headers, aliases)
        for name, aliases in _COLUMN_ALIASES.items()
    }
    missing = [name for name, index in indexes.items() if index is None]
    if missing:
        readable = {"time": "Time", "drain_i": "Drain I", "gate_v": "Gate V", "gate_i": "Gate I"}
        raise ValueError(
            "Saved data file is missing required column(s): "
            + ", ".join(readable[name] for name in missing)
        )

    time_s: list[float] = []
    drain_i_a: list[float] = []
    gate_v_v: list[float] = []
    gate_i_a: list[float] = []
    max_index = max(int(index) for index in indexes.values() if index is not None)

    for line in lines[1:]:
        fields = _split_line(line, delimiter)
        if len(fields) <= max_index:
            continue
        try:
            t = float(fields[indexes["time"]])  # type: ignore[index]
            drain_i = float(fields[indexes["drain_i"]])  # type: ignore[index]
            gate_v = float(fields[indexes["gate_v"]])  # type: ignore[index]
            gate_i = float(fields[indexes["gate_i"]])  # type: ignore[index]
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (t, drain_i, gate_v, gate_i)):
            continue
        time_s.append(t)
        drain_i_a.append(drain_i)
        gate_v_v.append(gate_v)
        gate_i_a.append(gate_i)

    if not time_s:
        raise ValueError("No valid numeric rows were found in the selected data file.")

    return SavedOECTData(
        time_s=time_s,
        drain_i_a=drain_i_a,
        gate_v_v=gate_v_v,
        gate_i_a=gate_i_a,
    )
