"""
Main entry point for the Opentrons OT-2 edge service.

This module provides the main event loop for the Opentrons machine, handling command
execution via NATS messaging, telemetry publishing, and connection management.
"""
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from puda_comms import EdgeNatsClient, EdgeRunner
from opentrons_driver.opentrons import Opentrons


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logging.getLogger("opentrons_drivers").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def parse_camera_resolution(value: Optional[str]) -> Optional[tuple[int, int]]:
    """Parse a camera resolution string like '1280x720'."""
    if not value:
        return None

    try:
        width, height = value.lower().split("x", maxsplit=1)
        return (int(width), int(height))
    except Exception:
        logger.warning("Invalid CAMERA_RESOLUTION '%s', ignoring", value)
        return None


class Config(BaseSettings):
    machine_id: str
    opentrons_ip: str
    nats_servers: str
    # Camera — leave unset to run without a camera
    camera_rtsp_url: Optional[str] = None
    camera_resolution: Optional[str] = None   # "1280x720"
    camera_captures_folder: str = "captures"
    camera_db_path: Optional[str] = None      # path to puda.db; defaults to "puda.db" cwd
    # OpenRouter
    openrouter_api_key: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def nats_server_list(self) -> list[str]:
        return [s.strip() for s in self.nats_servers.split(",") if s.strip()]

    @property
    def camera_resolution_tuple(self) -> Optional[tuple]:
        return parse_camera_resolution(self.camera_resolution)


def load_config() -> Config:
    """Load and validate configuration; exit process on failure."""
    try:
        return Config()
    except Exception as e:
        logger.error("Failed to load configuration: %s", e)
        sys.exit(1)


async def main():
    """Initialize the Opentrons driver and NATS edge runner."""
    config = load_config()
    logger.info(
        "Config: machine_id=%s opentrons_ip=%s camera_rtsp_url=%s",
        config.machine_id, config.opentrons_ip, config.camera_rtsp_url,
    )

    logger.info("Initializing machine driver")
    driver = Opentrons(
        robot_ip=config.opentrons_ip,
        camera_index=config.camera_rtsp_url,
        camera_resolution=config.camera_resolution_tuple,
        captures_folder=config.camera_captures_folder,
        db_path=config.camera_db_path,
    )
    driver.startup()
    logger.info("OT2 machine initialized successfully")

    logger.info("Connecting to NATS at %s", config.nats_servers)
    edge_nats_client = EdgeNatsClient(
        servers=config.nats_server_list,
        machine_id=config.machine_id,
    )

    async def telemetry_handler():
        await edge_nats_client.publish_heartbeat()
        await edge_nats_client.publish_position({})
        await edge_nats_client.publish_health(driver.get_status())

    runner = EdgeRunner(
        nats_client=edge_nats_client,
        machine_driver=driver,
        telemetry_handler=telemetry_handler,
        state_handler=lambda: {},
    )
    await runner.connect()
    logger.info("NATS client initialized successfully")
    logger.info(
        "==================== %s Edge Service Ready. Publishing telemetry... ====================",
        config.machine_id,
    )
    await runner.run()


# Run main in a loop; retry on fatal errors, ignore KeyboardInterrupt.
if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.warning("Received KeyboardInterrupt, but continuing to run...")
            time.sleep(1)
        except Exception as e:
            logger.error("Fatal error: %s", e, exc_info=True)
            time.sleep(5)
