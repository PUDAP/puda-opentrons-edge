"""High-level Opentrons OT-2 machine interface.

The :class:`Driver` class is the primary entry-point for this package.  It
exposes a clean, intent-oriented API that wires together the underlying
controllers for protocol execution, run control, labware management, and
resource discovery.

Example::

    robot = Driver(robot_ip="10.0.239.103")
    robot.startup()

    if not robot.is_connected():
        raise RuntimeError("Robot is unreachable")

    from opentrons.protocol import Protocol, ProtocolCommand

    protocol = Protocol(
        protocol_name="Simple Transfer",
        author="Lab",
        description="Transfer 100 µL from A1 to B1",
        robot_type="OT-2",
        api_level="2.23",
        commands=[...],
    )

    result = robot.upload_and_run(protocol.to_python_code(), wait=True)
    print(result["run_status"])   # "succeeded"
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

import requests

from opentrons.protocol import (
    apply_tip_tracking,
    commit_tip_advances,
    preprocess_protocol_code,
    upload_protocol,
    get_labware_types,
    get_pipette_types,
)

logger = logging.getLogger(__name__)

_TERMINAL_STATES = {"succeeded", "failed", "stopped"}
_CREATE_RUN_RETRIES = 4
_CREATE_RUN_RETRY_DELAY = 1.0
_JPEG_SOI = b"\xff\xd8"
_JPEG_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


class Driver:
    """Opentrons OT-2 robot driver.

    Args:
        robot_ip: IPv4 address of the robot on the local network.
        port: HTTP port (default ``31950``).
        timeout: Default HTTP timeout in seconds (default ``15``).
        status_retries: Number of attempts before treating the robot as
            unreachable (default ``3``).  Retries are only triggered by
            transient network errors (timeout / connection reset).
        retry_delay: Seconds to wait between retry attempts (default ``2.0``).
    """

    def __init__(
        self,
        robot_ip: str,
        port: int = 31950,
        timeout: int = 15,
        status_retries: int = 3,
        retry_delay: float = 2.0,
    ) -> None:
        self._robot_ip = robot_ip
        self._timeout = timeout
        self._base_url = f"http://{robot_ip}:{port}"
        self._status_retries = status_retries
        self._retry_delay = retry_delay
        self._request_lock = threading.RLock()
        self._run_op_lock = threading.RLock()
        self._tip_offsets: dict[tuple[str, ...], int] = {}

        logger.info(
            "OT2 driver initialised (ip=%s port=%s)",
            robot_ip, port,
        )

    @property
    def robot_ip(self) -> str:
        return self._robot_ip

    def startup(self) -> None:
        """
        Start the OT-2 driver.

        The OT-2 HTTP connection is stateless, so startup currently logs the
        target robot URL and does not open persistent resources.
        """
        logger.info("OT2 driver started (url=%s)", self._base_url)

    def shutdown(self) -> None:
        """
        Release driver resources.

        The OT-2 HTTP connection is stateless so no robot teardown is required.

        Returns:
            None
        """
        logger.info("OT2 driver stopped")

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    _HEADERS = {"Opentrons-Version": "*"}

    def get(self, path: str, timeout: Optional[int] = None, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("headers", self._HEADERS)
        with self._request_lock:
            return requests.get(
                f"{self._base_url}{path}",
                timeout=timeout or self._timeout,
                **kwargs,
            )

    def post(self, path: str, timeout: Optional[int] = None, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("headers", self._HEADERS)
        with self._request_lock:
            return requests.post(
                f"{self._base_url}{path}",
                timeout=timeout or self._timeout,
                **kwargs,
            )

    def delete(self, path: str, timeout: Optional[int] = None, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("headers", self._HEADERS)
        with self._request_lock:
            return requests.delete(
                f"{self._base_url}{path}",
                timeout=timeout or self._timeout,
                **kwargs,
            )

    def _get_with_retry(self, path: str, timeout: int = 5, **kwargs: Any) -> requests.Response:
        """GET *path* with up to ``_status_retries`` attempts on transient errors.

        Only :class:`requests.exceptions.Timeout` and
        :class:`requests.exceptions.ConnectionError` trigger a retry; any
        other exception is re-raised immediately.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._status_retries + 1):
            try:
                return self.get(path, timeout=timeout, **kwargs)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_exc = exc
                if attempt < self._status_retries:
                    logger.debug(
                        "GET %s timed out (attempt %d/%d), retrying in %.1fs …",
                        path, attempt, self._status_retries, self._retry_delay,
                    )
                    time.sleep(self._retry_delay)
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        """
        Check whether the OT-2 robot is reachable on the network.

        Sends a GET /health request with a 5-second timeout, retrying up to
        ``status_retries`` times before returning False.

        Returns:
            True if the robot responds with HTTP 200, False otherwise.
        """
        try:
            return self._get_with_retry("/health", timeout=5).status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Protocol upload and run
    # ------------------------------------------------------------------

    def upload_and_run(
        self,
        code: str,
        filename: str = "protocol.py",
        wait: bool = True,
        max_wait: int = 300,
        poll_interval: int = 3,
    ) -> dict:
        """
        Upload a protocol and immediately start it on the robot.

        Preprocesses the source code (strips markdown fences, fixes indentation),
        uploads it to the robot, creates a run, and sends the play action.
        When wait=True, blocks until the run reaches a terminal state.

        Use Protocol.to_python_code() to generate valid source code from
        Protocol / ProtocolCommand models before passing it here.

        Args:
            code (str): Opentrons Python protocol source code.
            filename (str): Filename stored in the robot's run history. Defaults to "protocol.py".
            wait (bool): If True (default), block until the run finishes or max_wait elapses.
                         If False, return immediately after the run is started.
            max_wait (int): Maximum seconds to wait when wait=True. Defaults to 300.
            poll_interval (int): Status poll interval in seconds when wait=True. Defaults to 3.

        Returns:
            dict: When wait=True — full status dict with keys:
                    run_status (str)       "succeeded", "failed", or "stopped"
                    run_id (str)           Run ID assigned by the robot
                    robot_ip (str)         Robot IP address
                    elapsed_seconds (int)  Seconds waited
                    current_step (int)     Last completed step index
                    current_command (str)  Last command type executed
                    errors (list)          Robot-reported errors
                  When wait=False — dict with keys:
                    run_status (str)       "started"
                    run_id (str)           Run ID (use for pause/resume/stop)
                    protocol_id (str)      Protocol ID
                    robot_ip (str)         Robot IP address

        Raises:
            RuntimeError: If the protocol upload or run creation fails.
        """
        with self._run_op_lock:
            prepared_code = preprocess_protocol_code(code)
            tracked_code, tip_advances = apply_tip_tracking(prepared_code, self._tip_offsets)
            protocol_id = upload_protocol(self, tracked_code, filename)
            run_id = self._create_run(protocol_id)

            if not self._send_action(run_id, "play"):
                raise RuntimeError(f"Failed to start run {run_id}")

            commit_tip_advances(self._tip_offsets, tip_advances)

            if not wait:
                logger.info("Run %s started (non-blocking)", run_id)
                return {
                    "run_id": run_id,
                    "protocol_id": protocol_id,
                    "run_status": "started",
                    "robot_ip": self.robot_ip,
                }

            return self._wait_for_completion(
                run_id,
                max_wait=max_wait,
                poll_interval=poll_interval,
            )

    # ------------------------------------------------------------------
    # Run control
    # ------------------------------------------------------------------

    def get_status(self, run_id: Optional[str] = None) -> dict:
        """
        Retrieve the status of a protocol run.

        When run_id is omitted, returns the status of the most recent run
        on the robot.  If the robot is unreachable, returns an offline dict
        instead of raising an exception.

        Args:
            run_id (str, optional): Run ID to query. Omit to get the latest run.

        Returns:
            dict: Status dict with keys:
                status (str)           "success", "error", or "offline"
                run_id (str)           Run ID
                robot_ip (str)         Robot IP address
                run_status (str)       "running", "succeeded", "failed", "stopped", or "offline"
                current_step (int)     Index of the most recently executed step
                current_command (str)  Type of the most recently executed command
                command_details (dict) Full detail of the most recent command
                errors (list)          Robot-reported errors
                started_at (str)       ISO timestamp of run start
                completed_at (str)     ISO timestamp of run completion
                run_data (dict)        Full run data from the robot API
        """
        try:
            return self._get_run_status(run_id)
        except Exception as exc:
            logger.warning("Could not reach robot for status: %s", exc)
            return {
                "status": "offline",
                "robot_ip": self.robot_ip,
                "run_status": "offline",
                "message": str(exc),
            }

    def pause(self, run_id: str) -> bool:
        """
        Pause a running protocol.

        Sends the 'pause' action to the robot.  The run can be resumed
        afterwards with resume().

        Args:
            run_id (str): Run ID of the protocol to pause (returned by upload_and_run).

        Returns:
            bool: True if the robot accepted the action, False otherwise.
        """
        with self._run_op_lock:
            return self._send_action(run_id, "pause")

    def resume(self, run_id: str) -> bool:
        """
        Resume a paused protocol.

        Sends the 'play' action to the robot to continue a paused run.

        Args:
            run_id (str): Run ID of the protocol to resume.

        Returns:
            bool: True if the robot accepted the action, False otherwise.
        """
        with self._run_op_lock:
            return self._send_action(run_id, "play")

    def stop(self, run_id: str) -> bool:
        """
        Stop (cancel) a running or paused protocol.

        Sends the 'stop' action to the robot.  The run cannot be restarted
        after stopping; a new upload_and_run call is required.

        Args:
            run_id (str): Run ID of the protocol to stop.

        Returns:
            bool: True if the robot accepted the action, False otherwise.
        """
        with self._run_op_lock:
            return self._send_action(run_id, "stop")

    # ------------------------------------------------------------------
    # Integrated camera
    # ------------------------------------------------------------------

    def capture_robot_image(
        self,
        filename: Optional[str] = None,
        captures_folder: Union[str, Path] = "captures",
    ) -> dict:
        """
        Capture an image from the OT-2 integrated camera.

        Calls the robot-server ``POST /camera/picture`` endpoint and saves the
        returned JPEG bytes to disk. This does not require an external USB or
        RTSP camera.

        Args:
            filename: Optional output filename. A timestamped name is generated
                when omitted. If no suffix is supplied, ``.jpg`` is appended.
                Relative paths are resolved inside ``captures_folder``.
            captures_folder: Directory used for relative output filenames.

        Returns:
            dict with keys:
                path (str): Absolute path of the saved image file.
                saved (bool): True when the file exists after capture.
                image_base64 (str): Base64-encoded JPEG bytes.
                image_format (str): Always ``"jpeg"``.
                width (int | None): JPEG width when it can be parsed.
                height (int | None): JPEG height when it can be parsed.
                robot_ip (str): Robot IP address.

        Raises:
            RuntimeError: If the robot does not return image bytes.
        """
        resp = self.post("/camera/picture", timeout=30)
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to capture robot image (HTTP {resp.status_code}): {resp.text}"
            )

        image_bytes = resp.content
        if not image_bytes:
            raise RuntimeError("Robot camera capture returned an empty response.")
        content_type = resp.headers.get("content-type", "").lower()
        if "jpeg" not in content_type and not image_bytes.startswith(_JPEG_SOI):
            preview = resp.text[:200] if resp.text else "<binary response>"
            raise RuntimeError(f"Robot camera capture returned a non-JPEG response: {preview}")

        file_path = self._resolve_capture_path(filename, captures_folder)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(image_bytes)

        width, height = self._jpeg_dimensions(image_bytes)
        logger.info("Robot image captured: %s", file_path)
        return {
            "path": str(file_path),
            "saved": file_path.exists(),
            "image_base64": base64.b64encode(image_bytes).decode("ascii"),
            "image_format": "jpeg",
            "width": width,
            "height": height,
            "robot_ip": self.robot_ip,
        }

    # ------------------------------------------------------------------
    # Resource catalogues (no robot connection required)
    # ------------------------------------------------------------------

    def get_labware_types(self) -> list[str]:
        """
        Return all known labware load-names.

        Includes standard Opentrons labware (e.g. corning_96_wellplate_360ul_flat)
        and any custom definitions loaded from the labware/ directory at import time.
        No robot connection is required.

        Returns:
            list[str]: Labware load-names that can be used in load_labware protocol commands.
        """
        return get_labware_types()

    def get_pipette_types(self) -> list[str]:
        """
        Return all known pipette instrument names.

        Lists all supported pipette model names that can be used in
        load_instrument protocol commands. No robot connection is required.

        Returns:
            list[str]: Pipette instrument names (e.g. "p300_single_gen2", "p1000_single_gen2").
        """
        return get_pipette_types()

    @staticmethod
    def _resolve_capture_path(
        filename: Optional[str],
        captures_folder: Union[str, Path],
    ) -> Path:
        if filename is None or not str(filename).strip():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"robot_capture_{timestamp}.jpg"

        file_path = Path(filename)
        if not file_path.suffix:
            file_path = file_path.with_suffix(".jpg")
        if not file_path.is_absolute():
            file_path = Path(captures_folder) / file_path
        return file_path.resolve()

    @staticmethod
    def _jpeg_dimensions(image_bytes: bytes) -> tuple[Optional[int], Optional[int]]:
        """Return ``(width, height)`` from JPEG SOF metadata when available."""
        if not image_bytes.startswith(_JPEG_SOI):
            return None, None

        i = 2
        size = len(image_bytes)
        while i + 9 < size:
            if image_bytes[i] != 0xFF:
                i += 1
                continue

            while i < size and image_bytes[i] == 0xFF:
                i += 1
            if i >= size:
                break

            marker = image_bytes[i]
            i += 1
            if marker in (0xD8, 0xD9):
                continue
            if i + 2 > size:
                break

            segment_length = int.from_bytes(image_bytes[i : i + 2], "big")
            if segment_length < 2 or i + segment_length > size:
                break

            if marker in _JPEG_SOF_MARKERS and segment_length >= 7:
                height = int.from_bytes(image_bytes[i + 3 : i + 5], "big")
                width = int.from_bytes(image_bytes[i + 5 : i + 7], "big")
                return width, height

            i += segment_length
        return None, None

    # ------------------------------------------------------------------
    # Run internals (formerly controllers/run.py)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_transient_create_run_error(resp: requests.Response) -> bool:
        """Return True when robot-server is still tearing down the prior run."""
        return resp.status_code == 500 and "NoRunOrchestrator" in resp.text

    def _create_run(self, protocol_id: str, timeout: int = 30) -> str:
        """Create a run for *protocol_id* and return the run ID."""
        for attempt in range(1, _CREATE_RUN_RETRIES + 1):
            resp = self.post("/runs", json={"data": {"protocolId": protocol_id}}, timeout=timeout)
            if resp.status_code in (200, 201):
                run_id = resp.json().get("data", {}).get("id")
                if not run_id:
                    raise RuntimeError(f"Run created but no ID returned. Response: {resp.text}")
                logger.info("Run created: id=%s protocol_id=%s", run_id, protocol_id)
                return run_id

            if self._is_transient_create_run_error(resp) and attempt < _CREATE_RUN_RETRIES:
                delay = _CREATE_RUN_RETRY_DELAY * attempt
                logger.warning(
                    "Run creation hit transient robot-server teardown race for protocol %s; "
                    "retrying in %.1fs (%d/%d).",
                    protocol_id,
                    delay,
                    attempt,
                    _CREATE_RUN_RETRIES,
                )
                time.sleep(delay)
                continue

            raise RuntimeError(
                f"Failed to create run (HTTP {resp.status_code}): {resp.text}"
            )

        raise RuntimeError("Failed to create run: exhausted retry attempts.")

    def _send_action(self, run_id: str, action_type: str) -> bool:
        """POST an action to ``/runs/{run_id}/actions``."""
        resp = self.post(
            f"/runs/{run_id}/actions",
            json={"data": {"actionType": action_type}},
        )
        ok = resp.status_code in (200, 201)
        if ok:
            logger.info("Action '%s' sent to run %s", action_type, run_id)
        else:
            logger.error(
                "Action '%s' failed for run %s (HTTP %s): %s",
                action_type, run_id, resp.status_code, resp.text,
            )
        return ok

    @staticmethod
    def _extract_current_command(
        commands_data: list[dict],
    ) -> tuple[Optional[str], Optional[dict], Optional[int]]:
        """Return ``(commandType, command_details, step_index)`` for the most relevant command."""
        for _, predicate in [
            ("running", lambda s: s == "running"),
            ("queued", lambda s: s == "queued"),
            ("succeeded", lambda s: s == "succeeded"),
        ]:
            for i in range(len(commands_data) - 1, -1, -1):
                cmd = commands_data[i]
                if predicate(cmd.get("status", "")):
                    return (
                        cmd.get("commandType"),
                        {
                            "commandType": cmd.get("commandType"),
                            "status": cmd.get("status"),
                            "params": cmd.get("params", {}),
                            "id": cmd.get("id"),
                            "key": cmd.get("key"),
                        },
                        i + 1,
                    )
        return None, None, None

    def _get_run_status(self, run_id: Optional[str] = None) -> dict:
        """Retrieve detailed status for a run, defaulting to the most recent."""
        if run_id is not None and (
            not str(run_id).strip() or str(run_id).strip().lower() == "none"
        ):
            run_id = None

        if run_id:
            return self._get_specific_run_status(run_id)
        return self._get_latest_run_status()

    def _get_specific_run_status(self, run_id: str) -> dict:
        resp = self._get_with_retry(f"/runs/{run_id}", timeout=5)
        if resp.status_code != 200:
            return {
                "status": "error",
                "robot_ip": self.robot_ip,
                "error": f"HTTP {resp.status_code}",
                "response": resp.text,
            }
        run_data = resp.json().get("data", {})
        current_command, command_details, step_index = None, None, None
        try:
            cmd_resp = self._get_with_retry(f"/runs/{run_id}/commands", timeout=5)
            if cmd_resp.status_code == 200:
                current_command, command_details, step_index = self._extract_current_command(
                    cmd_resp.json().get("data", [])
                )
        except Exception:
            pass
        return {
            "status": "success",
            "run_id": run_id,
            "robot_ip": self.robot_ip,
            "run_status": run_data.get("status", "unknown"),
            "current_step": step_index,
            "current_command": current_command,
            "command_details": command_details,
            "errors": run_data.get("errors", []),
            "started_at": run_data.get("startedAt"),
            "completed_at": run_data.get("completedAt"),
            "run_data": run_data,
        }

    def _get_latest_run_status(self) -> dict:
        resp = self._get_with_retry("/runs", timeout=5)
        if resp.status_code != 200:
            return {
                "status": "error",
                "robot_ip": self.robot_ip,
                "error": f"HTTP {resp.status_code}",
            }
        runs_data = resp.json().get("data", [])
        if not runs_data:
            return {
                "status": "success",
                "robot_ip": self.robot_ip,
                "message": "No runs found on robot",
                "runs": [],
            }
        latest = runs_data[-1]
        latest_id = latest.get("id")
        current_command, command_details, step_index = None, None, None
        if latest_id:
            try:
                cmd_resp = self._get_with_retry(f"/runs/{latest_id}/commands", timeout=5)
                if cmd_resp.status_code == 200:
                    current_command, command_details, step_index = self._extract_current_command(
                        cmd_resp.json().get("data", [])
                    )
            except Exception:
                pass
        return {
            "status": "success",
            "run_id": latest_id,
            "robot_ip": self.robot_ip,
            "run_status": latest.get("status", "unknown"),
            "current_step": step_index,
            "current_command": current_command,
            "command_details": command_details,
            "errors": latest.get("errors", []),
            "started_at": latest.get("startedAt"),
            "completed_at": latest.get("completedAt"),
            "run_data": latest,
        }

    def _wait_for_completion(
        self,
        run_id: str,
        max_wait: int = 300,
        poll_interval: int = 3,
    ) -> dict:
        """Poll until *run_id* reaches a terminal state or *max_wait* seconds elapse."""
        elapsed = 0
        logger.info("Monitoring run %s (max_wait=%ss)", run_id, max_wait)

        while elapsed < max_wait:
            try:
                status_info = self._get_run_status(run_id)
                run_status = status_info.get("run_status", "unknown")
                logger.debug("Run %s status=%s elapsed=%ss", run_id, run_status, elapsed)

                if run_status in _TERMINAL_STATES:
                    status_info["elapsed_seconds"] = elapsed
                    return status_info

            except Exception as exc:
                logger.warning("Error polling run status: %s", exc)

            time.sleep(poll_interval)
            elapsed += poll_interval

        logger.warning("Timed out waiting for run %s after %ss", run_id, max_wait)
        return {
            "status": "timeout",
            "run_id": run_id,
            "robot_ip": self.robot_ip,
            "run_status": "timeout",
            "elapsed_seconds": elapsed,
            "message": f"Monitoring timed out after {max_wait}s.",
        }
