# opentrons-driver

High-level Python driver for the [Opentrons OT-2](https://opentrons.com/ot-2/) liquid-handling robot, used by the PUDA edge service.

Communicates directly with the OT-2 REST API over HTTP — no Opentrons App, no Docker required.

## Features

- **Robot control** — upload and run Opentrons protocols directly from Python
- **Run management** — play, pause, stop, and monitor protocol runs
- **Protocol builder** — construct OT-2 protocols programmatically using Pydantic models
- **Labware management** — upload custom labware definitions; built-ins auto-discovered from `labware/`
- **Camera support** — capture images and record video via USB camera (optional)
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
| **Camera** | USB and V4L2-compatible cameras for image and video capture |

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

## Camera

```python
from opentrons_driver.cv import CameraController, list_cameras

# Discover available cameras
cameras = list_cameras()
print(cameras)   # [(0, (1280, 720)), (1, (640, 480))]

cam = CameraController(camera_index=0, resolution=(1280, 720))
cam.connect()

frame, path = cam.capture_image(save=True)                       # auto-timestamped
frame, path = cam.capture_image(save=True, filename="well_A1")  # saves as captures/well_A1.jpg

cam.record_video(duration_seconds=10, filename="experiment")

cam.start_video_recording(filename="run_001", fps=30)
# ... robot moves ...
cam.stop_video_recording()

cam.disconnect()
```

See docstrings in `driver/src/opentrons_driver/cv/camera.py` for full parameter and error details.

## Custom labware

JSON definitions in `labware/` are auto-loaded at import — no Python edits needed.

```python
from opentrons_driver.protocol import BUILTIN_LABWARE
from opentrons_driver import Opentrons

robot = Opentrons("10.0.239.103")
robot.startup()

robot.upload_labware(BUILTIN_LABWARE["mass_balance_vial_30000"])
robot.upload_labware("/path/to/my_labware.json")
```

Drop any `.json` file following the [Opentrons labware schema](https://github.com/Opentrons/opentrons/tree/edge/shared-data/labware) into `labware/`. Its `parameters.loadName` becomes the key in `BUILTIN_LABWARE`.

## Requirements

| Dependency | Version | Purpose |
|---|---|---|
| Python | >= 3.14 | — |
| `pydantic` | >= 2.12.5 | Protocol model validation |
| `requests` | >= 2.32.0 | HTTP transport |
| `opencv-python` | >= 4.8.0 | Camera capture and video recording |
| `numpy` | >= 1.26.0 | Image array handling |

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
