"""Flex edge package — PUDA driver for the Opentrons Flex robot.

Quick start::

    from driver import opentron_flex

    robot = opentron_flex(robot_ip="10.0.239.103")
    robot.startup()
    print(robot.is_connected())
    robot.home()
"""

from driver import opentron_flex
from protocol import Protocol, ProtocolCommand

__version__ = "0.1.0"

__all__ = [
    "opentron_flex",
    "Protocol",
    "ProtocolCommand",
    "__version__",
]
