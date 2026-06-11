from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from queue import Empty, Queue
import math
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from serial.tools import list_ports

from alicat_mfc import AlicatMFC
from profile_runner import (
    FlowProfile,
    ProfileLogRow,
    ProfileRunner,
    ProfileRunnerConfig,
    constant_profile,
    exponential_decay_profile,
    exponential_rise_profile,
    gaussian_profile,
    piecewise_linear_profile,
    ramp_profile,
    sine_profile,
)


PROFILE_FIELDS: Dict[str, List[Tuple[str, str]]] = {
    "constant": [("value", "0.25")],
    "ramp": [
        ("start", "0.0"),
        ("stop", "0.5"),
        ("duration_s", "30.0"),
        ("hold_after", "true"),
    ],
    "gaussian": [
        ("baseline", "0.0"),
        ("amplitude", "0.25"),
        ("center_s", "20.0"),
        ("sigma_s", "5.0"),
    ],
    "exponential decay": [
        ("start", "0.5"),
        ("tau_s", "10.0"),
        ("baseline", "0.0"),
    ],
    "exponential rise": [
        ("final", "0.5"),
        ("tau_s", "10.0"),
        ("initial", "0.0"),
    ],
    "sine": [
        ("baseline", "0.25"),
        ("amplitude", "0.1"),
        ("frequency_hz", "0.05"),
        ("phase_rad", "0.0"),
    ],
}


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"expected a boolean value, got {value!r}")


def parse_points(text: str) -> List[Tuple[float, float]]:
    points: List[Tuple[float, float]] = []
    for raw_line in text.replace(";", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part for part in line.replace(",", " ").split() if part]
        if len(parts) != 2:
            raise ValueError(
                "piecewise points must be one 'time, flow' pair per line"
            )
        points.append((float(parts[0]), float(parts[1])))
    return points


class MFCGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Alicat MFC Profile Controller")
        self.geometry("1180x760")
        self.minsize(960, 640)

        self.queue: Queue[Tuple[str, object]] = Queue()
        self.mfc: Optional[AlicatMFC] = None
        self.runner: Optional[ProfileRunner] = None
        self.run_thread: Optional[threading.Thread] = None
        self.action_thread: Optional[threading.Thread] = None

        self.status_var = tk.StringVar(value="disconnected")
        self.port_var = tk.StringVar(value="COM3")
        self.unit_id_var = tk.StringVar(value="A")
        self.baudrate_var = tk.StringVar(value="19200")
        self.profile_var = tk.StringVar(value="gaussian")

        self.duration_var = tk.StringVar(value="45.0")
        self.control_period_var = tk.StringVar(value="0.2")
        self.poll_period_var = tk.StringVar(value="0.2")
        self.min_flow_var = tk.StringVar(value="0.0")
        self.max_flow_var = tk.StringVar(value="1.0")
        self.send_delta_var = tk.StringVar(value="0.00001")
        self.settle_var = tk.StringVar(value="1.0")
        self.log_path_var = tk.StringVar(value=str(Path("logs/gui_run.csv")))
        self.zero_start_var = tk.BooleanVar(value=True)
        self.zero_finish_var = tk.BooleanVar(value=True)

        self.param_vars: Dict[str, tk.StringVar] = {}
        self.piecewise_text: Optional[tk.Text] = None
        self.editable_widgets: List[tk.Widget] = []

        self.live_t: List[float] = []
        self.live_commanded: List[float] = []
        self.live_mass_flow_t: List[float] = []
        self.live_mass_flow: List[float] = []
        self.live_setpoint_t: List[float] = []
        self.live_setpoint: List[float] = []

        self._build_ui()
        self._refresh_ports()
        self._on_profile_changed()
        self.after(100, self._process_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        controls = ttk.Frame(self, padding=10)
        controls.grid(row=0, column=0, sticky="ns")
        controls.columnconfigure(1, weight=1)

        main = ttk.Frame(self, padding=(0, 10, 10, 10))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=3)
        main.rowconfigure(1, weight=2)

        self._build_connection_panel(controls)
        self._build_profile_panel(controls)
        self._build_run_panel(controls)
        self._build_plot_panel(main)
        self._build_log_panel(main)

    def _build_connection_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Connection", padding=8)
        frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Port").grid(row=0, column=0, sticky="w")
        self.port_combo = ttk.Combobox(frame, textvariable=self.port_var, width=16)
        self.port_combo.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Button(frame, text="Refresh", command=self._refresh_ports).grid(
            row=0, column=2, padx=(6, 0)
        )

        ttk.Label(frame, text="Unit ID").grid(row=1, column=0, sticky="w", pady=(6, 0))
        unit_entry = ttk.Entry(frame, textvariable=self.unit_id_var, width=8)
        unit_entry.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(frame, text="Baud").grid(row=2, column=0, sticky="w", pady=(6, 0))
        baud_entry = ttk.Entry(frame, textvariable=self.baudrate_var, width=10)
        baud_entry.grid(row=2, column=1, sticky="w", padx=(6, 0), pady=(6, 0))

        button_row = ttk.Frame(frame)
        button_row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.connect_button = ttk.Button(
            button_row, text="Connect", command=self._connect
        )
        self.connect_button.pack(side="left")
        self.disconnect_button = ttk.Button(
            button_row, text="Disconnect", command=self._disconnect, state="disabled"
        )
        self.disconnect_button.pack(side="left", padx=(6, 0))
        self.zero_button = ttk.Button(
            button_row, text="Zero Flow", command=self._zero_flow, state="disabled"
        )
        self.zero_button.pack(side="left", padx=(6, 0))

        ttk.Label(frame, textvariable=self.status_var).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )

        self.editable_widgets.extend([self.port_combo, unit_entry, baud_entry])

    def _build_profile_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Profile", padding=8)
        frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Type").grid(row=0, column=0, sticky="w")
        profile_combo = ttk.Combobox(
            frame,
            textvariable=self.profile_var,
            values=[
                "constant",
                "ramp",
                "gaussian",
                "exponential decay",
                "exponential rise",
                "sine",
                "piecewise-linear",
            ],
            state="readonly",
        )
        profile_combo.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        profile_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_profile_changed())
        self.editable_widgets.append(profile_combo)

        self.param_frame = ttk.Frame(frame)
        self.param_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.param_frame.columnconfigure(1, weight=1)

    def _build_run_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Run Settings", padding=8)
        frame.grid(row=2, column=0, columnspan=2, sticky="ew")
        frame.columnconfigure(1, weight=1)

        rows = [
            ("Duration s", self.duration_var),
            ("Control period s", self.control_period_var),
            ("Poll period s", self.poll_period_var),
            ("Min flow", self.min_flow_var),
            ("Max flow", self.max_flow_var),
            ("Send delta", self.send_delta_var),
            ("Settle s", self.settle_var),
        ]
        for row, (label, var) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
            entry = ttk.Entry(frame, textvariable=var, width=14)
            entry.grid(row=row, column=1, sticky="ew", padx=(6, 0), pady=(0, 4))
            self.editable_widgets.append(entry)

        zero_start = ttk.Checkbutton(
            frame, text="Zero on start", variable=self.zero_start_var
        )
        zero_start.grid(row=len(rows), column=0, columnspan=2, sticky="w")
        zero_finish = ttk.Checkbutton(
            frame, text="Zero on finish", variable=self.zero_finish_var
        )
        zero_finish.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w")
        self.editable_widgets.extend([zero_start, zero_finish])

        ttk.Label(frame, text="Log path").grid(
            row=len(rows) + 2, column=0, sticky="w", pady=(6, 0)
        )
        log_entry = ttk.Entry(frame, textvariable=self.log_path_var)
        log_entry.grid(
            row=len(rows) + 2, column=1, sticky="ew", padx=(6, 0), pady=(6, 0)
        )
        browse = ttk.Button(frame, text="Browse", command=self._choose_log_path)
        browse.grid(row=len(rows) + 2, column=2, padx=(6, 0), pady=(6, 0))
        self.editable_widgets.extend([log_entry, browse])

        button_row = ttk.Frame(frame)
        button_row.grid(row=len(rows) + 3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.preview_button = ttk.Button(
            button_row, text="Preview", command=self._preview_profile
        )
        self.preview_button.pack(side="left")
        self.run_button = ttk.Button(
            button_row, text="Run", command=self._start_run, state="disabled"
        )
        self.run_button.pack(side="left", padx=(6, 0))
        self.stop_button = ttk.Button(
            button_row, text="Stop", command=self._stop_run, state="disabled"
        )
        self.stop_button.pack(side="left", padx=(6, 0))

    def _build_plot_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.figure = Figure(figsize=(6, 4), dpi=100)
        self.ax = self.figure.add_subplot(111)
        self.ax.set_xlabel("time (s)")
        self.ax.set_ylabel("flow")
        self.preview_line, = self.ax.plot([], [], label="target")
        self.commanded_line, = self.ax.plot([], [], label="commanded")
        self.mass_flow_line, = self.ax.plot([], [], label="mass flow")
        self.setpoint_line, = self.ax.plot([], [], label="reported setpoint")
        self.ax.legend(loc="upper right")

        self.canvas = FigureCanvasTkAgg(self.figure, master=frame)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")

    def _build_log_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Serial and Run Log", padding=8)
        frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(frame, height=10, state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        ttk.Button(frame, text="Clear Log", command=self._clear_log).grid(
            row=1, column=0, sticky="e", pady=(6, 0)
        )

    def _refresh_ports(self) -> None:
        ports = [port.device for port in list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and self.port_var.get() == "COM3":
            self.port_var.set(ports[0])

    def _on_profile_changed(self) -> None:
        for child in self.param_frame.winfo_children():
            child.destroy()
        self.param_vars.clear()
        self.piecewise_text = None

        profile_name = self.profile_var.get()
        if profile_name == "piecewise-linear":
            ttk.Label(self.param_frame, text="Points").grid(row=0, column=0, sticky="nw")
            self.piecewise_text = tk.Text(self.param_frame, width=26, height=7)
            self.piecewise_text.insert("1.0", "0, 0\n5, 0.25\n20, 0.25\n30, 0")
            self.piecewise_text.grid(row=0, column=1, sticky="ew", padx=(6, 0))
            self.editable_widgets.append(self.piecewise_text)
            return

        for row, (name, default) in enumerate(PROFILE_FIELDS[profile_name]):
            ttk.Label(self.param_frame, text=name).grid(row=row, column=0, sticky="w")
            var = tk.StringVar(value=default)
            self.param_vars[name] = var
            entry = ttk.Entry(self.param_frame, textvariable=var)
            entry.grid(row=row, column=1, sticky="ew", padx=(6, 0), pady=(0, 4))
            self.editable_widgets.append(entry)

    def _choose_log_path(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=Path(self.log_path_var.get()).name,
        )
        if path:
            self.log_path_var.set(path)

    def _build_profile(self) -> FlowProfile:
        name = self.profile_var.get()
        values = {key: var.get() for key, var in self.param_vars.items()}

        if name == "constant":
            return constant_profile(float(values["value"]))
        if name == "ramp":
            return ramp_profile(
                start=float(values["start"]),
                stop=float(values["stop"]),
                duration_s=float(values["duration_s"]),
                hold_after=parse_bool(values["hold_after"]),
            )
        if name == "gaussian":
            return gaussian_profile(
                baseline=float(values["baseline"]),
                amplitude=float(values["amplitude"]),
                center_s=float(values["center_s"]),
                sigma_s=float(values["sigma_s"]),
            )
        if name == "exponential decay":
            return exponential_decay_profile(
                start=float(values["start"]),
                tau_s=float(values["tau_s"]),
                baseline=float(values["baseline"]),
            )
        if name == "exponential rise":
            return exponential_rise_profile(
                final=float(values["final"]),
                tau_s=float(values["tau_s"]),
                initial=float(values["initial"]),
            )
        if name == "sine":
            return sine_profile(
                baseline=float(values["baseline"]),
                amplitude=float(values["amplitude"]),
                frequency_hz=float(values["frequency_hz"]),
                phase_rad=float(values["phase_rad"]),
            )
        if name == "piecewise-linear":
            if self.piecewise_text is None:
                raise ValueError("piecewise point editor is not available")
            points = parse_points(self.piecewise_text.get("1.0", "end"))
            return piecewise_linear_profile(points)

        raise ValueError(f"unknown profile type {name!r}")

    def _build_config(self) -> ProfileRunnerConfig:
        log_path = self.log_path_var.get().strip()
        return ProfileRunnerConfig(
            duration_s=float(self.duration_var.get()),
            control_period_s=float(self.control_period_var.get()),
            poll_period_s=float(self.poll_period_var.get()),
            min_flow=float(self.min_flow_var.get()),
            max_flow=float(self.max_flow_var.get()),
            send_if_change_greater_than=float(self.send_delta_var.get()),
            zero_on_start=self.zero_start_var.get(),
            zero_on_finish=self.zero_finish_var.get(),
            settle_s=float(self.settle_var.get()),
            log_path=Path(log_path) if log_path else None,
        )

    def _preview_profile(self) -> None:
        try:
            profile = self._build_profile()
            duration_s = float(self.duration_var.get())
            min_flow = float(self.min_flow_var.get())
            max_flow = float(self.max_flow_var.get())
            if duration_s <= 0:
                raise ValueError("duration must be positive")
            count = 250
            t_values = [duration_s * i / (count - 1) for i in range(count)]
            y_values = [
                max(min_flow, min(max_flow, profile(t_value))) for t_value in t_values
            ]
        except Exception as exc:
            messagebox.showerror("Invalid profile", str(exc))
            return

        self.preview_line.set_data(t_values, y_values)
        self._rescale_plot()
        self.canvas.draw_idle()
        self._log("preview", f"{self.profile_var.get()} profile updated")

    def _connect(self) -> None:
        if self.mfc is not None and self.mfc.is_connected:
            return

        try:
            port = self.port_var.get().strip()
            unit_id = self.unit_id_var.get().strip() or "A"
            baudrate = int(self.baudrate_var.get())
            if not port:
                raise ValueError("serial port is required")
        except Exception as exc:
            messagebox.showerror("Connection settings", str(exc))
            return

        self._set_status("connecting")
        self._run_action(
            "connect",
            lambda: self._connect_worker(port=port, unit_id=unit_id, baudrate=baudrate),
        )

    def _connect_worker(self, port: str, unit_id: str, baudrate: int) -> None:
        mfc = AlicatMFC(
            port,
            unit_id=unit_id,
            baudrate=baudrate,
            tx_callback=lambda command: self.queue.put(("tx", command)),
            rx_callback=lambda response: self.queue.put(("rx", response)),
        )
        mfc.connect()
        self.mfc = mfc

    def _disconnect(self) -> None:
        if self.mfc is None:
            return
        self._set_status("disconnecting")
        self._run_action("disconnect", lambda: self.mfc.close(safe_zero=True))

    def _zero_flow(self) -> None:
        if self.mfc is None or not self.mfc.is_connected:
            messagebox.showerror("Not connected", "Connect to the MFC before zeroing.")
            return
        self._run_action("zero", self.mfc.zero_flow)

    def _run_action(self, name: str, action: Callable[[], object]) -> None:
        if self.action_thread is not None and self.action_thread.is_alive():
            return

        def worker() -> None:
            try:
                result = action()
                self.queue.put(("action_done", (name, result)))
            except Exception as exc:
                self.queue.put(("action_error", (name, exc)))

        self.action_thread = threading.Thread(target=worker, daemon=True)
        self.action_thread.start()

    def _start_run(self) -> None:
        if self.mfc is None or not self.mfc.is_connected:
            messagebox.showerror("Not connected", "Connect to the MFC before running.")
            return
        if self.run_thread is not None and self.run_thread.is_alive():
            return

        try:
            profile = self._build_profile()
            config = self._build_config()
        except Exception as exc:
            messagebox.showerror("Invalid run settings", str(exc))
            return

        self.live_t.clear()
        self.live_commanded.clear()
        self.live_mass_flow_t.clear()
        self.live_mass_flow.clear()
        self.live_setpoint_t.clear()
        self.live_setpoint.clear()
        self.commanded_line.set_data([], [])
        self.mass_flow_line.set_data([], [])
        self.setpoint_line.set_data([], [])
        self._preview_profile()

        self.runner = ProfileRunner(
            self.mfc,
            profile,
            config,
            progress_callback=lambda row: self.queue.put(("row", row)),
        )
        self._set_running(True)
        self._set_status("running")

        def worker() -> None:
            try:
                rows = self.runner.run() if self.runner is not None else []
                self.queue.put(("run_done", rows))
            except Exception as exc:
                self.queue.put(("run_error", exc))

        self.run_thread = threading.Thread(target=worker, daemon=True)
        self.run_thread.start()

    def _stop_run(self) -> None:
        if self.runner is not None:
            self.runner.request_stop()
            self._set_status("stopping")
            self._log("stop", "stop requested")

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for widget in self.editable_widgets:
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        self.run_button.configure(state="disabled" if running else self._run_state())
        self.stop_button.configure(state="normal" if running else "disabled")
        self.connect_button.configure(state="disabled" if running else "normal")
        self.disconnect_button.configure(state="disabled" if running else self._connected_state())
        self.zero_button.configure(state="disabled" if running else self._connected_state())

    def _connected_state(self) -> str:
        return "normal" if self.mfc is not None and self.mfc.is_connected else "disabled"

    def _run_state(self) -> str:
        return self._connected_state()

    def _set_status(self, value: str) -> None:
        self.status_var.set(value)

    def _process_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                self._handle_queue_message(kind, payload)
        except Empty:
            pass
        self.after(100, self._process_queue)

    def _handle_queue_message(self, kind: str, payload: object) -> None:
        if kind == "tx":
            self._log("TX", str(payload))
        elif kind == "rx":
            self._log("RX", str(payload))
        elif kind == "row":
            self._handle_row(payload)  # type: ignore[arg-type]
        elif kind == "action_done":
            name, result = payload  # type: ignore[misc]
            self._log(name, "done" if result is None else str(result))
            self._set_status("connected" if self._connected_state() == "normal" else "disconnected")
            self._update_button_states()
        elif kind == "action_error":
            name, exc = payload  # type: ignore[misc]
            self._log(name, f"error: {exc}")
            self._set_status("error")
            self._update_button_states()
            messagebox.showerror(f"{name} failed", str(exc))
        elif kind == "run_done":
            rows = payload  # type: ignore[assignment]
            self._log("run", f"finished with {len(rows)} rows")
            self._set_running(False)
            self._set_status("connected")
            self._update_button_states()
        elif kind == "run_error":
            self._log("run", f"error: {payload}")
            self._set_running(False)
            self._set_status("error")
            self._update_button_states()
            messagebox.showerror("Run failed", str(payload))

    def _handle_row(self, row: ProfileLogRow) -> None:
        details = asdict(row)
        error = details.pop("error")
        suffix = f" error={error}" if error else ""
        self._log(
            row.event,
            f"t={row.t_s:.3f} target={row.q_target:.6g} "
            f"cmd={row.q_commanded:.6g} sent={row.command_sent}{suffix}",
        )

        if row.event and row.q_commanded is not None:
            self.live_t.append(row.t_s)
            self.live_commanded.append(row.q_commanded)
            self.commanded_line.set_data(self.live_t, self.live_commanded)
        if row.mass_flow is not None:
            self.live_mass_flow_t.append(row.t_s)
            self.live_mass_flow.append(row.mass_flow)
            self.mass_flow_line.set_data(self.live_mass_flow_t, self.live_mass_flow)
        if row.setpoint is not None:
            self.live_setpoint_t.append(row.t_s)
            self.live_setpoint.append(row.setpoint)
            self.setpoint_line.set_data(self.live_setpoint_t, self.live_setpoint)

        self._rescale_plot()
        self.canvas.draw_idle()

    def _rescale_plot(self) -> None:
        all_x: List[float] = []
        all_y: List[float] = []
        for line in (
            self.preview_line,
            self.commanded_line,
            self.mass_flow_line,
            self.setpoint_line,
        ):
            x_data, y_data = line.get_data()
            all_x.extend(float(x) for x in x_data)
            all_y.extend(float(y) for y in y_data if math.isfinite(float(y)))

        if not all_x:
            all_x = [0.0, 1.0]
        if not all_y:
            all_y = [0.0, 1.0]

        x_min, x_max = min(all_x), max(all_x)
        y_min, y_max = min(all_y), max(all_y)
        if x_min == x_max:
            x_max = x_min + 1.0
        if y_min == y_max:
            y_max = y_min + 1.0
        y_pad = max((y_max - y_min) * 0.1, 0.05)
        self.ax.set_xlim(x_min, x_max)
        self.ax.set_ylim(y_min - y_pad, y_max + y_pad)

    def _update_button_states(self) -> None:
        connected = self._connected_state()
        self.disconnect_button.configure(state=connected)
        self.zero_button.configure(state=connected)
        self.run_button.configure(state=connected)

    def _log(self, source: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {source}: {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.runner is not None:
            self.runner.request_stop()
        if self.mfc is not None and self.mfc.is_connected:
            try:
                self.mfc.close(safe_zero=True)
            except Exception:
                pass
        self.destroy()


def main() -> None:
    app = MFCGui()
    app.mainloop()


if __name__ == "__main__":
    main()
