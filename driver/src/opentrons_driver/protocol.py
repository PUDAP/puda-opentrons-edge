"""Opentrons protocol builder, uploader, and labware/pipette resources.

Public API
----------
- :class:`Protocol` / :class:`ProtocolCommand` — build OT-2 protocols programmatically
  and generate runnable Python code via ``Protocol.to_python_code()``.
- :func:`upload_protocol` — preprocess and upload a protocol file to the robot.
- :func:`get_labware_types` / :func:`get_pipette_types` — enumerate known names.
- :data:`BUILTIN_LABWARE`

Example::

    from opentrons_driver.protocol import Protocol, ProtocolCommand, upload_protocol

    protocol = Protocol(
        protocol_name="Simple Transfer",
        author="Lab",
        description="Transfer 100 µL from A1 to B1",
        robot_type="OT-2",
        api_level="2.23",
        commands=[
            ProtocolCommand(command_type="load_labware", params={
                "name": "plate", "labware_type": "corning_96_wellplate_360ul_flat", "location": "1"
            }),
            ProtocolCommand(command_type="load_instrument", params={
                "name": "p300", "instrument_type": "p300_single_gen2",
                "mount": "right", "tip_racks": [],
            }),
            ProtocolCommand(command_type="transfer", params={
                "pipette": "p300", "volume": 100,
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
    "opentrons_96_tiprack_10ul",
    "opentrons_96_tiprack_20ul",
    "opentrons_96_tiprack_300ul",
    "opentrons_96_tiprack_1000ul",
    "nest_12_reservoir_15ml",
    "nest_96_wellplate_100ul_pcr_full_skirt",
    "nest_96_wellplate_200ul_flat",
]

LABWARE_TYPES: list[str] = _STANDARD_LABWARE_TYPES + [
    name for name in BUILTIN_LABWARE if name not in _STANDARD_LABWARE_TYPES
]

PIPETTE_TYPES: list[str] = [
    "p10_single_gen2",
    "p10_multi_gen2",
    "p20_single_gen2",
    "p20_multi_gen2",
    "p300_single_gen2",
    "p300_multi_gen2",
    "p1000_single_gen2",
    "p1000_multi_gen2",
]


def get_labware_types() -> list[str]:
    """
    Return all known labware load-names.

    Combines the standard Opentrons built-in list with any custom definitions
    discovered in the labware/ directory at import time.  No robot connection
    is required.

    Returns:
        Labware load-names usable in load_labware protocol commands.
        Examples: "corning_96_wellplate_360ul_flat",
                  "opentrons_96_tiprack_300ul", "mass_balance_vial_30000"
    """
    return list(LABWARE_TYPES)


def get_pipette_types() -> list[str]:
    """
    Return all known pipette instrument names.

    Lists every supported pipette model name usable in load_instrument
    protocol commands.  No robot connection is required.

    Returns:
        Pipette instrument names.
        Examples: "p300_single_gen2", "p1000_single_gen2", "p20_multi_gen2"
    """
    return list(PIPETTE_TYPES)


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
            "load_labware", "load_instrument", "pick_up_tip", "drop_tip",
            "aspirate", "dispense", "blow_out", "mix", "air_gap",
            "touch_tip", "transfer", "move_to", "flow_rate", "delay",
            "home", "comment", "loop", "loop_over_csv",
            "read_csv_file", "read_csv"
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
    Full Opentrons OT-2 protocol definition.

    Holds all metadata and the ordered list of ProtocolCommand steps.
    Call to_python_code() to generate runnable Opentrons Python source
    that can be uploaded directly to the robot.

    Attributes:
        protocol_name (str): Human-readable name shown in the robot's run history.
        author (str):        Author name embedded in the protocol metadata.
        description (str):   Short description embedded in the protocol metadata.
        robot_type (str):    Robot model string, e.g. "OT-2".
        api_level (str):     Opentrons API level string, e.g. "2.23".
        commands (list[ProtocolCommand]): Ordered list of protocol steps.

    Example:
        protocol = Protocol(
            protocol_name="Water Transfer",
            author="Lab",
            description="Transfer 100 µL from water plate to mixing plate",
            robot_type="OT-2",
            api_level="2.23",
            commands=[
                ProtocolCommand(command_type="load_labware", params={
                    "name": "tiprack",
                    "labware_type": "opentrons_96_tiprack_300ul",
                    "location": "11",
                }),
                ProtocolCommand(command_type="load_instrument", params={
                    "name": "p300",
                    "instrument_type": "p300_single_gen2",
                    "mount": "right",
                    "tip_racks": ["tiprack"],
                }),
                ProtocolCommand(command_type="transfer", params={
                    "pipette": "p300", "volume": 100,
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
    robot_type: str
    api_level: str
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
            to Opentrons.upload_and_run() or upload_protocol().
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
            f"    pipettes = {{}}\n\n"
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
        'labware_type' (load-name, e.g. "opentrons_96_tiprack_300ul"), and
        'location' (deck slot as a string, e.g. "11").

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

        Expects cmd.params to contain 'name' (variable name, e.g. "p300"),
        'instrument_type' (pipette model, e.g. "p300_single_gen2"), and
        'mount' ("left" or "right"). Optionally 'tip_racks' (list of labware
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

    This function is called internally by Opentrons.upload_and_run().
    Use that method for the full upload-and-run workflow.

    Args:
        client: Connected Opentrons driver instance.
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
