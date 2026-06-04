# Claude Memory — unitree-d1-control

Context file for Claude Code sessions on this project.

## What this project does

Python control stack for the Unitree D1-550 servo arm (6-DOF + gripper) over Ethernet on Pop!_OS / Ubuntu Linux.

## SDK — confirmed from official docs (April 2026)

- **SDK**: `d1_sdk` (official) using `unitree_sdk2` + CycloneDDS v0.10.2
- **NOT z1_sdk** — that is for the Z1 arm (different product)
- **No background daemon** — DDS communicates directly with arm over Ethernet
- **Python**: no official Python binding; use `cyclonedds==0.10.5` — PyPI wheel is **self-contained** (bundles libddsc 0.10.5), no system CycloneDDS build needed

## DDS Protocol

| | Topic | Type |
|---|---|---|
| Command | `rt/arm_Command` | `ArmString_` (JSON string) |
| Feedback | `rt/arm_Feedback` | `ArmString_` (JSON string, 10 Hz) |

Command JSON: `{"seq": <int>, "address": 1, "funcode": N, "data": {...}}`

### funcode reference

| code | function | data |
|------|----------|------|
| 1 | Single joint angle | `{"id": 0–6, "angle": deg, "delay_ms": 0}` |
| 2 | All joints | `{"mode": 0\|1, "angle0"…"angle6": deg}` |
| 4 | Single joint enable/disable | `{"id": 0–6, "mode": 0=release, 1=enable}` |
| 5 | All joints enable/disable | `{"mode": 0=release, 1=enable}` |
| 6 | Motor power | `{"power": 0=off, 1=on}` |
| 7 | Return to zero | (no data) |

### Feedback (arm → PC, address=2, seq=10)

| funcode | data |
|---------|------|
| 1 | `{"angle0"…"angle6": deg}` — 10 Hz |
| 3 | `{"enable_status", "power_status", "error_status": 0\|1}` |
| 4 | `{"motor0_status"…"motor6_status": 0\|1}` |

## Hardware

- Model: D1-550, 550mm arm (670mm with jaws), 500g rated load
- Power: 24V 10A (15–48V range, 240W max)
- Network: arm default IP `192.168.123.110`, PC must be `192.168.123.162/24`
- USB-C: serial debug port, 115200 baud

## Joint limits (from URDF + official spec)

| Joint | Axis | Range |
|-------|------|-------|
| J0 | base rotation | ±135° |
| J1 | shoulder | ±90° |
| J2 | elbow upper | ±90° |
| J3 | elbow lower | ±135° |
| J4 | wrist roll | ±90° |
| J5 | wrist pitch | ±135° |
| J6 | gripper | 0–65 servo units (0=closed) |

- Joints are **0-indexed** in all commands
- Angles are **degrees** (not radians)
- Control cycle: **10 Hz** — wait ≥100ms between commands

## ArmString_ IDL type (Python)

```python
from cyclonedds.idl import IdlStruct
from dataclasses import dataclass

@dataclass
class ArmString(IdlStruct, typename="unitree_arm::msg::dds_::ArmString_"):
    data_: str = ""
```

If typename causes type-mismatch errors, try `"unitree_arm.msg.dds_.ArmString_"` (dot notation).

## Project structure

```
unitree-d1-control/
├── plan.md              # full implementation plan
├── memory.md            # this file
├── setup_env.sh         # venv + pip install
├── requirements.txt
├── src/
│   ├── net_config.py    # save/apply/restore Ethernet IP
│   ├── usb_monitor.py   # USB-C serial debug reader
│   ├── arm_control.py   # DDS arm wrapper (D1Arm class)
│   └── main.py          # entry point
├── config/
│   └── settings.toml
└── logs/
```

## Run

```bash
source venv/bin/activate
python3 src/main.py
```

## Emergency IP restore (after SIGKILL)

```bash
python3 -c "from src.net_config import restore_from_backup; restore_from_backup()"
# OR manually:
sudo ip addr flush dev <iface>
sudo dhclient <iface>          # if was DHCP
sudo systemctl restart NetworkManager
```

## Known uncertainties

1. **ArmString_ typename** — `::` vs `.` notation; test if DDS communication fails
2. **Gripper J6** — described as servo angle 0–65, but exact mm conversion unverified on hardware
3. **cyclonedds Python API** — `reader.take(n)` API exact behaviour may vary; test feedback loop
