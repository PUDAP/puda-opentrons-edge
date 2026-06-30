"""opentrons — pure Python driver for the Opentrons OT-2 robot.

Quick start::

    from opentrons.driver import Driver

    robot = Driver(robot_ip="10.0.239.103")
    robot.startup()
    print(robot.is_connected())
    result = robot.upload_and_run(open("my_protocol.py").read())
    print(result["run_status"])
"""

from opentrons.driver import Driver
from opentrons.protocol import Protocol, ProtocolCommand

__version__ = "0.1.0"

__all__ = [
    "Driver",
    "Protocol",
    "ProtocolCommand",
    "__version__",
]
