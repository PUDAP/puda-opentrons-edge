"""Opentrons Flex protocol builder, uploader, and labware/pipette resources.

Public API
----------
- :class:`Protocol` / :class:`ProtocolCommand` — build Flex protocols programmatically
  and generate runnable Python code via ``Protocol.to_python_code()``.
- :func:`upload_protocol` — preprocess and upload a protocol file to the robot.
- :func:`get_labware_types` / :func:`get_pipette_types` — enumerate known names.
- :data:`BUILTIN_LABWARE`

Generated protocols follow the Opentrons Python Protocol API for Flex
(https://docs.opentrons.com/python-api/): ``robotType: Flex``, coordinate
deck slots (A1–D4), Flex pipette load names, ``load_trash_bin``, and
optional gripper ``move_labware``.

Example::

    from protocol import Protocol, ProtocolCommand, upload_protocol

    protocol = Protocol(
        protocol_name="Simple Transfer",
        author="Lab",
        description="Transfer 100 µL from A1 to B1",
        robot_type="Flex",
        api_level="2.29",
        commands=[
            ProtocolCommand(command_type="load_labware", params={
                "name": "plate", "labware_type": "corning_96_wellplate_360ul_flat", "location": "D1"
            }),
            ProtocolCommand(command_type="load_labware", params={
                "name": "tiprack", "labware_type": "opentrons_flex_96_tiprack_200ul", "location": "D2"
            }),
            ProtocolCommand(command_type="load_trash_bin", params={
                "name": "trash", "location": "A3"
            }),
            ProtocolCommand(command_type="load_instrument", params={
                "name": "p1000", "instrument_type": "flex_1channel_1000",
                "mount": "left", "tip_racks": ["tiprack"],
            }),
            ProtocolCommand(command_type="transfer", params={
                "pipette": "p1000", "volume": 100,
                "source_labware": "plate", "source_well": "A1",
                "dest_labware": "plate",  "dest_well":   "B1",
            }),
        ],
    )
    protocol_id = upload_protocol(client, protocol.to_python_code())
"""

from __future__ import annotations

import ast
import json
import logging
import os
from pprint import pformat
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)
_TIP_WELL_ORDER = tuple(f"{row}{col}" for row in "ABCDEFGH" for col in range(1, 13))


# ===========================================================================
# Labware & pipette catalogues
# ===========================================================================

_LABWARE_DIR = Path(__file__).parent / "labware"


def _normalise_labware_definition(definition: dict[str, Any]) -> None:
    """Normalise schema-sensitive custom labware metadata in place."""
    metadata = definition.get("metadata")
    if not isinstance(metadata, dict):
        return

    unit = metadata.get("displayVolumeUnits")
    if isinstance(unit, str) and unit.strip() in {"uL", "ul", "µL", "ÂµL"}:
        metadata["displayVolumeUnits"] = "\u00b5L"


def _load_builtin_labware() -> dict[str, dict]:
    """Load all labware JSON files from the labware/ directory."""
    definitions: dict[str, dict] = {}
    for json_file in sorted(_LABWARE_DIR.glob("*.json")):
        try:
            with open(json_file, encoding="utf-8") as f:
                definition = json.load(f)
            _normalise_labware_definition(definition)
            load_name = definition.get("parameters", {}).get("loadName", "")
            if not load_name:
                logger.warning(
                    "Skipping labware file '%s' — missing 'parameters.loadName'.",
                    json_file.name,
                )
                continue
            definitions[load_name] = definition
        except Exception as exc:
            logger.warning("Failed to load labware file '%s': %s", json_file.name, exc)
    return definitions


BUILTIN_LABWARE: dict[str, dict] = _load_builtin_labware()

_STANDARD_LABWARE_TYPES: list[str] = [
    "corning_96_wellplate_360ul_flat",
    "corning_384_wellplate_112ul_flat",
    "nest_12_reservoir_15ml",
    "nest_96_wellplate_100ul_pcr_full_skirt",
    "nest_96_wellplate_200ul_flat",
    "opentrons_flex_96_tiprack_50ul",
    "opentrons_flex_96_tiprack_200ul",
    "opentrons_flex_96_tiprack_1000ul",
    "opentrons_flex_96_filtertiprack_50ul",
    "opentrons_flex_96_filtertiprack_200ul",
    "opentrons_flex_96_filtertiprack_1000ul",
    "opentrons_flex_96_tiprack_adapter",
]

LABWARE_TYPES: list[str] = _STANDARD_LABWARE_TYPES + [
    name for name in BUILTIN_LABWARE if name not in _STANDARD_LABWARE_TYPES
]

PIPETTE_TYPES: list[str] = [
    "flex_1channel_50",
    "flex_1channel_1000",
    "flex_8channel_50",
    "flex_8channel_1000",
    "flex_96channel_1000",
]

MODULE_TYPES: list[str] = [
    "temperature module gen2",
    "thermocycler module gen2",
    "heaterShakerModuleV1",
    "magneticBlockV1",
    "absorbanceReaderV1",
    "flexStackerModuleV1",
]

DECK_SLOTS: list[str] = [
    f"{row}{col}" for row in "ABCD" for col in range(1, 5)
]

_DECK_SLOT_RE = re.compile(r"^[A-D][1-4]$")


def get_labware_types() -> list[str]:
    """
    Return all known labware load-names.

    Combines the standard Opentrons built-in list with any custom definitions
    discovered in the labware/ directory at import time.  No robot connection
    is required.

    Returns:
        Labware load-names usable in load_labware protocol commands.
        Examples: "corning_96_wellplate_360ul_flat",
                  "opentrons_flex_96_tiprack_200ul"
    """
    return list(LABWARE_TYPES)


def get_pipette_types() -> list[str]:
    """
    Return all known pipette instrument names.

    Lists every supported pipette model name usable in load_instrument
    protocol commands.  No robot connection is required.

    Returns:
        Pipette instrument names.
        Examples: "flex_1channel_1000", "flex_8channel_50", "flex_96channel_1000"
    """
    return list(PIPETTE_TYPES)


def get_module_types() -> list[str]:
    """Return Flex-compatible hardware module load names."""
    return list(MODULE_TYPES)


def get_deck_slots() -> list[str]:
    """Return Flex coordinate deck slots (A1–D4)."""
    return list(DECK_SLOTS)


# ===========================================================================
# Code generation helpers
# ===========================================================================

def _render_str_dict(d: dict, indent: int = 4) -> str:
    """Render a dict whose values are all strings as a Python dict literal."""
    lines = ["{"]
    for i, (k, v) in enumerate(d.items()):
        comma = "," if i < len(d) - 1 else ""
        lines.append(f'{" " * indent}"{k}": "{v}"{comma}')
    lines.append("}")
    return "\n".join(lines)


def _render_python_dict(d: dict, indent: int = 8) -> str:
    """Render *d* as a Python dict literal for embedding in generated protocols."""
    return pformat(d, indent=indent, width=100, sort_dicts=False).replace("µL", "\\u00b5L")


def _get_first(params: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    """Return the value of the first key found in *params*, or *default*."""
    return next((params[k] for k in keys if k in params), default)


def _location_expr(
    labware_name: str,
    well_expr: str,
    ref: Optional[str],
    offset: Optional[Any],
) -> str:
    """Build a pipette location expression, optionally with ``.top()``/``.bottom()`` offset."""
    base = f"labware[{repr(labware_name)}][{well_expr}]"
    if not ref:
        return base
    ref_lower = str(ref).lower()
    if ref_lower not in ("top", "bottom"):
        return base
    if offset is None or (isinstance(offset, str) and not offset.strip()):
        return f"{base}.{ref_lower}()"
    if isinstance(offset, str) and offset.strip().startswith("row["):
        return f"{base}.{ref_lower}({offset})"
    try:
        return f"{base}.{ref_lower}({float(offset)})"
    except (ValueError, TypeError):
        return f"{base}.{ref_lower}()"


def _vol_str(val: Any) -> str:
    """Format a volume value — CSV row expressions pass through; others become float strings."""
    if isinstance(val, str) and val.strip().startswith("row["):
        return val
    return str(float(val))


def _well_str(val: Any) -> str:
    """Format a well identifier — CSV row expressions pass through; others are quoted."""
    if isinstance(val, str) and val.strip().startswith("row["):
        return val
    return f"'{val}'"


def _rate_str(params: dict, key: str) -> str:
    """Return ``', rate=X'`` if *key* is present in *params*, else an empty string."""
    if key not in params:
        return ""
    v = params[key]
    return f", rate={v}" if (isinstance(v, str) and v.strip().startswith("row[")) else f", rate={float(v)}"


# ===========================================================================
# Pydantic models
# ===========================================================================

class ProtocolCommand(BaseModel):
    """
    A single step in an Opentrons protocol.

    Each command maps to one robot action (load labware, pick up tip, transfer
    liquid, etc.).  Commands are collected in a Protocol and converted to
    runnable Python source code via Protocol.to_python_code().

    Attributes:
        command_type (str): Action to perform. Supported values:
            "load_labware", "load_instrument", "load_trash_bin",
            "load_waste_chute", "load_module", "move_labware",
            "define_liquid", "load_liquid", "pick_up_tip", "drop_tip",
            "aspirate", "dispense", "blow_out", "mix", "air_gap",
            "touch_tip", "transfer", "move_to", "flow_rate", "delay",
            "pause", "home", "comment", "set_rail_lights", "capture_image",
            "loop", "loop_over_csv", "read_csv_file", "read_csv"
        params (dict): Parameters for the command. Required keys vary by
            command_type — see Protocol command reference for details.

    Example:
        ProtocolCommand(
            command_type="transfer",
            params={
                "pipette": "p300",
                "volume": 100,
                "source_labware": "water_plate",
                "source_well": "A1",
                "dest_labware": "mixing_plate",
                "dest_well": "A1",
            }
        )
    """

    command_type: str
    params: dict[str, Any]


class Protocol(BaseModel):
    """
    Full Opentrons Flex protocol definition.

    Holds all metadata and the ordered list of ProtocolCommand steps.
    Call to_python_code() to generate runnable Opentrons Python source
    that can be uploaded directly to the robot.

    Attributes:
        protocol_name (str): Human-readable name shown in the robot's run history.
        author (str):        Author name embedded in the protocol metadata.
        description (str):   Short description embedded in the protocol metadata.
        robot_type (str):    Robot model string, e.g. "Flex".
        api_level (str):     Opentrons API level string, e.g. "2.29".
        commands (list[ProtocolCommand]): Ordered list of protocol steps.

    Example:
        protocol = Protocol(
            protocol_name="Water Transfer",
            author="Lab",
            description="Transfer 100 µL from water plate to mixing plate",
            robot_type="Flex",
            api_level="2.29",
            commands=[
                ProtocolCommand(command_type="load_labware", params={
                    "name": "tiprack",
                    "labware_type": "opentrons_flex_96_tiprack_200ul",
                    "location": "D2",
                }),
                ProtocolCommand(command_type="load_trash_bin", params={
                    "name": "trash",
                    "location": "A3",
                }),
                ProtocolCommand(command_type="load_instrument", params={
                    "name": "p1000",
                    "instrument_type": "flex_1channel_1000",
                    "mount": "left",
                    "tip_racks": ["tiprack"],
                }),
                ProtocolCommand(command_type="transfer", params={
                    "pipette": "p1000", "volume": 100,
                    "source_labware": "water_plate", "source_well": "A1",
                    "dest_labware": "mixing_plate",  "dest_well":   "A1",
                }),
            ],
        )
        code = protocol.to_python_code()
    """

    protocol_name: str
    author: str
    description: str
    robot_type: str = "Flex"
    api_level: str = "2.29"
    commands: list[ProtocolCommand]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def to_python_code(self) -> str:
        """
        Convert this protocol definition to valid Opentrons Python source code.

        Processes every ProtocolCommand in order and renders them as Python
        statements inside a run(protocol) function.  CSV data-read snippets
        are inserted automatically after the labware/instrument setup block.
        DataFrame-style column references (df.loc[i, 'col']) are normalised
        to row['col'] for compatibility with the loop command.

        Returns:
            Complete Opentrons Python protocol source, ready to be passed
            to opentron_flex.upload_and_run() or upload_protocol().
        """
        body, data_read_code = self._build_body()
        code = self._build_header() + body
        if data_read_code:
            code = self._insert_data_read(code, data_read_code)
        return re.sub(r"df\.loc\[i, *'([^']+)'\]", r"row['\1']", code)

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    def _build_header(self) -> str:
        """
        Generate the protocol file header.

        Produces the import statement, metadata dict, requirements dict, and the
        opening of the run() function with empty labware and pipettes dicts.

        Returns:
            Python source string for the protocol header.
        """
        metadata = _render_str_dict({
            "protocolName": self.protocol_name,
            "author": self.author,
            "description": self.description,
        })
        requirements = _render_str_dict({
            "robotType": self.robot_type,
            "apiLevel": self.api_level,
        })
        return (
            f"from opentrons import protocol_api\n\n\n"
            f"metadata = {metadata}\n\n"
            f"requirements = {requirements}\n\n\n"
            f"def run(protocol: protocol_api.ProtocolContext):\n"
            f"    labware = {{}}\n"
            f"    pipettes = {{}}\n"
            f"    fixtures = {{}}\n"
            f"    modules = {{}}\n"
            f"    liquids = {{}}\n\n"
        )

    def _build_body(self) -> tuple[str, str]:
        """
        Generate the protocol body by iterating over all commands.

        Commands that fail to render are replaced with an inline error comment so
        the rest of the protocol remains usable.

        Returns:
            Tuple of (body, data_read_code). body is the rendered Python statements;
            data_read_code is the CSV-loading snippet to inject after the setup block,
            or an empty string when no CSV command is present.
        """
        body = ""
        data_read_code = ""

        for i, cmd in enumerate(self.commands):
            try:
                body += self._process_command(cmd, i)

                if cmd.command_type in ("read_csv_file", "read_csv") and "file_path" in cmd.params:
                    p = cmd.params
                    if "csv_data" in p:
                        data_read_code = (
                            f"    # CSV data embedded in protocol\n"
                            f"    data = {json.dumps(p['csv_data'], indent=4)}\n\n"
                        )
                    else:
                        data_read_code = (
                            f"    import pandas as pd\n"
                            f"    data = pd.read_csv('{p['file_path']}').to_dict(orient='records')\n\n"
                        )

            except Exception as exc:
                logger.warning("Failed to render command %d (%s): %s", i + 1, cmd.command_type, exc)
                body += f"    # Error processing command {i + 1} ({cmd.command_type}): {exc}\n\n"

        return body, data_read_code

    @staticmethod
    def _insert_data_read(code: str, data_read_code: str) -> str:
        """
        Insert the CSV data-read snippet after the last load_labware/load_instrument line.

        Args:
            code: Full protocol source assembled so far.
            data_read_code: The data-loading snippet to inject.

        Returns:
            Protocol source with the snippet inserted at the correct position
            inside the run() function.
        """
        lines = code.split("\n")
        insert_index = len(lines)
        for j, line in enumerate(lines):
            if "load_instrument" in line or "load_labware" in line:
                insert_index = j + 2
        lines.insert(insert_index, data_read_code.rstrip())
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def _process_command(self, cmd: ProtocolCommand, idx: int, indent: int = 4) -> str:  # noqa: C901
        """
        Dispatch a single ProtocolCommand to the appropriate code generator.

        Args:
            cmd: The command to render.
            idx: Zero-based index of the command in the protocol (used in error messages).
            indent: Indentation level in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python source line(s) for this command, including trailing newlines.
            Returns an inline comment if the command_type is unrecognised.
        """
        ct = cmd.command_type

        if ct in ("read_csv_file", "read_csv"):
            return ""
        if ct in ("loop", "loop_over_csv"):
            return self._gen_loop(cmd)
        if ct == "load_labware":
            return self._gen_load_labware(cmd, idx)
        if ct == "load_instrument":
            return self._gen_load_instrument(cmd, idx)
        if ct == "load_trash_bin":
            return self._gen_load_trash_bin(cmd, idx)
        if ct == "load_waste_chute":
            return self._gen_load_waste_chute(cmd, idx)
        if ct == "load_module":
            return self._gen_load_module(cmd, idx)
        if ct == "move_labware":
            return self._gen_move_labware(cmd, idx, indent=indent)
        if ct == "define_liquid":
            return self._gen_define_liquid(cmd, idx)
        if ct == "load_liquid":
            return self._gen_load_liquid(cmd, idx, indent=indent)
        if ct == "pause":
            return self._gen_pause(cmd, indent=indent)
        if ct == "set_rail_lights":
            return self._gen_set_rail_lights(cmd, indent=indent)
        if ct == "capture_image":
            return self._gen_capture_image(cmd, indent=indent)
        if ct == "pick_up_tip":
            return self._gen_pick_up_tip(cmd, idx, indent=indent)
        if ct == "drop_tip":
            return self._gen_drop_tip(cmd, idx, indent=indent)
        if ct == "aspirate":
            return self._gen_aspirate(cmd, idx, indent=indent)
        if ct == "dispense":
            return self._gen_dispense(cmd, idx, indent=indent)
        if ct == "blow_out":
            return self._gen_blow_out(cmd, idx, indent=indent)
        if ct == "mix":
            return self._gen_mix(cmd, idx, indent=indent)
        if ct == "air_gap":
            return self._gen_air_gap(cmd, idx, indent=indent)
        if ct == "touch_tip":
            return self._gen_touch_tip(cmd, idx, indent=indent)
        if ct == "transfer":
            return self._gen_transfer(cmd, idx)
        if ct == "move_to":
            return self._gen_move_to(cmd, idx, indent=indent)
        if ct == "flow_rate":
            return self._gen_flow_rate(cmd, idx, indent=indent)
        if ct == "delay":
            return self._gen_delay(cmd, indent=indent)
        if ct == "home":
            return self._gen_home(indent=indent)
        if ct == "comment":
            return self._gen_comment(cmd, indent=indent)

        logger.warning("Unknown command type '%s' at command %d — skipping", ct, idx + 1)
        pad = " " * indent
        tail = "\n\n" if indent == 4 else "\n"
        return f"{pad}# Unknown command type: {ct} at command {idx + 1}{tail}"

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    def _gen_loop(self, cmd: ProtocolCommand) -> str:
        """
        Generate a for-loop over CSV data rows.

        Expects cmd.params to contain 'commands': a list of ProtocolCommand dicts
        to execute for each row in the data list.

        Args:
            cmd: Command with command_type "loop" or "loop_over_csv".

        Returns:
            Python for-loop source iterating over the data list.
        """
        lines = ["    for row in data:\n"]
        for subcmd_raw in cmd.params.get("commands", []):
            subcmd = ProtocolCommand(**subcmd_raw) if isinstance(subcmd_raw, dict) else subcmd_raw
            lines.append(self._process_command(subcmd, 0, indent=8))
        lines.append("\n")
        return "".join(lines)

    # ------------------------------------------------------------------
    # Setup — load labware / instruments
    # ------------------------------------------------------------------

    def _gen_load_labware(self, cmd: ProtocolCommand, idx: int) -> str:
        """
        Generate a protocol.load_labware() or load_labware_from_definition() call.

        Expects cmd.params to contain 'name' (variable name, e.g. "tiprack"),
        'labware_type' (load-name, e.g. "opentrons_flex_96_tiprack_200ul"), and
        'location' (Flex deck slot as a string, e.g. "D1").

        Args:
            cmd: Command with command_type "load_labware".
            idx: Command index used in error comments.

        Returns:
            Python source that assigns the labware into labware[name].
            Uses load_labware_from_definition() for JSON definitions discovered
            in labware/; load_labware() for standard Opentrons labware.
        """
        p = cmd.params
        if not all(k in p for k in ("name", "labware_type", "location")):
            return (
                f"    # Error: Missing required params for load_labware at command {idx + 1}\n"
                f"    # Required: name, labware_type, location — got: {p}\n\n"
            )
        labware_type = p["labware_type"].split("/", 1)[-1]
        name_r = repr(p["name"])
        loc_r = repr(p["location"])

        if labware_type in BUILTIN_LABWARE:
            defn = BUILTIN_LABWARE[labware_type]
            var = f"_{labware_type}_def"
            display = defn["metadata"]["displayName"]
            return (
                f"    # Load {display}\n"
                f"    {var} = {_render_python_dict(defn, indent=8)}\n"
                f"    labware[{name_r}] = protocol.load_labware_from_definition(\n"
                f"        {var}, location={loc_r}\n"
                f"    )\n\n"
            )

        return (
            f"    labware[{name_r}] = protocol.load_labware(\n"
            f"        {repr(labware_type)}, location={loc_r}\n"
            f"    )\n\n"
        )

    def _gen_load_instrument(self, cmd: ProtocolCommand, idx: int) -> str:
        """
        Generate a protocol.load_instrument() call.

        Expects cmd.params to contain 'name' (variable name, e.g. "p1000"),
        'instrument_type' (pipette model, e.g. "flex_1channel_1000"), and
        'mount' ("left", "right", or "left" for 96-channel). Optionally 'tip_racks' (list of labware
        variable names to pass as tip racks).

        Args:
            cmd: Command with command_type "load_instrument".
            idx: Command index used in error comments.

        Returns:
            Python source that assigns the pipette into pipettes[name].
        """
        p = cmd.params
        if not all(k in p for k in ("name", "instrument_type", "mount")):
            return (
                f"    # Error: Missing required params for load_instrument at command {idx + 1}\n"
                f"    # Required: name, instrument_type, mount — got: {p}\n\n"
            )
        tip_racks = p.get("tip_racks", [])
        tip_racks_str = ", ".join(f"labware[{repr(r)}]" for r in tip_racks)
        lines = [
            f"    pipettes[{repr(p['name'])}] = protocol.load_instrument(\n",
            f"        {repr(p['instrument_type'])},\n",
            f"        mount={repr(p['mount'])}",
        ]
        if tip_racks:
            lines.append(f",\n        tip_racks=[{tip_racks_str}]")
        lines.append("\n    )\n\n")
        return "".join(lines)

    def _gen_load_trash_bin(self, cmd: ProtocolCommand, idx: int) -> str:
        """Generate protocol.load_trash_bin() (required on Flex API 2.16+)."""
        p = cmd.params
        name = p.get("name", "trash")
        location = p.get("location", "A3")
        if "location" not in p:
            return (
                f"    # Error: Missing required params for load_trash_bin at command {idx + 1}\n"
                f"    # Required: location — got: {p}\n\n"
            )
        return (
            f"    fixtures[{repr(name)}] = protocol.load_trash_bin(location={repr(location)})\n\n"
        )

    def _gen_load_waste_chute(self, cmd: ProtocolCommand, idx: int) -> str:
        """Generate protocol.load_waste_chute() for Flex D3 waste chute."""
        p = cmd.params
        name = p.get("name", "waste_chute")
        return f"    fixtures[{repr(name)}] = protocol.load_waste_chute()\n\n"

    def _gen_load_module(self, cmd: ProtocolCommand, idx: int) -> str:
        """Generate protocol.load_module() for Flex hardware modules."""
        p = cmd.params
        if not all(k in p for k in ("name", "module_type", "location")):
            return (
                f"    # Error: Missing required params for load_module at command {idx + 1}\n"
                f"    # Required: name, module_type, location — got: {p}\n\n"
            )
        return (
            f"    modules[{repr(p['name'])}] = protocol.load_module(\n"
            f"        {repr(p['module_type'])}, location={repr(p['location'])}\n"
            f"    )\n\n"
        )

    @staticmethod
    def _flex_location_expr(location: Any) -> str:
        """Render a Flex move/load location: deck slot, OFF_DECK, labware, or module."""
        loc = str(location)
        if loc.upper() in {"OFF_DECK", "off_deck"}:
            return "protocol_api.OFF_DECK"
        if _DECK_SLOT_RE.match(loc) or loc.isdigit():
            return repr(loc)
        if loc.startswith("modules["):
            return loc
        return f"labware[{repr(loc)}]"

    def _gen_move_labware(self, cmd: ProtocolCommand, idx: int, *, indent: int = 4) -> str:
        """Generate protocol.move_labware() with optional Flex gripper."""
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        labware_name = _get_first(p, ["labware", "name"])
        new_location = _get_first(p, ["new_location", "location", "destination"])
        if labware_name is None or new_location is None:
            return (
                f"{pad}# Error: Missing required params for move_labware at command {idx + 1}\n"
                f"{pad}# Required: labware, new_location — got: {p}{tail}"
            )
        use_gripper = p.get("use_gripper", True)
        dest = self._flex_location_expr(new_location)
        return (
            f"{pad}protocol.move_labware(\n"
            f"{pad}    labware[{repr(labware_name)}], {dest}, use_gripper={bool(use_gripper)}\n"
            f"{pad}){tail}"
        )

    def _gen_define_liquid(self, cmd: ProtocolCommand, idx: int) -> str:
        """Generate protocol.define_liquid()."""
        p = cmd.params
        name = p.get("name")
        if not name:
            return f"    # Error: Missing 'name' for define_liquid at command {idx + 1}\n\n"
        liquid_name = p.get("liquid_name", name)
        args = [f"name={repr(liquid_name)}"]
        if "description" in p:
            args.append(f"description={repr(p['description'])}")
        if "display_color" in p:
            args.append(f"display_color={repr(p['display_color'])}")
        return (
            f"    liquids[{repr(name)}] = protocol.define_liquid({', '.join(args)})\n\n"
        )

    def _gen_load_liquid(self, cmd: ProtocolCommand, idx: int, *, indent: int = 4) -> str:
        """Generate labware.load_liquid() for a well or all wells."""
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        labware_name = _get_first(p, ["labware", "labware_name"])
        liquid_name = _get_first(p, ["liquid", "liquid_name"])
        volume = p.get("volume")
        if labware_name is None or liquid_name is None or volume is None:
            return (
                f"{pad}# Error: Missing required params for load_liquid at command {idx + 1}\n"
                f"{pad}# Required: labware, liquid, volume — got: {p}{tail}"
            )
        well = p.get("well")
        target = f"labware[{repr(labware_name)}]"
        if well:
            target = f"{target}[{_well_str(well)}]"
        return (
            f"{pad}{target}.load_liquid(liquid=liquids[{repr(liquid_name)}], volume={_vol_str(volume)}){tail}"
        )

    def _gen_pause(self, cmd: ProtocolCommand, *, indent: int = 4) -> str:
        """Generate protocol.pause()."""
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        msg = cmd.params.get("message") or cmd.params.get("text")
        if msg:
            return f"{pad}protocol.pause({repr(msg)}){tail}"
        return f"{pad}protocol.pause(){tail}"

    def _gen_set_rail_lights(self, cmd: ProtocolCommand, *, indent: int = 4) -> str:
        """Generate protocol.set_rail_lights()."""
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        on = cmd.params.get("on", True)
        return f"{pad}protocol.set_rail_lights({bool(on)}){tail}"

    def _gen_capture_image(self, cmd: ProtocolCommand, *, indent: int = 4) -> str:
        """Generate protocol.capture_image() for Flex camera during a run."""
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        kwargs: list[str] = []
        if "home_before" in p:
            kwargs.append(f"home_before={bool(p['home_before'])}")
        if "filename" in p:
            kwargs.append(f"filename={repr(p['filename'])}")
        arg = ", ".join(kwargs)
        return f"{pad}protocol.capture_image({arg}){tail}"

    # ------------------------------------------------------------------
    # Tip handling
    # ------------------------------------------------------------------

    def _gen_pick_up_tip(self, cmd: ProtocolCommand, idx: int = 0, *, indent: int = 4) -> str:
        """
        Generate a pick_up_tip() call.

        Expects cmd.params to contain 'pipette' (variable name, e.g. "p300").
        Optionally 'labware' and 'well' to pick from a specific well; both must
        be provided together.

        Args:
            cmd: Command with command_type "pick_up_tip".
            idx: Command index used in error comments.
            indent: Indentation in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python source for the pick_up_tip() call.
        """
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        if "pipette" not in p:
            return f"{pad}# Error: Missing 'pipette' for pick_up_tip at command {idx + 1}\n"
        if "well" in p and "labware" in p:
            return f"{pad}pipettes['{p['pipette']}'].pick_up_tip(labware['{p['labware']}']['{p['well']}']){tail}"
        return f"{pad}pipettes['{p['pipette']}'].pick_up_tip(){tail}"

    def _gen_drop_tip(self, cmd: ProtocolCommand, idx: int = 0, *, indent: int = 4) -> str:
        """
        Generate a drop_tip() call.

        Expects cmd.params to contain 'pipette' (variable name, e.g. "p300").
        Optionally 'labware' and 'well' to drop into a specific well; both must
        be provided together.

        Args:
            cmd: Command with command_type "drop_tip".
            idx: Command index used in error comments.
            indent: Indentation in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python source for the drop_tip() call.
        """
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        if "pipette" not in p:
            return f"{pad}# Error: Missing 'pipette' for drop_tip at command {idx + 1}\n"
        if "well" in p and "labware" in p:
            return f"{pad}pipettes['{p['pipette']}'].drop_tip(labware['{p['labware']}']['{p['well']}']){tail}"
        return f"{pad}pipettes['{p['pipette']}'].drop_tip(){tail}"

    # ------------------------------------------------------------------
    # Liquid handling
    # ------------------------------------------------------------------

    def _gen_aspirate(self, cmd: ProtocolCommand, idx: int = 0, *, indent: int = 4) -> str:
        """
        Generate an aspirate() call.

        Expects cmd.params to contain 'pipette' and 'volume'. Optionally 'labware'
        and 'well' to aspirate from a specific location, 'aspirate_ref' ("top" or
        "bottom"), 'aspirate_offset' (Z offset in mm), and 'aspirate_rate' (flow
        rate multiplier).

        Args:
            cmd: Command with command_type "aspirate".
            idx: Command index used in error comments.
            indent: Indentation in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python source for the aspirate() call.
        """
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        if "pipette" not in p or "volume" not in p:
            return f"{pad}# Error: Missing pipette/volume for aspirate at command {idx + 1}\n"
        vol = _vol_str(p["volume"])
        rate = _rate_str(p, "aspirate_rate")
        if "labware" in p and "well" in p:
            loc = _location_expr(
                p["labware"], _well_str(p["well"]),
                _get_first(p, ["aspirate_ref", "aspirate_position", "aspirate_height_ref", "position", "ref"]),
                _get_first(p, ["aspirate_offset", "aspirate_height", "aspirate_z_offset", "offset", "z_offset"]),
            )
            return f"{pad}pipettes['{p['pipette']}'].aspirate({vol}, {loc}{rate}){tail}"
        return f"{pad}pipettes['{p['pipette']}'].aspirate({vol}{rate}){tail}"

    def _gen_dispense(self, cmd: ProtocolCommand, idx: int = 0, *, indent: int = 4) -> str:
        """
        Generate a dispense() call.

        Expects cmd.params to contain 'pipette' and 'volume'. Optionally 'labware'
        and 'well' to dispense into a specific location, 'dispense_ref' ("top" or
        "bottom"), 'dispense_offset' (Z offset in mm), and 'dispense_rate' (flow
        rate multiplier).

        Args:
            cmd: Command with command_type "dispense".
            idx: Command index used in error comments.
            indent: Indentation in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python source for the dispense() call.
        """
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        if "pipette" not in p or "volume" not in p:
            return f"{pad}# Error: Missing pipette/volume for dispense at command {idx + 1}\n"
        vol = _vol_str(p["volume"])
        rate = _rate_str(p, "dispense_rate")
        if "labware" in p and "well" in p:
            loc = _location_expr(
                p["labware"], _well_str(p["well"]),
                _get_first(p, ["dispense_ref", "dispense_position", "dispense_height_ref", "position", "ref"]),
                _get_first(p, ["dispense_offset", "dispense_height", "dispense_z_offset", "offset", "z_offset"]),
            )
            return f"{pad}pipettes['{p['pipette']}'].dispense({vol}, {loc}{rate}){tail}"
        return f"{pad}pipettes['{p['pipette']}'].dispense({vol}{rate}){tail}"

    def _gen_blow_out(self, cmd: ProtocolCommand, idx: int = 0, *, indent: int = 4) -> str:
        """
        Generate a blow_out() call.

        Expects cmd.params to contain 'pipette'. Optionally 'labware' and 'well'
        to blow out into a specific location, 'blow_ref' ("top" or "bottom"), and
        'blow_offset' (Z offset in mm).

        Args:
            cmd: Command with command_type "blow_out".
            idx: Command index used in error comments.
            indent: Indentation in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python source for the blow_out() call.
        """
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        if "pipette" not in p:
            return f"{pad}# Error: Missing 'pipette' for blow_out at command {idx + 1}\n"
        if "labware" in p and "well" in p:
            loc = _location_expr(
                p["labware"], _well_str(p["well"]),
                _get_first(p, ["blow_ref", "blow_position", "blow_height_ref", "position", "ref"]),
                _get_first(p, ["blow_offset", "blow_height", "blow_z_offset", "offset", "z_offset"]),
            )
            return f"{pad}pipettes['{p['pipette']}'].blow_out({loc}){tail}"
        return f"{pad}pipettes['{p['pipette']}'].blow_out(){tail}"

    def _gen_mix(self, cmd: ProtocolCommand, idx: int = 0, *, indent: int = 4) -> str:
        """
        Generate a mix() call.

        Expects cmd.params to contain 'pipette'. Optionally 'repetitions' (number
        of mix cycles, default 3), 'volume' (µL, default 100), 'labware' and 'well'
        to mix at a specific location, 'mix_ref' ("top" or "bottom"), and
        'mix_offset' (Z offset in mm).

        Args:
            cmd: Command with command_type "mix".
            idx: Command index used in error comments.
            indent: Indentation in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python source for the mix() call.
        """
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        if "pipette" not in p:
            return f"{pad}# Error: Missing 'pipette' for mix at command {idx + 1}\n"
        reps_param = p.get("repetitions", 3)
        reps = (
            reps_param
            if isinstance(reps_param, str) and reps_param.strip().startswith("row[")
            else str(int(reps_param))
        )
        vol = _vol_str(p.get("volume", 100))
        if "labware" in p and "well" in p:
            loc = _location_expr(
                p["labware"], _well_str(p["well"]),
                _get_first(p, ["mix_ref", "mix_position", "mix_height_ref", "position", "ref"]),
                _get_first(p, ["mix_offset", "mix_height", "mix_z_offset", "offset", "z_offset"]),
            )
            return f"{pad}pipettes['{p['pipette']}'].mix({reps}, {vol}, {loc}){tail}"
        return f"{pad}pipettes['{p['pipette']}'].mix({reps}, {vol}){tail}"

    def _gen_air_gap(self, cmd: ProtocolCommand, idx: int = 0, *, indent: int = 4) -> str:
        """
        Generate an air_gap() call.

        Expects cmd.params to contain 'pipette'. Optionally 'volume' (µL, default
        10) and 'height' (mm above liquid, default 5).

        Args:
            cmd: Command with command_type "air_gap".
            idx: Command index used in error comments.
            indent: Indentation in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python source for the air_gap() call.
        """
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        if "pipette" not in p:
            return f"{pad}# Error: Missing 'pipette' for air_gap at command {idx + 1}\n"
        vol = _vol_str(p.get("volume", 10))
        height = _vol_str(p.get("height", 5))
        return f"{pad}pipettes['{p['pipette']}'].air_gap({vol}, height={height}){tail}"

    def _gen_touch_tip(self, cmd: ProtocolCommand, idx: int = 0, *, indent: int = 4) -> str:
        """
        Generate a touch_tip() call.

        Expects cmd.params to contain 'pipette'. Optionally 'labware' and 'well'
        to touch at a specific well, 'radius' (fractional well radius, default 1.0),
        'v_offset' (mm from well top, default -1), and 'speed' (mm/s, default 60).

        Args:
            cmd: Command with command_type "touch_tip".
            idx: Command index used in error comments.
            indent: Indentation in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python source for the touch_tip() call.
        """
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        if "pipette" not in p:
            return f"{pad}# Error: Missing 'pipette' for touch_tip at command {idx + 1}\n"
        if "labware" in p and "well" in p:
            well = _well_str(p["well"])
            radius = p.get("radius", 1.0)
            v_offset = p.get("v_offset", -1)
            speed = p.get("speed", 60)
            return (
                f"{pad}pipettes['{p['pipette']}'].touch_tip("
                f"labware['{p['labware']}'][{well}], "
                f"radius={radius}, v_offset={v_offset}, speed={speed}){tail}"
            )
        return f"{pad}pipettes['{p['pipette']}'].touch_tip(){tail}"

    # ------------------------------------------------------------------
    # Transfer (compound: handles multi-chunk volumes automatically)
    # ------------------------------------------------------------------

    def _gen_transfer(self, cmd: ProtocolCommand, idx: int) -> str:  # noqa: C901
        """
        Generate a transfer with automatic volume chunking.

        Expects cmd.params to contain 'pipette', 'volume', 'source_labware',
        'source_well', 'dest_labware', and 'dest_well'. Optionally 'rate' (unified
        flow rate multiplier), 'aspirate_rate' / 'dispense_rate' (per-direction
        multipliers), and source/dest position refs and offsets.

        When aspirate_rate or dispense_rate are set, falls back to explicit
        aspirate/dispense calls via _gen_transfer_separate; otherwise uses the
        built-in pipette.transfer() API via _gen_transfer_builtin. Volumes
        exceeding the pipette's max capacity are automatically chunked into loops.

        Args:
            cmd: Command with command_type "transfer".
            idx: Command index used in error comments.

        Returns:
            Python source for the transfer operation.
        """
        p = cmd.params
        required = ("pipette", "volume", "source_labware", "source_well", "dest_labware", "dest_well")
        if not all(k in p for k in required):
            return (
                f"    # Error: Missing required params for transfer at command {idx + 1}\n"
                f"    # Required: {', '.join(required)}\n\n"
            )

        pipette = p["pipette"]
        volume = float(p["volume"])
        max_vol = 1000 if "p1000" in pipette else 300 if "p300" in pipette else 20

        src = _location_expr(
            p["source_labware"], f"'{p['source_well']}'",
            _get_first(p, ["source_ref", "source_position", "source_height_ref"]),
            _get_first(p, ["source_offset", "source_height", "source_z_offset"]),
        )
        dst = _location_expr(
            p["dest_labware"], f"'{p['dest_well']}'",
            _get_first(p, ["dest_ref", "dest_position", "dest_height_ref"]),
            _get_first(p, ["dest_offset", "dest_height", "dest_z_offset"]),
        )

        transfer_rate = p.get("rate")
        aspirate_rate = p.get("aspirate_rate")
        dispense_rate = p.get("dispense_rate")
        use_separate = (aspirate_rate is not None or dispense_rate is not None) and transfer_rate is None

        if use_separate:
            return self._gen_transfer_separate(pipette, volume, max_vol, src, dst, aspirate_rate, dispense_rate)
        return self._gen_transfer_builtin(pipette, volume, max_vol, src, dst, transfer_rate)

    def _gen_transfer_separate(
        self,
        pipette: str,
        volume: float,
        max_vol: int,
        src: str,
        dst: str,
        aspirate_rate: Any,
        dispense_rate: Any,
    ) -> str:
        """
        Generate an explicit pick_up_tip / aspirate / dispense / drop_tip sequence.

        Used when aspirate_rate and/or dispense_rate are set independently.
        Volumes exceeding max_vol are split into a for-loop with a remainder step.

        Args:
            pipette: Pipette variable name.
            volume: Total volume to transfer in µL.
            max_vol: Maximum single-aspirate volume for this pipette.
            src: Rendered source location expression.
            dst: Rendered destination location expression.
            aspirate_rate: Aspirate rate multiplier, or None.
            dispense_rate: Dispense rate multiplier, or None.

        Returns:
            Python source for the explicit transfer sequence.
        """
        asp_r = f", rate={aspirate_rate}" if aspirate_rate is not None else ""
        dsp_r = f", rate={dispense_rate}" if dispense_rate is not None else ""

        if volume <= max_vol:
            return (
                f"    pipettes['{pipette}'].pick_up_tip()\n"
                f"    pipettes['{pipette}'].aspirate({volume}, {src}{asp_r})\n"
                f"    pipettes['{pipette}'].dispense({volume}, {dst}{dsp_r})\n"
                f"    pipettes['{pipette}'].drop_tip()\n\n"
            )

        iterations = int(volume // max_vol)
        remainder = volume % max_vol
        lines = [f"    pipettes['{pipette}'].pick_up_tip()\n"]
        if iterations:
            lines += [
                f"    for _ in range({iterations}):\n",
                f"        pipettes['{pipette}'].aspirate({max_vol}, {src}{asp_r})\n",
                f"        pipettes['{pipette}'].dispense({max_vol}, {dst}{dsp_r})\n",
            ]
        if remainder:
            lines += [
                f"    pipettes['{pipette}'].aspirate({remainder}, {src}{asp_r})\n",
                f"    pipettes['{pipette}'].dispense({remainder}, {dst}{dsp_r})\n",
            ]
        lines.append(f"    pipettes['{pipette}'].drop_tip()\n\n")
        return "".join(lines)

    def _gen_transfer_builtin(
        self,
        pipette: str,
        volume: float,
        max_vol: int,
        src: str,
        dst: str,
        rate: Any,
    ) -> str:
        """
        Generate a pipette.transfer() call using the Opentrons built-in transfer API.

        Used when a unified rate (or no rate) is set. Volumes exceeding max_vol
        are split into a for-loop using new_tip='once'.

        Args:
            pipette: Pipette variable name.
            volume: Total volume to transfer in µL.
            max_vol: Maximum single-transfer volume for this pipette.
            src: Rendered source location expression.
            dst: Rendered destination location expression.
            rate: Unified flow rate multiplier, or None.

        Returns:
            Python source for the transfer() call or chunked loop.
        """
        rate_param = f", rate={rate}" if rate is not None else ""

        if volume <= max_vol:
            return f"    pipettes['{pipette}'].transfer({volume}, {src}, {dst}{rate_param})\n\n"

        iterations = int(volume // max_vol)
        remainder = volume % max_vol
        lines = []
        if iterations:
            lines += [
                f"    for _ in range({iterations}):\n",
                f"        pipettes['{pipette}'].transfer({max_vol}, {src}, {dst}, new_tip='once'{rate_param})\n",
            ]
        if remainder:
            lines.append(
                f"    pipettes['{pipette}'].transfer({remainder}, {src}, {dst}, new_tip='once'{rate_param})\n"
            )
        lines.append("\n")
        return "".join(lines)

    # ------------------------------------------------------------------
    # Movement & flow control
    # ------------------------------------------------------------------

    def _gen_move_to(self, cmd: ProtocolCommand, idx: int = 0, *, indent: int = 4) -> str:
        """
        Generate a move_to() call.

        Expects cmd.params to contain 'pipette', 'labware', and 'well'. Optionally
        'move_ref' ("top" or "bottom", default "top") and 'move_offset' (Z offset
        in mm; defaults to 10 when ref is "top" and no offset is provided).

        Args:
            cmd: Command with command_type "move_to".
            idx: Command index used in error comments.
            indent: Indentation in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python source for the move_to() call.
        """
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        if not all(k in p for k in ("pipette", "labware", "well")):
            return f"{pad}# Error: Missing required params for move_to at command {idx + 1}\n"
        ref = _get_first(p, ["move_ref", "move_position", "move_height_ref", "position", "ref"]) or "top"
        offset = _get_first(p, ["move_offset", "move_height", "move_z_offset", "offset", "z_offset"])
        if offset is None and ref == "top":
            offset = 10
        loc = _location_expr(p["labware"], _well_str(p["well"]), ref, offset)
        return f"{pad}pipettes['{p['pipette']}'].move_to({loc}){tail}"

    def _gen_flow_rate(self, cmd: ProtocolCommand, idx: int = 0, *, indent: int = 4) -> str:
        """
        Generate flow_rate assignment statements.

        Expects cmd.params to contain 'pipette' and at least one of 'aspirate',
        'dispense', or 'blow_out' (all in µL/s).

        Args:
            cmd: Command with command_type "flow_rate".
            idx: Command index used in error comments.
            indent: Indentation in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python source setting pipette.flow_rate.aspirate / .dispense / .blow_out.
        """
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        if "pipette" not in p:
            return f"{pad}# Error: Missing 'pipette' for flow_rate at command {idx + 1}\n"
        asp, dsp, blow = p.get("aspirate"), p.get("dispense"), p.get("blow_out")
        if asp is None and dsp is None and blow is None:
            return f"{pad}# Warning: flow_rate called with no rate parameters\n"
        lines = []
        if asp is not None:
            lines.append(f"{pad}pipettes['{p['pipette']}'].flow_rate.aspirate = {asp}  # µL/s")
        if dsp is not None:
            lines.append(f"{pad}pipettes['{p['pipette']}'].flow_rate.dispense = {dsp}  # µL/s")
        if blow is not None:
            lines.append(f"{pad}pipettes['{p['pipette']}'].flow_rate.blow_out = {blow}  # µL/s")
        return "\n".join(lines) + tail

    def _gen_delay(self, cmd: ProtocolCommand, *, indent: int = 4) -> str:
        """
        Generate a protocol.delay() or pipette.delay() call with an optional comment.

        Expects cmd.params to optionally contain 'seconds', 'minutes', 'message'
        (printed to the robot display before the delay), and 'pipette' (if set,
        uses pipette.delay() instead of protocol.delay()). Defaults to
        delay(seconds=1) when no duration params are provided.

        Args:
            cmd: Command with command_type "delay".
            indent: Indentation in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python source for the delay call, preceded by protocol.comment() if
            a message param is present.
        """
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        p = cmd.params
        seconds = _get_first(p, ["seconds"])
        minutes = _get_first(p, ["minutes"])
        message = _get_first(p, ["message", "text", "comment"])
        pipette = _get_first(p, ["pipette"])
        lines = []
        if message:
            lines.append(f"{pad}protocol.comment({repr(str(message))})")
        if pipette:
            args = []
            if minutes is not None:
                args.append(f"minutes={minutes}")
            if seconds is not None:
                args.append(f"seconds={seconds}")
            lines.append(f"{pad}pipettes['{pipette}'].delay({', '.join(args) or 'seconds=1'})")
        else:
            if minutes is not None and seconds is not None:
                lines.append(f"{pad}protocol.delay(minutes={minutes}, seconds={seconds})")
            elif minutes is not None:
                lines.append(f"{pad}protocol.delay(minutes={minutes})")
            elif seconds is not None:
                lines.append(f"{pad}protocol.delay(seconds={seconds})")
            else:
                lines.append(f"{pad}protocol.delay(seconds=1)")
        return "\n".join(lines) + tail

    def _gen_home(self, *, indent: int = 4) -> str:
        """
        Generate a protocol.home() call to home all robot axes.

        Args:
            indent: Indentation in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python source for protocol.home(), preceded by a comment at top level.
        """
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        if indent == 4:
            return f"{pad}# Home all robot axes\n{pad}protocol.home(){tail}"
        return f"{pad}protocol.home(){tail}"

    @staticmethod
    def _gen_comment(cmd: ProtocolCommand, *, indent: int = 4) -> str:
        """
        Generate an inline comment line in the protocol source.

        Expects cmd.params to contain 'text' (the comment string to embed).

        Args:
            cmd: Command with command_type "comment".
            indent: Indentation in spaces; 4 for top-level, 8 inside a loop.

        Returns:
            Python comment line (# text).
        """
        pad, tail = " " * indent, "\n\n" if indent == 4 else "\n"
        return f"{pad}# {cmd.params.get('text', '')}{tail}"


# ===========================================================================
# Protocol preprocessing & upload
# ===========================================================================

def preprocess_protocol_code(code: str) -> str:
    """
    Fix common issues in protocol source code before uploading to the robot.

    Applied automatically by upload_protocol() — calling this manually is
    not normally required.

    Transformations applied:
        - Strips markdown fences (```python ... ```)
        - Inserts "from opentrons import protocol_api" if missing
        - Re-indents lines inside run() that are missing the 4-space indent
        - Replaces df.loc[i, 'col'] / data.loc[i, 'col'] with row['col']

    Args:
        code: Raw Opentrons Python protocol source code.

    Returns:
        Cleaned and normalised protocol source code.
    """
    code = re.sub(r"```python\s*", "", code)
    code = re.sub(r"```\s*", "", code)

    if "from opentrons import protocol_api" not in code:
        lines = code.split("\n")
        insert_pos = 0
        for j, line in enumerate(lines):
            if line.strip().startswith(("import ", "from ")):
                insert_pos = j + 1
            elif line.strip() and not line.strip().startswith("#"):
                break
        lines.insert(insert_pos, "from opentrons import protocol_api")
        code = "\n".join(lines)

    lines = code.split("\n")
    in_run = False
    corrected: list[str] = []
    for line in lines:
        if line.strip().startswith("def run("):
            in_run = True
            corrected.append(line)
        elif in_run and line.strip() and not line.startswith(("    ", "\t")):
            in_run = False
            corrected.append(line)
        elif in_run and line.strip() and not line.startswith("    "):
            corrected.append("    " + line.lstrip())
        else:
            corrected.append(line)
    code = "\n".join(corrected)

    code = re.sub(r"df\.loc\[i,\s*['\"]([^'\"]+)['\"]\]", r"row['\1']", code)
    code = re.sub(r"data\.loc\[i,\s*['\"]([^'\"]+)['\"]\]", r"row['\1']", code)
    return code


def apply_tip_tracking(
    code: str,
    tip_offsets: dict[tuple[str, ...], int],
) -> tuple[str, dict[tuple[str, ...], int]]:
    """
    Rewrite implicit ``pick_up_tip()`` calls to explicit tip wells.

    The traversal order is row-wise: ``A1`` → ``A2`` → ... → ``A12`` → ``B1``.
    Offsets are grouped by the tuple of tip-rack variable names attached to each
    pipette so successive protocol uploads can continue from the next unused tip.

    Args:
        code: Preprocessed Opentrons protocol source.
        tip_offsets: Existing consumed-tip counts keyed by tip-rack groups.

    Returns:
        Tuple of ``(rewritten_code, advances)`` where ``advances`` contains how
        many tips each rack group would consume if the protocol starts.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        logger.warning("Tip tracking skipped because protocol code could not be parsed.")
        return code, {}

    run_fn = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "run"
        ),
        None,
    )
    if run_fn is None:
        return code, {}

    tiprack_names: set[str] = set()
    pipette_tipracks: dict[str, tuple[str, ...]] = {}

    for node in run_fn.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target_name = ast.unparse(node.targets[0]).strip()
        value = node.value
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
            continue

        if (
            value.func.attr == "load_labware"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
            and "tiprack" in value.args[0].value
        ):
            tiprack_names.add(target_name)
            continue

        if value.func.attr != "load_instrument":
            continue

        tip_racks_exprs: tuple[str, ...] = ()
        for keyword in value.keywords:
            if keyword.arg != "tip_racks" or not isinstance(keyword.value, ast.List):
                continue
            tip_racks_exprs = tuple(
                ast.unparse(elt).strip()
                for elt in keyword.value.elts
                if ast.unparse(elt).strip() in tiprack_names
            )
            break
        if tip_racks_exprs:
            pipette_tipracks[target_name] = tip_racks_exprs

    if not pipette_tipracks:
        return code, {}

    advances: dict[tuple[str, ...], int] = {}
    start_offsets = {
        key: tip_offsets.get(key, 0)
        for key in set(pipette_tipracks.values())
    }
    wells_per_rack = len(_TIP_WELL_ORDER)

    class _TipTrackingTransformer(ast.NodeTransformer):
        def visit_Call(self, node: ast.Call) -> ast.AST:
            self.generic_visit(node)
            if (
                not isinstance(node.func, ast.Attribute)
                or node.func.attr != "pick_up_tip"
                or node.args
                or node.keywords
            ):
                return node

            pipette_name = ast.unparse(node.func.value).strip()
            rack_group = pipette_tipracks.get(pipette_name)
            if not rack_group:
                return node

            used = advances.get(rack_group, 0)
            absolute_index = start_offsets.get(rack_group, 0) + used
            rack_index, well_index = divmod(absolute_index, wells_per_rack)
            if rack_index >= len(rack_group):
                raise RuntimeError(
                    f"Out of tracked tips for {pipette_name}; add another tip rack or reset tracking."
                )

            tip_expr = ast.parse(
                f"{rack_group[rack_index]}[{_TIP_WELL_ORDER[well_index]!r}]",
                mode="eval",
            ).body
            advances[rack_group] = used + 1
            return ast.copy_location(
                ast.Call(func=node.func, args=[tip_expr], keywords=[]),
                node,
            )

    transformed = _TipTrackingTransformer().visit(tree)
    ast.fix_missing_locations(transformed)
    return ast.unparse(transformed), advances


def commit_tip_advances(
    tip_offsets: dict[tuple[str, ...], int],
    advances: dict[tuple[str, ...], int],
) -> None:
    """Persist consumed-tip counts after the protocol has started successfully."""
    for rack_group, used in advances.items():
        tip_offsets[rack_group] = tip_offsets.get(rack_group, 0) + used


def upload_protocol(client: Any, code: str, filename: str = "protocol.py") -> str:
    """
    Preprocess and upload a protocol file to the robot.

    Runs preprocess_protocol_code() on the source, writes it to a temporary
    file, and POSTs it to the robot's /protocols endpoint.  The temporary
    file is deleted after upload regardless of success or failure.

    This function is called internally by opentron_flex.upload_and_run().
    Use that method for the full upload-and-run workflow.

    Args:
        client: Connected opentron_flex instance.
        code: Raw Python protocol source code (markdown fences and indentation
              issues are corrected automatically).
        filename: Filename stored in the robot's run history. Defaults to "protocol.py".

    Returns:
        The protocolId string assigned by the robot, used to create a run.

    Raises:
        RuntimeError: If the upload fails or the robot returns no protocol ID.
    """
    processed = preprocess_protocol_code(code)

    temp_path = os.path.join(tempfile.gettempdir(), os.path.basename(filename))
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(processed)

    try:
        with open(temp_path, "rb") as f:
            resp = client.post(
                "/protocols",
                files={"files": (os.path.basename(filename), f, "text/x-python")},
            )
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Failed to upload protocol (HTTP {resp.status_code}): {resp.text}")

    protocol_id = resp.json().get("data", {}).get("id")
    if not protocol_id:
        raise RuntimeError(f"Protocol uploaded but no ID returned. Response: {resp.text}")

    logger.info("Protocol uploaded: id=%s filename=%s", protocol_id, filename)
    return protocol_id
