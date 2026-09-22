# Opentrons Flex Edge

PUDA edge service for an Opentrons Flex robot. Run this project from this folder.

Protocols follow the [Opentrons Python Protocol API](https://docs.opentrons.com/python-api/).

## Layout

```text
opentron_flex/
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
MACHINE_ID=opentrons-flex
OPENTRONS_IP=<flex_ip>
NATS_SERVERS=<nats_servers>
```

## Run

```bash
uv run opentrons-flex-edge
```

On Windows you can also run `start_edge.bat`.

See [user-guide.md](user-guide.md) for lab setup.
