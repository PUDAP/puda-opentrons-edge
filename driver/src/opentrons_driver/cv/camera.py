"""
Camera controller for image and video capture.

This module provides a Python interface for controlling cameras and webcams
to capture images and videos for laboratory automation applications using OpenCV.
"""

import logging
import sqlite3
import time
import threading
from datetime import datetime
from typing import Optional, Union, List, Tuple
from pathlib import Path
import cv2
import numpy as np

logger = logging.getLogger(__name__)


def list_cameras(max_index: int = 10) -> List[Tuple[int, bool, Optional[tuple[int, int]]]]:
    """
    Lists available cameras on the system by testing camera indices.
    
    This is a utility function that can be used independently of any controller instance.
    It's useful for discovering available cameras before initializing a controller.
    
    Args:
        max_index: Maximum camera index to test (default: 10). The function will test
                  indices from 0 to max_index-1.
    
    Returns:
        List of tuples, where each tuple contains (index, is_available, resolution).
        resolution is a (width, height) tuple if available, None otherwise.
    """
    available_cameras = []
    
    for index in range(max_index):
        cap = cv2.VideoCapture(index)
        if cap.isOpened():
            # Try to read a frame to confirm it's actually working
            ret, _ = cap.read()
            if ret:
                # Get resolution
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                resolution = (width, height) if width > 0 and height > 0 else None
                available_cameras.append((index, resolution))
            cap.release()
        else:
            cap.release()
    
    return available_cameras


class CameraController:
    """
    OpenCV-based camera controller for image and video capture.

    Attributes:
        camera_index: Device index (int) or path (str) passed to cv2.VideoCapture.
        resolution: Active (width, height) resolution, or None to use the camera default.
        captures_folder: Directory where images and videos are written.
        db_path: Path to the SQLite database used for image persistence.
    """
    
    DEFAULT_CAPTURES_FOLDER = "captures"
    DEFAULT_DB_PATH = "puda.db"

    def __init__(
        self,
        camera_index: Union[int, str] = 0,
        resolution: Optional[tuple[int, int]] = None,
        captures_folder: Union[str, Path, None] = None,
        db_path: Union[str, Path, None] = None,
    ):
        """
        Args:
            camera_index: Device index (0 = default) or device path string.
            resolution: Optional (width, height) applied on connect. Uses camera
                default when omitted.
            captures_folder: Directory for saved images/videos.
                Defaults to ``"captures"`` relative to the current working directory.
            db_path: SQLite database path for image persistence.
                Defaults to ``"puda.db"`` relative to the current working directory.
        """
        self.camera_index = camera_index
        self.resolution = resolution
        self.captures_folder = Path(captures_folder) if captures_folder else Path(self.DEFAULT_CAPTURES_FOLDER)
        self.db_path = Path(db_path) if db_path else Path(self.DEFAULT_DB_PATH)
        self._logger = logging.getLogger(__name__)
        self._camera: Optional[cv2.VideoCapture] = None
        self._is_connected = False
        self._camera_lock = threading.RLock()

        # Continuous stream state. Network streams such as RTSP can keep a
        # deep FFmpeg buffer; draining them in the background keeps snapshots live.
        self._is_stream_source = self._detect_stream_source(camera_index)
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_frame_at: Optional[float] = None
        self._stream_thread: Optional[threading.Thread] = None
        self._stop_stream_event: Optional[threading.Event] = None

        # Video recording state
        self._video_writer: Optional[cv2.VideoWriter] = None
        self._is_recording = False
        self._video_file_path: Optional[Path] = None
        self._fps: float = 30.0  # Default FPS for video recording
        self._recording_thread: Optional[threading.Thread] = None
        self._stop_recording_event: Optional[threading.Event] = None

        # Create captures folder if it doesn't exist
        self.captures_folder.mkdir(parents=True, exist_ok=True)

        # Ensure parent directory for db exists and initialise schema
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        self._logger.info(
            "Camera Controller initialized with camera_index='%s', resolution=%s, "
            "captures_folder='%s', db_path='%s'",
            camera_index,
            resolution,
            self.captures_folder,
            self.db_path,
        )

    @staticmethod
    def _detect_stream_source(camera_index: Union[int, str]) -> bool:
        """Return True for sources that should be drained continuously."""
        if not isinstance(camera_index, str):
            return False
        source = camera_index.lower()
        return source.startswith(("rtsp://", "rtmp://", "http://", "https://"))

    def _init_db(self) -> None:
        """Create the captures table in puda.db if it does not already exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS captures (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT    NOT NULL,
                    camera    TEXT    NOT NULL,
                    width     INTEGER NOT NULL,
                    height    INTEGER NOT NULL,
                    filename  TEXT,
                    image     BLOB    NOT NULL
                )
                """
            )
            conn.commit()
        self._logger.debug("Database initialised at %s", self.db_path)

    def _save_image_to_db(
        self,
        frame: np.ndarray,
        filename: Optional[str] = None,
    ) -> int:
        """
        Encode *frame* as JPEG and persist it in the captures table.

        Args:
            frame: BGR image array from OpenCV.
            filename: Optional associated file path string to store as metadata.

        Returns:
            Row id of the newly inserted record.
        """
        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            raise IOError("Failed to encode frame as JPEG for database storage")

        blob = buf.tobytes()
        height, width = frame.shape[:2]
        timestamp = datetime.now().isoformat()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO captures (timestamp, camera, width, height, filename, image) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (timestamp, str(self.camera_index), width, height, filename, blob),
            )
            conn.commit()
            row_id: int = cursor.lastrowid  # type: ignore[assignment]

        self._logger.info(
            "Image saved to database (id=%d, %dx%d) at %s", row_id, width, height, self.db_path
        )
        return row_id
    
    def connect(self, retries: int = 5, retry_delay: float = 1.0) -> None:
        """
        Open the configured camera source.

        If the camera is already connected, it is disconnected first before
        attempting to reopen the device.

        Local numeric camera indices use the V4L2 backend. String sources,
        including RTSP URLs, use OpenCV's default backend so FFmpeg/GStreamer
        can handle network streams.

        When the source cannot be opened on the first try (e.g. the V4L2 handle
        was recently held by a previous process iteration, or the RTSP stream
        is still accepting clients), the open is retried
        up to *retries* times with *retry_delay* seconds between each attempt.
        This makes the driver resilient to restarts without closing the terminal.

        The internal frame buffer is reduced to 1 slot so that
        :meth:`capture_image` always obtains a fresh frame rather than a
        stale one from the driver's queue.

        Args:
            retries: Maximum number of open attempts (default ``5``).
            retry_delay: Seconds to wait between attempts (default ``1.0``).

        Raises:
            IOError: If the device cannot be opened after all attempts.
        """
        if self._is_connected:
            self._logger.warning("Camera already connected. Disconnecting and reconnecting...")
            self.disconnect()

        self._logger.info("Connecting to camera %s...", self.camera_index)
        use_v4l2 = isinstance(self.camera_index, int)

        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                if use_v4l2:
                    self._camera = cv2.VideoCapture(self.camera_index, cv2.CAP_V4L2)
                else:
                    self._camera = cv2.VideoCapture(self.camera_index)
                if not self._camera.isOpened():
                    self._camera.release()
                    self._camera = None
                    raise IOError(f"Could not open camera {self.camera_index}")

                # Minimise the internal frame queue so capture_image() always
                # reads the most recent frame rather than a buffered one.
                self._camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)

                if self.resolution:
                    width, height = self.resolution
                    self._camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                    self._camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                    self._logger.info("Resolution set to %dx%d", width, height)

                self._is_connected = True
                if self._is_stream_source:
                    self._start_stream_reader()
                self._logger.info(
                    "Successfully connected to camera %s (attempt %d/%d)",
                    self.camera_index, attempt, retries,
                )
                return

            except Exception as exc:
                last_exc = exc
                self._camera = None
                self._is_connected = False
                if attempt < retries:
                    self._logger.warning(
                        "Camera open attempt %d/%d failed: %s — retrying in %.1fs...",
                        attempt, retries, exc, retry_delay,
                    )
                    time.sleep(retry_delay)

        self._logger.error(
            "Could not open camera %s after %d attempts: %s",
            self.camera_index, retries, last_exc,
        )
        raise IOError(
            f"Error connecting to camera {self.camera_index} after {retries} attempts: {last_exc}"
        ) from last_exc

    def _start_stream_reader(self) -> None:
        """Start a background reader that keeps only the newest stream frame."""
        self._stop_stream_reader()
        self._latest_frame = None
        self._latest_frame_at = None
        self._stop_stream_event = threading.Event()
        self._stream_thread = threading.Thread(target=self._stream_reader_loop, daemon=True)
        self._stream_thread.start()

    def _stop_stream_reader(self) -> None:
        """Stop the background stream reader if one is active."""
        if self._stop_stream_event is not None:
            self._stop_stream_event.set()
        if self._stream_thread is not None and self._stream_thread.is_alive():
            self._stream_thread.join(timeout=2.0)
        self._stream_thread = None
        self._stop_stream_event = None

    def _stream_reader_loop(self) -> None:
        """Continuously drain stream frames so capture_image sees the live view."""
        while (
            self._stop_stream_event is not None
            and not self._stop_stream_event.is_set()
            and self._camera is not None
        ):
            with self._camera_lock:
                ret, frame = self._camera.read()

            if ret and frame is not None:
                self._latest_frame = frame
                self._latest_frame_at = time.monotonic()
            else:
                self._logger.debug("Failed to read frame from stream camera")
                time.sleep(0.05)

        self._logger.debug("Stream reader loop stopped")

    def _read_latest_frame(self, timeout: float = 5.0) -> np.ndarray:
        """Read a fresh frame from the camera or the stream reader."""
        if self._is_stream_source:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with self._camera_lock:
                    frame = self._latest_frame
                    frame_at = self._latest_frame_at
                    if (
                        frame is not None
                        and frame_at is not None
                        and time.monotonic() - frame_at <= 2.0
                    ):
                        return frame.copy()
                time.sleep(0.02)
            raise IOError("Timed out waiting for a frame from stream camera")

        self._flush_camera_buffer()
        with self._camera_lock:
            ret, frame = self._camera.read()

        if not ret or frame is None:
            raise IOError("Failed to capture frame from camera")
        return frame
    
    def disconnect(self) -> None:
        """
        Release the camera device.

        Stops any active video recording before releasing the handle.
        Safe to call when the camera is already disconnected.
        """
        # Stop any ongoing video recording before disconnecting
        if self._is_recording:
            self._logger.warning("Stopping video recording before disconnecting...")
            self.stop_video_recording()

        self._stop_stream_reader()
        
        if self._camera is not None:
            self._logger.info("Disconnecting from camera %s...", self.camera_index)
            with self._camera_lock:
                self._camera.release()
            self._camera = None
            self._is_connected = False
            self._logger.info("Camera disconnected")
        else:
            self._logger.warning("Camera already disconnected or was never connected")
    
    @property
    def is_connected(self) -> bool:
        """
        ``True`` if the internal flag is set, the handle exists, and ``isOpened()`` returns ``True``.
        """
        return self._is_connected and self._camera is not None and self._camera.isOpened()
    
    def _flush_camera_buffer(self, frames: int = 4) -> None:
        """Discard buffered frames so the next read returns the live view.

        OpenCV maintains an internal ring buffer for V4L2 devices.  When the
        camera is open but frames are not consumed continuously, the buffer
        fills up with stale frames.  Calling ``cap.read()`` then returns the
        oldest queued frame rather than the current one.

        This method drains up to *frames* entries from the buffer using the
        lightweight ``grab()`` call (which does not decode the frame) so that
        the following ``read()`` in :meth:`capture_image` yields a fresh image.

        Args:
            frames: Number of frames to discard (default ``4``).
        """
        if self._is_stream_source:
            return
        for _ in range(frames):
            self._camera.grab()

    def capture_image(
        self,
        save: bool = False,
        filename: Optional[Union[str, Path]] = None,
        save_to_db: bool = False,
    ) -> Tuple[np.ndarray, Optional[Path]]:
        """
        Capture a single frame from the camera.

        Stale frames queued in the OpenCV buffer are discarded before the real
        read so the returned image always reflects the current camera view.

        Args:
            save: Write the frame to disk as a JPEG inside ``captures_folder``.
            filename: Destination filename.
                Auto-generates ``capture_YYYYMMDD_HHMMSS.jpg`` when omitted.
                ``.jpg`` is appended if no extension is given.
                Relative paths are resolved inside ``captures_folder``.
            save_to_db: Persist the frame as a JPEG BLOB in the ``captures`` table of
                ``puda.db``. The on-disk path (if any) is stored as metadata.

        Returns:
            ``(frame, saved_path)`` where *frame* is a BGR ``np.ndarray`` and
            *saved_path* is the absolute ``Path`` of the written file, or ``None``
            when ``save=False``.

        Raises:
            IOError: Camera is not connected, or the frame read fails.
        """
        if not self.is_connected:
            raise IOError("Camera is not connected. Call connect() first.")

        self._logger.info("Capturing image...")

        try:
            frame = self._read_latest_frame()
        except IOError:
            self._logger.error("Failed to capture frame from camera")
            raise

        self._logger.info("Image captured successfully (shape: %s)", frame.shape)

        saved_file_path: Optional[Path] = None

        if save:
            if filename is None:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"capture_{timestamp}.jpg"

            file_path = Path(filename)
            if not file_path.suffix:
                file_path = file_path.with_suffix(".jpg")
            if not file_path.is_absolute():
                file_path = self.captures_folder / file_path

            file_path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(file_path), frame)
            self._logger.info("Image saved to %s", file_path)
            saved_file_path = file_path

        if save_to_db:
            db_filename = str(saved_file_path) if saved_file_path else None
            self._save_image_to_db(frame, filename=db_filename)

        return frame, saved_file_path

    def set_resolution(self, width: int, height: int) -> None:
        """
        Set the capture resolution.

        Args:
            width: Frame width in pixels.
            height: Frame height in pixels.

        Applies immediately to the open device if the camera is connected.
        """
        self.resolution = (width, height)
        self._logger.info("Resolution set to %dx%d", width, height)
        
        # Apply resolution to camera if connected
        if self.is_connected:
            self._camera.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self._camera.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self._logger.info("Resolution applied to camera")
    
    def start_video_recording(
        self,
        filename: Optional[Union[str, Path]] = None,
        fps: Optional[float] = None
    ) -> Path:
        """
        Start a background video recording.

        Spawns a daemon thread that continuously reads frames and writes them
        to a ``mp4v``-encoded ``.mp4`` file.

        Args:
            filename: Destination filename.
                Auto-generates ``video_YYYYMMDD_HHMMSS.mp4`` when omitted.
                ``.mp4`` is appended if no extension is given.
                Relative paths are resolved inside ``captures_folder``.
            fps: Recording frame rate. Defaults to ``30.0``.

        Returns:
            Absolute ``Path`` of the video file being written.

        Raises:
            IOError: Camera is not connected, or the ``VideoWriter`` fails to open.
            ValueError: A recording is already in progress.
        """
        if not self.is_connected:
            raise IOError("Camera is not connected. Call connect() first.")
        
        if self._is_recording:
            raise ValueError("Video recording is already in progress. Call stop_video_recording() first.")
        
        # Generate filename if not provided
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"video_{timestamp}.mp4"
        
        file_path = Path(filename)
        # If filename doesn't have an extension, add .mp4
        if not file_path.suffix:
            file_path = file_path.with_suffix(".mp4")
        
        # If filename is not absolute, save to captures folder
        if not file_path.is_absolute():
            file_path = self.captures_folder / file_path
        
        # Ensure parent directory exists
        file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Get current camera resolution
        width = int(self._camera.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(self._camera.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
        # Use provided FPS or default
        fps = fps if fps is not None else self._fps
        
        # Define codec and create VideoWriter
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        self._video_writer = cv2.VideoWriter(str(file_path), fourcc, fps, (width, height))
        
        if not self._video_writer.isOpened():
            self._video_writer = None
            raise IOError(f"Failed to initialize video writer for {file_path}")
        
        self._is_recording = True
        self._video_file_path = file_path
        self._fps = fps
        
        # Start background thread to continuously capture frames
        self._stop_recording_event = threading.Event()
        self._recording_thread = threading.Thread(target=self._capture_frames_loop, daemon=True)
        self._recording_thread.start()
        
        self._logger.info("Started video recording to %s (FPS: %.1f, Resolution: %dx%d)", 
                         file_path, fps, width, height)
        
        return file_path
    
    def _capture_frames_loop(self) -> None:
        """
        Internal method that continuously captures frames and writes them to the video.
        Runs in a background thread while recording is active.
        """
        frame_interval = 1.0 / self._fps if self._fps > 0 else 0.033  # Default to ~30 FPS
        
        while self._is_recording and not self._stop_recording_event.is_set():
            if self._video_writer is None or self._camera is None:
                break

            try:
                frame = self._read_latest_frame(timeout=1.0)
                self._video_writer.write(frame)
            except IOError:
                self._logger.warning("Failed to read frame during video recording")
            
            time.sleep(frame_interval)
        
        self._logger.debug("Frame capture loop stopped")
    
    def stop_video_recording(self) -> Optional[Path]:
        """
        Stop the active background recording and finalise the video file.

        Signals the recording thread to exit, waits up to 2 s for it to finish,
        then releases the ``VideoWriter``.

        Returns:
            Absolute ``Path`` of the saved video, or ``None`` if no recording was active.
        """
        if not self._is_recording:
            self._logger.warning("No video recording in progress")
            return None
        
        self._logger.info("Stopping video recording...")
        
        # Signal the recording thread to stop
        self._is_recording = False
        if self._stop_recording_event is not None:
            self._stop_recording_event.set()
        
        # Wait for the recording thread to finish
        if self._recording_thread is not None and self._recording_thread.is_alive():
            self._recording_thread.join(timeout=2.0)
        
        # Release video writer
        if self._video_writer is not None:
            self._video_writer.release()
            self._video_writer = None
        
        file_path = self._video_file_path
        self._video_file_path = None
        self._recording_thread = None
        self._stop_recording_event = None
        
        if file_path and file_path.exists():
            self._logger.info("Video saved to %s", file_path)
        else:
            self._logger.warning("Video file may not have been saved correctly")
        
        return file_path
    
    def record_video(
        self,
        duration_seconds: float,
        filename: Optional[Union[str, Path]] = None,
        fps: Optional[float] = None
    ) -> Path:
        """
        Record a video for a fixed duration, blocking until complete.

        Delegates to ``start_video_recording`` then reads frames in the calling
        thread for *duration_seconds*. Always calls ``stop_video_recording()``
        on exit, even if an error occurs.

        Args:
            duration_seconds: How long to record. Must be > 0.
            filename: Destination filename (same rules as ``start_video_recording``).
            fps: Recording frame rate. Defaults to ``30.0``.

        Returns:
            Absolute ``Path`` of the saved video file.

        Raises:
            IOError: Camera is not connected.
            ValueError: ``duration_seconds`` is not positive.
        """
        if not self.is_connected:
            raise IOError("Camera is not connected. Call connect() first.")
        
        if duration_seconds <= 0:
            raise ValueError(f"Duration must be positive, got {duration_seconds}")
        
        # Start recording
        file_path = self.start_video_recording(filename=filename, fps=fps)
        
        self._logger.info("Recording video for %.2f seconds...", duration_seconds)
        
        start_time = time.time()
        frame_count = 0
        
        try:
            while time.time() - start_time < duration_seconds:
                try:
                    frame = self._read_latest_frame(timeout=1.0)
                    self._video_writer.write(frame)
                    frame_count += 1
                except IOError:
                    self._logger.warning("Failed to read frame during video recording")
                    break
        finally:
            # Always stop recording, even if there was an error
            self.stop_video_recording()
        
        actual_duration = time.time() - start_time
        self._logger.info("Video recording completed: %.2f seconds, %d frames (%.1f FPS actual)", 
                         actual_duration, frame_count, frame_count / actual_duration if actual_duration > 0 else 0)
        
        return file_path
