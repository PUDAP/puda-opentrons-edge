# Opentrons OT-2 Edge Workspace

Python workspace for controlling an Opentrons OT-2 robot through PUDA.

This repository contains two packages:

| Package | Purpose |
|---|---|
| `opentrons-driver` | High-level Python driver for the OT-2 HTTP API. It uploads protocols, controls runs, captures camera images, and builds protocol code. |
| `opentrons-edge` | NATS edge service that exposes the driver to PUDA. It receives commands, executes them on the robot, and publishes telemetry. |

For a lab-user setup walkthrough, see [user-guide.md](user-guide.md).

## Repository Layout

```text
Opentron_dev/
|-- pyproject.toml
|-- uv.lock
|-- user-guide.md
|-- driver/
|   |-- README.md
|   |-- pyproject.toml
|   `-- src/opentrons_driver/
|       |-- opentrons.py
|       |-- protocol.py
|       `-- labware/
`-- edge/
    |-- README.md
    |-- pyproject.toml
    |-- main.py
    `-- .env.example
```

## Requirements

- Python 3.14 or newer
- [`uv`](https://docs.astral.sh/uv/)
- A reachable Opentrons OT-2 robot
- A reachable NATS server for PUDA edge operation

## Setup

Install all workspace packages from the repository root:

```bash
uv sync --all-packages
```

This creates `.venv/` and installs both `opentrons-driver` and `opentrons-edge` in the same workspace environment.

## Configure the Edge Service

Create the local environment file:

```bash
cp edge/.env.example edge/.env
```

On Windows PowerShell:

```powershell
copy edge\.env.example edge\.env
```

Edit `edge/.env`:

```env
MACHINE_ID=opentrons
OPENTRONS_IP=10.0.239.103
NATS_SERVERS=nats://100.109.131.12:4222,nats://100.109.131.12:4223,nats://100.109.131.12:4224
```

| Variable | Description |
|---|---|
| `MACHINE_ID` | Robot identifier used on the PUDA/NATS bus. |
| `OPENTRONS_IP` | OT-2 robot IP address from the Opentrons App. |
| `NATS_SERVERS` | Comma-separated NATS server URLs. |

Do not commit `edge/.env`; it contains lab-specific network settings.

## Run the Edge Service

From the repository root:

```bash
uv run --package opentrons-edge python edge/main.py
```

Expected log messages include:

```text
OT2 machine initialized successfully
NATS client initialized successfully
Edge Service Ready
```

Keep the process running while PUDA is sending robot commands.

## Driver Quick Start

Use the driver directly when you want to test robot connectivity or run a protocol without PUDA:

```python
from opentrons_driver import Opentrons

robot = Opentrons(robot_ip="10.0.239.103")
robot.startup()

if not robot.is_connected():
    raise RuntimeError("Robot is unreachable")

result = robot.upload_and_run(open("my_protocol.py").read())
print(result["run_status"])
```

The driver also includes a protocol builder:

```python
from opentrons_driver.protocol import Protocol, ProtocolCommand

protocol = Protocol(
    protocol_name="Water Transfer",
    author="Lab",
    description="Transfer 100 uL from A1 to A1",
    robot_type="OT-2",
    api_level="2.23",
    commands=[
        ProtocolCommand(command_type="load_labware", params={
            "name": "tiprack",
            "labware_type": "opentrons_96_tiprack_300ul",
            "location": "11",
        }),
        ProtocolCommand(command_type="load_labware", params={
            "name": "plate",
            "labware_type": "corning_96_wellplate_360ul_flat",
            "location": "5",
        }),
        ProtocolCommand(command_type="load_instrument", params={
            "name": "p300",
            "instrument_type": "p300_single_gen2",
            "mount": "right",
            "tip_racks": ["tiprack"],
        }),
        ProtocolCommand(command_type="transfer", params={
            "pipette": "p300",
            "volume": 100,
            "source_labware": "plate",
            "source_well": "A1",
            "dest_labware": "plate",
            "dest_well": "A2",
        }),
    ],
)

print(protocol.to_python_code())
```

## PUDA/NATS Commands

The edge service maps incoming NATS command names directly to public `Opentrons` driver methods.

Common commands:

| Command | Description |
|---|---|
| `upload_and_run` | Upload protocol Python code and start a run. |
| `get_status` | Get the current or specified run status. |
| `pause` | Pause a run. |
| `resume` | Resume a paused run. |
| `stop` | Stop or cancel a run. |
| `capture_robot_image` | Capture a JPEG from the OT-2 integrated camera. |
| `is_connected` | Check robot reachability. |
| `get_labware_types` | List available labware load names. |
| `get_pipette_types` | List available pipette types. |

See [edge/README.md](edge/README.md) for the full NATS command flow and telemetry subjects.

## Custom Labware

Add custom Opentrons labware JSON files to:

```text
driver/src/opentrons_driver/labware/
```

The driver discovers these files automatically. The JSON `parameters.loadName` value becomes the `labware_type` used by protocol commands.

See [driver/README.md](driver/README.md) for details and examples.

## Development

Install dependencies:

```bash
uv sync --all-packages
```

Run the edge service:

```bash
uv run --package opentrons-edge python edge/main.py
```

Run a Python command inside the workspace:

```bash
uv run python -c "import opentrons_driver; print('ready')"
```

## Documentation

- [User guide](user-guide.md) - setup guide for lab users and technicians.
- [Driver README](driver/README.md) - direct Python driver usage and protocol builder examples.
- [Edge README](edge/README.md) - NATS edge service, command routing, and telemetry.
