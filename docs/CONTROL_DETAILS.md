# OECT Experiment Module

The module supports two modes:

- `linear`: direct Gate-voltage steps, or Reference feedback selected by a switch.
- `cycle`: direct Gate-voltage cycles, or Reference-controlled forward/backward cycles selected by a switch.

The separate `liner/ref` and `cycle/ref` modes have been removed from the mode selector.
Each mode has its own saved `Enable Reference feedback` setting in Experiment.
The active switch controls both Reference measurement and control for every
backend. There is no separate measurement-only DMM checkbox.

## linear: one Reference switch

In the Experiment tab, `Enable Reference feedback` controls both Reference
measurement and the feedback loop for all instrument backends (including
simulation). It cannot be changed during a run.

- OFF: control Gate directly using Gate start/step/end, step duration, sample
  interval and skipped points. No Reference measurement is requested. The
  exact Gate end is included even when the step does not divide the span.
- ON: track Reference start/end with the configured intermediate target count.
  Gate is the actuator; tolerance and Gate voltage step determine feedback.
  Direct Gate scan timing/step fields are hidden and do not control feedback.
- Both branches enforce the displayed Gate minimum/maximum limits.

`Hold final Gate` keeps Gate fixed in the OFF branch. `Hold final Reference`
continues active feedback in the ON branch. Hold starts when the final target
is first reached; 0 s finishes immediately, a positive duration counts elapsed
active experiment time (excluding pauses), and `Until Stop` holds continuously. The duration is not a
requirement to remain in tolerance for that entire time. During Reference hold,
drift outside tolerance triggers further Gate adjustments and the same safety
checks. A normal new linear configuration defaults to 60 s final hold.

## cycle: one Reference switch

OFF directly cycles Gate without requesting Reference. Gate start, Max voltage
(forward endpoint), Min voltage (return endpoint), Gate step, holds and sampling
parameters control the scan. The historical Max/Min labels denote endpoint
roles; the forward endpoint can be more negative than the return endpoint.
Both endpoints are reached exactly, even if the step does not divide the span
or is larger than the span. All requested voltages must fit the Gate limits.

ON uses the previous cycle/ref control algorithm:

1. Set the initial Gate voltage.
2. Change Gate by a fixed voltage step.
3. At each Gate point, collect Reference samples using Wait at each Gate step and Reference sample interval. The sample count is the number of whole intervals in the wait time (at least one); binary rounding no longer loses a sample for values such as 0.6 / 0.2.
4. Judge the point using target crossing or ±0.5% tolerance. The point is reached when at least 90% of samples satisfy the condition, or when their average satisfies it.
5. Continue until the target Reference is reached.
6. Reverse the Gate direction and return to the initial Reference.
7. Repeat for the selected number of cycles.

Gate minimum and maximum limits remain active. The task stops with an explicit error before the next Gate step exceeds either limit.
The current window is measured before deciding whether another Gate step is needed.
The first forward leg starts from Initial Gate V; it does not first acquire the
Initial Reference target. The return leg uses Initial Reference as its target.

Cycle Reference retains its multiple-sample/crossing decision. Linear Reference
retains its absolute-tolerance feedback at a configurable checking interval (default 1 s). The merge changes mode
selection and shared plumbing, not these distinct control algorithms.
The GUI keeps the existing cycle decision defaults of 0.5% tolerance and 90%
threshold. A zero Reference target has a minimum tolerance of 1e-6 V.
`Cycles` is now one shared input for both cycle branches. Hidden direct Gate
timing/hold fields do not affect the Reference branch.

## linear with Reference feedback ON

Reference targets are generated from:

- `Reference start V`
- `Reference end V`
- `Reference intermediate steps` (integer count, excluding start and end)

For N > 0, the sequence contains N + 2 targets: the start, N evenly spaced
intermediate targets, and the end. For example, start = 0 V, end = -0.6 V,
N = 2 gives 0, -0.2, -0.4, -0.6 V. Ascending sequences work the same way.

For N = 0, start must equal end; the sequence contains that single target once,
followed by the configured final hold. Negative or fractional counts are
invalid. Equal endpoints with N > 0 are also rejected; use N = 0 instead.

The default count is 7, preserving the default 0 to -0.8 V sequence at 0.1 V
spacing. The new count uses a separate saved-setting key. On first upgrade,
the old saved voltage step is ignored and the count starts at 7; review the
count before running. Subsequent count changes are saved normally.

Every Reference target, including the start target, is actively controlled.
Checks are scheduled using `Reference check interval`, default 1 s. It accepts
values such as 2 s and 5 s (minimum 0.05 s). Instrument/communication delays may
extend the actual interval. Outside `target ± Reference tolerance`, low Ref
increases Gate by `Gate voltage step`, and high Ref decreases Gate by the same
amount. One reading inside tolerance advances to the next target (or starts
the final hold). Both endpoints are included exactly for N > 0. The check
interval applies to every target and to final-hold feedback. A single check
reads the measurement data, compares Reference with the active target, then
changes Gate by one fixed step if necessary. There are no intermediate
measurement rows or extra Gate adjustments between checks. The first check
occurs after one interval; slow reads do not trigger catch-up adjustments.

The interval is saved with GUI settings and run metadata as
`reference_check_interval_s`. It can be edited while paused: Resume waits the
new interval before reading fresh data and continues the current target.
Cycle keeps its separate Reference sample interval and per-Gate wait controls.

`Reference target timeout` defaults to 300 s; 0 disables it. When enabled it
must exceed the checking interval. Each target starts
its own timer. During final hold, a new timer starts when the first out-of-band
reading occurs and clears whenever a reading returns inside tolerance.
Timeout, invalid Reference, instrument failure, or an attempted Gate-limit
violation stops the task and runs instrument cleanup. An ongoing instrument
request must return or reach its communication timeout before the runner can
process Stop or a feedback timeout. No further Gate adjustment is issued after
Stop is detected. Cleanup still runs if status logging fails.

The fixed step and assumed positive Gate-to-Ref response are unchanged. A large
step relative to the achievable tolerance can cause oscillation; reduce the
Gate step as needed. Timeout prevents indefinite unsuccessful tracking when
enabled; it does not guarantee convergence.

## Saved settings and Python compatibility

An old selected `liner/ref` mode migrates to `linear` with Reference feedback
ON. Its target settings are retained and final hold is set to 0 s to preserve
the old finish-on-target behavior. Existing direct-linear DMM checkbox settings
initialize the new Reference feedback switch on first upgrade. Later runs use
the separately saved new switch.

Old selected `cycle/ref` or `vref` migrates to `cycle` with Reference ON,
retaining Reference parameters and transferring Reference cycles into Cycles.
After saving, subsequent runs use the unified mode/count and its saved switch.
Old direct cycle remains OFF by default: its measurement-only DMM selection
does not silently become Gate feedback. Set the new switch explicitly to use
Reference control. The old measurement-only checkbox is removed.

New Python callers use `LinearParams(reference_control_enabled=True, ...)`
and `runner.run_linear(params)`, with `InstrumentConfig(use_dmm_vref=True)`.
Both flags must agree; a mismatch fails before connecting instruments. Use
`vref_intermediate_steps` for the count. `LinearReferenceParams` provides the
Reference target settings. `LinerRefParams` and `run_liner_ref` remain only as
legacy Python adapters; they route to linear mode and do not create another
UI mode. Legacy calls retain zero final hold.

For cycle use `CycleParams(reference_control_enabled=True, ...)` and
`runner.run_cycle(params)` with the same enabled InstrumentConfig flag.
The cycle Reference sampling interval is `reference_sample_interval_s`;
`sample_interval_s` belongs to direct Gate cycling. `CycleReferenceParams`
describes the Reference-specific parameters. `CycleRefParams`, `VrefSweepParams`,
`run_cycle_ref` and `run_vref` are thin aliases/adapters for existing scripts;
they call the unified cycle flow and do not duplicate a run loop.

New metadata uses `mode = linear/cycle`, `control_variable = gate/reference`
and `reference_control_enabled`. Both Reference-controlled modes retain the
Reference value, target, error, adjustment and state columns in saved data.

## Live readout precision

Voltage readings (including Vref, Gate V, target, error, and Gate adjustment)
use six decimal places. Currents use scientific notation with six decimal
places in the mantissa, and elapsed time uses three decimal places. Tiny
nonzero values use scientific notation rather than appearing as zero.
Invalid/non-finite readings display `--`. This changes display formatting
only; feedback calculations and saved measurement precision are unchanged.

## Pause, edit and resume

`Pause` is beside `Start` and `Stop`. It changes to `Pausing...` while an
in-flight instrument operation finishes, then to `Resume` when the runner
has actually paused. At that point no measurement rows or plot points are
added, the live values stay at their last readings, and feedback stops.
Gate and Drain keep their last commanded outputs; Pause does not switch
outputs off. Physical current/Reference may still drift while not sampled.

The active experiment clock freezes. `Time_s`, Gate dwell, final hold and
Reference target timeout exclude paused time. A reading that was in flight
when Pause was requested is discarded; resuming starts with a fresh reading
after a sample interval (the configured check interval for linear Reference feedback), without
catch-up samples.

Experiment parameters become editable after pause is confirmed. `Resume`
validates a complete snapshot and applies it in the instrument worker. Invalid
parameters leave the experiment paused and editable. New Gate limits must
also include the current Gate output. Connection settings, output directory,
mode, Reference switch and startup-only Initial Gate V remain locked.

- Scalar changes such as Drain V, tolerance, feedback Gate step, sampling
  interval and hold duration keep the current target/stage. Updated durations
  are total active durations for that stage, including time already spent.
- Changing direct Gate start/end/step (or cycle endpoints) starts a new scan
  segment from the configured Gate start. Changing Reference endpoints or
  intermediate count starts a new target sequence from its configured first
  Reference target, retaining the current Gate output for feedback.
- A new cyclic scan segment starts its cycle count at 1. A cycle count edit
  alone applies to remaining cycles after the active cycle completes.
- For cycle with Reference ON, parameter changes restart the current sample-analysis
  window so a decision uses only samples collected at the updated settings.
  Already written samples remain in the file.

All segments append to the same `data.txt`; existing data is not cleared or
overwritten. `metadata.json` keeps the initial parameters and a chronological
`events` history with pause/resume wall times, active elapsed times, sample
counts, and old/new settings. `Stop` works while paused and closes outputs.
Closing the application during a run also requests Stop and waits for cleanup.

The Experiment tab no longer displays the introductory Reference explanation.
The upper-right realtime plot is `Gate Current vs Time`, using measured Gate I
in amperes. Missing/non-finite Gate I is omitted from the plot and shows `--`
in the readout (`NaN` in the data file); it is never fabricated as zero.
Saved-data comparison plots retain their existing layout.

## Plot controls

Axis labels now use precision based on the visible tick spacing. Zooming Gate V
to a few microvolts shows labels such as -0.234568 instead of repeated -0.2.
Gate V and Vref normally use decimal labels; very small spacings may use
scientific notation. Current and time labels also preserve distinctions at
their current zoom level. This applies to live plots and saved-data plots,
including the Gate axis on the dual-axis comparison chart.

Margins expand to fit longer labels; long time labels are spaced to avoid
overlap. Curves are clipped to the plot area so zooming cannot draw over the
coordinate labels. Formatting does not change measured values or saved data.

Every plot has two controls in its lower-left corner:

- reset button: restores the complete data range;
- zoom-mode button: cycles through `XY`, `X`, and `Y`.

The mouse wheel zooms:

- `XY`: horizontal and vertical axes together;
- `X`: horizontal axis only;
- `Y`: vertical axis only.

Left-button dragging still pans the plot.

## Plot Saved Data

Saved current data is displayed in μA with ordinary decimal tick labels rather than scientific notation.

The saved-data window includes `Compare Result`:

1. Open the first result normally.
2. Select a second TXT or CSV result.
3. The first current plot changes to Current 1 and Current 2 versus time.
4. The dual-axis plot adds Current 2 while retaining Gate voltage on the right axis.
5. Press `Change Result 2` to replace the comparison file.

The Gate-voltage and Gate-current plots continue to show Result 1.

## Saved data

Each task creates:

- `data.txt`
- `metadata.json`
- `run.log`
- `status_history.txt`

Both modes with Reference ON add:

- `ReferenceV_V`
- `ReferenceTargetV_V`
- `ReferenceErrorV_V`
- `GateAdjustmentV_V`
- `VrefState`

## Other behavior

- Output paths must already exist and be writable.
- Run name remains editable during a task and is applied to the finished folder.
- Settings, selected mode, paths, active tab, and window geometry are restored through `QSettings`.
- Older selected `vref` and `cycle/ref` modes migrate to `cycle` with Reference ON.
