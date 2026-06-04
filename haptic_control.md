# haptic_control.md — D1 Arm: Backdrivable Teach / Record Mode
**Project:** `unitree-d1-control`  
**Target:** Claude AI agent editing existing project files  
**Platform:** Pop!_OS / Ubuntu Linux, Python venv, d1_sdk (unitree_sdk2 + CycloneDDS)  
**Arm:** Unitree D1-550, 6-DOF + gripper  
**Goal:** Enable a "haptic teach" mode where the operator can freely move joints by hand while the arm holds itself against gravity (does not fall), and record the resulting trajectory for replay.

---

## Background & Problem

The GUI currently locks the arm during "Record" mode regardless of the hardness/stiffness slider setting. The arm remains fully stiff because the D1 SDK command topic (`rt/arm_Command`) sends a high-stiffness PD command continuously. The arm will not backdrip until `kp` and `kd` are both lowered.

The D1 uses the same SDK2 control law as the broader Unitree platform:

```
τ_out = kp * (q_des − q_meas) + kd * (dq_des − dq_meas) + tau_ff
```

- With **high kp/kd** (default position mode): arm locks to `q_des` — rigid, non-backdrivable.
- With **kp=0, kd=0, tau_ff=0**: zero-torque / passive — arm falls under gravity immediately.
- With **kp=0, kd=small, tau_ff=gravity_torque**: gravity-compensated, backdrivable — operator can push joints freely, arm holds its own weight.

The "record" function needs the third mode.

### Known D1 ArmString Command Fields (from D1 SDK / Caltech research)

The D1 arm uses `ArmString`-wrapped commands on `rt/arm_Command` with fields:
- `mode`: `0` = "small smoothing" (10 Hz interpolation), `1` = "large smoothing" (trajectory use)
- `execution_time`: seconds for the arm to complete the move
- `q[0..5]`: joint positions (radians), `q[6]`: gripper angle (degrees)

The underlying per-joint motor command when using low-level DDS access includes:
- `q`, `dq`, `kp`, `kd`, `tau` (feedforward torque)

> **⚠️ SDK ambiguity note:** The D1 SDK wraps commands in `ArmString` rather than exposing raw `MotorCmd` fields directly. If the SDK exposes `kp`/`kd` per-joint (via a low-level mode or separate topic), use those. If not, gravity compensation must be implemented via the `tau_ff` field in `ArmString` or a separate low-level DDS topic. Claude should check the D1 SDK source for any `LowCmd`-style topic (e.g. `rt/arm_sdk` or `rt/arm_LowCmd`) that exposes per-joint kp/kd. If such a topic exists, use it for teach mode. If only `ArmString` on `rt/arm_Command` is available, use the tau-based approach described below.

---

## What Claude Must Implement

### New files / modules to create

```
src/
├── haptic_teach.py        # NEW: teach mode manager (gravity comp + record)
├── gravity_model.py       # NEW: simple static gravity torque estimator
└── replay.py              # NEW: replay recorded trajectory
```

### Modifications to existing files

- `src/main.py` — add `--mode teach` and `--mode replay` CLI flags
- `src/arm_control.py` — add `set_backdrivable()` and `set_stiff()` methods
- `config/settings.toml` — add `[teach]` and `[gravity]` sections
- GUI file (wherever "Record" button logic lives) — connect to `haptic_teach.py`

---

## Phase A — Gravity Compensation Model (`gravity_model.py`)

Claude must write `src/gravity_model.py` that computes the static gravity torque for each joint given the current joint angles.

### Method

Use a simplified **static torque model** based on link masses and geometry:

```
τ_gravity[i] = Σ_j (m_j * g * l_j * sin(θ_effective_j))
```

where the sum is over all joints distal to joint `i`.

For a first implementation, use a **precomputed lookup / polynomial** approach rather than full Pinocchio RNEA, unless Pinocchio is already in the venv.

### D1 link parameters (approximate, from URDF/research)

| Segment | Approx. Mass | CoM distance from proximal joint |
|---------|-------------|----------------------------------|
| Link 1 (J1 base) | 0.8 kg | 0.05 m |
| Link 2 (J2 shoulder) | 0.6 kg | 0.12 m |
| Link 3 (J3 upper arm) | 0.4 kg | 0.14 m |
| Link 4 (J4 elbow) | 0.3 kg | 0.10 m |
| Link 5 (J5 wrist roll) | 0.15 kg | 0.06 m |
| Link 6 (J6 wrist pitch) | 0.10 kg | 0.04 m |
| Gripper | 0.15 kg | 0.03 m |

> **Note:** These are estimates. Claude should load actual values from the D1 URDF (included in the SDK under `urdf/` or similar) if available.

### Skeleton

```python
# src/gravity_model.py

import numpy as np

G = 9.81  # m/s²

# If Pinocchio is available in the venv, use RNEA for full accuracy.
# Otherwise use the simplified chain model below.
try:
    import pinocchio as pin
    USE_PINOCCHIO = True
except ImportError:
    USE_PINOCCHIO = False

LINK_MASSES  = [0.8, 0.6, 0.4, 0.3, 0.15, 0.10, 0.15]   # kg, J1..J6+gripper
LINK_COM_LEN = [0.05, 0.12, 0.14, 0.10, 0.06, 0.04, 0.03] # m, CoM from proximal

def gravity_torques(q: list[float]) -> list[float]:
    """
    Estimate static gravity torques for all 6 joints given joint angles q (radians).
    Returns list of 6 torques in N·m.
    If Pinocchio is available, use RNEA; else use simplified chain model.
    """
    ...

def gravity_torques_simple(q: list[float]) -> list[float]:
    """Simplified recursive static torque from distal mass sums."""
    ...

def gravity_torques_pinocchio(q: list[float]) -> list[float]:
    """Full RNEA via Pinocchio. Requires pinocchio and D1 URDF path."""
    ...
```

### Tuning `tau_gain`

The gravity model will not be perfect. Add a `tau_gain` scalar (default `0.85`) in `settings.toml` that scales all gravity torques down slightly. This means the arm will drift very slowly downward instead of being perfectly compensated, which is **safer** — the operator can feel a gentle bias confirming the arm is under their control without it flying upward.

---

## Phase B — Teach Mode Manager (`haptic_teach.py`)

Claude must write `src/haptic_teach.py`:

### Mode states

```
IDLE  →  TEACH (gravity comp + record ON)  →  STOPPED
                                           ↓
                                        REPLAY (via replay.py)
```

### Core logic

```python
# src/haptic_teach.py

import time, pathlib, csv, threading
from src.gravity_model import gravity_torques
from src.arm_control import D1Arm

RECORD_HZ     = 10          # D1 control cycle is 10 Hz
BACKDRIVE_KD  = 0.5         # N·m/(rad/s) — light damping to prevent oscillation
BACKDRIVE_KP  = 0.0         # zero stiffness = fully backdrivable
TAU_GAIN      = 0.85        # loaded from settings.toml

class HapticTeach:
    def __init__(self, arm: D1Arm, output_path: str): ...

    def start_teach(self) -> None:
        """
        1. Read current joint positions from rt/arm_Feedback.
        2. Set arm to backdrivable mode: kp=0, kd=BACKDRIVE_KD, tau=gravity_torques(q)*TAU_GAIN.
        3. Start recording loop at RECORD_HZ in a daemon thread.
        4. Update gravity compensation every cycle using latest q from feedback.
        """
        ...

    def stop_teach(self) -> str:
        """Stop recording, restore default kp/kd, return path to saved CSV."""
        ...

    def _record_loop(self) -> None:
        """
        Daemon thread:
        - Subscribe to rt/arm_Feedback to get q_measured.
        - Each cycle: update tau = gravity_torques(q_measured) * TAU_GAIN.
        - Publish arm command: kp=0, kd=BACKDRIVE_KD, q=q_measured, tau=tau.
        - Append (timestamp, q[0..5], gripper) to in-memory list.
        """
        ...

    def save_trajectory(self, path: str) -> None:
        """Write recorded samples to CSV with columns: t, j0, j1, j2, j3, j4, j5, gripper."""
        ...
```

### Key point: `q_des = q_measured`

During teach mode, **set `q_des` to the most recent measured position at every cycle**. This means:
- The PD term: `kp * (q_des − q_meas) ≈ 0` (always tracking current position)
- The damping term: `kd * (0 − dq_meas)` provides velocity damping (prevents oscillation)
- The feedforward: `tau = gravity_compensation` holds the arm weight

This is the standard technique used across Unitree and other robot arm platforms for kinesthetic teaching.

> **Critical:** Never send `q_des` = old target position while in teach mode. The arm will fight the operator trying to move it. `q_des` must continuously track the actual measured position.

---

## Phase C — Backdrivable Mode in `arm_control.py`

Claude must add two methods to the existing `D1Arm` class:

```python
def set_backdrivable(self, tau_gain: float = 0.85) -> None:
    """
    Switch all joints to gravity-compensated, zero-stiffness mode.
    kp=0, kd=BACKDRIVE_KD, q=current_q, tau=gravity_torques(current_q)*tau_gain.
    Called by HapticTeach.start_teach().
    """
    ...

def set_stiff(self, kp: float = 80.0, kd: float = 5.0) -> None:
    """
    Restore normal position control mode.
    Called by HapticTeach.stop_teach() and on any error/SIGINT.
    Before restoring stiffness: interpolate from current q to nearest safe q over 1.5s
    to avoid sudden jerk.
    """
    ...
```

### DDS topic for per-joint kp/kd

The D1 SDK may expose per-joint control via one of these DDS topics:
- `rt/arm_sdk` (used in G1 arm SDK DDS examples — most likely for D1 low-level)
- `rt/arm_LowCmd` (possible variant)
- `rt/arm_Command` with embedded kp/kd fields

Claude must check the D1 SDK source (`d1_sdk/` directory) for the IDL/message type definitions to confirm the correct topic and field names.

If the D1 only supports `ArmString` on `rt/arm_Command`, the kp/kd control must be approximated using the `tau_ff` field:
- Set `execution_time` to a short value (e.g., 0.1 s) to keep the arm responsive.
- Send `q_des = q_meas` continuously.
- Rely purely on `tau = gravity_torques(q) * gain` for support.
- Accept that the arm may be slightly more "sticky" than true zero-kp mode.

---

## Phase D — Trajectory Replay (`replay.py`)

Claude must write `src/replay.py`:

```python
# src/replay.py

import csv, time
from src.arm_control import D1Arm

class TrajectoryReplay:
    def __init__(self, arm: D1Arm, csv_path: str): ...

    def play(self, speed: float = 1.0) -> None:
        """
        Load trajectory CSV.
        For each waypoint:
          - Move arm to q_waypoint using arm.move_joints() at appropriate speed.
          - Respect original timestamps scaled by `speed`.
          - If speed < original → slower; speed > 1 → faster.
        On KeyboardInterrupt: call arm.home().
        """
        ...

    def play_loop(self, n: int = -1, speed: float = 1.0) -> None:
        """Play trajectory n times (or forever if n=-1)."""
        ...
```

---

## Phase E — Configuration additions (`config/settings.toml`)

Add to the existing `settings.toml`:

```toml
[teach]
record_hz     = 10        # recording sample rate (matches D1 control cycle)
output_dir    = "recordings"
backdrive_kd  = 0.5       # N·m/(rad/s) damping during teach mode
max_session_s = 120       # auto-stop recording after this many seconds

[gravity]
tau_gain      = 0.85      # scale factor for gravity torque (0.7–1.0 typical)
use_pinocchio = false     # set true if pinocchio is installed in venv
urdf_path     = ""        # path to D1 URDF; blank = use SDK default
link_masses   = [0.8, 0.6, 0.4, 0.3, 0.15, 0.10, 0.15]
link_com_lens = [0.05, 0.12, 0.14, 0.10, 0.06, 0.04, 0.03]

[replay]
speed         = 1.0       # playback speed multiplier
loop_count    = 1         # -1 = loop forever
```

---

## Phase F — GUI integration

Claude must locate the existing Record button handler in the GUI file and replace its logic:

### Current (broken) behaviour
```python
# OLD — leaves arm in stiff mode while recording
def on_record_clicked():
    self.is_recording = True
    # no mode change — arm stays stiff
```

### New behaviour
```python
# NEW — switches to backdrivable teach mode
def on_record_clicked():
    self.haptic_teacher = HapticTeach(arm=self.arm,
                                       output_path=settings.teach.output_dir)
    self.haptic_teacher.start_teach()
    self.record_btn.setText("Stop Recording")

def on_stop_record_clicked():
    path = self.haptic_teacher.stop_teach()
    self.status_label.setText(f"Saved: {path}")
    self.record_btn.setText("Record")

def on_replay_clicked():
    player = TrajectoryReplay(arm=self.arm, csv_path=self.last_saved_path)
    player.play(speed=float(self.speed_slider.value()) / 100.0)
```

---

## Phase G — Prompt for Claude

Use this prompt verbatim when asking Claude to implement the above:

```
Read haptic_control.md in this repository and implement the following changes to
the unitree-d1-control project:

1. Create src/gravity_model.py — static gravity torque estimator for the D1 arm.
   - Use simplified chain model by default.
   - Optionally use Pinocchio RNEA if `use_pinocchio = true` in settings.toml.
   - Load link masses and COM lengths from settings.toml [gravity] section.

2. Create src/haptic_teach.py — HapticTeach class.
   - start_teach(): set kp=0, kd=backdrive_kd, q_des=q_meas, tau=gravity_torques(q)*tau_gain.
   - _record_loop(): runs in daemon thread at record_hz, saves (t, j0..j5, gripper) to list.
   - stop_teach(): restore stiffness (interpolate over 1.5 s), save CSV, return path.

3. Create src/replay.py — TrajectoryReplay class.
   - play(speed): load CSV, send each waypoint respecting original timestamps * speed.

4. Modify src/arm_control.py — add set_backdrivable() and set_stiff() to D1Arm.
   - set_backdrivable() must send kp=0, kd=settings.teach.backdrive_kd, q=current_q,
     tau=gravity_torques(current_q)*tau_gain continuously.
   - First: determine correct DDS topic for per-joint kp/kd by inspecting d1_sdk/ IDL files.
     If rt/arm_sdk or equivalent low-level topic exists, prefer it over ArmString.

5. Modify config/settings.toml — add [teach], [gravity], [replay] sections.

6. Modify the GUI Record button handler — wire to HapticTeach.start_teach() /
   stop_teach() and add Replay button wired to TrajectoryReplay.play().

7. All functions must have docstrings and type hints.
8. Register set_stiff() in atexit and SIGINT/SIGTERM handlers so the arm is never
   left in zero-kp mode if the process is killed.
9. Add a "recordings/" directory to .gitignore.
```

---

## Safety Constraints

> ⚠️ **Never leave kp=0, kd=0, tau=0 (passive mode) on a powered arm without a physical support.**  
> The arm will fall immediately and may damage hardware or injure the operator.  
> Always maintain at least `kd = backdrive_kd` and `tau = gravity_compensation` during teach mode.

> ⚠️ **Before switching from teach mode back to stiff mode, interpolate smoothly over ≥1.5 seconds.**  
> A sudden kp jump will cause the arm to snap to `q_des` at high speed.

> ⚠️ **Teach mode timeout:** Auto-stop recording after `max_session_s` seconds to prevent runaway sessions.

> ⚠️ **SIGKILL:** If the process is killed with SIGKILL (-9), the arm will remain in the last commanded mode.  
> Always run `src/arm_control.py restore` or power-cycle the arm if this happens.

### Manual restore (emergency)

```python
# Run this standalone to restore stiff mode from a separate terminal:
# python3 -c "
# import sys; sys.path.insert(0, 'src')
# from arm_control import D1Arm
# arm = D1Arm('...')
# arm.connect()
# arm.set_stiff()
# arm.home()
# arm.disconnect()
# "
```

---

## Known D1 Hardware Notes

- The D1 arm's control cycle is **10 Hz** — do not command faster than this via `ArmString`.
  If using low-level `rt/arm_sdk`, higher rates (50–100 Hz) are possible.
- Gravity compensation quality degrades significantly for J2 (shoulder) when the arm is extended
  horizontally — use `tau_gain ≈ 0.9` for that configuration.
- The D1 SDK group was reportedly rebuilt after internal disruption; some documentation
  gaps exist. Always inspect SDK source files before assuming field names.
- The arm **does not have an internal gravity compensation mode** that can be toggled via funcode —
  gravity compensation must be implemented externally in the control loop.
- The Caltech AMBER Lab found the D1 arm to be most stable when receiving commands at
  **100 Hz** rather than 10 Hz — consider this for the teach loop if a low-level topic is accessible.

---

## References

- Caltech AMBER Lab SURF 2025 — D1 arm integration report (mode 0/1, execution_time fields, 10/100 Hz findings)
- Unitree SDK2 G1 Arm Control: `rt/arm_sdk` topic, kp/kd/tau per-joint control law
- Unitree SDK2 Safety Docs: passive mode = kp=0, kd=0, tau=0; backdrivable = kp=0, kd>0
- Unitree SDK2 G1 low-level: `motor_cmd[i].kp = 0`, `motor_cmd[i].kd = 0.5` for damping mode
- Unitree H1/Go1 low-level forum: MotorCmd tau biases output torque for gravity compensation
- General gravity compensation theory: τ_grav[i] = Σ_distal(m_j * g * l_j * sin(θ_j))
