# puda-opentrons-edge

PUDA edge services for Opentrons robots. This repository contains two independent Python projects — one per robot — so you install and run only the machine you have.

| Folder | Robot | Driver class | Run command |
|---|---|---|---|
| [`opentron_OT2/`](opentron_OT2/) | Opentrons OT-2 | `opentron_OT2` | `uv run opentrons-ot2-edge` |
| [`opentron_flex/`](opentron_flex/) | Opentrons Flex | `opentron_flex` | `uv run opentrons-flex-edge` |

Each folder is a full edge service: NATS client, HTTP robot driver, protocol builder, and labware definitions.

## Repository layout

```text
puda-opentrons-edge/
|-- README.md
|-- .gitignore
|-- opentron_OT2/
|   |-- main.py
|   |-- driver.py
|   |-- protocol.py
|   |-- pyproject.toml
|   |-- .env.example
|   |-- start_edge.bat
|   |-- user-guide.md
|   `-- labware/
`-- opentron_flex/
    |-- main.py
    |-- driver.py
    |-- protocol.py
    |-- pyproject.toml
    |-- .env.example
    |-- start_edge.bat
    |-- user-guide.md
    `-- labware/
```

## Requirements

- Python 3.14 or newer
- [`uv`](https://docs.astral.sh/uv/)
- A reachable OT-2 or Flex robot (HTTP API, port `31950`)
- A reachable NATS server for PUDA

## Quick start

Work **inside the folder for your robot**. Do not run `uv sync` from this repository root.

### OT-2

```bash
cd opentron_OT2
uv sync
copy .env.example .env
```

Edit `.env` (`MACHINE_ID`, `OPENTRONS_IP`, `NATS_SERVERS`), then:

```bash
uv run opentrons-ot2-edge
```

On Windows you can run `start_edge.bat` instead.

Docs: [opentron_OT2/README.md](opentron_OT2/README.md), [opentron_OT2/user-guide.md](opentron_OT2/user-guide.md)

### Flex

```bash
cd opentron_flex
uv sync
copy .env.example .env
```

Edit `.env` (`MACHINE_ID`, `OPENTRONS_IP`, `NATS_SERVERS`), then:

```bash
uv run opentrons-flex-edge
```

On Windows you can run `start_edge.bat` instead.

Docs: [opentron_flex/README.md](opentron_flex/README.md), [opentron_flex/user-guide.md](opentron_flex/user-guide.md)

Flex protocols follow the [Opentrons Python Protocol API](https://docs.opentrons.com/python-api/) (`robotType: Flex`, coordinate deck slots, Flex pipettes, trash bin, optional gripper).

## Configuration

Do not commit `.env`. It is lab-specific.

| Variable | Description |
|---|---|
| `MACHINE_ID` | Robot identifier on the PUDA/NATS bus |
| `OPENTRONS_IP` | Robot IP from the Opentrons App |
| `NATS_SERVERS` | Comma-separated NATS server URLs |
