from __future__ import annotations

import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, QThread, Qt, Signal
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import (
    QApplication,
    QAbstractButton,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .data_logger import finalize_run_directory
from .models import (
    CycleParams,
    InstrumentConfig,
    LinearParams,
    MeasurementSample,
)
from .instruments import check_instrument_links
from .plot_widgets import DualAxisTimePlot, LinePlot
from .saved_data import read_saved_oect_data
from .runner import ExperimentRunner


APP_DIR = Path(__file__).resolve().parents[1]


def _display_number(value: float | None, unit: str = "", signed: bool = False) -> str:
    """Show detailed readings without rounding small nonzero values to zero."""
    if value is None or not math.isfinite(value):
        return "--"
    if value == 0.0:
        value = 0.0  # Avoid displaying negative zero.
    sign = "+" if signed else ""
    magnitude = abs(value)
    decimals = 3 if unit == "s" else 6
    if unit == "A" or (magnitude != 0.0 and (magnitude < 10 ** -decimals or magnitude >= 10000.0)):
        number = f"{value:{sign}.6E}"
    else:
        number = f"{value:{sign}.{decimals}f}"
    return f"{number} {unit}".rstrip()


def _output_directory_status(path: str | Path) -> dict:
    output = Path(path).expanduser()
    if not output.exists():
        return {"ok": False, "message": f"does not exist: {output}", "path": str(output)}
    if not output.is_dir():
        return {"ok": False, "message": f"is not a directory: {output}", "path": str(output)}
    if not os.access(output, os.W_OK):
        return {"ok": False, "message": f"is not writable: {output}", "path": str(output)}
    return {"ok": True, "message": f"ready: {output}", "path": str(output)}


class ExperimentWorker(QThread):
    status = Signal(str)
    sample = Signal(object)
    finished = Signal(str)
    failed = Signal(str, str)
    pause_changed = Signal(bool)

    def __init__(self, mode: str, config: InstrumentConfig, params):
        super().__init__()
        self.mode = mode
        self.config = config
        self.params = params
        self.runner = ExperimentRunner(config, status_callback=self.status.emit, sample_callback=self.sample.emit,
                                       pause_callback=self.pause_changed.emit)

    def stop(self) -> None:
        self.runner.stop()

    def pause(self) -> None:
        self.runner.pause()

    def resume(self, params) -> None:
        self.runner.resume(params)

    def run(self):
        try:
            if self.mode == "linear":
                run_dir = self.runner.run_linear(self.params)
            elif self.mode == "cycle":
                run_dir = self.runner.run_cycle(self.params)
            else:
                raise ValueError(f"Unsupported experiment mode: {self.mode}")
            self.finished.emit(run_dir)
        except Exception as exc:
            run_dir = ""
            if self.runner.logger is not None:
                run_dir = str(self.runner.logger.run_dir)
            self.failed.emit(str(exc), run_dir)


class LinkCheckWorker(QThread):
    finished = Signal(dict)

    def __init__(self, config: InstrumentConfig, output_dir: str):
        super().__init__()
        self.config = config
        self.output_dir = output_dir

    def run(self):
        result = check_instrument_links(self.config)
        result["output_dir"] = _output_directory_status(self.output_dir)
        self.finished.emit(result)


class SavedDataPlotWindow(QMainWindow):
    """Separate window for viewing and comparing saved OECT data files."""

    def __init__(self, data_path: Path):
        super().__init__()
        self.primary_path = data_path
        self.primary_data = read_saved_oect_data(data_path)
        self.comparison_path: Path | None = None
        self.setWindowTitle(f"Saved OECT Data - {data_path.name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)

        header = QHBoxLayout()
        self.path_label = QLabel(f"Result 1: {data_path}")
        self.path_label.setWordWrap(True)
        self.path_label.setObjectName("instrumentNotice")
        self.compare_button = QPushButton("Compare Result")
        self.compare_button.clicked.connect(self.select_comparison_result)
        header.addWidget(self.path_label, 1)
        header.addWidget(self.compare_button)
        layout.addLayout(header)

        self.comparison_label = QLabel("")
        self.comparison_label.setWordWrap(True)
        self.comparison_label.setObjectName("instrumentNotice")
        self.comparison_label.setVisible(False)
        layout.addWidget(self.comparison_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self.drain_plot = LinePlot(
            "Drain I vs Time",
            "time (s)",
            "drain I (μA)",
            lambda _sample: (0.0, 0.0),
            reverse_y=True,
            scientific_ticks=False,
            primary_label="Current 1",
            secondary_label="Current 2",
        )
        self.combined_plot = DualAxisTimePlot(
            "Drain I and Gate V vs Time",
            drain_scale=1e6,
            drain_axis_label="drain I (μA)",
            scientific_drain_ticks=False,
            scientific_gate_ticks=False,
        )
        self.gate_voltage_plot = LinePlot(
            "Gate V vs Time",
            "time (s)",
            "gate V (V)",
            lambda _sample: (0.0, 0.0),
            reverse_y=True,
            scientific_ticks=False,
        )
        self.gate_current_plot = LinePlot(
            "Gate I vs Time",
            "time (s)",
            "gate I (μA)",
            lambda _sample: (0.0, 0.0),
            reverse_y=True,
            scientific_ticks=False,
        )
        self._load_primary_plots()

        grid.addWidget(self.drain_plot, 0, 0)
        grid.addWidget(self.combined_plot, 0, 1)
        grid.addWidget(self.gate_voltage_plot, 1, 0)
        grid.addWidget(self.gate_current_plot, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        layout.addLayout(grid, 1)
        self.resize(1250, 820)

    def _load_primary_plots(self) -> None:
        data = self.primary_data
        self.drain_plot.set_points(zip(data.time_s, (value * 1e6 for value in data.drain_i_a)))
        self.combined_plot.set_points(zip(data.time_s, data.drain_i_a, data.gate_v_v))
        self.gate_voltage_plot.set_points(zip(data.time_s, data.gate_v_v))
        self.gate_current_plot.set_points(zip(data.time_s, (value * 1e6 for value in data.gate_i_a)))

    def select_comparison_result(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Select comparison OECT data",
            str(self.primary_path.parent),
            "OECT data (data.txt *.txt *.csv);;All files (*)",
        )
        if not selected:
            return
        path = Path(selected)
        try:
            data = read_saved_oect_data(path)
        except Exception as exc:
            QMessageBox.critical(self, "Compare Result", str(exc))
            return

        self.comparison_path = path
        self.comparison_label.setText(f"Result 2: {path}")
        self.comparison_label.setVisible(True)
        self.compare_button.setText("Change Result 2")
        self.setWindowTitle(f"Saved OECT Comparison - {self.primary_path.name} / {path.name}")

        self.drain_plot.title = "Current 1 and Current 2 vs Time"
        self.drain_plot.set_secondary_points(
            zip(data.time_s, (value * 1e6 for value in data.drain_i_a)),
            label="Current 2",
        )
        self.combined_plot.title = "Drain I 1, Drain I 2 and Gate V vs Time"
        self.combined_plot.set_secondary_drain_points(zip(data.time_s, data.drain_i_a))


class DirectDoubleSpinBox(QDoubleSpinBox):
    """Numeric input edited by keyboard only; no arrow buttons or wheel changes."""

    def __init__(self):
        super().__init__()
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

    def wheelEvent(self, event) -> None:
        event.ignore()


class DirectSpinBox(QSpinBox):
    """Integer input edited by keyboard only; no arrow buttons or wheel changes."""

    def __init__(self):
        super().__init__()
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

    def wheelEvent(self, event) -> None:
        event.ignore()


class FixedDoubleSpinBox(DirectDoubleSpinBox):
    """Keep calculation precision while displaying ordinary decimal notation."""

    def __init__(self, decimals: int, suffix: str = ""):
        super().__init__()
        # ``decimals`` remains the internal precision used by QDoubleSpinBox.
        # Visible text is intentionally compact and is formatted separately.
        self._storage_decimals = decimals
        self.setDecimals(decimals)
        self.setSuffix(suffix)
        self.setKeyboardTracking(False)
        self.editingFinished.connect(self.normalize_text)

    def textFromValue(self, value: float) -> str:
        # Never display scientific notation in parameter inputs. Keep enough
        # fixed-point digits to preserve the value, then remove only redundant
        # trailing zeros.
        text = f"{value:.{self._storage_decimals}f}".rstrip("0").rstrip(".")
        if text in {"", "-0"}:
            text = "0"
        if "." not in text:
            text += ".0"
        return text

    def _numeric_text(self, text: str) -> str:
        numeric = text.strip()
        suffix = self.suffix().strip()
        prefix = self.prefix().strip()
        if suffix and numeric.endswith(suffix):
            numeric = numeric[: -len(suffix)].strip()
        if prefix and numeric.startswith(prefix):
            numeric = numeric[len(prefix) :].strip()
        return numeric

    def validate(self, text: str, position: int):
        numeric = self._numeric_text(text)
        if numeric in {"", "+", "-", ".", "+.", "-."}:
            return QValidator.State.Intermediate, text, position
        pattern = r"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))"
        if re.fullmatch(pattern, numeric) is None:
            return QValidator.State.Invalid, text, position
        try:
            value = float(numeric)
        except ValueError:
            return QValidator.State.Invalid, text, position
        if self.minimum() <= value <= self.maximum():
            return QValidator.State.Acceptable, text, position
        return QValidator.State.Intermediate, text, position

    def valueFromText(self, text: str) -> float:
        return float(self._numeric_text(text))

    def normalize_text(self) -> None:
        self.interpretText()
        self.lineEdit().setText(self.text())


class SpinFactory:
    @staticmethod
    def voltage(value: float, step: float = 0.01) -> QDoubleSpinBox:
        box = FixedDoubleSpinBox(6, " V")
        box.setRange(-20.0, 20.0)
        box.setSingleStep(step)
        box.setValue(value)
        return box

    @staticmethod
    def seconds(value: float, minimum: float = 0.0) -> QDoubleSpinBox:
        box = FixedDoubleSpinBox(2, " s")
        box.setRange(minimum, 100000.0)
        box.setSingleStep(1.0)
        box.setValue(value)
        return box

    @staticmethod
    def current(value: float) -> QDoubleSpinBox:
        box = FixedDoubleSpinBox(12, " A")
        box.setRange(1e-12, 10.0)
        box.setSingleStep(value)
        box.setValue(value)
        return box

    @staticmethod
    def count(value: int = 1) -> QSpinBox:
        box = DirectSpinBox()
        box.setRange(0, 1000)
        box.setValue(value)
        return box


class OECTExperimentWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OECT Experiment Module")
        self.worker: ExperimentWorker | None = None
        self.link_worker: LinkCheckWorker | None = None
        self._stop_requested = False
        self._paused = False
        self._close_when_finished = False
        self._collect_status_for_run = False
        self._current_run_status: list[str] = []
        self._saved_plot_windows: list[SavedDataPlotWindow] = []
        self.settings = QSettings("OECTTools", "OECTExperimentModule")
        self._geometry_restored = False
        self._build_ui()
        self._apply_style()
        self._load_settings()
        self._connect_setting_persistence()
        self._install_focus_clear_filters()
        self._update_instrument_fields()
        self._update_experiment_fields()
        self.update_connection_lights()

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)

        top = QHBoxLayout()
        self.mode_box = QComboBox()
        self.mode_box.addItems(["linear", "cycle"])
        self.backend_box = QComboBox()
        self.backend_box.addItems(["simulated", "keithley_2634b", "adcmt_6541", "real_2450_dmm"])
        self.backend_box.setCurrentText("keithley_2634b")
        self.run_name = QLineEdit("oect_test")
        self.output_dir = QLineEdit(str(APP_DIR / "experiment_data" / "oect"))
        self.plot_saved_button = QPushButton("Plot Saved Data")
        self.plot_saved_button.clicked.connect(self.open_saved_data_plot)
        top.addWidget(self.plot_saved_button)
        top.addWidget(QLabel("Run name"))
        top.addWidget(self.run_name, 1)
        top.addWidget(QLabel("Output"))
        top.addWidget(self.output_dir, 1)
        layout.addLayout(top)

        body = QHBoxLayout()
        left_panel = QVBoxLayout()
        self.tabs = QTabWidget()
        self.instrument_tab_widget = self._instrument_tab()
        self.experiment_tab_widget = self._experiment_tab()
        self.realtime_tab_widget = self._realtime_control_tab()
        self.tabs.addTab(self.instrument_tab_widget, "Instruments")
        self.tabs.addTab(self.experiment_tab_widget, "Experiment")
        self.tabs.addTab(self.realtime_tab_widget, "Real-time Control")
        self.tabs.setMinimumWidth(460)
        self.tabs.setMaximumWidth(540)
        left_panel.addWidget(self.tabs, 1)
        body.addLayout(left_panel, 0)

        plot_grid = QGridLayout()
        plot_grid.setContentsMargins(10, 0, 0, 0)
        plot_grid.setHorizontalSpacing(12)
        plot_grid.setVerticalSpacing(12)
        self.plot_drain_time = LinePlot("Drain I vs Time", "time (s)", "drain I (A)", lambda s: (s.elapsed_s, s.drain_i), reverse_y=True)
        self.plot_gate_current_time = LinePlot(
            "Gate Current vs Time", "time (s)", "gate I (A)",
            lambda s: (s.elapsed_s, float("nan") if s.gate_i is None else s.gate_i),
        )
        self.plot_gate_time = LinePlot("Gate V vs Time", "time (s)", "gate V (V)", lambda s: (s.elapsed_s, s.gate_v), reverse_y=True, scientific_ticks=False)
        self.plot_vref_time = LinePlot(
            "Vref vs Time (DMM only)",
            "time (s)",
            "Vref (V)",
            lambda s: (s.elapsed_s, float("nan") if s.vref_v is None else s.vref_v),
            reverse_y=True,
            scientific_ticks=False,
        )
        for plot in (self.plot_drain_time, self.plot_gate_current_time, self.plot_gate_time, self.plot_vref_time):
            plot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        plot_grid.addWidget(self.plot_drain_time, 0, 0)
        plot_grid.addWidget(self.plot_gate_current_time, 0, 1)
        plot_grid.addWidget(self.plot_gate_time, 1, 0)
        plot_grid.addWidget(self.plot_vref_time, 1, 1)
        plot_grid.setColumnStretch(0, 1)
        plot_grid.setColumnStretch(1, 1)
        plot_grid.setRowStretch(0, 1)
        plot_grid.setRowStretch(1, 1)
        body.addLayout(plot_grid, 1)
        layout.addLayout(body, 1)

        bottom = QHBoxLayout()
        self.status_label = QLabel("Ready.")
        self.source_light = QLabel("● SourceMeter")
        self.source_light.setObjectName("lightUnknown")
        self.dmm_light = QLabel("● DMM Vref")
        self.dmm_light.setObjectName("lightOff")
        self.running_light = QLabel("● Running")
        self.running_light.setObjectName("lightOff")
        self.check_links_button = QPushButton("Check Links")
        self.check_links_button.clicked.connect(self.check_links)
        self.start_button = QPushButton("Start")
        self.pause_button = QPushButton("Pause")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_experiment)
        self.stop_button.clicked.connect(self.stop_experiment)
        bottom.addWidget(self.status_label, 1)
        bottom.addWidget(self.source_light)
        bottom.addWidget(self.dmm_light)
        bottom.addWidget(self.running_light)
        bottom.addWidget(self.check_links_button)
        bottom.addWidget(self.start_button)
        bottom.addWidget(self.pause_button)
        bottom.addWidget(self.stop_button)
        layout.addLayout(bottom)

        self.mode_box.currentTextChanged.connect(self._on_configuration_changed)
        self.backend_box.currentTextChanged.connect(self._on_configuration_changed)
        self.cycle_reference_enabled.toggled.connect(self._on_configuration_changed)
        self.linear_reference_enabled.toggled.connect(self._on_configuration_changed)
        self._update_instrument_fields()
        self._update_experiment_fields()
        self.update_connection_lights()
        self.update_live_readout(None)

    def _setting_widgets(self) -> dict[str, QWidget]:
        return {
            "backend": self.backend_box,
            "mode": self.mode_box,
            "run_name": self.run_name,
            "output_dir": self.output_dir,
            "k2634b_ip": self.k2634b_ip,
            "drain_channel": self.drain_channel,
            "gate_channel": self.gate_channel,
            "adcmt_6541_ip": self.adcmt_6541_ip,
            "adcmt_drain_channel": self.adcmt_drain_channel,
            "adcmt_gate_channel": self.adcmt_gate_channel,
            "cycle_reference_enabled": self.cycle_reference_enabled,
            "linear_reference_enabled": self.linear_reference_enabled,
            "dmm_ip": self.dmm_ip,
            "drain_ip": self.drain_ip,
            "gate_ip": self.gate_ip,
            "timeout_ms": self.timeout_ms,
            "retries": self.retries,
            "drain_compliance": self.drain_compliance,
            "gate_compliance": self.gate_compliance,
            "current_nplc": self.current_nplc,
            "voltage_nplc": self.voltage_nplc,
            "drain_v": self.exp_drain_v,
            "sample_interval": self.exp_sample_interval,
            "gate_start": self.exp_gate_start,
            "gate_step": self.exp_gate_step,
            "step_duration": self.exp_step_duration,
            "skip_points": self.exp_skip_points,
            "linear_gate_end": self.lin_gate_end,
            "linear_hold_duration": self.lin_hold_duration,
            "linear_hold_forever": self.lin_hold_forever,
            "cycle_gate_max": self.cyc_gate_max,
            "cycle_hold_max": self.cyc_hold_max,
            "cycle_gate_min": self.cyc_gate_min,
            "cycle_hold_min": self.cyc_hold_min,
            "cycle_count": self.cyc_cycles,
            "vref_initial": self.vref_initial_v,
            "vref_target": self.vref_target_v,
            "liner_ref_start": self.liner_ref_start_v,
            "liner_ref_end": self.liner_ref_end_v,
            "liner_ref_intermediate_steps": self.liner_ref_intermediate_steps,
            "liner_ref_tolerance": self.liner_ref_tolerance_v,
            "linear_ref_target_timeout": self.linear_ref_target_timeout,
            "linear_ref_check_interval": self.linear_ref_check_interval,
            "vref_initial_gate": self.vref_initial_gate_v,
            "vref_gate_step": self.vref_gate_step_v,
            "vref_step_wait": self.vref_step_wait_time,
            "vref_sample_interval": self.vref_sample_interval,
            "vref_gate_min": self.vref_gate_min_v,
            "vref_gate_max": self.vref_gate_max_v,
        }

    def _load_settings(self) -> None:
        old_mode = str(self.settings.value("inputs/mode", "linear"))
        has_linear_reference_setting = self.settings.contains("inputs/linear_reference_enabled")
        for key, widget in self._setting_widgets().items():
            setting_key = f"inputs/{key}"
            if not self.settings.contains(setting_key):
                continue
            if isinstance(widget, QComboBox):
                value = str(self.settings.value(setting_key, ""))
                if key == "mode" and value in {"vref", "cycle/ref"}:
                    value = "cycle"
                elif key == "mode" and value == "liner/ref":
                    value = "linear"
                if widget.findText(value) >= 0:
                    widget.setCurrentText(value)
            elif isinstance(widget, QCheckBox):
                widget.setChecked(self.settings.value(setting_key, False, type=bool))
            elif isinstance(widget, QDoubleSpinBox):
                widget.setValue(self.settings.value(setting_key, widget.value(), type=float))
            elif isinstance(widget, QSpinBox):
                widget.setValue(self.settings.value(setting_key, widget.value(), type=int))
            elif isinstance(widget, QLineEdit):
                widget.setText(str(self.settings.value(setting_key, widget.text())))

        if old_mode == "liner/ref":
            self.linear_reference_enabled.setChecked(True)
            # The removed mode used to finish immediately after the last target.
            self.lin_hold_duration.setValue(0.0)
            self.lin_hold_forever.setChecked(False)
        elif not has_linear_reference_setting and old_mode == "linear":
            self.linear_reference_enabled.setChecked(self.settings.value("inputs/use_dmm_vref", False, type=bool))

        if old_mode in {"cycle/ref", "vref"}:
            self.cycle_reference_enabled.setChecked(True)
            self.cyc_cycles.setValue(self.settings.value("inputs/vref_cycles", self.cyc_cycles.value(), type=int))
        # An old measurement-only checkbox must not silently activate Gate feedback.
        # Old direct cycle stays direct unless the new cycle switch was explicitly saved.

        if self.settings.contains("window/active_tab"):
            index = self.settings.value("window/active_tab", 0, type=int)
            self.tabs.setCurrentIndex(max(0, min(index, self.tabs.count() - 1)))
        geometry = self.settings.value("window/geometry")
        if geometry is not None:
            self._geometry_restored = self.restoreGeometry(geometry)

    def _save_settings(self, *_args) -> None:
        for key, widget in self._setting_widgets().items():
            setting_key = f"inputs/{key}"
            if isinstance(widget, QComboBox):
                value = widget.currentText()
            elif isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, (QDoubleSpinBox, QSpinBox)):
                value = widget.value()
            elif isinstance(widget, QLineEdit):
                value = widget.text()
            else:
                continue
            self.settings.setValue(setting_key, value)
        self.settings.setValue("window/active_tab", self.tabs.currentIndex())
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.sync()

    def _connect_setting_persistence(self) -> None:
        for widget in self._setting_widgets().values():
            if isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(self._save_settings)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self._save_settings)
            elif isinstance(widget, QDoubleSpinBox):
                widget.valueChanged.connect(self._save_settings)
            elif isinstance(widget, QSpinBox):
                widget.valueChanged.connect(self._save_settings)
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._save_settings)
        self.tabs.currentChanged.connect(self._save_settings)

    def _install_focus_clear_filters(self) -> None:
        interactive = (QLineEdit, QAbstractSpinBox, QComboBox, QPlainTextEdit, QAbstractButton)
        widgets = [self.centralWidget(), *self.findChildren(QWidget)]
        for widget in widgets:
            if widget is not None and not isinstance(widget, interactive):
                widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            focus = QApplication.focusWidget()
            if isinstance(focus, QLineEdit):
                focus.clearFocus()
        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None:
        self._save_settings()
        if self.worker is not None and self.worker.isRunning():
            self._close_when_finished = True
            self.stop_experiment()
            event.ignore()
            return
        super().closeEvent(event)

    def open_saved_data_plot(self) -> None:
        default_dir = str(
            self.settings.value("plot/last_directory", self.output_dir.text())
            or self.output_dir.text()
        )
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Select saved OECT data",
            default_dir,
            "OECT data (data.txt *.txt *.csv);;All files (*)",
        )
        if not selected:
            return
        path = Path(selected)
        self.settings.setValue("plot/last_directory", str(path.parent))
        try:
            window = SavedDataPlotWindow(path)
        except Exception as exc:
            self._handle_status(f"Could not plot saved data: {exc}")
            QMessageBox.critical(self, "Plot Saved Data", str(exc))
            return
        window.setStyleSheet(self.styleSheet())
        window.destroyed.connect(lambda _obj=None, item=window: self._forget_saved_plot_window(item))
        self._saved_plot_windows.append(window)
        window.show()
        self._handle_status(f"Opened saved data plot: {path}")

    def _forget_saved_plot_window(self, window: SavedDataPlotWindow) -> None:
        if window in self._saved_plot_windows:
            self._saved_plot_windows.remove(window)

    def _realtime_control_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        readout = QGroupBox("Live Readout")
        readout.setObjectName("liveReadout")
        grid = QGridLayout(readout)
        self.live_drain_i = QLabel("--")
        self.live_gate_i = QLabel("--")
        self.live_gate_v = QLabel("--")
        self.live_drain_v = QLabel("--")
        self.live_vref = QLabel("--")
        self.live_ref_target = QLabel("--")
        self.live_ref_error = QLabel("--")
        self.live_vref_state = QLabel("--")
        self.live_gate_adjustment = QLabel("--")
        self.live_time = QLabel("--")
        labels = (
            self.live_drain_i, self.live_gate_i, self.live_gate_v, self.live_drain_v,
            self.live_vref, self.live_ref_target, self.live_ref_error,
            self.live_vref_state, self.live_gate_adjustment, self.live_time,
        )
        for label in labels:
            label.setObjectName("readoutValue")
            label.setMinimumWidth(120)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.live_vref_state.setWordWrap(True)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        rows = (
            ("Drain I", self.live_drain_i, "Gate I", self.live_gate_i),
            ("Drain V", self.live_drain_v, "Gate V", self.live_gate_v),
            ("Vref", self.live_vref, "Target Vref", self.live_ref_target),
            ("Vref error", self.live_ref_error, "Gate adjustment", self.live_gate_adjustment),
            ("Vref phase", self.live_vref_state, "Time", self.live_time),
        )
        for row, (left_name, left_value, right_name, right_value) in enumerate(rows):
            # Labels above values give detailed readings the full column width.
            grid.addWidget(QLabel(left_name), row * 2, 0)
            grid.addWidget(left_value, row * 2 + 1, 0)
            grid.addWidget(QLabel(right_name), row * 2, 1)
            grid.addWidget(right_value, row * 2 + 1, 1)
        layout.addWidget(readout)

        history_group = QGroupBox("Status History")
        history_layout = QVBoxLayout(history_group)
        self.status_history = QPlainTextEdit()
        self.status_history.setObjectName("statusHistory")
        self.status_history.setReadOnly(True)
        self.status_history.setMaximumBlockCount(0)
        self.status_history.setPlaceholderText("Experiment status messages will appear here.")
        history_layout.addWidget(self.status_history)
        layout.addWidget(history_group, 1)
        return tab

    def _instrument_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.instrument_form = form
        self.simulated_notice = QLabel("No instrument IP address is required in simulated mode.")
        self.simulated_notice.setWordWrap(True)
        self.simulated_notice.setObjectName("instrumentNotice")
        self.k2634b_ip = QLineEdit("192.168.1.150")
        self.drain_channel = QComboBox()
        self.drain_channel.addItems(["smua", "smub"])
        self.gate_channel = QComboBox()
        self.gate_channel.addItems(["smub", "smua"])
        self.adcmt_6541_ip = QLineEdit("192.168.1.204")
        self.adcmt_drain_channel = QComboBox()
        self.adcmt_drain_channel.addItems(["1", "2", "3", "4"])
        self.adcmt_gate_channel = QComboBox()
        self.adcmt_gate_channel.addItems(["2", "1", "3", "4"])
        self.dmm_ip = QLineEdit("192.168.1.160")
        self.drain_ip = QLineEdit("192.168.1.150")
        self.gate_ip = QLineEdit("192.168.1.160")
        self.source_link_label = QLabel("not checked")
        self.source_link_label.setObjectName("linkUnknown")
        self.dmm_link_label = QLabel("optional / disabled")
        self.dmm_link_label.setObjectName("linkUnknown")
        self.timeout_ms = DirectSpinBox()
        self.timeout_ms.setRange(100, 120000)
        self.timeout_ms.setValue(10000)
        self.retries = DirectSpinBox()
        self.retries.setRange(1, 10)
        self.retries.setValue(3)
        self.drain_compliance = SpinFactory.current(0.1)
        self.gate_compliance = SpinFactory.current(0.001)
        self.current_nplc = FixedDoubleSpinBox(2)
        self.current_nplc.setRange(0.01, 10.0)
        self.current_nplc.setValue(1.0)
        self.voltage_nplc = FixedDoubleSpinBox(2)
        self.voltage_nplc.setRange(0.01, 10.0)
        self.voltage_nplc.setValue(1.0)
        form.addRow("Backend", self.backend_box)
        form.addRow("Mode", self.mode_box)
        form.addRow(self.simulated_notice)
        form.addRow("2634B IP", self.k2634b_ip)
        form.addRow("2634B drain channel", self.drain_channel)
        form.addRow("2634B gate channel", self.gate_channel)
        form.addRow("ADCMT 6541 IP", self.adcmt_6541_ip)
        form.addRow("ADCMT drain channel", self.adcmt_drain_channel)
        form.addRow("ADCMT gate channel", self.adcmt_gate_channel)
        form.addRow("DMM IP", self.dmm_ip)
        form.addRow("SourceMeter link", self.source_link_label)
        form.addRow("DMM link", self.dmm_link_label)
        form.addRow("Drain Keithley IP", self.drain_ip)
        form.addRow("Gate Keithley IP", self.gate_ip)
        form.addRow("Timeout", self.timeout_ms)
        form.addRow("Retries", self.retries)
        form.addRow("Drain compliance", self.drain_compliance)
        form.addRow("Gate compliance", self.gate_compliance)
        form.addRow("Current NPLC", self.current_nplc)
        form.addRow("Voltage NPLC", self.voltage_nplc)
        return tab

    def _on_configuration_changed(self, *_args) -> None:
        self._update_instrument_fields()
        self._update_experiment_fields()
        self.update_connection_lights()

    def _set_instrument_row_visible(self, field: QWidget, visible: bool) -> None:
        field.setVisible(visible)
        label = self.instrument_form.labelForField(field)
        if label is not None:
            label.setVisible(visible)

    def _uses_dmm_vref(self) -> bool:
        switch = self.linear_reference_enabled if self.mode_box.currentText() == "linear" else self.cycle_reference_enabled
        return switch.isChecked()

    def _update_instrument_fields(self) -> None:
        backend = self.backend_box.currentText()
        is_simulated = backend == "simulated"
        is_2634b = backend == "keithley_2634b"
        is_adcmt = backend == "adcmt_6541"
        is_2450 = backend == "real_2450_dmm"
        dmm_enabled = self._uses_dmm_vref()

        self.simulated_notice.setVisible(is_simulated)

        self._set_instrument_row_visible(self.k2634b_ip, is_2634b)
        self._set_instrument_row_visible(self.drain_channel, is_2634b)
        self._set_instrument_row_visible(self.gate_channel, is_2634b)

        self._set_instrument_row_visible(self.adcmt_6541_ip, is_adcmt)
        self._set_instrument_row_visible(self.adcmt_drain_channel, is_adcmt)
        self._set_instrument_row_visible(self.adcmt_gate_channel, is_adcmt)

        self._set_instrument_row_visible(self.drain_ip, is_2450)
        self._set_instrument_row_visible(self.gate_ip, is_2450)
        self._set_instrument_row_visible(self.dmm_ip, dmm_enabled and not is_simulated)

        self._set_instrument_row_visible(self.source_link_label, not is_simulated)
        self._set_instrument_row_visible(self.dmm_link_label, dmm_enabled)

        common_visible = not is_simulated
        for field in (
            self.timeout_ms,
            self.retries,
            self.drain_compliance,
            self.gate_compliance,
        ):
            self._set_instrument_row_visible(field, common_visible)
        self._set_instrument_row_visible(self.current_nplc, is_2634b or is_2450)
        self._set_instrument_row_visible(self.voltage_nplc, dmm_enabled)

        self.check_links_button.setVisible(not is_simulated)

    def _experiment_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.experiment_form = form

        self.linear_reference_enabled = QCheckBox("Enable Reference feedback")
        self.linear_reference_enabled.setToolTip(
            "Off: control Gate directly, without Reference measurement. "
            "On: measure Reference and adjust Gate at the configured check interval to track Reference targets."
        )

        self.cycle_reference_enabled = QCheckBox("Enable Reference feedback")
        self.cycle_reference_enabled.setToolTip(
            "Off: cycle Gate directly without Reference measurement. "
            "On: evaluate Reference samples at each Gate step, reverse at the target, and return to the initial Reference."
        )

        # Shared parameters.
        self.exp_drain_v = SpinFactory.voltage(-0.01)
        self.exp_sample_interval = SpinFactory.seconds(1.0, 0.05)

        # Direct Gate-voltage parameters used by linear and cycle modes.
        self.exp_gate_start = SpinFactory.voltage(0.0)
        self.exp_gate_step = SpinFactory.voltage(-0.01)
        self.exp_step_duration = SpinFactory.seconds(4.0, 0.05)
        self.exp_skip_points = SpinFactory.count(1)

        # Linear-only parameters.
        self.lin_gate_end = SpinFactory.voltage(-0.8)
        self.lin_hold_duration = SpinFactory.seconds(60.0)
        self.lin_hold_forever = QCheckBox("Until Stop")
        self.lin_hold_container = QWidget()
        lin_hold_layout = QHBoxLayout(self.lin_hold_container)
        lin_hold_layout.setContentsMargins(0, 0, 0, 0)
        lin_hold_layout.setSpacing(8)
        lin_hold_layout.addWidget(self.lin_hold_forever)
        lin_hold_layout.addWidget(self.lin_hold_duration, 1)
        self.lin_hold_forever.toggled.connect(self._update_linear_hold_controls)

        # Cycle-only parameters.
        self.cyc_gate_max = SpinFactory.voltage(-0.8)
        self.cyc_hold_max = SpinFactory.seconds(30.0)
        self.cyc_gate_min = SpinFactory.voltage(0.0)
        self.cyc_hold_min = SpinFactory.seconds(30.0)
        self.cyc_cycles = DirectSpinBox()
        self.cyc_cycles.setRange(1, 1000)
        self.cyc_cycles.setValue(3)

        # Vref-triggered forward/backward Gate step parameters.
        self.vref_initial_v = SpinFactory.voltage(0.0)
        self.vref_target_v = SpinFactory.voltage(-0.8)
        self.liner_ref_start_v = SpinFactory.voltage(0.0)
        self.liner_ref_end_v = SpinFactory.voltage(-0.8)
        self.liner_ref_intermediate_steps = DirectSpinBox()
        self.liner_ref_intermediate_steps.setRange(0, 100000)
        self.liner_ref_intermediate_steps.setValue(7)
        self.liner_ref_intermediate_steps.setToolTip(
            "Number of evenly spaced targets between start and end, excluding both endpoints. "
            "N > 0 gives N + 2 targets. For a single target, use 0 and set start equal to end."
        )
        self.liner_ref_tolerance_v = FixedDoubleSpinBox(6, " V")
        self.liner_ref_tolerance_v.setRange(0.0, 20.0)
        self.liner_ref_tolerance_v.setValue(0.005)
        self.linear_ref_target_timeout = SpinFactory.seconds(300.0, 0.0)
        self.linear_ref_check_interval = SpinFactory.seconds(1.0, 0.05)
        self.linear_ref_check_interval.setToolTip(
            "Interval between Reference readings and Gate feedback decisions, including final hold. "
            "Default 1 s; for example, 2 s or 5 s. Instrument delays may extend the interval."
        )
        self.linear_ref_target_timeout.setToolTip(
            "Maximum continuous time outside tolerance, including during final hold. "
            "0 disables the timeout. Timing starts at target entry, or at the first out-of-band reading during hold."
        )
        self.vref_initial_gate_v = SpinFactory.voltage(0.0)
        self.vref_gate_step_v = FixedDoubleSpinBox(6, " V")
        self.vref_gate_step_v.setRange(0.000001, 20.0)
        self.vref_gate_step_v.setValue(0.05)
        self.vref_gate_step_v.setToolTip(
            "Fixed Gate adjustment outside the Reference tolerance band. "
            "A step that is too large can cause repeated overshoot; choose a smaller step if needed."
        )
        self.vref_step_wait_time = SpinFactory.seconds(30.0, 0.05)
        self.vref_sample_interval = SpinFactory.seconds(2.0, 0.05)
        self.vref_gate_min_v = SpinFactory.voltage(-1.0)
        self.vref_gate_max_v = SpinFactory.voltage(1.0)

        form.addRow("Reference control", self.linear_reference_enabled)
        form.addRow("Reference control", self.cycle_reference_enabled)
        form.addRow("Drain V", self.exp_drain_v)
        form.addRow("Sample interval", self.exp_sample_interval)

        form.addRow("Gate start V", self.exp_gate_start)
        form.addRow("Gate step V", self.exp_gate_step)
        form.addRow("Each step duration", self.exp_step_duration)
        form.addRow("Skip points after V change", self.exp_skip_points)
        form.addRow("Gate end V", self.lin_gate_end)
        form.addRow("Max voltage", self.cyc_gate_max)
        form.addRow("Hold at max", self.cyc_hold_max)
        form.addRow("Min voltage", self.cyc_gate_min)
        form.addRow("Hold at min", self.cyc_hold_min)
        form.addRow("Cycles", self.cyc_cycles)

        form.addRow("Initial Reference", self.vref_initial_v)
        form.addRow("Target Reference", self.vref_target_v)
        form.addRow("Reference start V", self.liner_ref_start_v)
        form.addRow("Reference end V", self.liner_ref_end_v)
        form.addRow("Reference intermediate steps", self.liner_ref_intermediate_steps)
        form.addRow("Reference tolerance ±", self.liner_ref_tolerance_v)
        form.addRow("Reference check interval", self.linear_ref_check_interval)
        form.addRow("Reference target timeout", self.linear_ref_target_timeout)
        form.addRow("Initial Gate V", self.vref_initial_gate_v)
        form.addRow("Gate voltage step", self.vref_gate_step_v)
        form.addRow("Wait at each Gate step", self.vref_step_wait_time)
        form.addRow("Reference sample interval", self.vref_sample_interval)
        form.addRow("Gate minimum limit", self.vref_gate_min_v)
        form.addRow("Gate maximum limit", self.vref_gate_max_v)
        form.addRow("Hold at end", self.lin_hold_container)
        return tab

    def _set_experiment_row_visible(self, field: QWidget, visible: bool) -> None:
        field.setVisible(visible)
        label = self.experiment_form.labelForField(field)
        if label is not None:
            label.setVisible(visible)

    def _update_linear_hold_controls(self, _checked: bool = False) -> None:
        is_linear = self.mode_box.currentText() == "linear"
        running = self.worker is not None and not self._paused
        self.lin_hold_forever.setEnabled(is_linear and not running)
        self.lin_hold_duration.setEnabled(is_linear and not running and not self.lin_hold_forever.isChecked())

    def _update_experiment_fields(self) -> None:
        mode = self.mode_box.currentText()
        is_linear = mode == "linear"
        is_cycle = mode == "cycle"
        is_cycle_ref = is_cycle and self.cycle_reference_enabled.isChecked()
        is_linear_ref = is_linear and self.linear_reference_enabled.isChecked()
        is_ref = is_cycle_ref or is_linear_ref
        running = self.worker is not None and not self._paused

        self._set_experiment_row_visible(self.linear_reference_enabled, is_linear)
        self.linear_reference_enabled.setEnabled(is_linear and self.worker is None)
        self._set_experiment_row_visible(self.cycle_reference_enabled, is_cycle)
        self.cycle_reference_enabled.setEnabled(is_cycle and self.worker is None)
        self.vref_gate_step_v.setToolTip(
            "Fixed Gate step between sample windows in each sweep direction." if is_cycle_ref else
            "Fixed Gate adjustment outside Reference tolerance; low Ref raises Gate, high Ref lowers Gate."
        )

        self.exp_drain_v.setEnabled(not running)
        self._set_experiment_row_visible(self.exp_sample_interval, not is_ref)
        self.exp_sample_interval.setEnabled(not running and not is_ref)

        direct_gate_fields = (
            self.exp_gate_start,
            self.exp_gate_step,
            self.exp_step_duration,
            self.exp_skip_points,
        )
        for field in direct_gate_fields:
            self._set_experiment_row_visible(field, not is_ref)
            field.setEnabled(not running and not is_ref)

        self._set_experiment_row_visible(self.lin_gate_end, is_linear and not is_linear_ref)
        self._set_experiment_row_visible(self.lin_hold_container, is_linear)
        self.lin_gate_end.setEnabled(not running and is_linear and not is_linear_ref)
        self.experiment_form.labelForField(self.lin_hold_container).setText(
            "Hold final Reference" if is_linear_ref else "Hold final Gate"
        )
        self.lin_hold_container.setToolTip(
            "Starts when the final target is first reached. 0 s ends immediately; Until Stop keeps running. "
            + ("Reference stays under active feedback during hold." if is_linear_ref else "Gate voltage stays fixed during hold.")
        )
        self._update_linear_hold_controls()

        for field in (
            self.cyc_gate_max,
            self.cyc_hold_max,
            self.cyc_gate_min,
            self.cyc_hold_min,
        ):
            self._set_experiment_row_visible(field, is_cycle and not is_cycle_ref)
            field.setEnabled(not running and is_cycle and not is_cycle_ref)
        self._set_experiment_row_visible(self.cyc_cycles, is_cycle)
        self.cyc_cycles.setEnabled(not running and is_cycle)

        for field in (self.vref_initial_v, self.vref_target_v):
            self._set_experiment_row_visible(field, is_cycle_ref)
            field.setEnabled(not running and is_cycle_ref)

        for field in (
            self.liner_ref_start_v,
            self.liner_ref_end_v,
            self.liner_ref_intermediate_steps,
            self.liner_ref_tolerance_v,
            self.linear_ref_target_timeout,
            self.linear_ref_check_interval,
        ):
            self._set_experiment_row_visible(field, is_linear_ref)
            field.setEnabled(not running and is_linear_ref)

        for field in (self.vref_initial_gate_v, self.vref_gate_step_v):
            self._set_experiment_row_visible(field, is_ref)
            field.setEnabled(not running and is_ref)
        self.vref_initial_gate_v.setEnabled(is_ref and self.worker is None)
        for field in (self.vref_gate_min_v, self.vref_gate_max_v):
            self._set_experiment_row_visible(field, True)
            field.setEnabled(not running)

        for field in (self.vref_step_wait_time, self.vref_sample_interval):
            self._set_experiment_row_visible(field, is_cycle_ref)
            field.setEnabled(not running and is_cycle_ref)

    def _set_run_ui_state(self, running: bool) -> None:
        # Tabs stay open during a run. Only settings that would invalidate the active connection/sequence are locked.
        self.backend_box.setEnabled(not running)
        # Run name remains editable. The finished run folder is renamed using
        # the value present when the task ends.
        self.run_name.setEnabled(True)
        self.output_dir.setEnabled(not running)
        self.mode_box.setEnabled(not running)
        self.cycle_reference_enabled.setEnabled(not running)
        self.linear_reference_enabled.setEnabled(not running)
        self.check_links_button.setEnabled(not running)

        for field in (
            self.k2634b_ip,
            self.drain_channel,
            self.gate_channel,
            self.adcmt_6541_ip,
            self.adcmt_drain_channel,
            self.adcmt_gate_channel,
            self.dmm_ip,
            self.drain_ip,
            self.gate_ip,
            self.timeout_ms,
            self.retries,
            self.drain_compliance,
            self.gate_compliance,
            self.current_nplc,
            self.voltage_nplc,
        ):
            field.setEnabled(not running)

        if running:
            self.tabs.setCurrentWidget(self.realtime_tab_widget)
        self._update_instrument_fields()
        self._update_experiment_fields()

    def _append_status(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.status_history.appendPlainText(line)
        if self._collect_status_for_run:
            self._current_run_status.append(line)
        scrollbar = self.status_history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _export_current_status(self, run_dir: str) -> None:
        lines = list(self._current_run_status)
        self._collect_status_for_run = False
        if not lines:
            return
        try:
            if run_dir:
                destination = Path(run_dir) / "status_history.txt"
            else:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", self.run_name.text()).strip("_") or "oect_run"
                destination = Path(self.output_dir.text()).expanduser() / f"{stamp}_{safe_name}_status_history.txt"
            if not destination.parent.exists():
                raise FileNotFoundError(f"Output directory does not exist: {destination.parent}")
            destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.status_label.setText(f"Status history saved: {destination}")
        except Exception as exc:
            self._append_status(f"Status history export failed: {exc}")

    def _handle_status(self, message: str) -> None:
        self.status_label.setText(message)
        self._append_status(message)

    def update_connection_lights(self) -> None:
        backend = self.backend_box.currentText()
        if backend == "simulated":
            self.set_light(self.source_light, "SourceMeter", False)
            self.set_light(self.dmm_light, "DMM Vref", False)
        elif backend in {"keithley_2634b", "adcmt_6541"}:
            self.set_light(self.source_light, "SourceMeter", None)
            self.set_light(self.dmm_light, "DMM Vref", None if self._uses_dmm_vref() else False)
        else:
            self.set_light(self.source_light, "SourceMeter", None)
            self.set_light(self.dmm_light, "DMM Vref", None)

    def check_links(self) -> None:
        if self.link_worker is not None:
            return
        self._handle_status("Checking instrument links...")
        self.check_links_button.setEnabled(False)
        self.set_link_label(self.source_link_label, "checking...", None)
        self.set_link_label(self.dmm_link_label, "checking...", None)
        self.set_light(self.source_light, "SourceMeter", None)
        self.set_light(self.dmm_light, "DMM Vref", None if self._uses_dmm_vref() else False)
        self.link_worker = LinkCheckWorker(self._instrument_config(), self.output_dir.text())
        self.link_worker.finished.connect(self.on_links_checked)
        self.link_worker.start()

    def on_links_checked(self, result: dict) -> None:
        source = result.get("source_meter", {})
        dmm = result.get("dmm", {})
        self.set_link_label(self.source_link_label, source.get("message", "unknown"), source.get("ok"))
        self.set_link_label(self.dmm_link_label, dmm.get("message", "unknown"), dmm.get("ok"))
        self.set_light(self.source_light, "SourceMeter", source.get("ok"))
        dmm_required = bool(dmm.get("required"))
        self.set_light(self.dmm_light, "DMM Vref", dmm.get("ok") if dmm_required else False)
        source_ok = bool(source.get("ok"))
        dmm_ok = bool(dmm.get("ok"))
        output = result.get("output_dir", _output_directory_status(self.output_dir.text()))
        output_ok = bool(output.get("ok"))
        if source_ok and (dmm_ok or not dmm_required):
            link_message = "Links ready."
        elif source_ok:
            link_message = "SourceMeter ready. DMM is not connected."
        else:
            link_message = "SourceMeter is not connected."
        if output_ok:
            self._handle_status(f"{link_message} Output directory ready.")
        else:
            self._handle_status(f"{link_message} Output directory warning: {output.get('message', 'not ready')}.")
        self.check_links_button.setEnabled(True)
        self.link_worker = None

    def set_light(self, label: QLabel, name: str, ok: bool | None) -> None:
        if ok is True:
            label.setObjectName("lightOk")
            label.setText(f"● {name}")
        elif ok is False:
            label.setObjectName("lightOff")
            label.setText(f"● {name}")
        else:
            label.setObjectName("lightUnknown")
            label.setText(f"● {name}")
        label.style().unpolish(label)
        label.style().polish(label)

    def set_link_label(self, label: QLabel, text: str, ok: bool | None) -> None:
        compact = text.strip() if text else "unknown"
        if len(compact) > 58:
            compact = compact[:55] + "..."
        if ok is True:
            label.setObjectName("linkOk")
            label.setText(f"Connected - {compact}")
        elif ok is False:
            label.setObjectName("linkBad")
            label.setText(f"Offline - {compact}")
        else:
            label.setObjectName("linkUnknown")
            label.setText(compact)
        label.style().unpolish(label)
        label.style().polish(label)

    def _confirm_output_directory(self) -> bool:
        status = _output_directory_status(self.output_dir.text())
        if status["ok"]:
            return True
        message = (
            "The selected Output directory is not available. No experiment data can be saved.\n\n"
            f"{status['message']}"
        )
        self._handle_status(f"Cannot start: Output directory {status['message']}.")
        QMessageBox.warning(self, "Output Directory", message)
        return False

    def start_experiment(self) -> None:
        if not self._confirm_output_directory():
            return
        self._clear_plots()
        mode = self.mode_box.currentText()
        config = self._instrument_config()
        try:
            params = self._experiment_params(mode)
            params.validate()
        except ValueError as exc:
            self._handle_status(f"Error: {exc}")
            return

        self._stop_requested = False
        self._paused = False
        self._current_run_status = []
        self._collect_status_for_run = True
        self._handle_status(f"Task started: {mode} mode.")
        if config.backend == "simulated":
            self._handle_status("Running SIMULATED data. No instrument is being read.")

        self.worker = ExperimentWorker(mode, config, params)
        self.worker.status.connect(self._handle_status)
        self.worker.sample.connect(self.on_sample)
        self.worker.pause_changed.connect(self.on_pause_changed)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.pause_button.setText("Pause")
        self.pause_button.setEnabled(True)
        self._set_run_ui_state(True)
        self.set_light(self.running_light, "Running", True)
        self.worker.start()

    def toggle_pause(self) -> None:
        if self.worker is None or self._stop_requested:
            return
        if not self._paused:
            self.pause_button.setEnabled(False)
            self.pause_button.setText("Pausing...")
            self._handle_status("Pause requested. Waiting for the current instrument operation to finish...")
            self.worker.pause()
            return
        try:
            params = self._experiment_params(self.worker.mode)
            self.worker.resume(params)
        except ValueError as exc:
            self._handle_status(f"Still paused: {exc}")
            return
        self.pause_button.setEnabled(False)
        self.pause_button.setText("Resuming...")
        # Lock edits as soon as the validated parameter snapshot has been submitted.
        self._paused = False
        self._update_experiment_fields()

    def on_pause_changed(self, paused: bool) -> None:
        if self.worker is None or self._stop_requested:
            return
        self._paused = paused
        self.pause_button.setText("Resume" if paused else "Pause")
        self.pause_button.setEnabled(True)
        self.set_light(self.running_light, "Paused" if paused else "Running", not paused)
        self._update_experiment_fields()

    def stop_experiment(self) -> None:
        if self.worker is not None:
            self._stop_requested = True
            self._handle_status("Stop requested. Disabling outputs safely...")
            self.worker.stop()
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)

    def on_sample(self, sample: MeasurementSample) -> None:
        if self._paused:
            return
        self.plot_drain_time.add_sample(sample)
        self.plot_gate_current_time.add_sample(sample)
        self.plot_gate_time.add_sample(sample)
        self.plot_vref_time.add_sample(sample)
        self.update_live_readout(sample)

    def _finalize_run_name(self, run_dir: str) -> str:
        if not run_dir:
            return run_dir
        try:
            final_dir = finalize_run_directory(run_dir, self.run_name.text())
            if str(final_dir) != str(run_dir):
                self._handle_status(f"Run name applied. Final folder: {final_dir}")
            return str(final_dir)
        except Exception as exc:
            self._handle_status(f"Could not apply final Run name: {exc}")
            return run_dir

    def on_finished(self, run_dir: str) -> None:
        self._finish_run(run_dir)

    def on_failed(self, error: str, run_dir: str) -> None:
        self._finish_run(run_dir, error)

    def _finish_run(self, run_dir: str, error: str | None = None) -> None:
        # The worker signals after cleanup; let run() return before releasing it.
        if self.worker is not None:
            self.worker.wait()
        run_dir = self._finalize_run_name(run_dir)
        if error is not None:
            self._handle_status(f"Task failed: {error}")
        else:
            result = "stopped" if self._stop_requested else "completed"
            self._handle_status(f"Task {result}. Data saved: {run_dir}")
        self._export_current_status(run_dir)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.worker = None
        self.pause_button.setText("Pause")
        self.pause_button.setEnabled(False)
        self._stop_requested = False
        self._paused = False
        self.set_light(self.running_light, "Running", False)
        self._set_run_ui_state(False)
        if self._close_when_finished:
            self.close()

    def _clear_plots(self) -> None:
        for plot in (self.plot_drain_time, self.plot_gate_current_time, self.plot_gate_time, self.plot_vref_time):
            plot.clear()
        self.update_live_readout(None)

    def update_live_readout(self, sample: MeasurementSample | None) -> None:
        if sample is None:
            values = {
                self.live_drain_i: "--", self.live_gate_i: "--",
                self.live_gate_v: "--", self.live_drain_v: "--",
                self.live_vref: "--", self.live_ref_target: "--",
                self.live_ref_error: "--", self.live_vref_state: "--",
                self.live_gate_adjustment: "--", self.live_time: "--",
            }
        else:
            state_names = {
                "cycle_ref_forward_sampling": "CYCLE REF FORWARD",
                "cycle_ref_backward_sampling": "CYCLE REF BACKWARD",
                "linear_ref_adjust_up": "REF: GATE UP",
                "linear_ref_adjust_down": "REF: GATE DOWN",
                "linear_ref_in_tolerance": "REF: IN TOLERANCE",
                "direct_gate": "DIRECT GATE",
            }
            values = {
                self.live_drain_i: _display_number(sample.drain_i, "A"),
                self.live_gate_i: _display_number(sample.gate_i, "A"),
                self.live_gate_v: _display_number(sample.gate_v, "V"),
                self.live_drain_v: _display_number(sample.drain_v, "V"),
                self.live_vref: "--" if sample.vref_v is None else _display_number(sample.vref_v, "V"),
                self.live_ref_target: (
                    "--" if sample.reference_target_v is None else _display_number(sample.reference_target_v, "V")
                ),
                self.live_ref_error: (
                    "--" if sample.reference_error_v is None else _display_number(sample.reference_error_v, "V")
                ),
                self.live_vref_state: state_names.get(sample.vref_state, sample.vref_state or "--"),
                self.live_gate_adjustment: (
                    "--" if sample.gate_adjustment_v is None else _display_number(sample.gate_adjustment_v, "V", signed=True)
                ),
                self.live_time: _display_number(sample.elapsed_s, "s"),
            }
        for label, text in values.items():
            label.setText(text)

    def _instrument_config(self) -> InstrumentConfig:
        return InstrumentConfig(
            backend=self.backend_box.currentText(),
            dmm_ip=self.dmm_ip.text().strip(),
            drain_smu_ip=self.drain_ip.text().strip(),
            gate_smu_ip=self.gate_ip.text().strip(),
            keithley_2634b_ip=self.k2634b_ip.text().strip(),
            drain_channel=self.drain_channel.currentText(),
            gate_channel=self.gate_channel.currentText(),
            adcmt_6541_ip=self.adcmt_6541_ip.text().strip(),
            adcmt_drain_channel=int(self.adcmt_drain_channel.currentText()),
            adcmt_gate_channel=int(self.adcmt_gate_channel.currentText()),
            use_dmm_vref=self._uses_dmm_vref(),
            timeout_ms=self.timeout_ms.value(),
            retries=self.retries.value(),
            drain_compliance_a=self.drain_compliance.value(),
            gate_compliance_a=self.gate_compliance.value(),
            current_nplc=self.current_nplc.value(),
            voltage_nplc=self.voltage_nplc.value(),
        )

    def _experiment_params(self, mode: str):
        builders = {"linear": self._linear_params, "cycle": self._cycle_params}
        if mode not in builders:
            raise ValueError(f"Unsupported experiment mode: {mode}")
        return builders[mode]()

    def _shared_experiment_values(self) -> dict:
        return dict(
            run_name=self.run_name.text(), output_dir=self.output_dir.text(),
            drain_voltage=self.exp_drain_v.value(), gate_start=self.exp_gate_start.value(),
            gate_step=self.exp_gate_step.value(), step_duration_s=self.exp_step_duration.value(),
            sample_interval_s=self.exp_sample_interval.value(),
            skip_points_after_voltage_change=self.exp_skip_points.value(),
            initial_gate_voltage=self.vref_initial_gate_v.value(),
            gate_step_voltage=self.vref_gate_step_v.value(),
            gate_min_voltage=self.vref_gate_min_v.value(), gate_max_voltage=self.vref_gate_max_v.value(),
        )

    def _linear_params(self) -> LinearParams:
        return LinearParams(
            **self._shared_experiment_values(),
            gate_end=self.lin_gate_end.value(), hold_duration_s=self.lin_hold_duration.value(),
            hold_forever=self.lin_hold_forever.isChecked(),
            reference_control_enabled=self.linear_reference_enabled.isChecked(),
            vref_start=self.liner_ref_start_v.value(), vref_end=self.liner_ref_end_v.value(),
            vref_intermediate_steps=self.liner_ref_intermediate_steps.value(),
            reference_tolerance_v=self.liner_ref_tolerance_v.value(),
            reference_target_timeout_s=self.linear_ref_target_timeout.value(),
            reference_check_interval_s=self.linear_ref_check_interval.value(),
        )

    def _cycle_params(self) -> CycleParams:
        return CycleParams(
            **self._shared_experiment_values(),
            gate_max=self.cyc_gate_max.value(), hold_at_max_s=self.cyc_hold_max.value(),
            gate_min=self.cyc_gate_min.value(), hold_at_min_s=self.cyc_hold_min.value(),
            cycles=self.cyc_cycles.value(),
            reference_control_enabled=self.cycle_reference_enabled.isChecked(),
            vref_initial=self.vref_initial_v.value(), vref_target=self.vref_target_v.value(),
            step_wait_time_s=self.vref_step_wait_time.value(),
            reference_sample_interval_s=self.vref_sample_interval.value(),
        )

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #0f131a;
                color: #eef1f7;
                font-size: 13px;
            }
            QGroupBox, QTabWidget::pane {
                border: 1px solid #384253;
                border-radius: 8px;
                margin-top: 10px;
                background: #10161f;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: #aeb7c8;
                font-weight: 800;
            }
            QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {
                min-height: 30px;
                border: 1px solid #404a5f;
                border-radius: 7px;
                background: #171d27;
                color: #eef1f7;
                padding: 2px 8px;
                selection-background-color: #18c9a6;
            }
            QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {
                border-color: #60e8b8;
                background: #1b2430;
            }
            QPushButton {
                min-height: 32px;
                border: 1px solid #4d596d;
                border-radius: 6px;
                background: #202735;
                color: #eef1f7;
                padding: 0 14px;
            }
            QPushButton:hover {
                border-color: #60e8b8;
            }
            QToolButton#plotResetButton {
                border: 1px solid #4d596d;
                border-radius: 5px;
                background: #202735;
                padding: 2px;
            }
            QToolButton#plotResetButton:hover {
                border-color: #60e8b8;
                background: #273142;
            }
            QPushButton:disabled {
                color: #737d91;
            }
            QLabel#linkOk {
                color: #60e8b8;
                font-weight: 800;
            }
            QLabel#linkBad {
                color: #ff6b7a;
                font-weight: 800;
            }
            QLabel#linkUnknown {
                color: #aeb7c8;
                font-weight: 700;
            }
            QLabel#lightOk {
                color: #60e8b8;
                font-size: 14px;
                font-weight: 900;
                padding: 5px 10px;
                border: 1px solid rgba(96, 232, 184, 130);
                border-radius: 13px;
                background: rgba(96, 232, 184, 28);
            }
            QLabel#lightOff {
                color: #5f6878;
                font-size: 14px;
                font-weight: 800;
                padding: 5px 10px;
                border: 1px solid #303846;
                border-radius: 13px;
                background: #151a22;
            }
            QLabel#lightUnknown {
                color: #ffd166;
                font-size: 14px;
                font-weight: 900;
                padding: 5px 10px;
                border: 1px solid rgba(255, 209, 102, 120);
                border-radius: 13px;
                background: rgba(255, 209, 102, 22);
            }
            QGroupBox#liveReadout {
                background: #121923;
                border: 1px solid #3a4557;
                border-radius: 8px;
                padding: 8px;
            }
            QLabel#readoutValue {
                color: #60e8b8;
                font-family: Consolas, Menlo, Monaco, monospace;
                font-size: 14px;
                font-weight: 800;
                background: #0c1118;
                border: 1px solid #263142;
                border-radius: 6px;
                padding: 5px 8px;
            }
            QPlainTextEdit#statusHistory {
                min-height: 180px;
                background: #0c1118;
                color: #d8deea;
                border: 1px solid #263142;
                border-radius: 6px;
                padding: 8px;
                font-family: Menlo, Monaco, monospace;
                font-size: 12px;
            }
            QTabBar::tab {
                background: #171d27;
                border: 1px solid #384253;
                padding: 8px 14px;
                border-top-left-radius: 7px;
                border-top-right-radius: 7px;
            }
            QTabBar::tab:selected {
                background: #233044;
                color: #60e8b8;
                font-weight: 800;
            }
            """
        )


def main() -> int:
    app = QApplication(sys.argv)
    window = OECTExperimentWindow()
    window.setMinimumSize(1180, 760)
    if not window._geometry_restored:
        window.resize(1600, 940)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
