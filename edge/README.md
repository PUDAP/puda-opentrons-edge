# opentrons-edge

NATS edge service for the Opentrons OT-2 robot.

Bridges the OT-2 HTTP REST API to the NATS message bus — receives commands, executes them on the robot, and publishes telemetry back.

---

## Architecture

```
NATS Server
    │  puda.{machine_id}.cmd.queue        (JetStream — inbound queue commands)
    │  puda.{machine_id}.cmd.immediate    (JetStream — inbound immediate commands)
    │  puda.{machine_id}.cmd.response.*   (JetStream — command replies)
    │  puda.{machine_id}.tlm.*            (core NATS — telemetry)
    ▼
┌──────────────────────────┐   HTTP :31950   ┌─────────────────┐
│  edge/main.py            │ ──────────────▶ │  OT-2 Robot     │
│  EdgeRunner(Opentrons)   │                 │  10.0.239.103   │
└──────────────────────────┘                 └─────────────────┘
```

`EdgeRunner` dispatches incoming NATS commands by matching `command.name` to public `Opentrons` driver method names and calling them with `**command.params` — no adapter layer required.

Every queue command must be wrapped in a **START → commands → COMPLETE** lifecycle (handled automatically by `CommandService.send_queue_commands`).

---

## Configuration

Edit `edge/.env`:

```ini
MACHINE_ID=opentrons
OPENTRONS_IP=10.0.239.103
CAMERA_INDEX=0
NATS_SERVERS=nats://100.109.131.12:4222,nats://100.109.131.12:4223,nats://100.109.131.12:4224
# CAMERA_RESOLUTION=1280x720       # optional
# CAMERA_CAPTURES_FOLDER=captures  # optional
# CAMERA_DB_PATH=puda.db           # optional
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `MACHINE_ID` | yes | — | Unique identifier for this robot on the NATS bus |
| `OPENTRONS_IP` | yes | — | IPv4 address of the OT-2 on the local network |
| `NATS_SERVERS` | yes | — | Comma-separated list of NATS server URLs |
| `CAMERA_INDEX` | no | unset | V4L2 device index (e.g. `0`, `1`). Omit to run without camera. |
| `CAMERA_RESOLUTION` | no | camera default | Resolution string e.g. `1280x720` |
| `CAMERA_CAPTURES_FOLDER` | no | `captures` | Directory for saved images and videos |
| `CAMERA_DB_PATH` | no | `puda.db` | Path to the SQLite database for image persistence |
| `OPENROUTER_API_KEY` | no | unset | API key for OpenRouter (reserved for future use) |

---

## Run

```powershell
# From repo root
uv sync --all-packages
uv run --package opentrons-edge python edge/main.py
```

The service retries automatically on fatal errors (5 s backoff) and continues on `KeyboardInterrupt` to keep running in unattended environments.

---

## Available NATS commands

Send commands via `puda_comms.CommandService` with `machine_id` matching your `.env`.
Command names map directly to `Opentrons` driver method names.

| Command | Key params | Description |
|---|---|---|
| `upload_and_run` | `code` (str), `filename?`, `wait?`, `max_wait?`, `poll_interval?` | Upload and run a protocol |
| `get_status` | `run_id?` (str) | Get run status (latest run if omitted) |
| `pause` | `run_id` (str) | Pause a running protocol |
| `resume` | `run_id` (str) | Resume a paused protocol |
| `stop` | `run_id` (str) | Stop / cancel a run |
| `upload_labware` | `labware` (dict) | Upload a custom labware definition |
| `capture_image` | `filename?` (str) | Capture from the external USB camera; returns path, saved flag, base64 JPEG, and dimensions |
| `capture_robot_image` | `filename?` (str) | Capture from the OT-2's on-board camera via `POST /camera/picture`; same return format |
| `is_connected` | — | Check robot reachability |
| `get_labware_types` | — | List known labware load-names |
| `get_pipette_types` | — | List known pipette instrument names |

Immediate commands (sent via `send_immediate_command`): `pause`, `resume`, `cancel`, `reset`.

---

## Running a protocol via NATS

`edge/run_water_transfer.py` is a ready-to-run example. It builds an OT-2 protocol using `Protocol` / `ProtocolCommand`, generates the Python code, and dispatches it via NATS:

```powershell
.venv\Scripts\python.exe edge\run_water_transfer.py
```

### Deck layout

| Slot | Labware | Role |
|---|---|---|
| 11 | `opentrons_96_tiprack_300ul` | Tip rack |
| 5 | `corning_96_wellplate_360ul_flat` | Mixing plate |
| 6 | `corning_96_wellplate_360ul_flat` | Water plate |

Pipette: **P300 single gen2** — right mount.
Action: transfer **100 µL** from `water_plate[A1]` → `mixing_plate[A1]`.

### Sending your own protocol

```python
import asyncio, uuid
from puda_comms import CommandService
from puda_comms.models import CommandRequest
from opentrons_driver.protocol import Protocol, ProtocolCommand

NATS_SERVERS = ["nats://100.109.131.12:4222", "nats://100.109.131.12:4223", "nats://100.109.131.12:4224"]
MACHINE_ID   = "opentrons"

async def main():
    code = Protocol(
        protocol_name="My Protocol",
        author="Lab",
        description="...",
        robot_type="OT-2",
        api_level="2.23",
        commands=[
            ProtocolCommand(command_type="load_labware", params={
                "name": "tiprack", "labware_type": "opentrons_96_tiprack_300ul", "location": "11"
            }),
            ProtocolCommand(command_type="load_instrument", params={
                "name": "p300", "instrument_type": "p300_single_gen2",
                "mount": "right", "tip_racks": ["tiprack"],
            }),
            # add more commands ...
        ],
    ).to_python_code()

    async with CommandService(servers=NATS_SERVERS) as svc:
        reply = await svc.send_queue_commands(
            requests=[
                CommandRequest(
                    name="upload_and_run",
                    machine_id=MACHINE_ID,
                    params={"code": code, "wait": True},
                    step_number=1,
                )
            ],
            run_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            username="lab",
            timeout=360,
        )
    print(reply.response.status, (reply.response.data or {}).get("run_status"))

asyncio.run(main())
```

`send_queue_commands` automatically sends `START` before the command sequence and `COMPLETE` after, managing the run lifecycle on your behalf.

---

## Telemetry

Published every second to these subjects (replace `opentrons` with your `MACHINE_ID`):

| Subject | Content |
|---|---|
| `puda.opentrons.tlm.heartbeat` | Timestamp heartbeat |
| `puda.opentrons.tlm.pos` | Position payload (empty `{}` for OT-2) |
| `puda.opentrons.tlm.health` | Full run status dict from `Opentrons.get_status()` |
| `puda.opentrons.tlm.state` | Current execution state (`idle` / `busy` / `error`) |

---

## Requirements

| Dependency | Version | Purpose |
|---|---|---|
| Python | >= 3.14 | — |
| `opentrons-driver` | >= 0.1.0 | Local OT-2 driver (workspace package) |
| `puda-comms` | == 0.0.10 | NATS `EdgeRunner` / `EdgeNatsClient` |
| `pydantic` | >= 2.12.5 | Settings model validation |
| `pydantic-settings` | >= 2.12.0 | `.env` loading |
| `python-dotenv` | >= 1.2.1 | `.env` file support |
