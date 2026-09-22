# Opentrons OT-2 Edge

PUDA edge service for an Opentrons OT-2 robot. Run this project from this folder.

## Layout

```text
opentron_OT2/
|-- pyproject.toml
|-- uv.lock
|-- user-guide.md
|-- main.py
|-- driver.py
|-- protocol.py
|-- .env.example
|-- start_edge.bat
`-- labware/
```

## Setup

```bash
uv sync
copy .env.example .env
```

Edit `.env`:

```env
MACHINE_ID=opentrons-ot2
OPENTRONS_IP=<ot2_ip>
NATS_SERVERS=<nats_servers>
```

## Run

```bash
uv run opentrons-ot2-edge
```

On Windows you can also run `start_edge.bat`.

See [user-guide.md](user-guide.md) for lab setup.
