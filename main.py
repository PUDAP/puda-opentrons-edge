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
from pydantic_settings import BaseSettings, SettingsConfigDict
from puda_comms import EdgeNatsClient, EdgeRunner
from opentrons.driver import Driver


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logging.getLogger("opentrons_drivers").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class Config(BaseSettings):
    machine_id: str
    opentrons_ip: str
    nats_servers: str

    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def nats_server_list(self) -> list[str]:
        return [s.strip() for s in self.nats_servers.split(",") if s.strip()]


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
        "Config: machine_id=%s opentrons_ip=%s",
        config.machine_id, config.opentrons_ip,
    )

    logger.info("Initializing machine driver")
    driver = Driver(
        robot_ip=config.opentrons_ip,
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


def run() -> None:
    """Run the edge service forever, retrying after fatal errors."""
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.warning("Received KeyboardInterrupt, but continuing to run...")
            time.sleep(1)
        except Exception as e:
            logger.error("Fatal error: %s", e, exc_info=True)
            time.sleep(5)


# Run main in a loop; retry on fatal errors, ignore KeyboardInterrupt.
if __name__ == "__main__":
    run()
