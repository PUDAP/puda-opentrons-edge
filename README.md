# Opentrons OT-2 Edge Workspace

Python workspace for controlling an Opentrons OT-2 robot through PUDA.

This repository contains one root Python package:

| Package | Purpose |
|---|---|
| `opentrons-edge` | PUDA/NATS edge service plus the bundled `opentrons` Python package for the OT-2 HTTP API. |

For a lab-user setup walkthrough, see [user-guide.md](user-guide.md).

## Repository Layout

```text
Opentron_dev/
|-- pyproject.toml
|-- uv.lock
|-- user-guide.md
|-- main.py
|-- .env.example
|-- EDGE.md
|-- DRIVER.md
`-- opentrons/
    |-- __init__.py
    |-- driver.py
    |-- protocol.py
    `-- labware/
```

## Requirements

- Python 3.14 or newer
- [`uv`](https://docs.astral.sh/uv/)
- A reachable Opentrons OT-2 robot
- A reachable NATS server for PUDA edge operation

## Setup

Install the root project from the repository root:

```bash
uv sync
```

This creates `.venv/` and installs `opentrons-edge` plus the bundled `opentrons` package in editable mode.

## Configure the Edge Service

Create the local environment file:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
copy .env.example .env
```

Edit `.env`:

```env
MACHINE_ID=opentrons-ot2
OPENTRONS_IP=<opentrons_ip>
NATS_SERVERS=<nats_servers>
```

| Variable | Description |
|---|---|
| `MACHINE_ID` | Robot identifier used on the PUDA/NATS bus. |
| `NATS_SERVERS` | Comma-separated NATS server URLs. |
| `OPENTRONS_IP` | OT-2 robot IP address from the Opentrons App. |

Do not commit `.env`; it contains lab-specific network settings.

## Run the Edge Service

From the repository root:

```bash
uv run opentrons-edge
```

Keep the process running while PUDA is sending robot commands.

## Development

Install dependencies:

```bash
uv sync
```

Run the edge service:

```bash
uv run opentrons-edge
```

## Documentation

- [User guide](user-guide.md) - setup guide for lab users and technicians.
