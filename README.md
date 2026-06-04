# unitree-d1-control

Python control stack for the **Unitree D1-550 servo arm** (6-DOF + gripper) over Ethernet on Pop!_OS / Ubuntu Linux.

## Hardware

| Property | Value |
|---|---|
| Model | D1-550 (550 mm reach, 670 mm with jaws) |
| Rated load | 500 g |
| Power | 24 V 10 A (15–48 V range, 240 W max) |
| Arm IP | `192.168.123.100` (default; confirmed by tcpdump — **not** 192.168.123.110 as some docs state) |
| PC IP | `192.168.123.162/24` (required by arm firmware) |
| USB-C | Serial debug port, 115200 baud |

## Protocol

The arm uses `d1_sdk` (`unitree_sdk2` + CycloneDDS v0.10.x) — **not** `z1_sdk`.

| Direction | DDS Topic | Type |
|---|---|---|
| PC → arm | `rt/arm_Command` | `ArmString_` (JSON string) |
| arm → PC | `rt/arm_Feedback` | `ArmString_` (JSON string, 10 Hz) |
| arm → PC | `current_servo_angle` | `PubServoInfo_` (7 floats, 10 Hz) |

Command JSON: `{"seq": int, "address": 1, "funcode": N, "data": {...}}`

### funcode reference

| code | function | data |
|---|---|---|
| 1 | Single joint angle | `{"id": 0–6, "angle": deg, "delay_ms": 0}` |
| 2 | All joints | `{"mode": 0\|1, "angle0"…"angle6": deg}` |
| 4 | Single joint enable/disable | `{"id": 0–6, "mode": 0=release, 1=enable}` |
| 5 | All joints enable/disable | `{"mode": 0=release, 1=enable}` |
| 6 | Motor power | `{"power": 0=off, 1=on}` |
| 7 | Return to zero | (no data) |

### Joint limits

| Joint | Axis | Range |
|---|---|---|
| J0 | base rotation | ±135° |
| J1 | shoulder | ±90° |
| J2 | elbow upper | ±90° |
| J3 | elbow lower | ±135° |
| J4 | wrist roll | ±90° |
| J5 | wrist pitch | ±135° |
| J6 | gripper | 0–65 servo units (0 = closed) |

Joints are 0-indexed. Angles are **degrees**. Control cycle: **10 Hz** (100 ms minimum between commands).

## Quick start

```bash
# 1. Install (one-time) — sets up venv, sudoers, and firewall rules
./install.sh

# 2. Activate venv
source venv/bin/activate

# 3. Run the CLI demo
python3 src/main.py

# 4. Or launch the control GUI
python3 src/gui.py
```

`main.py` will:
1. Apply `192.168.123.162/24` to the Ethernet interface (requires passwordless `sudo ip`)
2. Ping the arm to confirm it is reachable
3. Start USB-C serial monitor (silent if no cable)
4. Connect DDS, enable all motors, home the arm
5. Read joint angles from feedback
6. Move J0 to 30° and return home
7. Restore the original Ethernet config on exit

## Control GUI

`src/gui.py` is a [Dear PyGui](https://github.com/hoffstadt/DearPyGui) control panel for interactive arm use.

**Features:**
- Live joint feedback table (10 Hz, from `current_servo_angle` DDS topic)
- 7 joint sliders (J0–J5 in degrees, J6 in servo units 0–65)
- Manual mode: drag sliders to move the arm in real time (10 Hz streaming)
- **Drag-teach recording**: release motors, move the arm by hand, record poses as JSON
- **Playback**: replay any saved recording at 10 Hz
- Homing, connect/disconnect, emergency power-off buttons
- Log panel with timestamped status messages

**Usage:**

```bash
python3 src/gui.py
```

1. Click **Connect** — configures Ethernet, starts DDS, enables motors
2. Use **Manual** mode to jog joints via sliders
3. Click **Record** — motors release; physically move the arm to teach poses
4. Click **Stop Rec** — recording saved to `recordings/rec_YYYYMMDD_HHMMSS.json`
5. Select a recording in the list and click **Play** to replay

**Recordings format** (`recordings/*.json`):

```json
{
  "name": "rec_20260604_154230.json",
  "recorded_at": "2026-06-04T15:42:30",
  "fps": 10,
  "frame_count": 100,
  "duration_s": 10.0,
  "frames": [[j0, j1, j2, j3, j4, j5, j6], ...]
}
```

### Windows

`install.bat` / `install.ps1` set up the Python environment on Windows. However, **direct arm control requires Linux** — `net_config.py` uses Linux `ip` commands. For full control from Windows, use WSL2 with Ubuntu and run `install.sh` inside the WSL2 environment.

## Setup

### Sudoers (required)

The `ip` commands to configure the network interface need passwordless sudo. Add this with `sudo visudo -f /etc/sudoers.d/d1-arm`:

```
<username> ALL=(ALL) NOPASSWD: /sbin/ip, /sbin/dhclient
```

### Firewall (required)

**Firewalld blocks the arm's DDS multicast by default.** Even moving the interface to the `trusted` zone is insufficient because the interface is manually configured (not managed by NetworkManager). The reliable fix is to add the arm's subnet as a trusted source:

```bash
sudo firewall-cmd --zone=trusted --add-source=192.168.123.0/24 --permanent
sudo firewall-cmd --zone=public --add-port=7400-7401/udp --permanent
sudo firewall-cmd --reload
```

Without this, `tcpdump` will show arm UDP traffic arriving on `enp63s0` at 10 Hz but all Python sockets will receive zero packets. The arm will appear to accept commands (or may auto-home on enable) but DDS discovery will find 0 publishers and joint feedback will time out.

## Known issues and gotchas

### 1. Arm IP is 192.168.123.100, not 192.168.123.110

Official docs and some third-party sources list the arm's default IP as `192.168.123.110`. On hardware, the arm answers at **`192.168.123.100`**. Verify with `sudo tcpdump -i <iface> -n` — look for UDP traffic from the arm at 10 Hz.

### 2. Firewalld blocks DDS multicast silently

Described above. Symptoms: `tcpdump` sees packets, Python sockets receive nothing, `DCPSPublication` builtin reader discovers 0 publishers. Fix: add `192.168.123.0/24` to the trusted source zone.

### 3. Topic names: `rt/` prefix is added by `unitree_sdk2` C++ wrapper

The `d1_sdk` C++ examples define topics like `#define TOPIC1 "arm_Feedback"` (no `rt/`), but `ChannelSubscriber`/`ChannelPublisher` add the `rt/` prefix automatically. The actual DDS topic on the wire is `rt/arm_Feedback`. Do **not** omit the prefix in Python.

### 4. Gripper units unverified

J6 is commanded in servo angle units (0–65). The exact mapping to jaw-opening millimetres has not been measured on hardware. Use 0 for closed, 65 for fully open until calibrated.

### 5. Emergency IP restore after SIGKILL

If the process is killed hard (SIGKILL), the Ethernet interface may be left with the arm's static IP:

```bash
python3 -c "from src.net_config import restore_from_backup; restore_from_backup()"
# or manually:
sudo ip addr flush dev enp63s0
sudo systemctl restart NetworkManager
```

## Project structure

```
unitree-d1-control/
├── README.md
├── install.sh           # Linux installer (venv, sudoers, firewall)
├── install.ps1          # Windows PowerShell installer (Python env only)
├── install.bat          # Windows batch wrapper for install.ps1
├── setup_env.sh         # forwards to install.sh
├── requirements.txt
├── config/
│   └── settings.toml
├── src/
│   ├── net_config.py    # save/apply/restore Ethernet IP
│   ├── usb_monitor.py   # USB-C serial debug reader
│   ├── arm_control.py   # DDS arm wrapper (D1Arm class)
│   ├── main.py          # CLI demo entry point
│   └── gui.py           # Dear PyGui control panel (drag-teach + playback)
├── recordings/          # saved drag-teach recordings (JSON)
└── logs/
```
