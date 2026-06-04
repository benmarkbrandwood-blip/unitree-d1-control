# Unitree D1 Arm – Ethernet Control & USB Debug Setup Plan
**Target:** Claude AI agent on local PC  
**Platform:** Pop!_OS / Ubuntu Linux  
**Arm:** Unitree D1 (non-T) — 6-DOF servo arm  
**Interfaces:** RJ45 Ethernet (control) + USB Type-C (debug/monitor)

---

## Plan Review Notes
*(Added after cross-checking against unitreerobotics/z1_sdk and chen37058/Grasp-with-the-Unitree-D1 on GitHub)*

### ✅ Confirmed: D1 uses unitree_sdk2 + CycloneDDS — NOT z1_sdk

Verified from the official D1 developer docs (`D1 arm services.pdf`), the official `d1_sdk.zip`, and the `d1_description` URDF. Key findings:

- **SDK**: `d1_sdk` (official, C++ only) wraps `unitree_sdk2` + CycloneDDS v0.10.2
- **Command topic**: `rt/arm_Command` — publish `ArmString_` (JSON string)
- **Feedback topic**: `rt/arm_Feedback` — subscribe `ArmString_` (JSON string)
- **Angle units**: **degrees** (not radians)
- **Joint numbering**: 0-indexed — J0 (base) through J5 (wrist pitch) + J6 (gripper)
- **Control cycle**: 10 Hz
- **No z1_ctrl daemon** — DDS communicates directly with the arm over Ethernet
- **Python**: No official Python binding; use `cyclonedds==0.10.2` Python library to publish JSON directly

Phases 3 and 6 have been **fully rewritten** for d1_sdk below.

### Other corrections applied

| # | Location | Issue | Fix |
|---|----------|-------|-----|
| 1 | requirements.txt | `tomllib` is not a PyPI package (stdlib 3.11+) | Removed; kept `tomli` only |
| 2 | Hardware Reference | Power spec was wrong (`24V, 2.5A`) | Corrected to `24V 10A (15–48V, 240W max)` from official spec |
| 3 | Phase 3 | Entire phase assumed z1_sdk | **Rewritten** for d1_sdk + unitree_sdk2 + CycloneDDS |
| 4 | Phase 6 | Entire phase assumed z1_sdk API (radians, wrong gripper, wrong methods) | **Rewritten** for d1_sdk JSON-over-DDS API (degrees, funcode table) |
| 5 | Phase 6 | Joint limits hardcoded and wrong | Now sourced from official URDF + spec doc |
| 6 | Phase 7 | Demo used 1-based joint indexing | Corrected to 0-indexed (J0 = base) |
| 7 | Phase 8 | `sdk_lib_path` pointed at z1_sdk | Updated for d1_sdk / cyclonedds |
| 8 | Safety notes | Referenced z1_ctrl | Removed; D1 has no z1_ctrl daemon |

---

## Overview

This plan instructs Claude to:
1. Scaffold a Python venv project with all dependencies
2. Write a setup script that **saves and restores** the host PC's Ethernet IP configuration
3. Configure the PC NIC to `192.168.123.162/24` (same subnet as arm default `192.168.123.110`)
4. Enable USB-C serial monitoring via `/dev/ttyACM0` or `/dev/ttyUSB0`
5. Provide a minimal motion demo (joint homing → test move → return to zero)
6. Restore network settings on exit or error

---

## Hardware Reference

| Interface | Connector | Purpose |
|-----------|-----------|---------|
| Ethernet  | RJ45      | 100 Mbps control — DDS over this interface |
| Debug     | USB Type-C | Serial port monitoring / firmware debug |
| Power     | DC barrel  | 24 V, 10 A rated (15–48 V range, 240 W max) |

- **Model:** D1-550 — arm length 550 mm (670 mm with jaws), rated load 500 g, weight 3152 g
- **Arm default IP:** `192.168.123.110`
- **Required PC IP:** `192.168.123.162/24` (or any unused `.123.x` address)
- **USB-C serial device:** typically `/dev/ttyACM0` or `/dev/ttyUSB0` at 115200 baud
- **SDK:** `d1_sdk` (C++ only) using `unitree_sdk2` + CycloneDDS v0.10.2; Python via `cyclonedds==0.10.2`
- **Control method:** DDS publish/subscribe at 10 Hz
- **Joints:** 7 total — J0 (base) to J5 (wrist pitch), 0-indexed + J6 (gripper)

---

## Project Structure

```
unitree_d1_control/
├── plan.md                  # This document
├── setup_env.sh             # One-shot: venv + system deps install
├── requirements.txt         # Python dependencies
├── src/
│   ├── net_config.py        # Save/apply/restore Ethernet IP
│   ├── usb_monitor.py       # USB-C serial reader (async thread)
│   ├── arm_control.py       # Arm SDK wrapper (homing, joint moves)
│   └── main.py              # Entry point — wires everything together
├── config/
│   └── settings.toml        # Tunable parameters (IP, port, speeds)
└── logs/
    └── .gitkeep
```

---

## Phase 1 — System Prerequisites

**Ask Claude to verify / install these before any Python work:**

```bash
# Required system packages
sudo apt update
sudo apt install -y \
    python3-pip python3-venv python3-dev \
    cmake g++ make git \
    iproute2 net-tools \
    screen minicom
    # screen/minicom for USB-C serial monitoring fallback

# Add user to dialout group (needed for /dev/ttyACM0 access)
sudo usermod -aG dialout $USER
# Log out and back in, or: newgrp dialout
```

---

## Phase 2 — Python venv Setup (`setup_env.sh`)

Claude should generate `setup_env.sh` that:

1. Creates `venv/` inside the project root
2. Installs `requirements.txt`
3. Optionally clones and builds the `z1_sdk` Python wrapper if not already present

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "Venv ready. Activate with: source venv/bin/activate"
```

### `requirements.txt` contents

```
pyserial>=3.5          # USB-C serial port
tomli>=2.0.0; python_version < '3.11'  # tomllib is stdlib >=3.11; tomli backport for older
numpy>=1.24
cyclonedds==0.10.2     # CycloneDDS Python binding — must match system library version
# cyclonedds requires CYCLONEDDS_HOME env var pointing to compiled CycloneDDS install
# See Phase 3 for CycloneDDS system library build instructions
```

---

## Phase 3 — D1 SDK Setup (d1_sdk + unitree_sdk2 + CycloneDDS)

The D1 communicates via **DDS over Ethernet** using `unitree_sdk2` and CycloneDDS v0.10.2.
The official `d1_sdk` is C++ only; Python sends JSON directly via the `cyclonedds` library.
There is no background daemon — DDS communicates with the arm directly.

### 3a. Install CycloneDDS system library (v0.10.2)

```bash
# CycloneDDS v0.10.2 — exact version required
git clone https://github.com/eclipse-cyclonedds/cyclonedds -b releases/0.10.x
cd cyclonedds && mkdir build install && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=../install
cmake --build . --target install
# Install system-wide so unitree_sdk2 and Python binding can find it
sudo cp -r install/include/* /usr/local/include/
sudo cp -r install/lib/*.so* /usr/local/lib/
sudo ldconfig
export CYCLONEDDS_HOME=$(pwd)/../install   # needed for pip install cyclonedds
```

### 3b. Install unitree_sdk2 C++ library

```bash
git clone https://github.com/unitreerobotics/unitree_sdk2.git
cd unitree_sdk2
mkdir build && cd build
cmake ..
make -j$(nproc)
sudo make install
sudo ldconfig
```

### 3c. Install Python CycloneDDS binding

The `cyclonedds` PyPI wheel is **self-contained** — it bundles its own `libddsc.so` and
does **not** require the system CycloneDDS build from step 3a. Steps 3a/3b are only
needed if you also want to build the C++ examples from `d1_sdk/`.

```bash
# Python-only — this is sufficient for arm_control.py
pip install cyclonedds==0.10.5   # wheel bundles libddsc 0.10.5; no CYCLONEDDS_HOME needed
# Verify:
python3 -c "from cyclonedds.domain import DomainParticipant; print('cyclonedds OK')"
```

### 3d. Build d1_sdk C++ examples (optional — hardware smoke test)

The `d1_sdk.zip` from the D1 developer page contains official examples.

```bash
# From the extracted d1_sdk/ directory
mkdir build && cd build
cmake ..
make -j$(nproc)
# Binaries: joint_angle_control  multiple_joint_angle_control
#           joint_enable_control  arm_zero_control  get_arm_joint_angle
```

With the arm powered and Ethernet configured to `192.168.123.162/24`:
```bash
./arm_zero_control       # funcode 7: sends arm to zero/home position
./get_arm_joint_angle    # subscribes to current_servo_angle, prints live angles
```

### 3e. DDS interface overview

The arm exposes two DDS topics (both carry `ArmString_` — a single JSON string field):

| Direction | Topic | Content |
|-----------|-------|---------|
| PC → Arm | `rt/arm_Command` | Command JSON |
| Arm → PC | `rt/arm_Feedback` | Feedback JSON (10 Hz) |

**Command JSON structure:**
```json
{"seq": <int>, "address": 1, "funcode": <int>, "data": {...}}
```

`seq` is an auto-incrementing counter at the calling end. `address` is always `1` for commands.

**funcode reference (commands):**

| funcode | Function | `data` fields |
|---------|----------|---------------|
| 1 | Single joint angle | `{"id": 0–6, "angle": deg, "delay_ms": 0}` |
| 2 | All joints angle | `{"mode": 0\|1, "angle0": deg, …, "angle6": deg}` — `mode 0`=10 Hz smoothing, `mode 1`=trajectory |
| 4 | Single joint enable/disable | `{"id": 0–6, "mode": 0=release, 1=enable}` |
| 5 | All joints enable/disable | `{"mode": 0=release, 1=enable}` |
| 6 | Motor power switch | `{"power": 0=off, 1=on}` |
| 7 | Return to zero | *(no data field)* |

**Feedback JSON (active push from arm, `address: 2`, `seq: 10`):**

| funcode | Content |
|---------|---------|
| 1 | Joint angles: `{"angle0"…"angle6": deg}` — 10 Hz |
| 3 | Status: `{"enable_status": 0\|1, "power_status": 0\|1, "error_status": 0\|1}` |
| 4 | Motor health: `{"motor0_status"…"motor6_status": 0\|1}` |

**Command acknowledgement (from arm, `address: 3`):**

| funcode | Content |
|---------|---------|
| 1 | Receive ACK: `{"recv_status": 0\|1}` |
| 2 | Execution ACK: `{"exec_status": 0\|1}` |

---

## Phase 4 — Network Configuration (`net_config.py`)

This is the **most critical safety requirement**: all changes must be reversible.

Claude must write `src/net_config.py` with these behaviours:

### Logic

1. **Detect** the Ethernet interface connected to the arm (user-specified or auto-detected via `ip link`)
2. **Read and save** the current IP config (IP, netmask, gateway, DHCP state) to `config/net_backup.json`
3. **Apply** the static IP `192.168.123.162/24` using `ip addr` (no-reboot, temporary)
4. **Verify** connectivity with `ping -c 2 -W 1 192.168.123.110`
5. On `atexit` / `SIGINT` / `SIGTERM`: **restore** original config from backup

### Key implementation requirements for Claude

```python
# src/net_config.py  — skeleton for Claude to implement

import subprocess, json, atexit, signal, sys, pathlib, shutil
from dataclasses import dataclass, asdict

BACKUP_FILE = pathlib.Path("config/net_backup.json")
ARM_IP      = "192.168.123.110"
PC_IP       = "192.168.123.162"
NETMASK     = "24"

@dataclass
class NetState:
    iface: str
    was_dhcp: bool
    original_ip: str | None
    original_prefix: str | None
    original_gateway: str | None

def detect_ethernet_iface() -> str:
    """Return the first non-loopback Ethernet interface name."""
    ...

def save_current_state(iface: str) -> NetState:
    """Read current IP/DHCP state; write to BACKUP_FILE; return NetState."""
    ...

def apply_arm_ip(state: NetState) -> None:
    """Flush existing IP, apply 192.168.123.162/24 temporarily."""
    # subprocess.run(["sudo", "ip", "addr", "flush", "dev", state.iface], check=True)
    # subprocess.run(["sudo", "ip", "addr", "add", f"{PC_IP}/{NETMASK}", "dev", state.iface], check=True)
    # subprocess.run(["sudo", "ip", "link", "set", state.iface, "up"], check=True)
    ...

def restore_state(state: NetState) -> None:
    """Flush arm IP and restore original state (DHCP or static)."""
    ...

def verify_arm_reachable() -> bool:
    """Ping arm; return True if reachable."""
    result = subprocess.run(
        ["ping", "-c", "2", "-W", "1", ARM_IP],
        capture_output=True
    )
    return result.returncode == 0

def setup(iface: str | None = None) -> NetState:
    """Full setup: detect, save, apply, verify. Register cleanup."""
    iface = iface or detect_ethernet_iface()
    state = save_current_state(iface)
    apply_arm_ip(state)
    atexit.register(restore_state, state)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, f: (restore_state(state), sys.exit(0)))
    if not verify_arm_reachable():
        restore_state(state)
        raise RuntimeError(f"Arm at {ARM_IP} not reachable after IP setup.")
    return state
```

### Restore modes

| Scenario | Action |
|----------|--------|
| Was DHCP | Run `sudo dhclient <iface>` or `sudo ip addr flush dev <iface>` + restart NetworkManager |
| Was static | `sudo ip addr flush dev <iface>` then `sudo ip addr add <original>/<prefix> dev <iface>` |
| Was disconnected (no prior IP) | `sudo ip addr flush dev <iface>` |

---

## Phase 5 — USB Serial Monitor (`usb_monitor.py`)

Claude must write `src/usb_monitor.py`:

```python
# Objectives:
# 1. Auto-detect /dev/ttyACM0 or /dev/ttyUSB0
# 2. Open at 115200 8N1 (D1 default debug baud)
# 3. Read lines in a daemon thread, write to logs/usb_monitor.log
# 4. Expose a get_last_lines(n) function for main.py status display
# 5. Graceful close on shutdown

import serial, serial.tools.list_ports, threading, pathlib, datetime

LOG_FILE  = pathlib.Path("logs/usb_monitor.log")
BAUD_RATE = 115200
USB_VIDS  = [0x0483, 0x10C4, 0x1A86]  # STM32, CP210x, CH340 common VIDs

def find_d1_usb_port() -> str | None:
    """Scan serial ports and return the most likely D1 debug port path."""
    ...

class USBMonitor:
    def __init__(self, port: str | None = None, baud: int = BAUD_RATE): ...
    def start(self) -> None: ...           # start daemon reader thread
    def stop(self) -> None: ...            # close serial + join thread
    def get_last_lines(self, n=20) -> list[str]: ...
```

---

## Phase 6 — Arm Control Wrapper (`arm_control.py`)

Claude must write `src/arm_control.py` using the `cyclonedds` Python library to publish JSON commands to `rt/arm_Command` and subscribe to `rt/arm_Feedback`.

**Joint limits (from official URDF + spec doc):**

| Joint | Axis | Range | Torque |
|-------|------|-------|--------|
| J0 | Base rotation | ±135° | 3.3 Nm |
| J1 | Shoulder | ±90° | 3.3 Nm |
| J2 | Elbow upper | ±90° | 1.7 Nm |
| J3 | Elbow lower | ±135° | 1.7 Nm |
| J4 | Wrist roll | ±90° | 1.7 Nm |
| J5 | Wrist pitch | ±135° | 1.7 Nm |
| J6 | Gripper | 0–65 mm stroke | 1.7 Nm |

```python
# Objectives:
# 1. Define ArmString_ IDL type for CycloneDDS
# 2. Publish JSON commands to rt/arm_Command
# 3. Subscribe to rt/arm_Feedback for joint angles and status
# 4. Provide: connect, home, enable_all, move_joint, move_joints, get_joints,
#             set_gripper, power, disconnect
# 5. Pre-check joint limits before every move; log all commands

from dataclasses import dataclass, field
from cyclonedds.domain import DomainParticipant
from cyclonedds.topic import Topic
from cyclonedds.pub import DataWriter
from cyclonedds.sub import DataReader
from cyclonedds.idl import IdlStruct
import json, threading, time, logging

@dataclass
class ArmString(IdlStruct, typename="unitree_arm.msg.dds_.ArmString_"):
    data_: str = ""

JOINT_LIMITS_DEG = [
    (-135, 135),   # J0 base rotation
    ( -90,  90),   # J1 shoulder
    ( -90,  90),   # J2 elbow upper
    (-135, 135),   # J3 elbow lower
    ( -90,  90),   # J4 wrist roll
    (-135, 135),   # J5 wrist pitch
    (   0,  65),   # J6 gripper (mm, not degrees — use move_joint(6, mm))
]

class D1Arm:
    def __init__(self): ...

    def connect(self) -> None: ...
    # participant = DomainParticipant(0)
    # cmd_topic  = Topic(participant, "rt/arm_Command", ArmString)
    # fb_topic   = Topic(participant, "rt/arm_Feedback", ArmString)
    # self._writer = DataWriter(participant, cmd_topic)
    # self._reader = DataReader(participant, fb_topic)
    # Start background subscriber thread for feedback

    def _send(self, funcode: int, data: dict | None = None) -> None: ...
    # Builds {"seq": next_seq(), "address": 1, "funcode": funcode, "data": data}
    # Writes ArmString(data_=json.dumps(msg)) to self._writer
    # Increments seq counter

    def home(self) -> None: ...
    # self._send(7)  — funcode 7: return to zero, no data field

    def enable_all(self, enable: bool = True) -> None: ...
    # self._send(5, {"mode": 1 if enable else 0})  — funcode 5

    def power(self, on: bool) -> None: ...
    # self._send(6, {"power": 1 if on else 0})  — funcode 6

    def move_joint(self, joint_id: int, angle: float) -> None: ...
    # Pre-check JOINT_LIMITS_DEG[joint_id]; raise ValueError if out of range
    # self._send(1, {"id": joint_id, "angle": angle, "delay_ms": 0})

    def move_joints(self, angles: list[float], mode: int = 1) -> None: ...
    # angles: 7-element list [J0..J6] in degrees (J6 in mm)
    # mode 0 = 10 Hz smooth streaming; mode 1 = trajectory (recommended for single waypoints)
    # Pre-check all limits; self._send(2, {"mode": mode, "angle0": ..., "angle6": ...})

    def get_joints(self) -> list[float]: ...
    # Read latest feedback from subscriber thread
    # Returns list from {"address":2,"funcode":1,"data":{"angle0"...}} feedback message

    def set_gripper(self, pos_mm: float) -> None: ...
    # Convenience: self.move_joint(6, pos_mm)  — 0mm=closed, 65mm=fully open

    def disconnect(self) -> None: ...
    # self.home(); stop subscriber thread; release DDS resources
```

---

## Phase 7 — Entry Point (`main.py`)

Claude must write `src/main.py` that:

1. Reads `config/settings.toml`
2. Calls `net_config.setup(iface)` — captures current state, applies arm IP
3. Starts `USBMonitor` (non-blocking)
4. Connects `D1Arm`
5. Runs a minimal demo sequence:
   - Home the arm
   - Read and print joint angles
   - Move J1 to +30°, wait 2 s
   - Return to home
6. Prints USB monitor last 10 lines
7. Calls `arm.disconnect()` → `net_config` cleanup fires automatically via `atexit`

### Demo sequence pseudocode

```python
arm.enable_all(True)
arm.home()
print("Initial joints (deg):", arm.get_joints())
# Move J0 (base rotation) to +30°; all other joints stay at zero; J6 gripper open (0mm)
arm.move_joints([30, 0, 0, 0, 0, 0, 0], mode=1)
time.sleep(2.0)
arm.home()
print("USB debug tail:", usb.get_last_lines(10))
# Note: set_gripper(0) = closed, set_gripper(65) = fully open (mm stroke)
# Control cycle is 10 Hz — wait at least 100ms between rapid commands
```

---

## Phase 8 — Configuration (`config/settings.toml`)

```toml
[network]
iface         = ""                  # leave blank for auto-detect
arm_ip        = "192.168.123.110"
pc_ip         = "192.168.123.162"
netmask       = "24"
ping_timeout  = 2                   # seconds

[arm]
dds_iface_idx = 0                   # CycloneDDS interface index (0 = first non-loopback)
control_hz    = 10                  # arm control cycle; don't send commands faster than this
gripper_open_mm  = 65.0             # fully open (mm)
gripper_closed_mm = 0.0             # fully closed (mm)

[usb]
port          = ""                  # blank = auto-detect
baud          = 115200
log_file      = "logs/usb_monitor.log"

[demo]
j0_test_angle = 30.0                # degrees — J0 is base rotation (0-indexed)
pause_seconds = 2.0
```

---

## Phase 9 — Prompt for Claude

Use this prompt when invoking Claude to generate the project:

```
Create a Python project in the directory `unitree_d1_control/` according to plan.md.

Requirements:
- Python 3.11+, venv in `unitree_d1_control/venv/`
- Files: setup_env.sh, requirements.txt, src/net_config.py, src/usb_monitor.py,
  src/arm_control.py, src/main.py, config/settings.toml
- net_config.py MUST save the current Ethernet IP/DHCP state to config/net_backup.json
  before making any changes, and restore it on exit (atexit + SIGINT/SIGTERM handlers).
- usb_monitor.py must auto-detect /dev/ttyACM0 or /dev/ttyUSB0 and log to logs/usb_monitor.log.
- arm_control.py uses the cyclonedds Python library (NOT z1_sdk) to publish JSON commands
  to DDS topic rt/arm_Command and subscribe to rt/arm_Feedback. The ArmString_ IDL type
  must be defined as: @dataclass class ArmString(IdlStruct, typename="unitree_arm.msg.dds_.ArmString_"): data_: str = ""
  Commands use funcode 1 (single joint), 2 (all joints), 5 (enable all), 6 (power), 7 (home).
  Angles are in DEGREES. Joint index is 0-based (J0=base, J6=gripper in mm).
- main.py: setup network → start USB monitor → connect arm → enable motors →
  demo sequence → home → disconnect → restore network.
- Demo: enable_all, home, move_joints([30,0,0,0,0,0,0], mode=1), sleep 2s, home.
- All functions must have docstrings and type hints.
- Use subprocess with check=True; never use os.system().
- Raise ImportError with a clear install message if cyclonedds is not importable.
- Do NOT hardcode sudo password; commands requiring sudo will prompt interactively.
- Respect the 10 Hz control cycle — add 100 ms sleep between rapid sequential commands.
```

---

## Safety Notes

> ⚠️ **Ensure the arm is in the zero/home position before powering on.**  
> ⚠️ **Send `enable_all(True)` before sending any joint commands — motors start in released state.**  
> ⚠️ **The net_backup.json file is your rollback — do not delete it while the arm is connected.**  
> ⚠️ **If the script is killed with SIGKILL (kill -9), the IP will NOT be restored automatically. Run `src/net_config.py restore` manually.**  
> ⚠️ **No background daemon is needed — DDS communicates directly with the arm over Ethernet. Do not run multiple DDS publishers to `rt/arm_Command` simultaneously.**  
> ⚠️ **Rated load is 500 g. Do not exceed. Arm length is 550 mm (670 mm with jaws).**

### Manual IP restore (emergency)

```bash
# If DHCP was original:
sudo ip addr flush dev <iface>
sudo dhclient <iface>
# OR restart NetworkManager:
sudo systemctl restart NetworkManager

# If static IP was original (check config/net_backup.json):
sudo ip addr flush dev <iface>
sudo ip addr add <original_ip>/<prefix> dev <iface>
sudo ip link set <iface> up
```

---

## References

- [D1 Mechanical Arm Services Interface](https://support.unitree.com/home/en/developer/D1Arm_services) — **official D1 API docs** (funcode table, JSON format, spec)
- [d1_sdk.zip](https://support.unitree.com/home/en/developer/D1Arm_services) — official C++ SDK (downloaded from above page)
- [d1_description URDF](https://support.unitree.com/home/en/developer/D1Arm_services) — joint limits, kinematics (URDF zip from above page)
- [unitree_sdk2 GitHub](https://github.com/unitreerobotics/unitree_sdk2) — C++ DDS communication library
- [CycloneDDS v0.10.2](https://github.com/eclipse-cyclonedds/cyclonedds/tree/releases/0.10.x) — required DDS middleware
- [chen37058/Grasp-with-the-Unitree-D1](https://github.com/chen37058/Grasp-with-the-Unitree-D1) — real-world grasping example using d1_sdk
- [Unitree D1 product page (Botland)](https://botland.store/robot-arms/28170-teleoperation-robot-arm-unitree-d1.html) — hardware overview
