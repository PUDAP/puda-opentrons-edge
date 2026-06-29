"""opentrons-driver — pure Python driver for the Opentrons OT-2 robot.

Quick start::

    from opentrons_driver import Opentrons

    robot = Opentrons(robot_ip="10.0.239.103")
    robot.startup()
    print(robot.is_connected())
    result = robot.upload_and_run(open("my_protocol.py").read())
    print(result["run_status"])
"""

from opentrons_driver.opentrons import Opentrons, DEFAULT_ROBOT_IP
from opentrons_driver.protocol import Protocol, ProtocolCommand

__version__ = "0.1.0"

__all__ = [
    "Opentrons",
    "DEFAULT_ROBOT_IP",
    "Protocol",
    "ProtocolCommand",
    "__version__",
]
