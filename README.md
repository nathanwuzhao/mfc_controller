# Control Interface for Alicat SLPM MFC Devices

python tools for controlling an Alicat mass flow controller over serial to generate time-varying flow profiles. designed for prototype testing where gas concentration is shaped by changing the MFC setpoint over time instead of using only an ON/OFF solenoid valve

## features

- serial communication with Alicat MFCs using `pyserial`
- run open-loop flow profiles:
  - constant
  - ramp
  - Gaussian pulse
  - exponential rise/decay
  - sine wave
  - piecewise-linear profiles
- log commanded flow and parsed MFC data to CSV
- safe zero-flow shutdown on finish or error

## install dependencies

```bash
pip install pyserial numpy matplotlib pandas