# Alicat MFC Flow Profile Controller

Python tools for controlling an Alicat mass flow controller over an ASCII serial
interface. The project is aimed at open-loop gas concentration shaping: the MFC
setpoint is changed over time to follow a desired flow profile, while the test
system naturally clears gas without an active purge or controlled exhaust pump.

The current hardware target is an Alicat MC-series SLPM MFC over RS-232/RS-485.
The examples assume unit ID `A`, baud rate `19200`, and a Windows serial port
such as `COM3`.

## Features

- Serial communication with Alicat MFCs using `pyserial`.
- Open-loop flow profiles:
  - constant
  - ramp
  - Gaussian pulse
  - exponential rise
  - exponential decay
  - sine wave
  - piecewise-linear points
- Tkinter GUI for connection control, profile editing, preview plots, live run
  plots, and serial TX/RX event logging.
- CSV logging of commanded flow and parsed MFC data.
- Zero-flow commands on start, finish, stop, and disconnect when configured.

## Install

Create and activate a virtual environment, then install the runtime
dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install pyserial numpy matplotlib pandas
```

`tkinter` is included with most standard Python installers on Windows.

## GUI Usage

Launch the GUI:

```bash
python gui.py
```

Typical bench workflow:

1. Select the serial port, unit ID, and baud rate.
2. Click `Connect`.
3. Click `Zero Flow` to confirm the device responds and starts from zero.
4. Select a profile type and enter the profile parameters.
5. Set duration, command period, poll period, flow limits, and log path.
6. Click `Preview` to inspect the target curve.
7. Click `Run` to execute the profile.
8. Use `Stop` if the run should end early; the runner will still attempt the
   configured zero-on-finish command.
9. Review the serial/run log and CSV file in `logs/`.

The GUI shows the target profile, commanded setpoints, and any reported mass
flow or setpoint values parsed from MFC polling responses.

## CLI Example

The same API can run a Gaussian profile directly from Python:

```python
from pathlib import Path

from alicat_mfc import AlicatMFC
from profile_runner import ProfileRunner, ProfileRunnerConfig, gaussian_profile

profile = gaussian_profile(
    baseline=0.0,
    amplitude=0.25,
    center_s=20.0,
    sigma_s=5.0,
)

config = ProfileRunnerConfig(
    duration_s=45.0,
    control_period_s=0.2,
    poll_period_s=0.2,
    min_flow=0.0,
    max_flow=1.0,
    log_path=Path("logs/gaussian_test.csv"),
)

with AlicatMFC("COM3", unit_id="A") as mfc:
    mfc.select_co2()
    runner = ProfileRunner(mfc, profile, config)
    runner.run()
```

Run the script with:

```bash
python test.py
```

## Safety Notes

- This is open-loop control. It commands flow but does not measure gas
  concentration or guarantee that concentration follows the same curve.
- Verify the MFC engineering units and full-scale range before running a
  profile. The default GUI maximum is `1.0`, matching a 1 SLPM workflow.
- Confirm the Alicat setpoint source allows serial/front-panel commands.
- `zero_on_finish` should normally stay enabled so the runner attempts to close
  the commanded flow on completion, stop, or error.
- Natural clearing depends on the physical system volume, leaks, mixing, and
  exhaust path. Exponential decay in concentration may need empirical tuning.

## CSV Logs

When `log_path` is set, `ProfileRunner` writes a CSV with one row per control
or poll event. Fields include:

- `t_s`
- `event`
- `q_target`
- `q_commanded`
- `command_sent`
- raw Alicat response fields such as pressure, temperature, flow, setpoint,
  totalizer, and gas when available
- `error`

The GUI default log path is `logs/gui_run.csv`.

## Troubleshooting

- Port busy: close any other terminal, GUI, or serial monitor using the same
  COM port.
- Timeout: check wiring, adapter direction, baud rate, and unit ID.
- Wrong unit ID: try the configured Alicat address, commonly `A`.
- Setpoint does not change: verify the device setpoint source accepts serial
  commands.
- Parsed fields look wrong: confirm the device data frame matches the standard
  format expected by `AlicatMFC.parse_mfc_reading()`.

## Developer Notes

- `alicat_mfc.py` owns serial commands, response parsing, and TX/RX callbacks.
- `profile_runner.py` owns profile execution, clamping, logging, stop handling,
  and progress callbacks.
- `gui.py` owns Tkinter state, validation, background threads, plots, and log
  display.
