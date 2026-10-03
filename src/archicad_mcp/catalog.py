"""The Tapir command catalog: schemas, groups and safety classification."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

import jsonschema

# Commands exposed as their own MCP tools by default. Everything else stays
# reachable through tapir_run_command, so the model sees a short tool list.
CURATED_COMMANDS = [
    "GetProjectInfo",
    "GetStories",
    "GetSelectedElements",
    "GetElementsByType",
    "GetAllElements",
    "FilterElements",
    "GetDetailsOfElements",
    "Get3DBoundingBoxes",
    "HighlightElements",
    "ChangeSelectionOfElements",
    "GetAllProperties",
    "GetPropertyValuesOfElements",
    "SetPropertyValuesOfElements",
    "GetClassificationsOfElements",
    "SetClassificationsOfElements",
    "GetGDLParametersOfElements",
    "SetGDLParametersOfElements",
    "GetLayers",
    "GetAttributesByType",
    "GetZoneBoundaries",
    "CreateWalls",
    "CreateSlabs",
    "CreateColumns",
    "CreateZones",
    "CreateObjects",
    "MoveElements",
    "DeleteElements",
    "GetNavigatorItemTree",
    "GetIssues",
    "CreateIssue",
]

READ_ONLY_PREFIXES = ("Get", "Filter")
# Commands named Get* that change state or block on the user.
NOT_READ_ONLY = {"GetPointFromUser", "GetScriptUIResult"}
DESTRUCTIVE_PREFIXES = ("Delete", "Remove", "Set", "Modify", "Update", "Move", "Import", "Trim")
# Commands matching a destructive prefix that only change the UI state.
NOT_DESTRUCTIVE = {"SetViewRotation", "Set3DCutPlanes", "SetSuspendGroupsMode", "SetElementNotificationClient"}
DESTRUCTIVE_COMMANDS = {
    "QuitArchicad",
    "OpenProject",
    "CloseProject",
    "SaveProject",
    "ReloadLibraries",
    "UnlockElements",
    "TeamworkSend",
    "TeamworkReceive",
    "ReleaseElements",
    "ConnectMEPElements",
    "RenameFavorites",
    "RenameNavigatorItem",
    "PublishPublisherSet",
    "IFCFileOperation",
}


@dataclass
class Command:
    name: str
    group: str
    description: str
    version: str | None
    raw_input_schema: dict[str, Any] | None
    raw_output_schema: dict[str, Any] | None

    @property
    def read_only(self) -> bool:
        return self.name.startswith(READ_ONLY_PREFIXES) and self.name not in NOT_READ_ONLY

    @property
    def destructive(self) -> bool:
        return not self.read_only and (
            (self.name.startswith(DESTRUCTIVE_PREFIXES) and self.name not in NOT_DESTRUCTIVE)
            or self.name in DESTRUCTIVE_COMMANDS
        )


def parse_version(text: str | None) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return None


def _collect_refs(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.add(value.split("/")[-1])
            else:
                _collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, found)


def _rewrite_refs(node: Any) -> Any:
    """Turns Tapir's "#/Name" references into standard "#/$defs/Name" ones."""
    if isinstance(node, dict):
        return {
            key: ("#/$defs/" + value.split("/")[-1] if key == "$ref" and isinstance(value, str) else _rewrite_refs(value))
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_rewrite_refs(item) for item in node]
    return node


class Catalog:
    def __init__(self, data: dict[str, Any]) -> None:
        self.tapir_version: str = data.get("tapirVersion", "?")
        self.definitions: dict[str, Any] = data["definitions"]
        self.commands: dict[str, Command] = {}
        self._input_schemas: dict[str, dict[str, Any]] = {}
        self.groups: dict[str, list[str]] = {}
        for group in data["groups"]:
            self.groups[group["name"]] = [c["name"] for c in group["commands"]]
            for c in group["commands"]:
                self.commands[c["name"]] = Command(
                    name=c["name"],
                    group=group["name"],
                    description=c.get("description", ""),
                    version=c.get("version"),
                    raw_input_schema=c.get("inputSchema"),
                    raw_output_schema=c.get("outputSchema"),
                )

    @classmethod
    def load(cls) -> "Catalog":
        text = resources.files("archicad_mcp").joinpath("data/tapir_commands.json").read_text(encoding="utf8")
        return cls(json.loads(text))

    def unsupported_by(self, addon_version: str | None) -> list[str]:
        """Commands added to Tapir after the installed add-on version."""
        installed = parse_version(addon_version)
        if installed is None:
            return []
        return [
            c.name
            for c in self.commands.values()
            if (added := parse_version(c.version)) is not None and added > installed
        ]

    def _with_defs(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Returns a self-contained schema carrying only the definitions it uses."""
        needed: set[str] = set()
        pending = set()
        _collect_refs(schema, pending)
        while pending:
            name = pending.pop()
            if name in needed:
                continue
            needed.add(name)
            _collect_refs(self.definitions.get(name, {}), pending)
        result = _rewrite_refs(copy.deepcopy(schema))
        if needed:
            result["$defs"] = {name: _rewrite_refs(self.definitions[name]) for name in sorted(needed)}
        return result

    def input_schema(self, name: str) -> dict[str, Any]:
        """The command's input schema, shaped as an MCP tool input schema."""
        cached = self._input_schemas.get(name)
        if cached is not None:
            return cached
        raw = self.commands[name].raw_input_schema
        if not raw:
            schema: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
        elif raw.get("type") == "object":
            schema = self._with_defs(raw)
        else:
            # A bare $ref at the root: MCP requires "type": "object" there.
            schema = self._with_defs({"type": "object", "allOf": [raw]})
        self._input_schemas[name] = schema
        return schema

    def output_schema(self, name: str) -> dict[str, Any] | None:
        raw = self.commands[name].raw_output_schema
        return self._with_defs(raw) if raw else None

    def validate(self, name: str, parameters: dict[str, Any]) -> list[str]:
        """Returns readable validation errors, or an empty list when valid."""
        validator = jsonschema.Draft202012Validator(self.input_schema(name))
        errors = sorted(validator.iter_errors(parameters), key=lambda e: list(e.absolute_path))
        return [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in errors[:10]]

    def search(self, group: str | None = None, text: str | None = None) -> list[Command]:
        found = []
        for command in self.commands.values():
            if group and group.lower() not in command.group.lower():
                continue
            if text and text.lower() not in (command.name + " " + command.description).lower():
                continue
            found.append(command)
        return found
