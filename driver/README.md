# opentrons-driver

High-level Python driver for the [Opentrons OT-2](https://opentrons.com/ot-2/) liquid-handling robot, used by the PUDA edge service.

Communicates directly with the OT-2 REST API over HTTP — no Opentrons App, no Docker required.

## Features

- **Robot control** — upload and run Opentrons protocols directly from Python
- **Run management** — play, pause, stop, and monitor protocol runs
- **Protocol builder** — construct OT-2 protocols programmatically using Pydantic models
- **Labware management** — auto-discover custom labware definitions from `labware/`
- **Integrated camera capture** — capture JPEG images from the OT-2 robot camera through the robot API
- **Cross-platform** — works on Windows, macOS, and Linux

## Installation

### From PyPI

```bash
pip install opentrons-driver
```

### From source

This package is part of a UV workspace monorepo. Install `uv` first — see the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).

**From the repository root:**

```bash
uv sync --all-packages
```

This will:
- Create a virtual environment at the repository root (`.venv/`)
- Install all workspace package dependencies
- Install `opentrons-driver` in editable mode automatically

**Using the package:**

```bash
# Run scripts with workspace context (recommended)
uv run python your_script.py

# Or activate the virtual environment directly
source .venv/bin/activate        # Linux / macOS
.venv\Scripts\activate           # Windows
python your_script.py
```

**Adding dependencies:**

```bash
# From the package directory
cd driver
uv add some-package

# Or from the repository root
uv add --package opentrons-driver some-package
```

## Device Support

| Device | Description |
|---|---|
| **OT-2** | Opentrons OT-2 liquid-handling robot (HTTP REST API) |

## Quick start

```python
from opentrons_driver import Opentrons

robot = Opentrons(robot_ip="10.0.239.103")
robot.startup()

if not robot.is_connected():
    raise RuntimeError("Robot is unreachable")

result = robot.upload_and_run(open("my_protocol.py").read())
print(result["run_status"])   # "succeeded" / "failed" / "stopped"
```

## Building a protocol programmatically

```python
from opentrons_driver import Opentrons
from opentrons_driver.protocol import Protocol, ProtocolCommand

robot = Opentrons(robot_ip="10.0.239.103")
robot.startup()

protocol = Protocol(
    protocol_name="Water Transfer",
    author="Lab",
    description="Transfer 100 µL of water from water plate A1 to mixing plate A1",
    robot_type="OT-2",
    api_level="2.23",
    commands=[
        ProtocolCommand(command_type="load_labware", params={
            "name": "tiprack", "labware_type": "opentrons_96_tiprack_300ul", "location": "11"
        }),
        ProtocolCommand(command_type="load_labware", params={
            "name": "mixing_plate", "labware_type": "corning_96_wellplate_360ul_flat", "location": "5"
        }),
        ProtocolCommand(command_type="load_labware", params={
            "name": "water_plate", "labware_type": "corning_96_wellplate_360ul_flat", "location": "6"
        }),
        ProtocolCommand(command_type="load_instrument", params={
            "name": "p300", "instrument_type": "p300_single_gen2",
            "mount": "right", "tip_racks": ["tiprack"]
        }),
        ProtocolCommand(command_type="transfer", params={
            "pipette": "p300", "volume": 100,
            "source_labware": "water_plate", "source_well": "A1",
            "dest_labware":   "mixing_plate", "dest_well":   "A1",
        }),
    ],
)

result = robot.upload_and_run(protocol.to_python_code())
print(result["run_status"])
```

### Supported command types

| Command | Key params | Description |
|---|---|---|
| `load_labware` | `name`, `labware_type`, `location` | Load labware onto a deck slot |
| `load_instrument` | `name`, `instrument_type`, `mount`, `tip_racks?` | Load a pipette |
| `pick_up_tip` | `pipette`, `labware?`, `well?` | Pick up a tip |
| `drop_tip` | `pipette`, `labware?`, `well?` | Drop the tip |
| `aspirate` | `pipette`, `volume`, `labware?`, `well?`, `aspirate_ref?`, `aspirate_offset?`, `aspirate_rate?` | Aspirate liquid |
| `dispense` | `pipette`, `volume`, `labware?`, `well?`, `dispense_ref?`, `dispense_offset?`, `dispense_rate?` | Dispense liquid |
| `blow_out` | `pipette`, `labware?`, `well?` | Blow out remaining liquid |
| `mix` | `pipette`, `repetitions?`, `volume?`, `labware?`, `well?` | Mix in place |
| `air_gap` | `pipette`, `volume?`, `height?` | Draw an air gap |
| `touch_tip` | `pipette`, `labware?`, `well?`, `radius?`, `v_offset?`, `speed?` | Touch tip to well wall |
| `transfer` | `pipette`, `volume`, `source_labware`, `source_well`, `dest_labware`, `dest_well` | High-level transfer |
| `move_to` | `pipette`, `labware`, `well`, `ref?`, `offset?` | Move to a location |
| `flow_rate` | `pipette`, `aspirate?`, `dispense?`, `blow_out?` | Set flow rates (µL/s) |
| `delay` | `seconds?`, `minutes?`, `message?`, `pipette?` | Pause execution |
| `home` | — | Home all robot axes |
| `comment` | `text` | Inline comment in generated code |
| `loop` / `loop_over_csv` | `commands` (list) | Iterate over CSV data rows |
| `read_csv_file` / `read_csv` | `file_path`, `csv_data?` | Define CSV data source for loops |

## Run control

```python
robot = Opentrons("10.0.239.103")
robot.startup()

result = robot.upload_and_run(code, wait=False)
run_id = result["run_id"]

robot.pause(run_id)
robot.resume(run_id)
robot.stop(run_id)

status = robot.get_status(run_id)
print(status["run_status"])   # "running" / "succeeded" / "failed" / "stopped"
```

## Integrated camera

```python
robot = Opentrons("10.0.239.103")
robot.startup()

image = robot.capture_robot_image(filename="deck_after_transfer")
print(image["path"])
print(image["width"], image["height"])
```

`capture_robot_image()` calls the OT-2 `POST /camera/picture` endpoint, saves the returned JPEG to `captures/` by default, and returns the saved path plus a base64 JPEG payload. It does not require an external camera.

## Custom labware

JSON definitions in `labware/` are auto-loaded at import — no Python edits needed.

```python
from opentrons_driver.protocol import BUILTIN_LABWARE, get_labware_types

print(get_labware_types())
print(BUILTIN_LABWARE["mass_balance_vial_30000"]["metadata"]["displayName"])
```

Drop any `.json` file following the [Opentrons labware schema](https://github.com/Opentrons/opentrons/tree/edge/shared-data/labware) into `labware/`. Its `parameters.loadName` becomes the key in `BUILTIN_LABWARE` and can be used as `labware_type` in a `load_labware` protocol command. For custom definitions, the protocol builder embeds the definition with Opentrons `load_labware_from_definition()`.

## Requirements

| Dependency | Version | Purpose |
|---|---|---|
| Python | >= 3.14 | — |
| `pydantic` | >= 2.12.5 | Protocol model validation |
| `requests` | >= 2.32.0 | HTTP transport |

## Development

### Setup

```bash
# From the repository root
uv sync --all-packages
```

### Testing

```bash
# Run all tests
uv run pytest tests/

# Run a specific file
uv run pytest tests/test_opentrons.py

# Verbose + coverage
uv run pytest tests/ -v --cov=opentrons_driver --cov-report=html
```

### Building and publishing

```bash
uv build

# From the repository root
uv publish
# Username: __token__
# Password: <your PyPI API token>
```

### Version management

```bash
uv version 0.1.0   # set explicitly
uv bump minor      # e.g. 0.1.0 → 0.2.0
```

## Documentation

- [PyPI Package](https://pypi.org/project/opentrons-driver/)
- [Opentrons OT-2 REST API](https://labautomation.io/ot2-api)

## License

MIT License — see LICENSE file for details.

## Contributing

Contributions are welcome! Please open an issue or submit a pull request on GitHub.
