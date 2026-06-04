"""D1 arm control via DDS JSON commands (d1_sdk / unitree_sdk2 / CycloneDDS).

Communication:
  Publish  → rt/arm_Command        (ArmString_   — JSON string)
  Subscribe← rt/arm_Feedback       (ArmString_   — JSON string, 10 Hz active push)
  Subscribe← current_servo_angle   (PubServoInfo_— 7 floats, direct servo angles)

Topic names confirmed live from arm's DCPSPublication builtin reader.
unitree_sdk2 ChannelSubscriber/Publisher adds 'rt/' prefix automatically in C++.
"""

try:
    from cyclonedds.domain import DomainParticipant
    from cyclonedds.idl import IdlStruct
    from cyclonedds.pub import DataWriter
    from cyclonedds.sub import DataReader
    from cyclonedds.topic import Topic
except ImportError as _exc:
    raise ImportError(
        "cyclonedds is not installed. Install with: pip install cyclonedds==0.10.5\n"
        f"Original error: {_exc}"
    ) from _exc

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

# Minimum time between successive commands — arm control cycle is 10 Hz.
CTRL_INTERVAL = 0.1


@dataclass
class ArmString(IdlStruct, typename="unitree_arm::msg::dds_::ArmString_"):
    data_: str = ""


@dataclass
class PubServoInfo(IdlStruct, typename="unitree_arm::msg::dds_::PubServoInfo_"):
    servo0_data_: float = 0.0
    servo1_data_: float = 0.0
    servo2_data_: float = 0.0
    servo3_data_: float = 0.0
    servo4_data_: float = 0.0
    servo5_data_: float = 0.0
    servo6_data_: float = 0.0


# Joint limits sourced from official d1_description URDF + D1 Mechanical Arm spec doc.
# Index maps directly to joint ID in funcode 1/2 commands.
JOINT_LIMITS_DEG: list[tuple[float, float]] = [
    (-135.0, 135.0),  # J0  base rotation      ±135°  3.3 Nm
    ( -90.0,  90.0),  # J1  shoulder            ±90°   3.3 Nm
    ( -90.0,  90.0),  # J2  elbow upper         ±90°   1.7 Nm
    (-135.0, 135.0),  # J3  elbow lower        ±135°   1.7 Nm
    ( -90.0,  90.0),  # J4  wrist roll          ±90°   1.7 Nm
    (-135.0, 135.0),  # J5  wrist pitch        ±135°   1.7 Nm
    (   0.0,  65.0),  # J6  gripper       0–65 servo units (0≈closed, 65≈open)
]


class D1Arm:
    """Controls the Unitree D1 arm over DDS (d1_sdk protocol)."""

    def __init__(self, iface: Optional[str] = None):
        """
        Args:
            iface: Ethernet interface name connected to the arm (e.g. 'eth0').
                   If None, CycloneDDS auto-detects. You can also set the
                   CYCLONEDDS_URI environment variable before calling connect().
        """
        self._iface = iface
        self._participant: Optional[DomainParticipant] = None
        self._writer: Optional[DataWriter] = None
        self._reader: Optional[DataReader] = None
        self._servo_reader: Optional[DataReader] = None
        self._fb_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._seq = 0
        self._joint_cache: Optional[list[float]] = None
        self._cache_lock = threading.Lock()
        self._feedback_received = threading.Event()

    def connect(self) -> None:
        """Initialise DDS participant, create writer/reader, start feedback thread.

        Call this after net_config.setup() has configured the Ethernet interface.
        Set CYCLONEDDS_URI env var before this call if you need to bind to a
        specific network interface explicitly.
        """
        if self._iface and "CYCLONEDDS_URI" not in os.environ:
            os.environ["CYCLONEDDS_URI"] = (
                "<CycloneDDS><Domain><General><Interfaces>"
                f'<NetworkInterface name="{self._iface}" />'
                "</Interfaces></General></Domain></CycloneDDS>"
            )

        self._participant = DomainParticipant(0)
        cmd_topic = Topic(self._participant, "rt/arm_Command", ArmString)
        fb_topic = Topic(self._participant, "rt/arm_Feedback", ArmString)
        servo_topic = Topic(self._participant, "current_servo_angle", PubServoInfo)
        self._writer = DataWriter(self._participant, cmd_topic)
        self._reader = DataReader(self._participant, fb_topic)
        self._servo_reader = DataReader(self._participant, servo_topic)

        self._fb_thread = threading.Thread(
            target=self._feedback_loop, daemon=True, name="d1-feedback"
        )
        self._fb_thread.start()
        logging.info("D1Arm connected via DDS.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        """Return the next command sequence number (wraps at 9999)."""
        self._seq = (self._seq % 9999) + 1
        return self._seq

    def _send(self, funcode: int, data: Optional[dict] = None) -> None:
        """Build and publish a JSON command to rt/arm_Command.

        Args:
            funcode: Command function code (1–7).
            data:    Optional data payload dict.
        """
        if self._writer is None:
            raise RuntimeError("Call D1Arm.connect() before sending commands.")
        msg: dict = {"seq": self._next_seq(), "address": 1, "funcode": funcode}
        if data is not None:
            msg["data"] = data
        payload = json.dumps(msg)
        logging.debug("D1Arm TX: %s", payload)
        self._writer.write(ArmString(data_=payload))
        time.sleep(CTRL_INTERVAL)  # respect 10 Hz control cycle

    def _feedback_loop(self) -> None:
        """Background thread: poll current_servo_angle and rt/arm_Feedback, cache joint angles."""
        while not self._stop_event.is_set():
            try:
                # Primary: direct float servo angles (no JSON parsing)
                servo_samples = self._servo_reader.take(10) if self._servo_reader else []
                for s in (servo_samples or []):
                    try:
                        angles = [s.servo0_data_, s.servo1_data_, s.servo2_data_,
                                  s.servo3_data_, s.servo4_data_, s.servo5_data_, s.servo6_data_]
                        with self._cache_lock:
                            self._joint_cache = angles
                        self._feedback_received.set()
                    except (AttributeError, TypeError):
                        pass

                # Fallback: JSON feedback on rt/arm_Feedback
                json_samples = self._reader.take(10) if self._reader else []
                for sample in (json_samples or []):
                    try:
                        msg = json.loads(sample.data_)
                        if msg.get("address") == 2 and msg.get("funcode") == 1:
                            d = msg.get("data", {})
                            angles = [float(d.get(f"angle{i}", 0.0)) for i in range(7)]
                            with self._cache_lock:
                                self._joint_cache = angles
                            self._feedback_received.set()
                    except (json.JSONDecodeError, AttributeError, TypeError):
                        pass
            except Exception as exc:
                logging.debug("D1Arm feedback error: %s", exc)
            time.sleep(0.05)  # poll at ~20 Hz; arm publishes at 10 Hz

    # ------------------------------------------------------------------
    # Public control API
    # ------------------------------------------------------------------

    def home(self) -> None:
        """Return arm to zero/home position (funcode 7)."""
        logging.info("D1Arm: homing.")
        self._send(7)

    def enable_all(self, enable: bool = True) -> None:
        """Enable or release all joint motors (funcode 5).

        Must be called with enable=True before any joint movement commands.
        mode=1 locks joints (active control); mode=0 releases them (backdrivable,
        useful for drag-teaching combined with get_joints() feedback).

        Args:
            enable: True to enable (lock), False to release.
        """
        logging.info("D1Arm: %s all joints.", "enabling" if enable else "releasing")
        self._send(5, {"mode": 1 if enable else 0})

    def power(self, on: bool) -> None:
        """Switch motor power on or off (funcode 6).

        Can be used as an emergency stop. Power off disables all servo torque.

        Args:
            on: True to power on, False to power off.
        """
        logging.info("D1Arm: power %s.", "ON" if on else "OFF")
        self._send(6, {"power": 1 if on else 0})

    def move_joint(self, joint_id: int, angle: float) -> None:
        """Move a single joint to a target angle (funcode 1).

        Args:
            joint_id: 0–6 (J0=base rotation, J1=shoulder, …, J5=wrist pitch, J6=gripper).
            angle:    Target angle in degrees. For J6 (gripper): 0≈closed, ~65≈open.

        Raises:
            ValueError: if joint_id or angle is out of range.
        """
        if not 0 <= joint_id <= 6:
            raise ValueError(f"joint_id must be 0–6, got {joint_id}.")
        lo, hi = JOINT_LIMITS_DEG[joint_id]
        if not lo <= angle <= hi:
            raise ValueError(
                f"J{joint_id} angle {angle}° is outside limits [{lo}°, {hi}°]."
            )
        logging.info("D1Arm: J%d → %.1f°", joint_id, angle)
        self._send(1, {"id": joint_id, "angle": angle, "delay_ms": 0})

    def move_joints(self, angles: list[float], mode: int = 1) -> None:
        """Move all joints simultaneously (funcode 2).

        Args:
            angles: 7-element list [J0, J1, J2, J3, J4, J5, J6] in degrees.
                    J6 is gripper (0≈closed, ~65≈open).
            mode:   1 = single trajectory waypoint (recommended for point-to-point).
                    0 = 10 Hz streaming mode (for continuous control loops).

        Raises:
            ValueError: if angles list length != 7 or any angle is out of range.
        """
        if len(angles) != 7:
            raise ValueError(f"Expected 7 joint angles, got {len(angles)}.")
        for i, angle in enumerate(angles):
            lo, hi = JOINT_LIMITS_DEG[i]
            if not lo <= angle <= hi:
                raise ValueError(
                    f"J{i} angle {angle}° is outside limits [{lo}°, {hi}°]."
                )
        data: dict = {"mode": mode}
        data.update({f"angle{i}": a for i, a in enumerate(angles)})
        logging.info("D1Arm: move all joints → %s (mode=%d)", angles, mode)
        self._send(2, data)

    def get_joints(self, timeout: float = 3.0) -> list[float]:
        """Return the latest joint angles from arm feedback.

        Blocks until the first feedback message arrives, then returns the cache.

        Args:
            timeout: Seconds to wait for feedback before raising TimeoutError.

        Returns:
            7-element list [J0..J6] in degrees.

        Raises:
            TimeoutError: if no feedback received within timeout.
        """
        if not self._feedback_received.wait(timeout):
            raise TimeoutError(
                "No joint angle feedback received within timeout. "
                "Check arm power and Ethernet connection."
            )
        with self._cache_lock:
            return list(self._joint_cache)  # type: ignore[arg-type]

    def set_gripper(self, pos: float) -> None:
        """Move gripper to target position via single-joint command (funcode 1, J6).

        Args:
            pos: Servo position value. 0≈closed, ~65≈fully open.
                 Exact stroke-to-angle mapping should be verified on hardware.
        """
        self.move_joint(6, pos)

    def disconnect(self) -> None:
        """Home the arm, stop feedback thread, and release DDS resources."""
        logging.info("D1Arm: disconnecting.")
        try:
            self.home()
        except Exception:
            pass
        self._stop_event.set()
        if self._fb_thread and self._fb_thread.is_alive():
            self._fb_thread.join(timeout=2.0)
