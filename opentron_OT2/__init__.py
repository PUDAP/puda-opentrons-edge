"""OT-2 edge package — PUDA driver for the Opentrons OT-2 robot.

Quick start::

    from driver import opentron_OT2

    robot = opentron_OT2(robot_ip="10.0.239.103")
    robot.startup()
    print(robot.is_connected())
"""

from driver import opentron_OT2
from protocol import Protocol, ProtocolCommand

__version__ = "0.1.0"

__all__ = [
    "opentron_OT2",
    "Protocol",
    "ProtocolCommand",
    "__version__",
]
