
# Dependencies: ttkbootstrap, matplotlib, pyserial

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox
import ttkbootstrap as tb
from ttkbootstrap.constants import *
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import threading
import time
import csv
import os
import json
from datetime import datetime, timedelta
import logging
import queue

import shutil
from pathlib import Path

try:
    import serial
    import serial.tools.list_ports
except Exception:
    serial = None

APP_TITLE = "BIOREACTOR OS v20.0"

PH_ADVISORY_MIN = 10.0
PH_ADVISORY_MAX = 11.0
DATA_FILE = "bioreactor_data.csv"
CONFIG_FILE = "bioreactor_config.json"
PORTS_FILE = "bioreactor_ports.json"

BAUD = 115200

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bioreactor")

# ---------------- Utility ----------------
def discover_serial_ports():
    """Return list of candidate serial port device paths."""
    if serial is None:
        return []
    ports = []
    for p in serial.tools.list_ports.comports():
        dev = p.device
        desc = (p.description or "")
        if any(k in dev for k in ("ACM", "USB", "COM")) or any(k in desc for k in ("Arduino", "CH340", "CP210", "FTDI")):
            ports.append(dev)
    return sorted(set(ports))

def now_ts():
    return datetime.now().strftime("%H:%M:%S")


def safe_fromiso(s: str):
    """Best-effort datetime parser for our CSV timestamps."""
    try:
        if not s:
            return None
        s = str(s).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def list_by_id_arduinos():
    """Return stable /dev/serial/by-id paths for Arduino Nano Every (Linux)."""
    base = "/dev/serial/by-id"
    try:
        if os.name != "posix":
            return []
        if not os.path.isdir(base):
            return []
        out = []
        for name in os.listdir(base):
            if "Arduino_Nano_Every" in name and name.startswith("usb-"):
                out.append(os.path.join(base, name))
        return sorted(out)
    except Exception:
        return []


def probe_id_on_port(port: str, baud: int = BAUD):
    """Open port briefly, send ID?, and return 'A1'/'A2'/None."""
    if serial is None:
        return None
    try:
        ser = serial.Serial(port, baudrate=baud, timeout=0.25)
        try:
            ser.setDTR(False); ser.setRTS(False)


            # A1 calibration status
            if line.startswith("A1:CAL_LOADED"):
                self.a1_cal_ok = True
            elif line.startswith("A1:CAL_REQUIRED"):
                self.a1_cal_ok = False
            elif line.startswith("A1:CALIBRATED"):
                self.a1_cal_ok = True
            elif line.startswith("A1:CAL_ERR"):
                self.a1_cal_ok = False
            elif line.startswith("A1:CAL:"):
                v = line.split("A1:CAL:", 1)[1].strip()
                if v.startswith("1"):
                    self.a1_cal_ok = True
                elif v.startswith("0"):
                    self.a1_cal_ok = False
        except Exception:
            pass
        time.sleep(1.0)
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        ser.write(b"ID?\n")
        deadline = time.time() + 2.0
        ident = None
        while time.time() < deadline:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            # Accept either exact A1/A2 or ID:A1 style
            if line == "A1" or line.endswith(":A1"):
                ident = "A1"; break
            if line == "A2" or line.endswith(":A2"):
                ident = "A2"; break
        ser.close()
        return ident
    except Exception:
        try:
            ser.close()
        except Exception:
            pass
        return None

class SerialDevice:
    """
    Threaded serial connection with:
      - reader thread pushing incoming lines to a queue
      - writer thread sending queued commands with optional ack/done correlation

    Protocol support (recommended, backward compatible):
      - If command is prefixed "@<id> <cmd>", device should respond "ACK:<id>" then later "DONE:<id>" (or ERR).
      - If device doesn't support ACK/DONE, we still work (best effort).
    """
    def __init__(self, name: str):
        self.name = name  # "A1" or "A2"
        self.port = None
        self.ser = None

        self.connected = False
        self._stop = threading.Event()

        self.rx_queue = queue.Queue()
        self.tx_queue = queue.Queue()

        self._reader = None
        self._writer = None

        self.log_cb = None  # function(direction, line)
        self.on_line_cb = None  # function(line)
        self.on_disconnect_cb = None  # function(name)

        self.last_rx_time = 0.0

        # Latest telemetry
        self.ph = None
        self.temp = None
        self.lux = None
        self.flow = None
        self.light_state = None  # True/False/None unknown
        self.a1_cal_ok = None  # None/True/False based on A1 calibration

        # ACK tracking
        self._next_id = 1
        self._pending = {}  # cmd_id -> dict(event_ack, event_done, result)

        # Throttle
        self.min_send_interval_s = 0.03
        self._last_send_time = 0.0

    def connect(self, port: str):
        self.close()
        if serial is None:
            return False
        try:
            self.ser = serial.Serial(port, BAUD, timeout=0.2)
            # Best-effort: reduce auto-reset glitches on open
            try:
                self.ser.setDTR(False)
                self.ser.setRTS(False)
            except Exception:
                pass
            # Give board time to boot if it reset on open
            time.sleep(1.0)
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass

            self.port = port
            self.connected = True
            self.last_rx_time = time.time()
            self._stop.clear()
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._writer = threading.Thread(target=self._write_loop, daemon=True)
            self._reader.start()
            self._writer.start()
            logger.info("Connected %s on %s", self.name, port)
            return True
        except Exception as e:
            logger.error("Failed to connect %s on %s: %s", self.name, port, e)
            self.close()
            return False

    def close(self):
        self.connected = False
        self._stop.set()
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.port = None


    def _signal_disconnect(self):
        if self.connected:
            self.connected = False
            try:
                if self.ser:
                    self.ser.close()
            except Exception:
                pass
            if self.on_disconnect_cb:
                try:
                    self.on_disconnect_cb(self.name)
                except Exception:
                    pass

    def _emit_log(self, direction: str, line: str):
        if self.log_cb:
            try:
                self.log_cb(direction, line)
            except Exception:
                pass

    def _read_loop(self):
        # Discard initial chatter for a short period (device reset on port open)
        t0 = time.time()
        while not self._stop.is_set() and self.connected:
            try:
                if not self.ser:
                    break
                raw = self.ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                # Some devices may print startup chatter; keep it (for log), but parse only known patterns
                self.last_rx_time = time.time()
                self.rx_queue.put(line)
                self._emit_log("<", line)
                self._handle_line(line)
                if self.on_line_cb:
                    self.on_line_cb(line)
            except Exception:
                break

        self.connected = False

    def _write_loop(self):
        while not self._stop.is_set():
            try:
                cmd, cmd_id, want_ack, want_done = self.tx_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if not self.connected or not self.ser:
                # drop command if disconnected
                if cmd_id is not None:
                    pend = self._pending.get(cmd_id)
                    if pend:
                        pend["result"] = ("ERR", "DISCONNECTED")
                        pend["event_done"].set()
                continue

            # Throttle
            dt = time.time() - self._last_send_time
            if dt < self.min_send_interval_s:
                time.sleep(self.min_send_interval_s - dt)

            try:
                self.ser.write((cmd + "\n").encode("utf-8"))
                self._last_send_time = time.time()
                self._emit_log(">", cmd)
            except Exception:
                self.connected = False
                continue

            # Best-effort wait for ACK/DONE if requested
            if cmd_id is not None and want_ack:
                pend = self._pending.get(cmd_id)
                if pend:
                    pend["event_ack"].wait(timeout=1.0)

            if cmd_id is not None and want_done:
                pend = self._pending.get(cmd_id)
                if pend:
                    pend["event_done"].wait(timeout=20.0)

    def _handle_line(self, line: str):
        # Handle ACK/DONE/ERR
        if line.startswith("ACK:"):
            cmd_id = line.split("ACK:", 1)[1].strip()
            if cmd_id in self._pending:
                self._pending[cmd_id]["event_ack"].set()
            return
        if line.startswith("DONE:"):
            cmd_id = line.split("DONE:", 1)[1].strip()
            if cmd_id in self._pending:
                self._pending[cmd_id]["result"] = ("DONE", None)
                self._pending[cmd_id]["event_done"].set()
            return
        if line.startswith("ERR:"):
            # ERR:<id>:<msg> or ERR:<id>
            rest = line.split("ERR:", 1)[1]
            parts = rest.split(":", 1)
            cmd_id = parts[0].strip()
            msg = parts[1].strip() if len(parts) > 1 else ""
            if cmd_id in self._pending:
                self._pending[cmd_id]["result"] = ("ERR", msg)
                self._pending[cmd_id]["event_done"].set()
            return

        # Telemetry parsing
        try:
            # A1 calibration status
            if line.startswith('A1:CAL_LOADED') or line.startswith('A1:CALIBRATED'):
                self.a1_cal_ok = True
            elif line.startswith('A1:CAL_REQUIRED') or line.startswith('A1:CAL_ERR'):
                self.a1_cal_ok = False
            elif line.startswith('A1:CAL:'):
                v = line.split('A1:CAL:', 1)[1].strip()
                if v.startswith('1'): self.a1_cal_ok = True
                elif v.startswith('0'): self.a1_cal_ok = False

            if "A1:PH:" in line:
                self.ph = float(line.split("A1:PH:", 1)[1].strip())
            elif "A2:TEMP:" in line:
                self.temp = float(line.split("A2:TEMP:", 1)[1].strip())
            elif "A2:LUX:" in line:
                self.lux = float(line.split("A2:LUX:", 1)[1].strip())
            elif "A2:FLOW:" in line:
                self.flow = float(line.split("A2:FLOW:", 1)[1].strip())
            elif "A2:LIGHT:" in line:
                v = line.split("A2:LIGHT:", 1)[1].strip()
                if v in ("1", "ON", "TRUE"):
                    self.light_state = True
                elif v in ("0", "OFF", "FALSE"):
                    self.light_state = False
        except Exception:
            pass

    def send(self, cmd: str, *, wait_ack=False, wait_done=False):
        """Queue a command; optionally wait for ACK/DONE if device supports it."""
        if not cmd or not isinstance(cmd, str):
            return None

        # Create correlation id and wrap with "@id "
        cmd_id = None
        if wait_ack or wait_done:
            cmd_id = str(self._next_id)
            self._next_id += 1
            wrapped = f"@{cmd_id} {cmd}"
            self._pending[cmd_id] = {
                "event_ack": threading.Event(),
                "event_done": threading.Event(),
                "result": None,
                "cmd": cmd,
            }
            self.tx_queue.put((wrapped, cmd_id, wait_ack, wait_done))
            return cmd_id

        self.tx_queue.put((cmd, None, False, False))
        return None

    def get_result(self, cmd_id: str):
        pend = self._pending.get(cmd_id)
        if not pend:
            return None
        return pend["result"]

# ---------------- Engine ----------------
class BioreactorEngine:
    def __init__(self):
        self.a1 = SerialDevice("A1")
        self.a2 = SerialDevice("A2")

        # Busy locks
        self._a1_busy = threading.Lock()
        self._a2_busy = threading.Lock()

        # Last-known desired light state
        self.desired_light_on = None  # True/False/None

    def is_connected_a1(self): return self.a1.connected
    def is_connected_a2(self): return self.a2.connected

    # --- Safe gated send helpers ---
    def a1_cmd(self, cmd: str, *, wait_done=False):
        if not self.is_connected_a1():
            return None
        # Serialize long actions
        if wait_done:
            with self._a1_busy:
                return self.a1.send(cmd, wait_ack=True, wait_done=True)
        else:
            return self.a1.send(cmd)

    def a2_cmd(self, cmd: str, *, wait_done=False):
        if not self.is_connected_a2():
            return None
        if wait_done:
            with self._a2_busy:
                return self.a2.send(cmd, wait_ack=True, wait_done=True)
        else:
            return self.a2.send(cmd)

    # --- Reads ---
    def poll_sensors(self):
        # A2 is request/response for sensors
        if self.is_connected_a2():
            self.a2_cmd("READ")
        # A1 pH probe is a long read; only poll during sampling

    # --- Lights ---
    def ensure_lights(self, want_on: bool):
        """
        Ensure lights are in requested state.
        Requires A2 patched firmware to be fully deterministic.
        If firmware is not patched, this is best-effort (toggle-based).
        """
        if not self.is_connected_a2():
            self.desired_light_on = want_on
            return

        self.a2_cmd("LIGHT_ON" if want_on else "LIGHT_OFF")
        self.desired_light_on = want_on


    # --- Temperature setpoint ---
    def set_temp_setpoint(self, temp_c: float):
        """Send SET_TEMP to A2 (best-effort)."""
        try:
            if not self.is_connected_a2():
                return None
            return self.a2_cmd(f"SET_TEMP {float(temp_c):.2f}")
        except Exception:
            return None

    def should_lights_be_on(self, on_h: float = 16.0, off_h: float = 8.0, now: datetime | None = None) -> bool:
        """Compute schedule from wall-clock time (used for applying correct temp setpoint on connect/save)."""
        try:
            if now is None:
                now = datetime.now()
            cycle = float(on_h) + float(off_h)
            if cycle <= 0:
                return True
            hour = now.hour + now.minute / 60.0 + now.second / 3600.0
            pos = hour % cycle
            return pos < float(on_h)
        except Exception:
            return True

# ---------------- GUI ----------------
class BioreactorGUI:
    def __init__(self, kiosk: bool = False, windowed: bool = False):
        self.root = tb.Window(themename="darkly")
        self.root.title(APP_TITLE)

        # UI scaling
        base_w, base_h = 800, 480
        HMI_TK_SCALING = 1.35  # tuned for 800x480 DSI 

        try:
            sw = int(self.root.winfo_screenwidth())
            sh = int(self.root.winfo_screenheight())
        except Exception:
            sw, sh = base_w, base_h
        try:
            current_tk_scale = float(self.root.tk.call("tk", "scaling"))
        except Exception:
            current_tk_scale = 1.0
        if (sw, sh) == (base_w, base_h):
            tk_scale = HMI_TK_SCALING
        else:
            size_factor = max(0.85, min(sw / base_w, sh / base_h))
            tk_scale = max(0.85, min(current_tk_scale * size_factor, 2.0))

        try:
            self.root.tk.call("tk", "scaling", tk_scale)
        except Exception:
            tk_scale = current_tk_scale

        ui_scale = max(0.95, min((tk_scale / HMI_TK_SCALING) * 1.05, 2.0))
        self._ui_scale = ui_scale  # used by a few style tweaks later


        # Helper constants
        self.PAD_S = 4
        self.PAD_M = 8
        self.PAD_L = 12
        self.GAP_S = 6
        self.GAP_M = 10
        self.GAP_L = 14
        self.BTN_IPADX = 10
        self.BTN_IPADY = 6
        self.ENTRY_WIDTH_SM = 8
        self.ENTRY_WIDTH_MD = 10
        self.ENTRY_WIDTH_LG = 14

        def _set_named_font(name: str, size_base: int, weight: str = "normal"):
            try:
                f = tkfont.nametofont(name)
                f.configure(size=max(9, int(size_base * ui_scale)), weight=weight)
            except Exception:
                pass

        # UI fonts
        _set_named_font("TkDefaultFont", 12)
        _set_named_font("TkTextFont", 12)
        _set_named_font("TkMenuFont", 12)
        _set_named_font("TkHeadingFont", 13, weight="bold")
        _set_named_font("TkCaptionFont", 11)

        # Window mode
        self.kiosk = bool(kiosk) and (not windowed)
        if self.kiosk:
            try:
                self.root.attributes("-fullscreen", True)
            except Exception:
                pass
            self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))
        else:
            self.root.geometry("800x480")

        self.ui_queue = queue.Queue()
        self._data_lock = threading.Lock()
        self._last_log_time = datetime.min

        # Engine
        self.engine = BioreactorEngine()

        # React to disconnects
        self.engine.a1.on_disconnect_cb = lambda n: self.post_log("A1 disconnected")
        self.engine.a2.on_disconnect_cb = lambda n: self.post_log("A2 disconnected")

        # React to disconnects
        self.engine.a1.on_disconnect_cb = lambda n: self.post_log("A1 disconnected.")
        self.engine.a2.on_disconnect_cb = lambda n: self.post_log("A2 disconnected.")

        # Connection state flags
        self.connected_a1 = False
        self.connected_a2 = False
        self.keep_running = True

        # Connection probing backoff to avoid repeatedly resetting Arduinos by opening ports
        self._last_probe_time = {}  
        self._probe_backoff_s = 20.0

        # Subsystem toggles (permissions)
        self.enable_ph_module = tk.BooleanVar(value=False)
        self.enable_heater = tk.BooleanVar(value=False)
        self.enable_pumps = tk.BooleanVar(value=False)
        self.enable_automation = tk.BooleanVar(value=False)
        # Manual lights control (A2)
        self.lights_manual_state = tk.BooleanVar(value=False)

        # Manual access toggles for refill access (A2)
        self.left_access_state = tk.BooleanVar(value=False)
        self.right_access_state = tk.BooleanVar(value=False)

        # Manual bubbler control (A2)
        self.bubbler_state = tk.BooleanVar(value=False)

        # Setpoints
        self.setpoints = {
                        "Light On (hrs)": 16.0,
            "Light Off (hrs)": 8.0,
            "Temp Day (°C)": 35.0,
            "Temp Night (°C)": 25.0,
            "pH Check Hours": 12.0,
            "pH Cal Days": 7.0,
            "Poll Seconds": 5.0
        }
        self.load_config()

        # CSV history
        self.data_history = self.load_data()
        self.harvest_history = []
        self.load_harvest_history()

        # Schedule timestamps when automation toggled ON
        self.last_ph_measure_time = datetime.now()
        self.last_ph_cal_time = datetime.now()

        # Scheduler
        self.next_ph_due = None
        self.next_cal_due = None
        self.next_light_due = None  # next scheduled light transition (datetime) when automation is ON
        self.light_phase = None     # night or day when automation is ON
        self.light_phase_start_time = None  # when the current light phase began (datetime)
        self.automation_start_time = None  # when automation was enabled
        self.pending_verify_attempts = 0
        self.pending_verify_last_dose_ml = 0.0

        # Build UI
        self._build_ui()

        # Hook logging from serial devices into UI log
        self.engine.a1.log_cb = lambda d, m: self.post_log(f"A1{d} {m}")
        self.engine.a2.log_cb = lambda d, m: self.post_log(f"A2{d} {m}")

        # Startup log
        self.post_log("Boot: GUI started (SAFE mode).")
        self.post_log("Boot: Waiting for Arduinos...")

        # Start background workers
        threading.Thread(target=self.connection_manager_loop, daemon=True).start()
        threading.Thread(target=self.scheduler_loop, daemon=True).start()

        # Start UI queue processing
        self.root.after(50, self.process_ui_queue)

        # Making sure automation is off on boot
        self.enable_automation.set(False)
        self.force_all_off()

    # ---------- UI build ----------
    def _build_ui(self):
        style = tb.Style()
        tab_font_sz = int(16 * getattr(self, "_ui_scale", 1.0))
        tab_pad_x = int(14 * getattr(self, "_ui_scale", 1.0))
        tab_pad_y = int(20 * getattr(self, "_ui_scale", 1.0))
        style.configure(
            "TNotebook.Tab",
            font=("TkDefaultFont", tab_font_sz),
            padding=(tab_pad_x, tab_pad_y)
        )
        tb.Button(self.root, text="×", bootstyle="danger", width=3,
                  command=self.on_exit).place(x=665, y=5)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)

        self.create_dashboard()
        self.create_schedule()
        self.create_setpoints()
        self.create_manual()
        self.create_log()
        self.create_graphs()

    def create_dashboard(self):
        page = tb.Frame(self.notebook)
        self.notebook.add(page, text="Dashboard")

        meter_frame = tb.Frame(page)
        meter_frame.pack(pady=20)

        self.ph_meter = tb.Meter(meter_frame, metersize=235, amountused=0, amounttotal=100,
                                 metertype="semi", textfont=("TkDefaultFont", 40, "bold"), subtextfont=("TkDefaultFont", 18), subtext="pH", bootstyle="secondary", interactive=False)
        self.ph_meter.pack(side=LEFT, padx=12, pady=8)

        self.temp_meter = tb.Meter(meter_frame, metersize=235, amountused=0, amounttotal=100,
                                   metertype="semi", textfont=("TkDefaultFont", 40, "bold"), subtextfont=("TkDefaultFont", 18), subtext="Temp °C", bootstyle="secondary", interactive=False)
        self.temp_meter.pack(side=LEFT, padx=12, pady=8)

        self.light_meter = tb.Meter(meter_frame, metersize=235, amountused=0, amounttotal=100,
                                    metertype="semi", textfont=("TkDefaultFont", 40, "bold"), subtextfont=("TkDefaultFont", 18), subtext="Light lux", bootstyle="secondary", interactive=False)
        self.light_meter.pack(side=LEFT, padx=12, pady=8)

        status = tb.Frame(page)
        status.pack(pady=2)

        # Status badges (green = ON/CONNECTED, orange = OFF/DISCONNECTED)
        self.a1_badge = tb.Label(status, text="A1", bootstyle="warning inverse", padding=(10, 4))
        self.a1_badge.pack(side=LEFT, padx=4)

        self.a2_badge = tb.Label(status, text="A2", bootstyle="warning inverse", padding=(10, 4))
        self.a2_badge.pack(side=LEFT, padx=4)
        self.cal_badge = tb.Label(status, text="CAL", bootstyle="warning inverse", padding=(10, 4))
        self.cal_badge.pack(side=LEFT, padx=4)

        self.heater_badge = tb.Label(status, text="HEATER", bootstyle="warning inverse", padding=(10, 4))
        self.heater_badge.pack(side=LEFT, padx=4)

        self.pumps_badge = tb.Label(status, text="PUMPS", bootstyle="warning inverse", padding=(10, 4))
        self.pumps_badge.pack(side=LEFT, padx=4)

        self.lights_badge = tb.Label(status, text="LIGHTS", bootstyle="warning inverse", padding=(10, 4))
        self.lights_badge.pack(side=LEFT, padx=4)

        self.auto_badge = tb.Label(status, text="AUTO", bootstyle="warning inverse", padding=(10, 4))
        self.auto_badge.pack(side=LEFT, padx=4)

        btn_row = tb.Frame(page)
        btn_row.pack(pady=12)

        tb.Button(btn_row, text="EMERGENCY STOP", bootstyle="danger outline",
                  command=self.force_all_off, width=18).pack(side=LEFT, padx=8)

        # Harvest button
        self.harvest_button = tb.Button(
            btn_row, text="EXTRACT ALGAE", bootstyle="warning outline",
            command=self.extract_algae, width=18
        )
        self.harvest_button.pack(side=LEFT, padx=8)


    def create_schedule(self):
        page = tb.Frame(self.notebook)
        self.notebook.add(page, text="Schedule")

        tb.Label(page, text="Scheduler", font=("Arial", 25, "bold"), bootstyle="info").pack(pady=10)

        card = tb.Frame(page)
        card.pack(fill="x", padx=18, pady=10)

        # String variables for live updates
        self.next_ph_var = tk.StringVar(value="—")
        self.next_cal_var = tk.StringVar(value="—")
        self.next_light_var = tk.StringVar(value="—")
        self.automation_state_var = tk.StringVar(value="OFF")

        row = 0
        def add_row(label, var):
            nonlocal row
            tb.Label(card, text=label, font=("Arial", 20)).grid(row=row, column=0, sticky="w", pady=12, padx=(0, 12))
            tb.Label(card, textvariable=var, font=("Arial", 20, "bold")).grid(row=row, column=1, sticky="w", pady=12)
            row += 1

        add_row("Automation", self.automation_state_var)
        add_row("Next pH check", self.next_ph_var)
        add_row("Next pH calibration", self.next_cal_var)
        add_row("Next light change", self.next_light_var)

        # Helpful note
        tb.Label(page,
                 text="Enabling Automate schedules the next run forward.",
                 bootstyle="secondary",
                 font=("Arial", 9)).pack(pady=(6, 0), padx=18, anchor="w")

    def create_setpoints(self):
        page = tb.Frame(self.notebook)
        self.notebook.add(page, text="Setpoints")

        self.setpoint_entries = {}

        labels_left = ["Temp Day (°C)", "Temp Night (°C)", "Poll Seconds"]
        labels_right = ["Light On (hrs)", "Light Off (hrs)", "pH Check Hours", "pH Cal Days"]

        for i, label in enumerate(labels_left):
            tb.Label(page, text=label).grid(row=i, column=0, padx=18, pady=20, sticky="e")
            e = tb.Entry(page, width=13)
            e.insert(0, str(self.setpoints[label]))
            e.grid(row=i, column=1, padx=10, pady=25, sticky="w")
            self.setpoint_entries[label] = e

        for i, label in enumerate(labels_right):
            tb.Label(page, text=label).grid(row=i, column=2, padx=18, pady=10, sticky="e")
            e = tb.Entry(page, width=13)
            e.insert(0, str(self.setpoints[label]))
            e.grid(row=i, column=3, padx=10, pady=10, sticky="w")
            self.setpoint_entries[label] = e

        tb.Button(page, text="SAVE", bootstyle="success", command=self.save_setpoints)\
            .grid(row=6, column=0, columnspan=4, pady=18, padx=18, sticky="ew")

    def create_manual(self):
        page = tb.Frame(self.notebook)
        self.notebook.add(page, text="Manual")

        subsys = tb.LabelFrame(page, text="Subsystem Enable", bootstyle="danger")
        subsys.pack(pady=18, padx=16, fill="x")

        tb.Checkbutton(subsys, text="pH Module", variable=self.enable_ph_module,
                       bootstyle="round-toggle", command=self.on_toggle_ph_module)\
            .pack(side=LEFT, padx=10, pady=6)

        tb.Checkbutton(subsys, text="Heater", variable=self.enable_heater,
                       bootstyle="round-toggle", command=self.on_toggle_heater_enable)\
            .pack(side=LEFT, padx=10, pady=6)

        tb.Checkbutton(subsys, text="Pumps", variable=self.enable_pumps,
                       bootstyle="round-toggle", command=self.on_toggle_pumps_enable)\
            .pack(side=LEFT, padx=10, pady=6)

        tb.Checkbutton(subsys, text="Automate", variable=self.enable_automation,
                       bootstyle="success round-toggle", command=self.on_toggle_automation)\
            .pack(side=LEFT, padx=10, pady=6)

        # A1 controls
        a1f = tb.LabelFrame(page, text="Functions", bootstyle="info")
        a1f.pack(pady=18, padx=16, fill="x")

        tb.Button(a1f, text="HOME", command=lambda: self.safe_a1("HOME", wait_done=True)).pack(side=LEFT, padx=5, pady=5)
        tb.Button(a1f, text="STORE", command=lambda: self.safe_a1("STORE", wait_done=True)).pack(side=LEFT, padx=5, pady=5)
        tb.Button(a1f, text="WASH", command=lambda: self.safe_a1("WASH", wait_done=True)).pack(side=LEFT, padx=5, pady=5)
        tb.Button(a1f, text="SAMPLE", bootstyle="success", command=lambda: self.safe_a1("SAMPLE", wait_done=True)).pack(side=LEFT, padx=5, pady=5)
        tb.Button(a1f, text="READ pH", bootstyle="secondary",
                  command=lambda: self.safe_a1("PH?", wait_done=True)).pack(side=LEFT, padx=5, pady=5)
        tb.Button(a1f, text="CALIBRATE", bootstyle="warning",
                  command=lambda: self.safe_a1("CALIBRATE", wait_done=True)).pack(side=LEFT, padx=5, pady=5)

        posf = tb.LabelFrame(page, text="Index Positions", bootstyle="info")
        posf.pack(pady=18, padx=16, fill="x")
        for pos in ["POS_PH10", "POS_PH7", "POS_WASH", "POS_STORE"]:
            tb.Button(posf, text=pos.replace("POS_", ""), command=lambda p=pos: self.safe_a1(p, wait_done=True))\
                .pack(side=LEFT, padx=5, pady=5)
        tb.Button(posf, text="PROBE UP", command=lambda: self.safe_a1("LIFT", wait_done=True)).pack(side=LEFT, padx=5, pady=5)
        tb.Button(posf, text="PROBE DOWN", command=lambda: self.safe_a1("LOWER", wait_done=True)).pack(side=LEFT, padx=5, pady=5)

        # A2 controls
        a2f = tb.LabelFrame(page, text="Environment", bootstyle="info")
        a2f.pack(pady=18, padx=16, fill="x")

        tb.Checkbutton(a2f, text="Lights", variable=self.lights_manual_state,
                       bootstyle="round-toggle", command=self.on_lights_manual_toggle)\
            .pack(side=LEFT, padx=10, pady=6)

        tb.Checkbutton(a2f, text="Bubbler", variable=self.bubbler_state,
                       bootstyle="round-toggle", command=self.on_bubbler_toggle)\
            .pack(side=LEFT, padx=10, pady=6)

        tb.Checkbutton(a2f, text="Left", variable=self.left_access_state,
                       bootstyle="round-toggle", command=self.on_left_access_toggle)\
            .pack(side=LEFT, padx=10, pady=6)

        tb.Checkbutton(a2f, text="Right", variable=self.right_access_state,
                       bootstyle="round-toggle", command=self.on_right_access_toggle)\
            .pack(side=LEFT, padx=10, pady=6)



    def create_log(self):
        page = tb.Frame(self.notebook)
        self.notebook.add(page, text="Log")
        page.grid_rowconfigure(0, weight=0)
        page.grid_rowconfigure(1, weight=1)   
        page.grid_rowconfigure(2, weight=0)   
        page.grid_columnconfigure(0, weight=1)

        tb.Label(page, text="System Log", font=("Arial", 18, "bold"), bootstyle="info").grid(
            row=0, column=0, pady=8
        )

        wrap = tb.Frame(page)
        wrap.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)

        self.log_text = tk.Text(
            wrap, bg="#1e1e1e", fg="white", font=("Courier", 10), wrap="word"
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.log_text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=sb.set)

        # Command console
        console = tb.Frame(page)
        console.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        console.grid_columnconfigure(1, weight=1)
        console.grid_rowconfigure(1, weight=0)

        tb.Label(console, text="Console:", bootstyle="secondary").grid(row=0, column=0, padx=(0, 8))
        self.cmd_entry = tb.Entry(console)
        self.cmd_entry.grid(row=0, column=1, sticky="ew")
        self.cmd_entry.insert(0, "A1:PH?   or   A2:READ")

        def _focus_in(_evt):
            if self.cmd_entry.get().strip() == "A1:PH?   or   A2:READ":
                self.cmd_entry.delete(0, tk.END)

        self.cmd_entry.bind("<FocusIn>", _focus_in)
        self.cmd_entry.bind("<Return>", lambda _e: self.send_console_command())

        tb.Button(
            console,
            text="Send",
            bootstyle="primary outline",
            command=self.send_console_command,
        ).grid(row=0, column=2, padx=(8, 0))

        # Export / Clear CSV actions
        action_row = tb.Frame(console)
        action_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        action_row.grid_columnconfigure(0, weight=1)
        action_row.grid_columnconfigure(1, weight=1)

        tb.Button(
            action_row,
            text="Export CSV to USB",
            bootstyle="success outline",
            command=self.export_data_csv_to_usb,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))

        tb.Button(
            action_row,
            text="CLEAR CSV",
            bootstyle="danger outline",
            command=self.clear_data_csv,
        ).grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def send_console_command(self):
        """Send a one-line command to A1 or A2 using prefixes A1: / A2:."""
        if not hasattr(self, "cmd_entry"):
            return
        raw = self.cmd_entry.get().strip()
        if not raw:
            return
        self.cmd_entry.delete(0, tk.END)

        # Parse prefix
        target = None
        cmd = raw
        if raw.upper().startswith("A1:"):
            target = "A1"
            cmd = raw[3:].strip()
        elif raw.upper().startswith("A2:"):
            target = "A2"
            cmd = raw[3:].strip()

        if not target or not cmd:
            self.post_log(f"Console: Please prefix commands with A1: or A2: (got '{raw}')")
            return

        # Log and send
        self.post_log(f"Console > {target}:{cmd}")
        if target == "A1":
            if not self.engine.is_connected_a1():
                self.post_log("Console: A1 not connected")
                return
            self.engine.a1_cmd(cmd)
        else:
            if not self.engine.is_connected_a2():
                self.post_log("Console: A2 not connected")
                return
            self.engine.a2_cmd(cmd)

    def create_graphs(self):
        names = ["pH", "Temp", "Light", "Harvest"]
        self.graph_canvases = {}
        for name in names:
            page = tb.Frame(self.notebook)
            self.notebook.add(page, text=name)
            fig, ax = plt.subplots(figsize=(7, 4), facecolor='#1e1e1e')
            ax.set_facecolor('#1e1e1e')
            ax.tick_params(colors='white')
            ax.title.set_color('white')
            ax.xaxis.label.set_color('white')
            ax.yaxis.label.set_color('white')
            for spine in ax.spines.values():
                spine.set_color('white')
            canvas = FigureCanvasTkAgg(fig, master=page)
            canvas.get_tk_widget().pack(fill="both", expand=True)
            self.graph_canvases[name] = (fig, ax, canvas)

    # ---------- Thread-safe UI helpers ----------
    def post_ui(self, func):
        self.ui_queue.put(func)

    def post_log(self, msg: str):
        def _do():
            ts = now_ts()
            self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
            self.log_text.see(tk.END)
            lines = int(self.log_text.index("end-1c").split(".")[0])
            while lines > 400:
                self.log_text.delete("1.0", "2.0")
                lines -= 1
        self.post_ui(_do)

    def process_ui_queue(self):
        try:
            for _ in range(200):
                func = self.ui_queue.get_nowait()
                try:
                    func()
                except Exception:
                    pass
        except queue.Empty:
            pass
        if self.keep_running:
            self.root.after(50, self.process_ui_queue)

    # ---------- Config / data ----------

    def _usb_mount_candidates(self):
        """Return a list of plausible writable USB mount directories."""
        try:
            user = os.getenv("USER") or os.getlogin()
        except Exception:
            user = None

        candidates = []
        roots = []
        if user:
            roots += [f"/media/{user}", f"/run/media/{user}"]
        roots += ["/media", "/run/media"]

        for root in roots:
            if not os.path.isdir(root):
                continue
            try:
                for name in os.listdir(root):
                    p = os.path.join(root, name)
                    if os.path.isdir(p):
                        candidates.append(p)
            except Exception:
                pass

        writable = []
        for p in candidates:
            try:
                if os.access(p, os.W_OK):
                    writable.append(p)
            except Exception:
                pass

        out = []
        seen = set()
        for p in writable:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def export_data_csv_to_usb(self):
        """Copy DATA_FILE to an auto-detected USB drive with a timestamped filename."""
        src = Path(DATA_FILE)
        if not src.exists():
            self.post_log(f"Export: Source file not found: {src}")
            try:
                messagebox.showerror("Export failed", f"Could not find {src}")
            except Exception:
                pass
            return

        mounts = self._usb_mount_candidates()
        if not mounts:
            self.post_log("Export: No writable USB mount found.")
            try:
                messagebox.showwarning(
                    "No USB drive found",
                    "No writable USB drive was found.\n\n"
                    "Plug in a USB drive and wait a few seconds for it to mount, then try again."
                )
            except Exception:
                pass
            return

        dest_dir = Path(mounts[0])
        if len(mounts) > 1:
            try:
                msg = "Multiple USB drives found:\n\n" + "\n".join(f"- {m}" for m in mounts) + "\n\nUse the first one?"
                use_first = messagebox.askyesno("Select USB", msg)
                dest_dir = Path(mounts[0] if use_first else mounts[-1])
            except Exception:
                dest_dir = Path(mounts[0])

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        dest = dest_dir / f"bioreactor_data_{ts}.csv"

        try:
            shutil.copy2(src, dest)
            self.post_log(f"Export: Copied {src} -> {dest}")
            try:
                messagebox.showinfo("Export complete", f"Saved to:\n{dest}")
            except Exception:
                pass
        except PermissionError:
            self.post_log(f"Export: Permission error writing to {dest_dir}")
            try:
                messagebox.showerror(
                    "Export failed",
                    "Permission error writing to:\n"
                    f"{dest_dir}\n\n"
                    "Try a different USB drive or reformat to FAT32/exFAT."
                )
            except Exception:
                pass
        except Exception as e:
            self.post_log(f"Export: Failed: {e}")
            try:
                messagebox.showerror("Export failed", f"Copy failed:\n{e}")
            except Exception:
                pass

    # ---------- Status badge updates ----------

    def clear_data_csv(self):
        """Clear the internal DATA_FILE (keeps header) to start a new batch."""
        src = Path(DATA_FILE)
        msg = (
            "This will permanently erase the logged data in:\n"
            f"{src}\n\n"
            "Use this when starting a new batch. Continue?"
        )
        try:
            ok = messagebox.askyesno("Clear Data CSV", msg, icon="warning")
        except Exception:
            ok = False
        if not ok:
            self.post_log("Clear CSV cancelled.")
            return

        try:
            with self._data_lock:
                with open(DATA_FILE, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["timestamp", "ph", "temp", "light", "event"])

            # Reset in-memory histories
            self.data_history = []
            self.harvest_history = []

            # Reset logging cadence so the next point is fresh
            self._last_log_time = datetime.min

            self.post_log("CSV cleared (new batch started).")

            # Redraw UI 
            self.post_ui(self.update_graphs)
            self.refresh_dashboard()
        except Exception as e:
            self.post_log(f"Clear CSV failed: {e}")

    def update_status_badges(self):
        # Helper to set a badge 
        def _set(widget, on: bool):
            widget.configure(bootstyle=("success inverse" if on else "warning inverse"))

        # Connection badges
        _set(self.a1_badge, bool(self.connected_a1))
        _set(self.a2_badge, bool(self.connected_a2))
        _set(self.cal_badge, bool(self.connected_a1 and self.engine.a1.a1_cal_ok))

        # Subsystem enable badges (green only if enabled AND A2 connected where applicable)
        _set(self.heater_badge, bool(self.connected_a2 and self.enable_heater.get()))
        _set(self.pumps_badge, bool(self.connected_a2 and self.enable_pumps.get()))

        # Lights badge shows desired light state when connected; otherwise OFF/DISCONNECTED color
        lights_on = (self.engine.desired_light_on is True)
        _set(self.lights_badge, bool(self.connected_a2 and lights_on))

        # Automation badge
        _set(self.auto_badge, bool(self.enable_automation.get()))

    def load_config(self):
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                self.setpoints.update({k: cfg.get(k, v) for k, v in self.setpoints.items()})
        except Exception:
            pass

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.setpoints, f, indent=2)
        except Exception as e:
            self.post_log(f"Failed to save config: {e}")

    def load_ports_mapping(self):
        try:
            if not os.path.exists(PORTS_FILE):
                return None
            with open(PORTS_FILE, 'r') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            if 'A1' in data and 'A2' in data:
                return {'A1': data['A1'], 'A2': data['A2']}
            return None
        except Exception:
            return None

    def save_ports_mapping(self, mapping: dict):
        try:
            with open(PORTS_FILE, 'w') as f:
                json.dump(mapping, f, indent=2)
        except Exception as e:
            self.post_log(f"Failed to save ports mapping: {e}")

    def load_data(self):
        with self._data_lock:
            if not os.path.exists(DATA_FILE):
                with open(DATA_FILE, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["timestamp", "ph", "temp", "light", "event"])
                return []
            out = []
            with open(DATA_FILE, "r") as f:
                r = csv.DictReader(f)
                for row in r:
                    out.append(row)
            return out

    def log_data(self, ph=None, temp=None, light=None, event=None):
        ts = datetime.now().isoformat()

        # File write
        with self._data_lock:
            with open(DATA_FILE, "a", newline="") as f:
                w = csv.writer(f)
                w.writerow([ts, ph, temp, light, event])

        # In-memory history (keep last 7 days)
        if ph is not None or temp is not None or light is not None:
            self.data_history.append({"timestamp": ts, "ph": ph, "temp": temp, "light": light})
            cutoff = datetime.now() - timedelta(days=7)
            kept = []
            for d in self.data_history:
                dt = safe_fromiso(d.get("timestamp"))
                if dt and dt > cutoff:
                    kept.append(d)
            self.data_history = kept

        if event == "harvest":
            self.harvest_history.append({"timestamp": ts})
            self.trim_harvest_history()

    def load_harvest_history(self):
        self.harvest_history = []
        with self._data_lock:
            if not os.path.exists(DATA_FILE):
                return
            with open(DATA_FILE, "r") as f:
                r = csv.DictReader(f)
                for row in r:
                    if row.get("event") == "harvest":
                        self.harvest_history.append({"timestamp": row.get("timestamp")})
        self.trim_harvest_history()

    def trim_harvest_history(self):
        cutoff = datetime.now() - timedelta(days=30)
        kept = []
        for h in self.harvest_history:
            dt = safe_fromiso(h.get("timestamp"))
            if dt and dt > cutoff:
                kept.append(h)
        self.harvest_history = kept

    # ---------- Safe Startup ----------
    def force_all_off(self):
        # Disable automation and subsystems first
        self.enable_automation.set(False)
        self.enable_heater.set(False)
        self.enable_pumps.set(False)
        self.enable_ph_module.set(False)

        # Force OFF actions on hardware if connected
        if self.engine.is_connected_a2():
            self.engine.a2_cmd("HEATER_DISABLE")
            self.engine.a2_cmd("PUMP_PH_UP_OFF")
            self.engine.a2_cmd("PUMP_PH_DOWN_OFF")
            self.engine.a2_cmd("PUMP_AUX1_OFF")
            self.engine.a2_cmd("PUMP_AUX2_OFF")
            self.engine.a2_cmd("PUMP_ALGAE_OFF")
            self.engine.a2_cmd("PUMP_BUBBLER_OFF")
        # Do not force A1 to move on e-stop automatically.
        self.post_log("E-STOP: Automation OFF; Heater/Pumps/pH Module disabled; outputs forced OFF (if connected).")

        def _ui():
            self.update_status_badges()
        self.post_ui(_ui)

    def require_connected(self, which: str) -> bool:
        if which == "A1" and not self.engine.is_connected_a1():
            self.post_log("Blocked: A1 not connected.")
            return False
        if which == "A2" and not self.engine.is_connected_a2():
            self.post_log("Blocked: A2 not connected.")
            return False
        return True

    def require_enabled(self, var: tk.BooleanVar, name: str) -> bool:
        if not var.get():
            self.post_log(f"Blocked: {name} disabled (SAFE mode). Enable it first.")
            return False
        return True

    # ---------- Manual command wrappers ----------
    def safe_a1(self, cmd: str, wait_done=False):
        if not self.require_connected("A1"):
            return
        if not self.require_enabled(self.enable_ph_module, "pH Module"):
            return
        self.post_log(f"Manual: A1 {cmd}")
        self.engine.a1_cmd(cmd, wait_done=wait_done)

    def safe_heater(self, enable: bool):
        if not self.require_connected("A2"):
            return
        if not self.require_enabled(self.enable_heater, "Heater"):
            return
        cmd = "HEATER_ENABLE" if enable else "HEATER_DISABLE"
        self.post_log(f"Manual: {cmd}")
        self.engine.a2_cmd(cmd)

        def _ui():
            self.update_status_badges()
        self.post_ui(_ui)

    def safe_pump_cmd(self, cmd: str):
        if not self.require_connected("A2"):
            return
        if not self.require_enabled(self.enable_pumps, "Pumps"):
            return
        self.post_log(f"Manual: {cmd}")
        self.engine.a2_cmd(cmd)

    # ---------- Toggle handlers ----------
    def on_toggle_ph_module(self):
        if self.enable_ph_module.get():
            self.post_log("pH Module enabled (manual A1 motion allowed).")
        else:
            self.post_log("pH Module disabled (blocking A1 motion).")

    def on_toggle_heater_enable(self):
        # Heater enable is a GUI safety gate + controller to enable/disable on A2.
        if self.enable_heater.get():
            self.post_log("Heater enabled (A2 control loop allowed).")
            if self.engine.is_connected_a2():
                self.engine.a2_cmd("HEATER_ENABLE")
        else:
            self.post_log("Heater disabled (forced OFF).")
            if self.engine.is_connected_a2():
                self.engine.a2_cmd("HEATER_DISABLE")
        self.post_ui(lambda: self.update_status_badges())

    def on_toggle_pumps_enable(self):
        if self.enable_pumps.get():
            self.post_log("Pumps enabled (manual/auto dosing allowed).")
        else:
            self.post_log("Pumps disabled (forced OFF).")
            if self.engine.is_connected_a2():
                self.engine.a2_cmd("PUMP_PH_UP_OFF")
                self.engine.a2_cmd("PUMP_PH_DOWN_OFF")
                self.engine.a2_cmd("PUMP_AUX1_OFF")
                self.engine.a2_cmd("PUMP_AUX2_OFF")
                self.engine.a2_cmd("PUMP_ALGAE_OFF")
                self.engine.a2_cmd("PUMP_BUBBLER_OFF")
            def _ui():
                self.update_status_badges()
            self.post_ui(_ui)
        self.post_ui(lambda: self.update_status_badges())


    def on_toggle_automation(self):
        if self.enable_automation.get():
            now = datetime.now()

            self.automation_start_time = now
            self.last_ph_measure_time = now
            self.last_ph_cal_time = now

            ph_interval_sec = float(self.setpoints.get("pH Check Hours", 12.0)) * 3600.0
            cal_interval_sec = float(self.setpoints.get("pH Cal Days", 7.0)) * 86400.0
            self.next_ph_due = now + timedelta(seconds=ph_interval_sec)
            self.next_cal_due = now + timedelta(seconds=cal_interval_sec)

            # Lights/temperature schedule starts in NIGHT mode at enable time to not stress culture on first run
            on_h = float(self.setpoints.get("Light On (hrs)", 16.0))
            off_h = float(self.setpoints.get("Light Off (hrs)", 8.0))
            self.light_phase = "night"
            self.light_phase_start_time = now
            self.next_light_due = self.light_phase_start_time + timedelta(seconds=max(0.0, off_h) * 3600.0)

            # Apply NIGHT immediately
            try:
                self.engine.ensure_lights(False)
                night_temp = float(self.setpoints.get("Temp Night (°C)", 25.0))
                try:
                    self.engine.set_temp_setpoint(night_temp)
                except Exception:
                    # In case engine method isn't available for some reason, fall back to raw command
                    if self.engine.is_connected_a2():
                        self.engine.a2_cmd(f"SET_TEMP {night_temp}")
            except Exception:
                pass

            # Clear any pending verify loop
            self.pending_verify_attempts = 0
            self.pending_verify_last_dose_ml = 0.0

            self.post_log("Automation ENABLED (scheduler armed).")
            self.post_log(f"Next pH check: {self.next_ph_due.strftime('%Y-%m-%d %H:%M:%S')}")
            self.post_log(f"Next calibration: {self.next_cal_due.strftime('%Y-%m-%d %H:%M:%S')}")
            self.post_log(f"Next light change: {self.next_light_due.strftime('%Y-%m-%d %H:%M:%S')} (start NIGHT)")
        else:
            # Disable scheduler actions; keep system SAFE
            self.post_log("Automation DISABLED.")
            self.automation_start_time = None
            self.next_ph_due = None
            self.next_cal_due = None
            self.next_light_due = None
            self.light_phase = None
            self.light_phase_start_time = None
            self.pending_verify_attempts = 0
            self.pending_verify_last_dose_ml = 0.0

        # Update schedule tab display if present
        try:
            self.refresh_schedule_tab()
        except Exception:
            pass
    def on_lights_manual_toggle(self):
        want_on = self.lights_manual_state.get()
        if not self.require_connected("A2"):
            self.post_ui(lambda: self.lights_manual_state.set(False))
            return
        if self.enable_automation.get():
            self.post_log("Manual: Lights changed while automation is ON (schedule may override).")
        self.post_log(f"Manual: Lights -> {'ON' if want_on else 'OFF'}")
        self.engine.ensure_lights(want_on)

        # Day/Night temperature setpoint follows the light cycle.
        temp_key = "Temp Day (°C)" if want_on else "Temp Night (°C)"
        try:
            temp_sp = float(self.setpoints.get(temp_key, 35.0 if want_on else 25.0))
            self.engine.set_temp_setpoint(temp_sp)
            self.post_log(f"Auto: Temp setpoint -> {temp_sp:.1f}°C ({'day' if want_on else 'night'})")
        except Exception:
            pass

        self.update_light_status_label(want_on)


    def on_bubbler_toggle(self):
        want_on = self.bubbler_state.get()
        if not self.require_connected("A2"):
            self.post_ui(lambda: self.bubbler_state.set(False))
            return
        if not self.require_enabled(self.enable_pumps, "Pumps"):
            self.post_ui(lambda: self.bubbler_state.set(False))
            return
        cmd = "PUMP_BUBBLER_ON" if want_on else "PUMP_BUBBLER_OFF"
        self.post_log(f"Manual: {cmd}")
        self.engine.a2_cmd(cmd)
        self.post_ui(lambda: self.update_status_badges())

    def on_left_access_toggle(self):
        want_open = self.left_access_state.get()
        if not self.require_connected("A2"):
            self.post_ui(lambda: self.left_access_state.set(False))
            return
        cmd = "OPEN_LEFT" if want_open else "CLOSE_LEFT"
        self.post_log(f"Manual: {cmd}")
        self.engine.a2_cmd(cmd)
        self.post_ui(lambda: self.update_status_badges())

    def on_right_access_toggle(self):
        want_open = self.right_access_state.get()
        if not self.require_connected("A2"):
            self.post_ui(lambda: self.right_access_state.set(False))
            return
        cmd = "OPEN_RIGHT" if want_open else "CLOSE_RIGHT"
        self.post_log(f"Manual: {cmd}")
        self.engine.a2_cmd(cmd)
        self.post_ui(lambda: self.update_status_badges())

    def update_light_status_label(self, on_state):
        self.post_ui(lambda: self.update_status_badges())

    # ---------- Setpoints ----------
    def save_setpoints(self):
        # Read entries
        for k, entry in self.setpoint_entries.items():
            try:
                self.setpoints[k] = float(entry.get())
            except ValueError:
                self.post_log(f"Invalid setpoint: {k}")

        self.save_config()
        self.post_log("Setpoints saved.")

        now = datetime.now()


        # If automation is running, adjust *existing* schedules without restarting the cycle.
        # This means keep the current phase / last-run anchors, and only move the "next due"
        # timestamps based on the new interval durations.
        if self.enable_automation.get():
            # ---- pH schedules: next due = last event time + new interval ----
            ph_interval_sec = float(self.setpoints.get("pH Check Hours", 12.0)) * 3600.0
            cal_interval_sec = float(self.setpoints.get("pH Cal Days", 7.0)) * 86400.0

            if self.last_ph_measure_time is None:
                self.last_ph_measure_time = now
            if self.last_ph_cal_time is None:
                self.last_ph_cal_time = now

            self.next_ph_due = self.last_ph_measure_time + timedelta(seconds=ph_interval_sec)
            self.next_cal_due = self.last_ph_cal_time + timedelta(seconds=cal_interval_sec)

            # ---- Light schedules: keep current phase, next due = phase start + new duration ----
            on_h = float(self.setpoints.get("Light On (hrs)", 16.0))
            off_h = float(self.setpoints.get("Light Off (hrs)", 8.0))

            # Determine current phase if missing
            if self.light_phase not in ("night", "day"):
                self.light_phase = "day" if (self.engine.desired_light_on is True) else "night"

            if self.light_phase_start_time is None:
                # If we don't know when the phase began, treat "now" as the start.
                self.light_phase_start_time = now

            dur_h = on_h if self.light_phase == "day" else off_h
            self.next_light_due = self.light_phase_start_time + timedelta(seconds=max(0.0, dur_h) * 3600.0)

            # Apply current phase temperature immediately so changing Temp Night/Day takes effect right away
            want_on = (self.light_phase == "day")
            temp_key = "Temp Day (°C)" if want_on else "Temp Night (°C)"
            try:
                temp_sp = float(self.setpoints.get(temp_key, 35.0 if want_on else 25.0))
                self.engine.set_temp_setpoint(temp_sp)
                self.post_log(f"Auto: Updated temp setpoint -> {temp_sp:.1f}°C ({'day' if want_on else 'night'})")
            except Exception:
                pass

            # Update schedule tab
            try:
                self.refresh_schedule_tab()
            except Exception:
                pass

        else:
            # Automation OFF: still apply temperature setpoint immediately based on current light state if known
            if self.engine.is_connected_a2():
                want_on = (self.engine.desired_light_on is True)
                temp_key = "Temp Day (°C)" if want_on else "Temp Night (°C)"
                try:
                    temp_sp = float(self.setpoints.get(temp_key, 35.0 if want_on else 25.0))
                    self.engine.set_temp_setpoint(temp_sp)
                except Exception:
                    try:
                        if self.engine.is_connected_a2():
                            self.engine.a2_cmd(f"SET_TEMP {temp_sp}")
                    except Exception:
                        pass
    def connection_manager_loop(self):
        """Establish and maintain connections.

        On Raspberry Pi / Linux, prefer stable /dev/serial/by-id paths and remember mapping.
        On other OSes, fall back to cautious scanning with per-port backoff.
        """
        while self.keep_running:
            try:
                # Linux/Pi path by-id 
                if os.name == "posix":
                    mapping = self.load_ports_mapping()
                    # If no mapping yet, try to discover by-id devices and identify via ID
                    if not mapping:
                        byid = list_by_id_arduinos()
                        if not byid:
                            self.set_connected(False, False, note="No /dev/serial/by-id Arduino devices found. Retrying...")
                            time.sleep(2)
                            continue
                        tmp = {}
                        for p in byid:
                            ident = probe_id_on_port(p)
                            if ident and ident not in tmp:
                                tmp[ident] = p
                        if "A1" in tmp and "A2" in tmp:
                            mapping = {"A1": tmp["A1"], "A2": tmp["A2"]}
                            self.save_ports_mapping(mapping)
                            self.post_log("Saved stable port mapping (by-id).")
                        else:
                            self.set_connected(False, False, note="Waiting for both Arduinos to answer ID?.")
                            time.sleep(2)
                            continue

                    # Attempt connect/reconnect using stored mapping
                    a1_path = mapping.get("A1")
                    a2_path = mapping.get("A2")

                    # If devices missing, stay offline but keep GUI
                    if a1_path and not os.path.exists(a1_path):
                        if self.engine.is_connected_a1():
                            self.engine.a1.close()
                        self.set_connected(False, self.engine.is_connected_a2(), note=f"A1 missing: {a1_path}")
                    if a2_path and not os.path.exists(a2_path):
                        if self.engine.is_connected_a2():
                            self.engine.a2.close()
                        self.set_connected(self.engine.is_connected_a1(), False, note=f"A2 missing: {a2_path}")

                    # Connect if not connected
                    if a1_path and os.path.exists(a1_path) and not self.engine.is_connected_a1():
                        ok = self.engine.a1.connect(a1_path)
                        if ok:
                            # Verify identity once
                            self.engine.a1_cmd("ID?")
                            self.engine.a1_cmd("CAL?")
                            self.post_log(f"Connected A1 on {a1_path}")
                    if a2_path and os.path.exists(a2_path) and not self.engine.is_connected_a2():
                        ok = self.engine.a2.connect(a2_path)
                        if ok:
                            self.engine.a2_cmd("ID?")
                            self.post_log(f"Connected A2 on {a2_path}")
                            want_on = self._should_lights_be_on()
                            temp_key = "Temp Day (°C)" if want_on else "Temp Night (°C)"
                            self.engine.a2_cmd(f"SET_TEMP {self.setpoints.get(temp_key, 35.0 if want_on else 25.0)}")

                    # Update flags
                    self.set_connected(self.engine.is_connected_a1(), self.engine.is_connected_a2())

                    time.sleep(2)
                    continue

                # Windows/macOS fallback cautious scanning
                ports = discover_serial_ports()
                if not ports:
                    self.set_connected(False, False, note="No serial ports detected.")
                    time.sleep(2)
                    continue

                found_a1 = None
                found_a2 = None

                for port in ports:
                    if found_a1 and found_a2:
                        break

                    last_t = self._last_probe_time.get(port, 0.0)
                    if (time.time() - last_t) < self._probe_backoff_s:
                        continue
                    self._last_probe_time[port] = time.time()

                    ident = probe_id_on_port(port)
                    if ident == "A1" and not found_a1:
                        found_a1 = port
                    if ident == "A2" and not found_a2:
                        found_a2 = port

                if found_a1 and not self.engine.is_connected_a1():
                    if self.engine.a1.connect(found_a1):
                        self.post_log(f"Connected A1 on {found_a1}")
                if found_a2 and not self.engine.is_connected_a2():
                    if self.engine.a2.connect(found_a2):
                        self.post_log(f"Connected A2 on {found_a2}")
                        want_on = self._should_lights_be_on()
                        temp_key = "Temp Day (°C)" if want_on else "Temp Night (°C)"
                        self.engine.a2_cmd(f"SET_TEMP {self.setpoints.get(temp_key, 35.0 if want_on else 25.0)}")

                self.set_connected(self.engine.is_connected_a1(), self.engine.is_connected_a2())

            except Exception as e:
                self.set_connected(False, False, note=f"Connection loop error: {e}")

            time.sleep(2)
    def set_connected(self, a1: bool, a2: bool, note: str = None):
        # Only log transitions
        if (a1 != self.connected_a1) or (a2 != self.connected_a2):
            self.connected_a1 = a1
            self.connected_a2 = a2
            self.post_log(f"Connection: A1={'OK' if a1 else 'OFF'} | A2={'OK' if a2 else 'OFF'}")

        if note:
            self.post_log(note)

        def _ui():
            self.update_status_badges()
        self.post_ui(_ui)

    # ---------- Scheduler / automation ----------
    def scheduler_loop(self):
        last_poll = datetime.min

        while self.keep_running:
            try:
                now = datetime.now()

                # Always poll sensors on cadence
                poll_s = max(1.0, float(self.setpoints.get("Poll Seconds", 5.0)))
                if (now - last_poll).total_seconds() >= poll_s:
                    self.engine.poll_sensors()
                    self.refresh_dashboard()
                    last_poll = now

                # Data logging every 5 minutes
                if (now - self._last_log_time).total_seconds() >= 300:
                    # Advance the timer first so a CSV/parse hiccup can't spam errors forever
                    self._last_log_time = now
                    try:
                        ph = self.engine.a1.ph if self.engine.is_connected_a1() else None
                        temp = self.engine.a2.temp if self.engine.is_connected_a2() else None
                        lux = self.engine.a2.lux if self.engine.is_connected_a2() else None
                        self.log_data(ph, temp, lux)
                    except Exception as e:
                        self.post_log(f"Data log error: {e}")

                # Automation schedule
                if self.enable_automation.get():
                    self.automation_tick(now)

                # Graph update occasionally
                if int(time.time()) % 30 == 0:
                    self.post_ui(self.update_graphs)

            except Exception as e:
                self.post_log(f"Scheduler error: {e}")

            time.sleep(0.2)


    def automation_tick(self, now: datetime):
        # Lights schedule (runs when automation is enabled)
        self.update_lights_by_schedule(now)
                # Scheduled pH check at next due timestamp
        ph_interval_sec = float(self.setpoints.get("pH Check Hours", 12.0)) * 3600.0
        if self.next_ph_due is None:
            self.next_ph_due = self.last_ph_measure_time + timedelta(seconds=ph_interval_sec)

        if now >= self.next_ph_due:
            # advance schedule immediately
            self.last_ph_measure_time = now
            self.next_ph_due = now + timedelta(seconds=ph_interval_sec)

            if not self.enable_ph_module.get():
                self.post_log("Auto: Skipped pH sample (pH Module disabled).")
            elif not self.engine.is_connected_a1():
                self.post_log("Auto: Skipped pH sample (A1 disconnected).")
            else:
                self.perform_ph_check()

        # Calibration schedule
        cal_interval_sec = float(self.setpoints.get("pH Cal Days", 7.0)) * 86400.0
        if self.next_cal_due is None:
            self.next_cal_due = self.last_ph_cal_time + timedelta(seconds=cal_interval_sec)

        if now >= self.next_cal_due:
            self.last_ph_cal_time = now
            self.next_cal_due = now + timedelta(seconds=cal_interval_sec)

            if not self.enable_ph_module.get():
                self.post_log("Auto: Skipped calibration (pH Module disabled).")
            elif not self.engine.is_connected_a1():
                self.post_log("Auto: Skipped calibration (A1 disconnected).")
            else:
                self.post_log("Auto: Calibration triggered.")
                self.engine.a1_cmd("CALIBRATE", wait_done=True)

    def _should_lights_be_on(self, now: datetime | None = None) -> bool:
        """Local schedule truth based on current setpoints (used on connect/save)."""
        if now is None:
            now = datetime.now()
        try:
            on_h = float(self.setpoints.get("Light On (hrs)", 16.0))
            off_h = float(self.setpoints.get("Light Off (hrs)", 8.0))
        except Exception:
            on_h, off_h = 16.0, 8.0
        cycle = on_h + off_h
        if cycle <= 0:
            return True
        hour = now.hour + now.minute / 60.0 + now.second / 3600.0
        pos = hour % cycle
        return pos < on_h

    def update_lights_by_schedule(self, now: datetime):
        """Light/temperature scheduling (automation-anchored).

        Behavior:
          - When automation turns ON, we start in NIGHT immediately.
          - The next transition happens after the configured phase duration from *that moment*.
          - Thereafter we alternate NIGHT <-> DAY on cadence.
        """
        if not self.enable_automation.get():
            return

        on_h = float(self.setpoints.get("Light On (hrs)", 16.0))
        off_h = float(self.setpoints.get("Light Off (hrs)", 8.0))

        # If automation was enabled before these vars were initialized, initialize now.
        if self.light_phase not in ("night", "day"):
            self.light_phase = "night"
        if self.light_phase_start_time is None:
            self.light_phase_start_time = now
        if self.next_light_due is None:
            dur_h = off_h if self.light_phase == "night" else on_h
            self.next_light_due = self.light_phase_start_time + timedelta(seconds=max(0.0, dur_h) * 3600.0)

        if now < self.next_light_due:
            return

        # Time to transition
        if self.light_phase == "night":
            # NIGHT to DAY
            self.light_phase = "day"
            want_on = True
            self.light_phase_start_time = now
            self.next_light_due = self.light_phase_start_time + timedelta(seconds=max(0.0, on_h) * 3600.0)
        else:
            # DAY to NIGHT
            self.light_phase = "night"
            want_on = False
            self.light_phase_start_time = now
            self.next_light_due = self.light_phase_start_time + timedelta(seconds=max(0.0, off_h) * 3600.0)

        if not self.engine.is_connected_a2():
            self.post_log("Auto: Lights schedule change pending (A2 disconnected).")
            self.engine.desired_light_on = want_on
            return

        self.post_log(f"Auto: Lights -> {'ON' if want_on else 'OFF'} (schedule)")
        self.engine.ensure_lights(want_on)

        # Day/Night temperature setpoint follows the light cycle.
        temp_key = "Temp Day (°C)" if want_on else "Temp Night (°C)"
        try:
            temp_sp = float(self.setpoints.get(temp_key, 35.0 if want_on else 25.0))
            self.engine.set_temp_setpoint(temp_sp)
            self.post_log(f"Auto: Temp setpoint -> {temp_sp:.1f}°C ({'day' if want_on else 'night'})")
        except Exception as e:
            self.post_log(f"Auto: Failed to apply {temp_key}: {e}")

        self.update_light_status_label(want_on)
    def _measure_ph_sample(self):
        # Run A1 SAMPLE and return parsed pH
        if self.engine.a1.a1_cal_ok is not True:
            self.post_log("Auto: pH sample skipped (A1 calibration required).")
            return None
        self.post_log("Auto: Starting pH SAMPLE...")
        self.engine.a1_cmd("SAMPLE", wait_done=True)

        t_end = time.time() + 2.0
        while time.time() < t_end:
            if self.engine.a1.ph is not None:
                break
            time.sleep(0.05)

        ph = self.engine.a1.ph
        if ph is None:
            self.post_log("Auto: SAMPLE complete but no pH reading parsed.")
            return None

        self.post_log(f"Auto: pH={ph:.2f}")
        return ph


        def _ask():
            try:
                result["yes"] = messagebox.askyesno("pH Correction", msg)
            except Exception:
                result["yes"] = False
            done.set()

        self.root.after(0, _ask)
        done.wait()
        return bool(result["yes"])

    def perform_ph_check(self):
        """Automatic pH check (advisory-only: no dosing)."""
        ph = self._measure_ph_sample()
        if ph is None:
            return {"ok": False, "reason": "no_ph"}

        lo, hi = PH_ADVISORY_MIN, PH_ADVISORY_MAX
        in_band = (lo <= ph <= hi)

        if in_band:
            self.post_log(f"Auto: pH OK {ph:.2f} (recommended {lo:.2f}-{hi:.2f})")
        else:
            self.post_log(f"Auto: pH OUTSIDE RECOMMENDED {ph:.2f} (recommended {lo:.2f}-{hi:.2f})")

        return {"ok": True, "ph": ph, "in_band": in_band}

    def refresh_dashboard(self):
        # Compute display values
        ph = self.engine.a1.ph if self.engine.is_connected_a1() else None
        temp = self.engine.a2.temp if self.engine.is_connected_a2() else None
        lux = self.engine.a2.lux if self.engine.is_connected_a2() else None

        def _ui():
            # pH
            if ph is None:
                self.ph_meter.configure(amountused=0, bootstyle="secondary")
                self.ph_meter.configure(subtext="pH —")
            else:
                self.ph_meter.configure(amountused=max(0, min(100, (ph / 14.0) * 100)), bootstyle="success")
                self.ph_meter.configure(subtext=f"pH {ph:.2f}")

            # Temp
            if temp is None:
                self.temp_meter.configure(amountused=0, bootstyle="secondary")
                self.temp_meter.configure(subtext="Temp —")
            else:
                pct = max(0, min(100, ((temp - 10.0) / 30.0) * 100))
                self.temp_meter.configure(amountused=pct, bootstyle="success")
                self.temp_meter.configure(subtext=f"Temp {temp:.1f}°C")

            # Lux
            if lux is None:
                self.light_meter.configure(amountused=0, bootstyle="secondary")
                self.light_meter.configure(subtext="Lux —")
            else:
                pct = max(0, min(100, (lux / 5000.0) * 100))
                self.light_meter.configure(amountused=pct, bootstyle="success")
                self.light_meter.configure(subtext=f"Lux {lux:.0f}")

            # Harvest readiness is an indicator only
            try:
                if self.check_harvest_ready():
                    self.harvest_button.configure(bootstyle="success outline")
                else:
                    self.harvest_button.configure(bootstyle="warning outline")
            except Exception:
                pass

            # Status badges
            try:
                self.refresh_schedule_tab()
            except Exception:
                pass
            self.update_status_badges()

        self.post_ui(_ui)


    def refresh_schedule_tab(self):
        # Update StringVars for Schedule tab
        auto_on = bool(self.enable_automation.get())
        if hasattr(self, "automation_state_var"):
            self.automation_state_var.set("ON" if auto_on else "OFF")

        def fmt_dt(dt):
            return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"

        # Next pH / Cal
        if auto_on:
            # If next due isn't set, compute it from last times
            if self.next_ph_due is None:
                ph_interval_sec = float(self.setpoints.get("pH Check Hours", 12.0)) * 3600.0
                self.next_ph_due = self.last_ph_measure_time + timedelta(seconds=ph_interval_sec)
            if self.next_cal_due is None:
                cal_interval_sec = float(self.setpoints.get("pH Cal Days", 7.0)) * 86400.0
                self.next_cal_due = self.last_ph_cal_time + timedelta(seconds=cal_interval_sec)
            next_ph = self.next_ph_due
            next_cal = self.next_cal_due
        else:
            next_ph = None
            next_cal = None

        if hasattr(self, "next_ph_var"):
            self.next_ph_var.set(fmt_dt(next_ph))
        if hasattr(self, "next_cal_var"):
            self.next_cal_var.set(fmt_dt(next_cal))

        # Next light change, when automation is ON
        next_light = self.next_light_due if auto_on else None
        if hasattr(self, "next_light_var"):
            self.next_light_var.set(fmt_dt(next_light))

    # ---------- Graphs ----------
    def update_graphs(self):
        for name, (fig, ax, canvas) in self.graph_canvases.items():
            ax.clear()
            if name == "Harvest":
                times = []
                for h in self.harvest_history:
                    dt = safe_fromiso(h.get("timestamp"))
                    if dt:
                        times.append(dt)
                if times:
                    ax.stem(times, [1] * len(times))
                ax.set_ylim(0, 2)
                ax.set_ylabel("Harvest Events")
            else:
                key = name.lower()
                data = [d for d in self.data_history if d.get(key) not in (None, "", "None")]
                times = []
                vals = []
                for d in data:
                    dt = safe_fromiso(d.get("timestamp"))
                    if dt is None:
                        continue
                    try:
                        v = float(d.get(key))
                    except Exception:
                        continue
                    times.append(dt)
                    vals.append(v)
                if times:
                    ax.plot(times, vals)
                ax.set_ylabel(name)
            ax.set_title(f"{name} Over Time")
            ax.set_xlabel("Time")
            ax.set_facecolor('#1e1e1e')
            ax.tick_params(colors='white')
            ax.title.set_color('white')
            ax.xaxis.label.set_color('white')
            ax.yaxis.label.set_color('white')
            for spine in ax.spines.values():
                spine.set_color('white')
            fig.patch.set_facecolor('#1e1e1e')
            fig.tight_layout()
            canvas.draw()

    # ---------- Harvest  ----------

    def extract_algae(self):
        # Manual command: always allow.
        # Readiness is an indicator only.
        if not self.require_connected("A2"):
            return

        ready = self.check_harvest_ready()

        # If pumps are disabled, still allow manual testing, but warn the user.
        pumps_ok = self.enable_pumps.get()

        msg_lines = []
        msg_lines.append(f"Send DISPENSE_ALGAE now?")
        if ready:
            msg_lines.append("")
            msg_lines.append("Harvest indicators: favorable ✅")
        else:
            msg_lines.append("")
            msg_lines.append("Harvest indicators: not yet ⚠️ (still allowed)")
        if not pumps_ok:
            msg_lines.append("")
            msg_lines.append("Note: Pumps are disabled in the GUI (this is allowed for testing).")

        if messagebox.askyesno("Confirm Algae Dispense", "\n".join(msg_lines)):
            self.post_log("Manual: DISPENSE_ALGAE")
            self.engine.a2_cmd("DISPENSE_ALGAE", wait_done=False)
            self.log_data(event="harvest")
            messagebox.showinfo("Sent", "Algae dispense command sent.")

    def check_harvest_ready(self):
        # Simple heuristic: last 48h pH above target, and recent lux low
        if len(self.data_history) < 10:
            return False
        now = datetime.now()
        two_days_ago = now - timedelta(days=2)

        recent = []
        for d in self.data_history:
            dt = safe_fromiso(d.get("timestamp"))
            if dt and dt > two_days_ago:
                recent.append(d)

        phs = []
        luxs = []
        for d in recent:
            try:
                if d.get("ph") not in (None, "", "None"):
                    phs.append(float(d.get("ph")))
                if d.get("light") not in (None, "", "None"):
                    luxs.append(float(d.get("light")))
            except Exception:
                pass

        if not phs or not luxs:
            return False
        return all(p >= float(PH_ADVISORY_MIN) for p in phs) and any(l < 500 for l in luxs)

    # ---------- Exit ----------
    def on_exit(self):
        if messagebox.askokcancel("Exit", "Exit BIOREACTOR OS?"):
            self.keep_running = False
            try:
                self.engine.a1.close()
                self.engine.a2.close()
            except Exception:
                pass
            self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    import argparse, platform

    ap = argparse.ArgumentParser()
    ap.add_argument("--kiosk", action="store_true", help="Fullscreen kiosk mode")
    ap.add_argument("--windowed", action="store_true", help="Force windowed mode")
    args = ap.parse_args()

    default_kiosk = (platform.system().lower() == "linux")
    kiosk = args.kiosk or (default_kiosk and not args.windowed)

    BioreactorGUI(kiosk=kiosk, windowed=args.windowed).run()


