from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List, Callable
import time
import serial
from serial import SerialException


class AlicatError(Exception):
    """base exception for alicat driver errors"""

class AlicatTimeoutError(AlicatError):
    """alicat does not respond in time"""

class AlicatParseError(AlicatError):
    """response cannot be parsed as expected"""

@dataclass
class MFCReading:
    """
    standard according to manual:
    ID AbsPressure Temperature VolFlow MassFlow Setpoint Totalizer Gas
    """

    raw: str
    unit_id: Optional[str] = None

    abs_pressure: Optional[float] = None
    temperature: Optional[float] = None
    volumetric_flow: Optional[float] = None
    mass_flow: Optional[float] = None
    setpoint: Optional[float] = None
    totalizer: Optional[float] = None
    gas: Optional[str] = None

    numeric_values: Optional[List[float]] = None
    trailing_tokens: Optional[List[str]] = None


class AlicatMFC:
    """
    port:
        serial port name, examples:
        - Windows: "COM3"
        - Linux: "/dev/ttyUSB0"
        - macOS: "/dev/tty.usbserial-XXXX"

    unit_id:
        alicat unit ID. default is "A"

    baudrate:
        default baud rate is 19200

    timeout:
        serial read timeout in seconds

    write_timeout:
        seral write timeout in seconds

    debug:
        if True, prints TX/RX traffic

    tx_callback:
        optional callback called with each command string sent to the device

    rx_callback:
        optional callback called with each response line received from the device
    """

    def __init__(
        self,
        port: str,
        unit_id: str = "A",
        baudrate: int = 19200,
        timeout: float = 1.0,
        write_timeout: float = 1.0,
        debug: bool = False,
        tx_callback: Optional[Callable[[str], None]] = None,
        rx_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.port = port
        self.unit_id = unit_id.upper()
        self.baudrate = baudrate
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.debug = debug
        self.tx_callback = tx_callback
        self.rx_callback = rx_callback

        self._ser: Optional[serial.Serial] = None

    def set_observers(
        self,
        tx_callback: Optional[Callable[[str], None]] = None,
        rx_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.tx_callback = tx_callback
        self.rx_callback = rx_callback

    def _emit_tx(self, command: str) -> None:
        if self.tx_callback is not None:
            self.tx_callback(command)

    def _emit_rx(self, response: str) -> None:
        if self.rx_callback is not None:
            self.rx_callback(response)

    def connect(self) -> None:
        if self._ser is not None and self._ser.is_open:
            return

        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=self.write_timeout,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            # give USB-serial adapters a moment to settle.
            time.sleep(0.2)
            self.clear_buffers()
        except SerialException as exc:
            raise AlicatError(f"could not open serial port {self.port!r}: {exc}") from exc

    def close(self, safe_zero: bool = True) -> None:
        """
        close the serial connection
        if safe_zero=True, tries to command zero flow before closing
        """
        if self._ser is None:
            return

        if self._ser.is_open and safe_zero:
            try:
                self.set_setpoint(0.0)
            except AlicatError:
                pass

        self._ser.close()

    def __enter__(self) -> "AlicatMFC":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(safe_zero=True)

    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def _require_connection(self) -> serial.Serial:
        if self._ser is None or not self._ser.is_open:
            raise AlicatError("serial port is not open. call connect() first.")
        return self._ser

    def clear_buffers(self) -> None:
        ser = self._require_connection()
        ser.reset_input_buffer()
        ser.reset_output_buffer()

    def _write_command(self, command: str) -> None:
        """
        write command with carriage return, should not include '\\r'.
        """
        ser = self._require_connection()

        command = command.strip()
        wire = (command + "\r").encode("ascii")

        if self.debug:
            print(f"TX: {wire!r}")

        try:
            self._emit_tx(command)
            ser.write(wire)
            ser.flush()
        except SerialException as exc:
            raise AlicatError(f"serial write failed: {exc}") from exc

    def _readline(self) -> str:
        ser = self._require_connection()

        try:
            raw = ser.readline()
        except SerialException as exc:
            raise AlicatError(f"serial read failed: {exc}") from exc

        if not raw:
            raise AlicatTimeoutError("timed out waiting for Alicat response.")

        text = raw.decode("ascii", errors="ignore").strip()

        if self.debug:
            print(f"RX: {text!r}")

        if not text:
            raise AlicatTimeoutError("received empty Alicat response.")

        self._emit_rx(text)
        return text

    def command(self, command: str, expect_response: bool = True) -> Optional[str]:
        self._write_command(command)

        if not expect_response:
            return None

        return self._readline()

    def unit_command(self, suffix: str, expect_response: bool = True) -> Optional[str]:
        suffix = suffix.strip()
        return self.command(f"{self.unit_id}{suffix}", expect_response=expect_response)

    def poll_raw(self) -> str:
        """
        A\\r
        """
        return self.command(self.unit_id, expect_response=True)  # type: ignore[return-value]

    def poll(self) -> MFCReading:
        """
        poll and parse a live MFC reading.
        """
        raw = self.poll_raw()
        return self.parse_mfc_reading(raw)

    def set_setpoint(self, value: float) -> str:
        """
        set flow setpoint in the Alicat's currently selected engineering units
        example:
            set_setpoint(0.250) sends "AS 0.25\\r" for unit A
            
        device's setpoint source must be configured as Serial/Front Panel
        """
        response = self.unit_command(f"S {value:.6g}", expect_response=True)
        return response or ""

    def zero_flow(self) -> str:
        """set flow setpoint to zero"""
        return self.set_setpoint(0.0)

    def tare_flow(self) -> str:
        response = self.unit_command("V", expect_response=True)
        return response or ""

    def tare_pressure(self) -> str:
        response = self.unit_command("P", expect_response=True)
        return response or ""

    def select_gas(self, gas_number: int) -> str:
        response = self.unit_command(f"G{gas_number}", expect_response=True)
        return response or ""

    def select_co2(self) -> str:
        return self.select_gas(4)

    def begin_streaming(self) -> str:
        """
        put device into streaming mode

        sends:
            A@@
        """
        response = self.unit_command("@@", expect_response=True)
        return response or ""

    def stop_streaming(self, new_unit_id: Optional[str] = None) -> str:
        """
        stop streaming mode and assign a unit ID
        """
        new_id = (new_unit_id or self.unit_id).upper()
        response = self.command(f"@@{new_id}", expect_response=True)
        self.unit_id = new_id
        return response or ""

    def set_stream_interval_ms(self, interval_ms: int) -> str:
        """
        set streaming interval in milliseconds

        example:
            ANCS 100
        """
        if interval_ms < 0:
            raise ValueError("interval_ms must be nonnegative.")

        response = self.unit_command(f"NCS {interval_ms}", expect_response=True)
        return response or ""

    def read_stream_line(self) -> MFCReading:
        """
        read one line while the device is already streaming
        """
        raw = self._readline()
        return self.parse_mfc_reading(raw)

    def hold_valve_closed(self) -> str:
        """
        hold valve closed.

        normal zero-flow operation should usually use set_setpoint(0.0).
        """
        response = self.unit_command("HC", expect_response=True)
        return response or ""

    def hold_valve_current_position(self) -> str:
        response = self.unit_command("HP", expect_response=True)
        return response or ""

    def cancel_valve_hold(self) -> str:
        response = self.unit_command("C", expect_response=True)
        return response or ""

    def query_data_frame_info(self) -> str:
        """
        query data frame descriptions
        send:
            A??D*
        """
        response = self.unit_command("??D*", expect_response=True)
        return response or ""

    def query_manufacturer_info(self) -> str:
        response = self.unit_command("??M*", expect_response=True)
        return response or ""

    def query_firmware_version(self) -> str:
        response = self.unit_command("VE", expect_response=True)
        return response or ""

    @staticmethod
    def _try_float(token: str) -> Optional[float]:
        try:
            return float(token)
        except ValueError:
            return None

    @classmethod
    def parse_mfc_reading(cls, raw: str) -> MFCReading:
        """
        parse a standard Alicat MFC data frame.

        standard polling example from manual:
            A +15.542 +24.57 +16.667 +15.444 +15.444 22741.4 N2

        interpreted as:
            ID AbsPressure Temperature VolFlow MassFlow Setpoint Totalizer Gas

        This parser also handles streaming lines that may omit the unit ID.
        """
        tokens = raw.split()
        if not tokens:
            raise AlicatParseError("cannot parse empty response.")

        unit_id: Optional[str] = None

        # in polling mode, first token is usually the unit ID
        # in streaming mode, the ID may be absent
        first_as_float = cls._try_float(tokens[0])
        if first_as_float is None and len(tokens[0]) == 1 and tokens[0].isalpha():
            unit_id = tokens[0].upper()
            data_tokens = tokens[1:]
        else:
            data_tokens = tokens

        numeric_values: List[float] = []
        trailing_tokens: List[str] = []

        for tok in data_tokens:
            value = cls._try_float(tok)
            if value is None:
                trailing_tokens.append(tok)
            else:
                numeric_values.append(value)

        reading = MFCReading(
            raw=raw,
            unit_id=unit_id,
            numeric_values=numeric_values,
            trailing_tokens=trailing_tokens,
        )

        # 0 abs pressure
        # 1 temperature
        # 2 volumetric flow
        # 3 mass flow
        # 4 setpoint
        # 5 totalizer
        if len(numeric_values) >= 1:
            reading.abs_pressure = numeric_values[0]
        if len(numeric_values) >= 2:
            reading.temperature = numeric_values[1]
        if len(numeric_values) >= 3:
            reading.volumetric_flow = numeric_values[2]
        if len(numeric_values) >= 4:
            reading.mass_flow = numeric_values[3]
        if len(numeric_values) >= 5:
            reading.setpoint = numeric_values[4]
        if len(numeric_values) >= 6:
            reading.totalizer = numeric_values[5]

        if trailing_tokens:
            reading.gas = trailing_tokens[0]

        return reading
