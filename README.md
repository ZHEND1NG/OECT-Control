# OECT Experiment Control

A desktop application for organic electrochemical transistor (OECT) experiments. It supports Gate-voltage sweeps, Reference feedback, cyclic measurements, live plots, and data logging.

The application has two modes: `linear` and `cycle`. Each mode saves its own **Enable Reference feedback** setting. With the switch off, the program scans Gate voltage directly. With it on, the program measures Reference voltage and uses it to control Gate voltage.

## 1. Repository structure

```text
repository-root/
├── README.md
├── requirements.txt
├── .gitignore
├── start_oect.bat
├── OECT/
│   ├── __init__.py
│   ├── cli.py
│   ├── gui.py
│   ├── models.py
│   ├── runner.py
│   ├── instruments.py
│   ├── data_logger.py
│   ├── plot_widgets.py
│   ├── saved_data.py
│   └── UPDATE_CN.md
├── docs/
│   ├── CONTROL_DETAILS.md
│   └── VALIDATION.md
└── experiment_data/
    └── oect/
        └── .gitkeep
```

This repository contains the complete source package. Keep `README.md` and the `OECT/` folder at the same level. Preserve the uppercase package name `OECT` in both the folder name and launch commands.

## 2. Installation and startup on Windows

Use Python 3.10 or newer; 64-bit Python is recommended. Local validation used Windows, Python 3.14.6, PySide6 6.11.1, and PyVISA 1.16.2. Other version combinations have not been individually verified.

1. Install [Python](https://www.python.org/downloads/), then download and extract the complete project.
2. Open PowerShell or a terminal in the repository root, where this README is located.
3. Run these commands in order:

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m OECT.cli
```

If your computer provides `py` instead of `python`, replace `python` with `py -3` in the first two commands. Continue using the `.venv` interpreter in the remaining commands. This approach does not require activating the environment or changing PowerShell execution policy.

After installation, you can double-click **`start_oect.bat`** in the repository root. It uses this project's `.venv` and prompts you to install the environment if it is missing.

If you already have a Python environment with the required dependencies, run this command from the repository root:

```text
python -m OECT.cli
```

Use the module launch command. Double-clicking `gui.py` or running `python OECT/gui.py` directly causes a relative-import error.

PySide6 supplies the GUI and plotting components. Its pip wheels include Qt, so a separate Qt installation is normally unnecessary. See the [official Qt installation guide](https://doc.qt.io/qtforpython-6/gettingstarted.html).

## 3. First run: a simulated experiment

Simulation does not connect to instruments. Its generated data is intended to check the software workflow.

1. On the **Instruments** tab, set **Backend** to **`simulated`** and **Mode** to **`linear`**. The initial selection may be `keithley_2634b`, or the application may restore a previous setting, so check it explicitly.
2. Enter a **Run name**, such as `simulation_test`. **Output** must point to an existing, writable directory. This package includes `experiment_data/oect`, which is the default on a fresh configuration. If an old path is restored, change it to a valid local directory.
3. On the **Experiment** tab, turn off **Enable Reference feedback** and use these simulation settings:

| Parameter | Value |
|---|---:|
| Drain V | -0.01 V |
| Gate start V | 0 V |
| Gate end V | -0.1 V |
| Gate step V | -0.02 V |
| Each step duration | 1 s |
| Sample interval | 0.2 s |
| Skip points after V change | 0 |
| Gate minimum limit | -1 V |
| Gate maximum limit | 1 V |
| Hold final Gate | 0 s; Until Stop unchecked |

4. Click **Check Links** to check the simulated connection and output directory, then click **Start**.
5. The run should finish in approximately six seconds. Check that the plots contain data and that a new run folder appears inside the output directory.

These values are for simulation. For a physical device, choose voltages, current limits, and Gate limits appropriate to that device.

## 4. Linear mode

### Reference off: direct Gate sweep

Set **Gate start V**, **Gate end V**, and **Gate step V**. The step must be nonzero. The program determines the direction from the endpoints and includes the exact final voltage. **Each step duration** sets the dwell time at each Gate voltage; **Sample interval** sets the recording interval.

**Skip points after V change** controls the initial sampling delay after a voltage change. A short dwell with a large skip count may leave no samples to record at that voltage.

### Reference on: Gate feedback

| Parameter | Meaning |
|---|---|
| Reference start V / end V | First and last Reference targets |
| Reference intermediate steps | Number of evenly spaced intermediate targets, **excluding both endpoints** |
| Reference tolerance ± | Allowed absolute error, in volts |
| Initial Gate V | Gate voltage applied at the beginning of the run |
| Gate voltage step | Fixed magnitude of each Gate adjustment; must be greater than zero |
| Reference check interval | Interval between readings and feedback decisions; default 1 s, minimum 0.05 s; values such as 2 s and 5 s are supported |
| Reference target timeout | Timeout for tracking a target; default 300 s, or 0 to disable; when enabled, it must exceed the check interval |
| Gate minimum / maximum limit | Allowed Gate-voltage range |

For example, `start = 0 V`, `end = -0.6 V`, and `intermediate steps = 2` produce the targets `0, -0.2, -0.4, -0.6 V`. With zero intermediate steps, start and end must be equal, and the program tracks that single target. With a positive intermediate count, the endpoints must be different.

Each check applies these rules:

```text
Ref < target - tolerance  -> Increase Gate by one Gate voltage step
Ref > target + tolerance  -> Decrease Gate by one Gate voltage step
target - tolerance <= Ref <= target + tolerance -> Current target reached
```

The first target is actively tracked too. The first reading occurs after one check interval. Recording and feedback checks share this interval: setting it to 5 s also records measurements at that interval. Instrument communication may extend the actual interval; delayed checks do not trigger a burst of catch-up Gate adjustments.

This feedback assumes that increasing Gate voltage increases Reference voltage. A large step or slow device response can cause repeated adjustments. Choose the step, tolerance, and check interval to suit the observed response.

### Final hold

Set **Hold final Gate** or **Hold final Reference** to 0 s to finish at the end of the sweep or target sequence, to a positive value for a timed hold, or select **Until Stop** for a continuous hold. With Reference off, Gate stays fixed. With Reference on, feedback continues at the final Reference target.

Reference hold time starts when the final target is first reached. It counts active experiment time and does not require the reading to remain within tolerance throughout the hold.

## 5. Cycle mode

With Reference off, Gate sweeps from its start voltage to **Max voltage**, then returns to **Min voltage**, for the selected number of **Cycles**. Max/Min are historical names for the forward and return endpoints; the forward endpoint may be more negative than the return endpoint. Each endpoint has its own hold duration.

With Reference on, the program retains the cyclic Reference algorithm: it starts at **Initial Gate V**, collects multiple Reference readings at each Gate voltage, progresses toward **Target Reference**, then reverses direction to return to **Initial Reference**. It repeats this sequence for the requested cycle count. The first forward leg does not first acquire Initial Reference.

This branch uses **Wait at each Gate step** and **Reference sample interval**, with a target-crossing or tolerance decision. The GUI retains a 0.5% tolerance and a 90% sample threshold; a qualifying sample average can also satisfy the target. A zero Reference target has a minimum tolerance of 1e-6 V.

Cycle Reference uses a different decision rule from linear's absolute-tolerance feedback. Linear's **Reference check interval** does not control cycle mode.

## 6. Pause, edit, resume, and stop

- **Pause:** the button shows `Pausing...` until the current instrument operation finishes, then changes to `Resume`. Once paused, displayed readings, plots, and measurement rows stop updating; feedback and the active experiment clock stop.
- **Gate and Drain retain their last outputs while paused.** Physical device conditions may still change.
- Edit the available experiment parameters while paused. **Resume** validates and applies them, then continues recording in the same data file. It does not backfill the paused interval.
- Changing tolerance, the feedback Gate step, or the linear check interval normally retains the current target. Changing sweep endpoints or the intermediate target count starts a new scan segment. Parameter changes are recorded in `metadata.json`.
- Mode, the Reference switch, instrument connections, output directory, and startup-only Initial Gate V remain locked during a run.
- **Stop** ends the run and performs instrument output and connection cleanup. An in-progress instrument request may need to return or reach its communication timeout before the stop request can be processed.

## 7. Connecting instruments

Stop any simulated run, select the appropriate **Backend** on the Instruments tab, and enter the actual IP addresses, distinct Drain/Gate channels, and suitable current limits and other settings. When Reference is enabled, configure the DMM connection. With Reference disabled, the program does not request Reference readings.

| Backend | Implemented devices and connection |
|---|---|
| simulated | Generated data; no physical instruments |
| keithley_2634b | Keithley 2634B dual channels, default Drain=smua and Gate=smub; DMM6500 for Reference; raw TCP sockets |
| adcmt_6541 | ADCMT 6541 with distinct Drain/Gate channels selected from 1–4; DMM6500 for Reference; PyVISA |
| real_2450_dmm | Two Keithley 2450 units for Drain and Gate; DMM6500 for Reference; PyVISA |

The ADCMT and dual-2450 backends require a working VISA backend, such as NI-VISA or Keysight IO Libraries, in addition to the Python packages. Installing `PyVISA` alone does not install a VISA driver. Python and the VISA library must use matching bitness. See the [official PyVISA installation guide](https://pyvisa.readthedocs.io/en/latest/introduction/getting.html).

Click **Check Links** after entering the connection settings, then start the experiment. The default IP addresses are examples and should be replaced with actual device addresses. Release checks used simulation; physical instruments were not tested.

## 8. Plots and saved data

Live plots show Drain I, Gate I, Gate V, and Vref. Missing Gate I values are omitted from the plot and displayed as `--`; they are not replaced with zero. Live voltage readouts normally show six decimal places. Axis precision adapts to zoom, allowing finer voltage differences to be distinguished. Display precision does not increase instrument accuracy.

Use the mouse wheel to zoom and drag with the left mouse button to pan. Controls at the lower left of each plot switch between XY/X/Y zoom and reset the view. **Plot Saved Data** opens saved TXT/CSV results; **Compare Result** in the saved-data window loads a second result for comparison.

Each GUI run creates a separate folder under **Output**:

| File | Contents |
|---|---|
| data.txt | Tab-separated measurements with a header |
| metadata.json | Initial parameters, instrument settings, and pause/resume and parameter-change events |
| run.log | Experiment log |
| status_history.txt | GUI status history |

With Reference enabled, the data also contains measured Reference, target, error, Gate adjustment, and control phase. `Time_s` measures active experiment time and excludes pauses.

## 9. Troubleshooting

| Symptom | Action |
|---|---|
| No module named PySide6 | Install requirements.txt with the project's `.venv` interpreter and launch with that same interpreter |
| No module named OECT | Start from the repository root; confirm that `OECT/` is beside README.md and contains `__init__.py` |
| attempted relative import | Use `python -m OECT.cli` instead of running gui.py directly |
| Output directory does not exist | Create the directory first, or change Output to an existing writable directory |
| Old IP addresses, paths, or parameters reappear | The application restores local QSettings even after downloading new source; update the settings in the GUI |
| Could not locate a VISA implementation | Install and configure a usable VISA library for the selected VISA backend |
| Gate-limit or Reference-timeout error | Review the target, step size, limits, response direction, and timing before starting again |

## 10. Uploading to GitHub

Upload **the contents of the directory containing this README** to the repository root: `OECT/`, `docs/`, `experiment_data/oect/.gitkeep`, README, requirements, the launcher, and `.gitignore`. Avoid an extra enclosing folder, and upload the extracted source files rather than only the ZIP archive.

`.gitignore` excludes `.venv`, Python caches, and experiment results in the default output location from Git commits. `.gitkeep` preserves the empty output directory. When uploading manually through the website, select files carefully so local environments and measurement data are not included. Results stored in custom directories need their own inclusion or exclusion decision. Adding `.gitignore` does not automatically remove files that Git already tracks.

See [CONTROL_DETAILS.md](docs/CONTROL_DETAILS.md) for detailed behavior and legacy Python interfaces. The [change log](OECT/UPDATE_CN.md) and [validation notes](docs/VALIDATION.md) are available in Chinese.
