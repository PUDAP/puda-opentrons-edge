# User Guide: Opentrons Setup for PUDA

> **Audience:** Lab users, technicians, and project owners who want to set up an Opentrons robot for PUDA.
>
> **Goal:** Install and connect the Opentrons-specific parts of the system:
>
> 1. **Opentrons App** - the official Opentrons desktop application used to connect to, manage, and calibrate the robot.
> 2. **Opentrons Edge** - the local service that bridges PUDA and the robot; includes the Opentrons robot driver.
> 3. **Opentrons Robot Driver** - the Python driver used by the Edge service to call the robot.

---

## PUDA Prerequisite

Before following this Opentrons setup guide, install and configure PUDA by following the official PUDA documentation:

 

After PUDA is installed, confirm that NATS is running or reachable before starting the Opentrons Edge service.

---

## 1. Install Basic Software

### 1.1 Install the Opentrons App

The Opentrons App is the official desktop application for connecting to and managing your Opentrons robot. Install this first so you can confirm the robot is reachable and record its IP address.

1. Go to the official Opentrons download page:
  - [https://opentrons.com/ot-app/](https://opentrons.com/ot-app/)
2. Download the installer for your operating system.
3. Run the installer and follow the on-screen instructions.
4. Open the Opentrons App after installation completes.

### 1.2 Connect to the robot and record the IP address

Follow the official Opentrons first-run setup guide for connecting and preparing the robot:

[https://docs.opentrons.com/ot-2/installation/first-run/](https://docs.opentrons.com/ot-2/installation/first-run/)

1. Connect your Opentrons robot by USB, Wi-Fi, or Ethernet.
2. In the Opentrons App, look for your robot in the device list.
3. Confirm the robot appears as connected or reachable.
4. Open the robot settings or network panel and note the robot's IP address.

### 1.3 Calibrate the Opentrons robot

Use the Opentrons App to complete robot calibration before running PUDA-controlled protocols.

Follow the official Opentrons robot calibration guide:

[https://docs.opentrons.com/ot-2/calibration/robot-calibration/](https://docs.opentrons.com/ot-2/calibration/robot-calibration/)

Complete the calibration steps required for your robot, pipettes, tip racks, and labware before starting the Opentrons Edge service.

### 1.4 Install Python

PUDA and the Edge service use Python.

1. Install Python 3.14 or newer.
2. During installation, enable **Add Python to PATH** if the installer shows this option.
3. Open a terminal:
  - Windows: PowerShell
  - macOS: Terminal
  - Linux: Terminal
4. Check Python:

```bash
python --version
```

If that does not work, try:

```bash
python3 --version
```

Expected result:

```text
Python 3.14.x
```

or newer.

### 1.5 Install `uv`

`uv` is used to install and run the Edge service reliably.

macOS/Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Verify:

```bash
uv --version
```

---

## 2. Set Up Opentrons Edge

The Edge service is the bridge between PUDA and the robot.

### 2.1 Get the Opentrons package folder

Your PUDA administrator may provide an existing Opentrons folder, ZIP file, or repository.

Expected repository structure:

```text
opentrons/
|-- pyproject.toml
|-- uv.lock
|-- driver/
|   |-- pyproject.toml
|   `-- src/opentrons_driver/
`-- edge/
    |-- main.py
    |-- pyproject.toml
    |-- README.md
    `-- .env.example
```

If your organization has a GitHub repository, download it as a ZIP file or clone it if Git is available.

### 2.2 Create the Edge configuration file

From the main `opentrons` folder:

```bash
cp edge/.env.example edge/.env
```

On Windows PowerShell:

```powershell
copy edge\.env.example edge\.env
```

Open `edge/.env` in a text editor.

Template:

```env
MACHINE_ID=opentrons
OPENTRONS_IP=<robot-ip-address>
NATS_SERVERS=nats://localhost:4222
```

Example:

```env
MACHINE_ID=opentrons
OPENTRONS_IP=192.168.1.25
NATS_SERVERS=nats://localhost:4222
```

Field meanings:


| Field                    | Meaning                                                       |
| ------------------------ | ------------------------------------------------------------- |
| `MACHINE_ID`             | The name PUDA uses for this robot                             |
| `OPENTRONS_IP`           | The robot IP address from the Opentrons App                   |
| `NATS_SERVERS`           | The message server used by PUDA and Edge                      |


> **Important:** Do not share `.env` files publicly. They may contain private lab network settings.

### 2.3 Install Edge dependencies and the Opentrons robot driver

From the main `opentrons` folder, run:

```bash
uv sync --all-packages
```

This installs both workspace packages:

- `opentrons-driver`
- `opentrons-edge`

You do not need to install the Opentrons robot driver manually. It is part of this workspace and is installed automatically by `uv sync --all-packages`.

Verification:

- [ ] Dependency installation finishes without errors.
- [ ] A `.venv` folder is created automatically.

### 2.4 Confirm the Opentrons robot driver is installed

After running `uv sync --all-packages`, verify the driver:

```bash
uv run python -c "import opentrons_driver; print('Opentrons driver ready')"
```

Expected output:

```text
Opentrons driver ready
```

If this fails, check that you are running the command from the main `opentrons` folder and re-run:

```bash
uv sync --all-packages
```

### 2.5 Add custom labware

Use this section when your protocol needs labware that is not one of the standard Opentrons labware types.

Custom labware definitions are JSON files. Add the definition file to this package so the Opentrons driver can discover it.

#### Add the labware definition file

Place the custom labware JSON file in:

```text
driver/src/opentrons_driver/labware/
```

Example:

```text
driver/src/opentrons_driver/labware/my_custom_plate_1.json
```

The file must include `parameters.loadName`. The driver uses this value as the labware type name.

Example JSON fields to check:

```json
{
  "namespace": "custom",
  "version": 1,
  "metadata": {
    "displayName": "My Custom Plate"
  },
  "parameters": {
    "loadName": "my_custom_plate_1"
  }
}
```

#### Confirm the labware is discovered

From the main `opentrons` folder, run:

```bash
uv run python -c "from opentrons_driver.protocol import get_labware_types; print(get_labware_types())"
```

Confirm your `parameters.loadName` value appears in the printed list.

#### Use the custom labware in a protocol

Use the `parameters.loadName` value as `labware_type` when loading labware.

Example:

```python
ProtocolCommand(command_type="load_labware", params={
    "name": "custom_plate",
    "labware_type": "my_custom_plate_1",
    "location": "3",
})
```

When a custom labware type is found in `driver/src/opentrons_driver/labware/`, the protocol builder generates an Opentrons `load_labware_from_definition()` call automatically.

After adding or changing a labware JSON file, restart the Opentrons Edge service so it reloads the labware list.

Custom labware is embedded into generated protocols with Opentrons `load_labware_from_definition()`, so a separate labware upload step is not required.

---

## 3. Start Required Services

Confirm NATS is already running or reachable, then start Opentrons Edge.

### 3.1 Start the Opentrons Edge service

Open a second terminal window. From the main `opentrons` folder, run:

```bash
uv run --package opentrons-edge python edge/main.py
```

Expected output includes:

```text
OT2 machine initialized successfully
NATS client initialized successfully
Edge Service Ready
```

Keep this terminal open while using PUDA.

> **Important:** Run only one Edge service for the same robot at a time. Duplicate processes cause confusing command errors.

### 3.2 Start the Opentrons Edge service with Docker

The repository also includes a Docker setup adapted from `PUDAP/edge-python-template`.

From the main `opentrons` folder, create the configuration file if it does not exist:

```powershell
copy edge\.env.example edge\.env
```

Edit `edge/.env`, then build and start the container:

```powershell
docker compose --env-file edge/.env -f compose.yml up -d --build
```

View logs:

```powershell
docker compose --env-file edge/.env -f compose.yml logs -f edge
```

Stop the container:

```powershell
docker compose --env-file edge/.env -f compose.yml down
```

### 3.3 Start livestream cameras

The livestream stack uses MediaMTX and ffmpeg to stream two Linux V4L2 cameras.
This is intended for a Linux Docker host with `/dev/video*` devices.

Add or update these values in `edge/.env`:

```env
TAILSCALE_IP=<host-tailscale-ip-or-lan-ip>
VIDEO_DEVICE_0=/dev/video0
VIDEO_DEVICE_1=/dev/video1
```

Start the livestream stack:

```bash
docker compose --env-file edge/.env -f compose.livestream.yml up -d
```

Stop the livestream stack:

```bash
docker compose --env-file edge/.env -f compose.livestream.yml down
```

Stream endpoints:

| Stream | Endpoint |
|---|---|
| RTMP `cam0` | `rtmp://<host>:1935/cam0` |
| RTMP `cam1` | `rtmp://<host>:1935/cam1` |
| HLS `cam0` | `http://<host>:8888/cam0` |
| HLS `cam1` | `http://<host>:8888/cam1` |
| WebRTC/WHEP `cam0` | `http://<host>:8889/cam0` |
| WebRTC/WHEP `cam1` | `http://<host>:8889/cam1` |

---

## 4. Create or Run an Opentrons Protocol using PUDA

There are two common ways to create and run an Opentrons protocol:

- **Option 1:** Use an agentic IDE such as Cursor or VS Code.
- **Option 2:** Use a messaging platform to create and execute the protocol using natural language.

### Option 1 - Use an agentic IDE

Use this option when your lab allows an agentic IDE to help create protocol files and run them through PUDA or the Opentrons Edge service.

An agentic IDE is a code editor with an AI assistant that can read the project folder, create files, edit protocols, and run commands when you approve them. Examples include Cursor, VS Code with an agent, or another lab-approved AI coding environment.

#### 4.1 Open the correct project folder

Open the main folder that contains both the PUDA project and the Opentrons package, or open the specific PUDA project folder if your lab keeps them separate.

Before asking the agent to create anything, confirm:

- [ ] The PUDA project folder is visible.
- [ ] The `protocols/` folder is visible.
- [ ] The Opentrons package folder is visible, if needed.
- [ ] NATS is running or reachable.
- [ ] The Opentrons Edge service is running and shows "Ready".

#### 4.2 Ask the agent to create the protocol

Use plain language, but include exact lab details. A good request includes:

- Robot model.
- Pipette type and mount.
- Labware names and deck slots.
- Source wells and destination wells.
- Transfer volumes.
- Tip rack location.
- Whether to do a dry run or real liquid transfer.
- Output file name.

Example prompt:

```text
Create an Opentrons OT-2 protocol for PUDA.

Use a P300 single-channel pipette on the right mount.
Use an Opentrons 300 uL tip rack in slot 11.
Use a 12-channel reservoir in slot 2 as the water source.
Use a 96-well plate in slot 3 as the destination.
Transfer 100 uL of water from reservoir well A1 to plate wells A1 through A6.
Save the protocol in the protocols folder as water_transfer_a1_to_a6.py.
Do not run the protocol until I approve it.
```

#### 4.3 Review the generated protocol

Before allowing the agent to run anything, ask it to summarize the protocol in plain language.

Check:

- [ ] Correct robot type.
- [ ] Correct pipette and mount.
- [ ] Correct labware names.
- [ ] Correct deck slots.
- [ ] Correct source and destination wells.
- [ ] Correct liquid volumes.
- [ ] No unexpected movements or extra steps.
- [ ] The file is saved in the expected `protocols/` folder.

Ask the agent to fix any mistake before continuing.

#### 4.4 Execute the protocol

After review, ask the agent to run the protocol using your lab's approved PUDA command or workflow.

Example prompt:

```text
Run protocols/water_transfer_a1_to_a6.py through PUDA using the opentrons machine.
Use the existing NATS and Opentrons Edge configuration.
Show me the command before running it.
```

If your lab uses a specific command, include it directly:

```text
Use this command to run the protocol:
puda run protocols/water_transfer_a1_to_a6.py
```

The agent should show the command, wait for your approval if required, and then execute it.

### Option 2 - Use a messaging platform with natural language

Use this option when your lab runs PUDA through a chat or messaging interface instead of a local agentic IDE.

#### 4.5 Start from the messaging interface

Open the approved messaging platform and select the PUDA or Opentrons agent for your lab.

Before asking it to run anything, provide the robot name and setup context:

```text
Use the Opentrons robot named opentrons.
Use the existing PUDA and NATS configuration.
Do not run any robot movement until I approve it.
```

#### 4.6 Describe the protocol

Send the protocol request in natural language with exact lab details.

Example prompt:

```text
Create an Opentrons OT-2 protocol for PUDA.

Use a P300 single-channel pipette on the right mount.
Use an Opentrons 300 uL tip rack in slot 11.
Use a 12-channel reservoir in slot 2 as the water source.
Use a 96-well plate in slot 3 as the destination.
Transfer 100 uL of water from reservoir well A1 to plate wells A1 through A6.
Use the opentrons machine.
Do not run the protocol until I approve it.
```

#### 4.7 Review and approve execution

Ask the messaging agent to summarize:

- The deck layout.
- The pipette and mount.
- The source and destination wells.
- The transfer volume.
- The command or workflow it will use to run the protocol through PUDA.

Only approve execution after the summary matches the physical robot deck.

#### 4.8 Watch the first run

During the first run:

- Stay near the robot.
- Watch the first few movements carefully.
- Be ready to pause or stop the run.
- Record any unexpected behavior in `project.md` or your lab notebook.

> **Safety rule:** Stay near the robot during the first run. Be ready to pause or stop the robot if labware, tips, or liquid positions are wrong.

---

## 5. Troubleshooting Opentrons Setup

For other hardware troubleshooting, refer to the official Opentrons OT-2 troubleshooting website:

[https://support.opentrons.com/s/ot-2/troubleshooting](https://support.opentrons.com/s/ot-2/troubleshooting)

### 5.1 Robot does not appear in the Opentrons App

Check:

- The robot is powered on.
- The computer and robot are on the same network.
- USB, Ethernet, or Wi-Fi is connected correctly.
- The Opentrons App has been restarted after changing network settings.
- The robot IP address was copied from the correct robot.

If the robot IP address changed, update `OPENTRONS_IP` in `edge/.env`, then restart the Opentrons Edge service.

### 5.2 Custom labware does not appear

Check:

- The JSON file is in `driver/src/opentrons_driver/labware/`.
- The file ends in `.json`.
- The JSON includes `parameters.loadName`.
- The `loadName` is the same value used as `labware_type` in the protocol.
- The Opentrons Edge service was restarted after adding or editing the file.

Confirm discovery:

```bash
uv run python -c "from opentrons_driver.protocol import get_labware_types; print(get_labware_types())"
```

### 5.3 Protocol starts but robot movement is wrong

Stop the run and check:

- Deck slot numbers.
- Pipette type and mount.
- Tip rack location.
- Source and destination well names.
- Transfer volumes.
- Labware calibration and physical placement.

Do not continue until the protocol has been reviewed against the physical deck.

### 5.4 Commands fail or behave inconsistently

Possible cause: more than one Opentrons Edge service is running for the same robot.

Fix:

- Close duplicate Edge terminal windows.
- Keep only one Edge service running for the robot.
- Restart NATS if needed.
- Restart the Opentrons Edge service.
