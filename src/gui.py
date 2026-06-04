#!/usr/bin/env python3
"""Dear PyGui control panel — Unitree D1 arm drag-teach & replay.

Modes
-----
MANUAL  : motors enabled; sliders drive joints at 10 Hz.
RECORD  : motors released; physically move arm; positions captured at 10 Hz.
PLAYING : motors enabled; recorded frames streamed back at 10 Hz.
"""

import datetime
import json
import logging
import pathlib
import sys
import threading
import time

import dearpygui.dearpygui as dpg

_HERE = pathlib.Path(__file__).parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib  # type: ignore
    with open(_ROOT / "config" / "settings.toml", "rb") as _f:
        cfg = tomllib.load(_f)
except Exception as _e:
    sys.exit(f"Config error: {_e}")

from src.net_config import setup as net_setup
from src.arm_control import D1Arm

RECS_DIR = _ROOT / "recordings"
RECS_DIR.mkdir(exist_ok=True)

JOINT_NAMES  = ["J0 Base", "J1 Shoulder", "J2 Elbow↑", "J3 Elbow↓",
                "J4 Wrist Roll", "J5 Wrist Pitch", "J6 Gripper"]
JOINT_LIMITS = [(-135.0, 135.0), (-90.0, 90.0), (-90.0, 90.0), (-135.0, 135.0),
                (-90.0, 90.0), (-135.0, 135.0), (0.0, 65.0)]
CTRL_HZ      = 10
CTRL_DT      = 1.0 / CTRL_HZ

log = logging.getLogger("d1_gui")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


# ── Arm controller (background) ───────────────────────────────────────────────

class ArmController:
    """Manages DDS connection and 10 Hz control loop off the GUI thread."""

    def __init__(self):
        self.arm: D1Arm | None = None
        self.mode = "DISCONNECTED"
        self._lock = threading.Lock()
        self._target = [0.0] * 7
        self._recording: list[list[float]] = []
        self._playback: list[list[float]] = []
        self._play_idx = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Callbacks set by GUI
        self.on_log: callable = lambda msg: None
        self.on_feedback: callable = lambda joints: None
        self.on_progress: callable = lambda p: None
        self.on_mode: callable = lambda m: None

    # ── Connection ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        # net_setup() registers signal handlers; signal.signal() requires the
        # main thread, so run it here (called from the DPG callback on the main
        # thread) before handing off to the background thread for DDS work.
        net_cfg = cfg["network"]
        self.on_log("Configuring network…")
        try:
            net_setup(iface=net_cfg["iface"] or None)
        except Exception as exc:
            self.on_log(f"Network setup failed: {exc}")
            return
        self.on_log(f"Arm at {net_cfg['arm_ip']} reachable.")
        threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self) -> None:
        try:
            net_cfg = cfg["network"]
            self.arm = D1Arm(iface=net_cfg["iface"] or None)
            self.arm.connect()
            self.arm.enable_all(True)
            time.sleep(0.5)
            joints = self.arm.get_joints_cached() or [0.0] * 7
            with self._lock:
                self._target = list(joints)
            self.on_feedback(joints)
            self._start_loop()
            self._set_mode("MANUAL")
            self.on_log("Connected — MANUAL mode.")
        except Exception as exc:
            self.on_log(f"Connection failed: {exc}")
            log.exception("connect")

    def _start_loop(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ctrl")
        self._thread.start()

    def disconnect(self) -> None:
        self._stop.set()
        if self.arm:
            try:
                self.arm.disconnect()
            except Exception:
                pass
        self.arm = None
        self._set_mode("DISCONNECTED")

    # ── Control loop ──────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            mode = self.mode

            if mode == "MANUAL" and self.arm:
                with self._lock:
                    tgt = list(self._target)
                try:
                    self.arm.move_joints(tgt, mode=0)
                except Exception as exc:
                    log.debug("move_joints: %s", exc)

            elif mode == "RECORD" and self.arm:
                joints = self.arm.get_joints_cached()
                if joints:
                    # Command the arm to hold its current position — enough
                    # resistance to fight gravity but can be back-driven by hand.
                    try:
                        self.arm.move_joints(joints, mode=0)
                    except Exception as exc:
                        log.debug("record hold: %s", exc)
                    self._recording.append(list(joints))
                    self.on_feedback(joints)
                    if len(self._recording) % CTRL_HZ == 0:
                        secs = len(self._recording) * CTRL_DT
                        self.on_log(f"● REC  {secs:.0f}s")

            elif mode == "PLAYING" and self.arm:
                if self._play_idx < len(self._playback):
                    frame = self._playback[self._play_idx]
                    try:
                        self.arm.move_joints(frame, mode=0)
                    except Exception:
                        pass
                    with self._lock:
                        self._target = list(frame)
                    self.on_feedback(frame)
                    self.on_progress(self._play_idx / max(1, len(self._playback) - 1))
                    self._play_idx += 1
                else:
                    self._play_idx = 0
                    self.on_progress(0.0)
                    self._set_mode("MANUAL")
                    self.on_log("Playback complete — MANUAL mode.")

            # Always refresh feedback display
            if self.arm and mode not in ("RECORD", "PLAYING"):
                joints = self.arm.get_joints_cached()
                if joints:
                    self.on_feedback(joints)

            time.sleep(max(0.0, CTRL_DT - (time.monotonic() - t0)))

    # ── Actions ───────────────────────────────────────────────────────────────

    def home(self) -> None:
        if not self.arm or self.mode != "MANUAL":
            return
        def _do():
            self._set_mode("BUSY")
            self.on_log("Homing…")
            self.arm.home()
            time.sleep(4.0)
            joints = self.arm.get_joints_cached() or [0.0] * 7
            with self._lock:
                self._target = list(joints)
            self.on_feedback(joints)
            self._set_mode("MANUAL")
            self.on_log("Homed — MANUAL mode.")
        threading.Thread(target=_do, daemon=True).start()

    def start_record(self) -> None:
        if not self.arm or self.mode != "MANUAL":
            return
        self._recording = []
        self._set_mode("RECORD")
        self.on_log("Recording — arm holds position, push gently to move. Stop Rec when done.")

    def stop_record(self) -> str | None:
        if self.mode != "RECORD":
            return None
        self._set_mode("BUSY")
        frames = list(self._recording)
        self._recording = []
        joints = self.arm.get_joints_cached() or (frames[-1] if frames else [0.0]*7)
        with self._lock:
            self._target = list(joints)
        if not frames:
            self._set_mode("MANUAL")
            self.on_log("Nothing recorded.")
            return None
        name = datetime.datetime.now().strftime("rec_%Y%m%d_%H%M%S.json")
        path = RECS_DIR / name
        path.write_text(json.dumps({
            "name": name,
            "recorded_at": datetime.datetime.now().isoformat(),
            "fps": CTRL_HZ,
            "frame_count": len(frames),
            "duration_s": round(len(frames) * CTRL_DT, 2),
            "frames": frames,
        }, indent=2))
        self._set_mode("MANUAL")
        self.on_log(f"Saved {len(frames)} frames ({len(frames)*CTRL_DT:.1f}s) → {name}")
        return name

    def start_play(self, path: pathlib.Path) -> None:
        if not self.arm or self.mode != "MANUAL":
            return
        try:
            data = json.loads(path.read_text())
            self._playback = data["frames"]
        except Exception as exc:
            self.on_log(f"Load failed: {exc}")
            return
        self._play_idx = 0
        self.arm.enable_all(True)
        self._set_mode("PLAYING")
        dur = len(self._playback) * CTRL_DT
        self.on_log(f"Playing {path.name}  ({dur:.1f}s, {len(self._playback)} frames)")

    def stop_play(self) -> None:
        if self.mode == "PLAYING":
            self._play_idx = len(self._playback)

    def set_joint(self, idx: int, value: float) -> None:
        with self._lock:
            self._target[idx] = value

    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        self.on_mode(mode)


# ── GUI ───────────────────────────────────────────────────────────────────────

class D1GUI:
    W, H = 1100, 720
    SLIDER_W = 420
    LOG_LINES = 10

    def __init__(self):
        self.ctrl = ArmController()
        self.ctrl.on_log      = self._on_log
        self.ctrl.on_feedback = self._on_feedback
        self.ctrl.on_progress = self._on_progress
        self.ctrl.on_mode     = self._on_mode
        self._log_buf: list[str] = []
        self._rec_files: list[str] = []

    # ── Callbacks from controller (called from background thread) ─────────────

    def _on_log(self, msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._log_buf.append(line)
        if len(self._log_buf) > self.LOG_LINES:
            self._log_buf.pop(0)
        if dpg.does_item_exist("log_box"):
            dpg.set_value("log_box", "\n".join(self._log_buf))

    def _on_feedback(self, joints: list[float]) -> None:
        for i, v in enumerate(joints):
            if dpg.does_item_exist(f"fb_{i}"):
                dpg.set_value(f"fb_{i}", f"{v:+.1f}°")
            if self.ctrl.mode in ("RECORD", "PLAYING"):
                if dpg.does_item_exist(f"sl_{i}"):
                    dpg.set_value(f"sl_{i}", v)

    def _on_progress(self, p: float) -> None:
        if dpg.does_item_exist("play_bar"):
            dpg.set_value("play_bar", p)
        if dpg.does_item_exist("play_pct"):
            dpg.set_value("play_pct", f"{int(p*100)}%")

    def _on_mode(self, mode: str) -> None:
        connected = self.ctrl.arm is not None
        manual  = mode == "MANUAL"
        record  = mode == "RECORD"
        playing = mode == "PLAYING"
        busy    = mode == "BUSY"

        pairs = {
            "btn_home":     connected and manual,
            "btn_enable":   connected and not record,
            "btn_record":   connected and manual,
            "btn_stoprec":  record,
            "btn_play":     connected and manual and bool(self._rec_files),
            "btn_stopplay": playing,
            "btn_connect":  not connected,
        }
        for tag, enabled in pairs.items():
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, enabled=bool(enabled))

        for i in range(7):
            if dpg.does_item_exist(f"sl_{i}"):
                dpg.configure_item(f"sl_{i}", enabled=manual)

        status_map = {
            "DISCONNECTED": ("DISCONNECTED", (150, 150, 150)),
            "MANUAL":       ("MANUAL — sliders active", (100, 220, 100)),
            "RECORD":       ("● RECORDING — move arm freely", (220, 80, 80)),
            "PLAYING":      ("▶ PLAYING", (80, 160, 220)),
            "BUSY":         ("BUSY…", (200, 160, 60)),
        }
        text, color = status_map.get(mode, (mode, (200, 200, 200)))
        if dpg.does_item_exist("status_txt"):
            dpg.set_value("status_txt", text)
        if dpg.does_item_exist("status_col"):
            dpg.configure_item("status_col", default_value=color)

    # ── GUI button callbacks ──────────────────────────────────────────────────

    def _cb_connect(self)          : self.ctrl.connect()
    def _cb_home(self)             : self.ctrl.home()
    def _cb_enable(self)           : self.ctrl.arm and self.ctrl.arm.enable_all(True) or None
    def _cb_record(self)           : self.ctrl.start_record()
    def _cb_stop_play(self)        : self.ctrl.stop_play()

    def _cb_stop_rec(self):
        name = self.ctrl.stop_record()
        if name:
            self._refresh_recordings()

    def _cb_play(self):
        sel = dpg.get_value("rec_list")
        if not sel or sel not in self._rec_files:
            self._on_log("No recording selected.")
            return
        self.ctrl.start_play(RECS_DIR / sel)

    def _cb_slider(self, sender, value, user_data):
        self.ctrl.set_joint(user_data, float(value))

    def _refresh_recordings(self):
        files = sorted(
            [f.name for f in RECS_DIR.glob("*.json")],
            reverse=True
        )
        self._rec_files = files
        if dpg.does_item_exist("rec_list"):
            dpg.configure_item("rec_list", items=files)
            if files:
                dpg.set_value("rec_list", files[0])
        # Enable play button if connected and manual
        if dpg.does_item_exist("btn_play"):
            enabled = (self.ctrl.arm is not None
                       and self.ctrl.mode == "MANUAL"
                       and bool(files))
            dpg.configure_item("btn_play", enabled=enabled)

    # ── Layout ────────────────────────────────────────────────────────────────

    def build(self):
        dpg.create_context()
        dpg.create_viewport(title="D1 Arm Control", width=self.W, height=self.H,
                            resizable=False)
        dpg.setup_dearpygui()

        with dpg.window(label="D1 Arm Control", tag="main",
                        width=self.W, height=self.H, no_close=True,
                        no_title_bar=True, no_move=True, no_resize=True):

            # ── Top bar ───────────────────────────────────────────────────────
            with dpg.group(horizontal=True):
                dpg.add_text("Unitree D1-550", color=(180, 180, 255))
                dpg.add_spacer(width=20)
                dpg.add_text("Status: ", color=(160, 160, 160))
                dpg.add_color_button(tag="status_col", default_value=(150, 150, 150),
                                     no_tooltip=True, width=14, height=14)
                dpg.add_text("DISCONNECTED", tag="status_txt")
                dpg.add_spacer(width=30)
                dpg.add_button(label="Connect", tag="btn_connect",
                               callback=self._cb_connect)

            dpg.add_separator()
            dpg.add_spacer(height=4)

            # ── Two-column body ───────────────────────────────────────────────
            with dpg.group(horizontal=True):

                # Left: sliders ───────────────────────────────────────────────
                with dpg.child_window(width=self.SLIDER_W, height=self.H - 80,
                                      border=True, label="Joint Control"):
                    dpg.add_text("JOINT CONTROL", color=(180, 180, 255))
                    dpg.add_separator()
                    dpg.add_spacer(height=6)
                    for i, (name, (lo, hi)) in enumerate(zip(JOINT_NAMES, JOINT_LIMITS)):
                        dpg.add_text(name, color=(200, 200, 200))
                        dpg.add_slider_float(
                            tag=f"sl_{i}",
                            min_value=lo, max_value=hi,
                            default_value=0.0,
                            width=self.SLIDER_W - 24,
                            format="%.1f°",
                            callback=self._cb_slider,
                            user_data=i,
                            enabled=False,
                        )
                        dpg.add_spacer(height=2)

                dpg.add_spacer(width=8)

                # Right panel ─────────────────────────────────────────────────
                with dpg.group():

                    # Feedback ────────────────────────────────────────────────
                    with dpg.child_window(width=self.W - self.SLIDER_W - 28,
                                          height=130, border=True):
                        dpg.add_text("LIVE FEEDBACK", color=(180, 180, 255))
                        dpg.add_separator()
                        with dpg.table(header_row=False, borders_innerV=True):
                            for _ in range(4):
                                dpg.add_table_column()
                            for row in [(0,1,2,3), (4,5,6)]:
                                with dpg.table_row():
                                    for i in row:
                                        with dpg.table_cell():
                                            dpg.add_text(f"{JOINT_NAMES[i]}:", color=(160,160,160))
                                            dpg.add_text("---", tag=f"fb_{i}")
                                    for _ in range(4 - len(row)):
                                        dpg.add_table_cell()

                    dpg.add_spacer(height=6)

                    # Actions ─────────────────────────────────────────────────
                    with dpg.child_window(width=self.W - self.SLIDER_W - 28,
                                          height=90, border=True):
                        dpg.add_text("ACTIONS", color=(180, 180, 255))
                        dpg.add_separator()
                        with dpg.group(horizontal=True):
                            dpg.add_button(label="Home",   tag="btn_home",
                                           callback=self._cb_home,    enabled=False)
                            dpg.add_button(label="Enable", tag="btn_enable",
                                           callback=self._cb_enable,  enabled=False)
                            dpg.add_spacer(width=20)
                            dpg.add_button(label="● Record", tag="btn_record",
                                           callback=self._cb_record,   enabled=False)
                            dpg.add_button(label="■ Stop Rec", tag="btn_stoprec",
                                           callback=self._cb_stop_rec, enabled=False)
                            dpg.add_spacer(width=20)
                            dpg.add_button(label="▶ Play",    tag="btn_play",
                                           callback=self._cb_play,     enabled=False)
                            dpg.add_button(label="■ Stop Play", tag="btn_stopplay",
                                           callback=self._cb_stop_play, enabled=False)

                    dpg.add_spacer(height=6)

                    # Recordings ──────────────────────────────────────────────
                    with dpg.child_window(width=self.W - self.SLIDER_W - 28,
                                          height=190, border=True):
                        dpg.add_text("RECORDINGS", color=(180, 180, 255))
                        dpg.add_separator()
                        dpg.add_listbox(tag="rec_list", items=self._rec_files,
                                        num_items=6,
                                        width=self.W - self.SLIDER_W - 40)
                        dpg.add_button(label="↻ Refresh", callback=self._refresh_recordings)

                    dpg.add_spacer(height=6)

                    # Playback progress ───────────────────────────────────────
                    with dpg.child_window(width=self.W - self.SLIDER_W - 28,
                                          height=50, border=True):
                        with dpg.group(horizontal=True):
                            dpg.add_text("Playback:", color=(160, 160, 160))
                            dpg.add_progress_bar(tag="play_bar", default_value=0.0,
                                                 width=self.W - self.SLIDER_W - 140)
                            dpg.add_text("0%", tag="play_pct")

                    dpg.add_spacer(height=6)

                    # Log ─────────────────────────────────────────────────────
                    with dpg.child_window(width=self.W - self.SLIDER_W - 28,
                                          height=self.H - 80 - 130 - 90 - 190 - 50 - 50,
                                          border=True):
                        dpg.add_text("LOG", color=(180, 180, 255))
                        dpg.add_separator()
                        dpg.add_text("", tag="log_box", wrap=self.W - self.SLIDER_W - 50)

        self._refresh_recordings()
        self._on_log("Ready — click Connect to start.")

    def run(self):
        self.build()
        dpg.show_viewport()
        dpg.set_primary_window("main", True)
        dpg.start_dearpygui()
        self.ctrl.disconnect()
        dpg.destroy_context()


def main():
    gui = D1GUI()
    gui.run()


if __name__ == "__main__":
    main()
