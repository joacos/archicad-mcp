"""MCP server exposing Archicad through the official JSON API and Tapir.

Tools:
  * archicad_status: finds running Archicad instances and checks Tapir.
  * archicad_model_summary / archicad_list_elements / archicad_quantities /
    archicad_check_model: read-only overviews built on several commands.
  * archicad_set_element_ids / archicad_create_room:
    model changes recorded in a journal, reverted with archicad_undo.
  * tapir_list_commands / tapir_describe_command / tapir_run_command:
    reach any of the ~250 Tapir commands, validated against their schema.
  * archicad_run_api_command: any official "API.*" command.
  * one tool per Tapir command in ARCHICAD_MCP_TOOLS (default: a curated set),
    using the command's own JSON schema as the tool input schema. Commands
    newer than the installed Tapir add-on are hidden.

Environment variables:
  ARCHICAD_HOST, ARCHICAD_PORT, ARCHICAD_TIMEOUT: connection settings.
  ARCHICAD_MCP_TOOLS: "curated" (default), "all" or "none".
  ARCHICAD_MCP_READ_ONLY=1: refuse every command that is not read-only.
  ARCHICAD_MCP_MAX_CHARS: maximum length of a tool response (default 100000).
  ARCHICAD_MCP_JOURNAL: journal file for archicad_undo (default ~/.archicad-mcp/journal.json).
"""

from __future__ import annotations

import json
import os
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import __version__
from .catalog import CURATED_COMMANDS, Catalog, Command
from .client import ArchicadClient, ArchicadError
from .edits import EDIT_DESCRIPTIONS, EDIT_SCHEMAS, READ_ONLY_EDIT_TOOLS, Edits
from .insights import DESCRIPTIONS, SCHEMAS, Insights

INSTRUCTIONS = """\
Controls a running Archicad through its JSON API and the Tapir add-on.
Start with archicad_status if unsure Archicad is reachable, and with
archicad_model_summary to understand the open project. Prefer
archicad_list_elements, archicad_quantities and archicad_check_model over
raw commands: they answer in one call with compact results. For changes,
prefer archicad_set_element_ids and archicad_create_room: they can be
reverted with archicad_undo. Avoid SetStories: it rebuilds the story
structure and has deleted elements in a real project. Use dryRun to
show the user large changes before applying them. When calling
GetDetailsOfElements, pass "fields" to get only what you need. Elements are
identified by {"elementId": {"guid": "..."}}; get them from
GetSelectedElements, GetElementsByType or FilterElements, then pass them to
detail, property, classification or modification commands. Commands not
exposed as their own tool are reachable with tapir_list_commands,
tapir_describe_command and tapir_run_command. Lengths are in meters and
angles in radians. Ask the user before deleting elements or making large
changes to their model.
"""

def tool(name: str, description: str, schema: dict[str, Any], **hints: Any) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        input_schema=schema,
        annotations=types.ToolAnnotations(open_world_hint=False, **hints),
    )


def object_schema(required: list[str] | None = None, **properties: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


GENERIC_TOOLS = [
    tool(
        "archicad_status",
        "Lists the running Archicad instances (port, version) and whether the Tapir add-on answers.",
        object_schema(),
        read_only_hint=True,
    ),
    tool(
        "tapir_list_commands",
        "Lists Tapir commands with a one-line description, optionally filtered by group name or text.",
        object_schema(
            group={"type": "string", "description": "Part of a group name, e.g. 'Element', 'Property', 'Navigator'."},
            search={"type": "string", "description": "Text to look for in the command name or description."},
        ),
        read_only_hint=True,
    ),
    tool(
        "tapir_describe_command",
        "Returns the input and output JSON schemas of a Tapir command. Read this before tapir_run_command.",
        object_schema(["command"], command={"type": "string", "description": "Tapir command name, e.g. 'CreateWalls'."}),
        read_only_hint=True,
    ),
    tool(
        "tapir_run_command",
        "Runs any Tapir command. The parameters are validated against the command's input schema "
        "(see tapir_describe_command). May modify or delete model data depending on the command.",
        object_schema(
            ["command"],
            command={"type": "string", "description": "Tapir command name."},
            parameters={"type": "object", "description": "Command parameters.", "default": {}},
        ),
        destructive_hint=True,
    ),
    tool(
        "archicad_run_api_command",
        "Runs an official Archicad JSON API command, e.g. 'API.GetElementsByType' or "
        "'API.GetBuiltInPropertyIds'. See Graphisoft's JSON API reference for parameters.",
        object_schema(
            ["command"],
            command={"type": "string", "pattern": "^API\\.", "description": "Command name starting with 'API.'."},
            parameters={"type": "object", "default": {}},
        ),
        destructive_hint=True,
    ),
]

INSIGHT_TOOLS = [tool(name, DESCRIPTIONS[name], schema, read_only_hint=True) for name, schema in SCHEMAS.items()]

EDIT_TOOLS = [
    tool(
        name,
        EDIT_DESCRIPTIONS[name],
        schema,
        read_only_hint=name in READ_ONLY_EDIT_TOOLS,
        destructive_hint=None if name in READ_ONLY_EDIT_TOOLS else name == "archicad_undo",
    )
    for name, schema in EDIT_SCHEMAS.items()
]

UPDATE_TAPIR_HINT = (
    "Update the Tapir add-on: https://github.com/ENZYME-APD/tapir-archicad-automation/releases/latest"
)


def select_commands(catalog: Catalog, setting: str) -> list[str]:
    setting = setting.strip().lower()
    if setting == "none":
        return []
    if setting == "all":
        return list(catalog.commands)
    return [name for name in CURATED_COMMANDS if name in catalog.commands]


def command_tool(catalog: Catalog, command: Command) -> types.Tool:
    return tool(
        command.name,
        f"{command.description} (Tapir, {command.group})",
        catalog.input_schema(command.name),
        read_only_hint=command.read_only,
        destructive_hint=None if command.read_only else True,
    )


def text_result(value: Any, max_chars: int, is_error: bool = False) -> types.CallToolResult:
    text = value if isinstance(value, str) else json.dumps(value, indent=1, ensure_ascii=False)
    if len(text) > max_chars:
        text = (
            text[:max_chars]
            + f"\n... [truncated: {len(text)} characters in total. Narrow the request, e.g. with filters or fewer elements.]"
        )
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=is_error)


class ArchicadMCP:
    def __init__(self, client: ArchicadClient | None = None, catalog: Catalog | None = None) -> None:
        self.client = client or ArchicadClient()
        self.catalog = catalog or Catalog.load()
        self.read_only = os.environ.get("ARCHICAD_MCP_READ_ONLY", "") not in ("", "0", "false")
        self.max_chars = int(os.environ.get("ARCHICAD_MCP_MAX_CHARS", "100000"))
        exposed = select_commands(self.catalog, os.environ.get("ARCHICAD_MCP_TOOLS", "curated"))
        if self.read_only:
            exposed = [n for n in exposed if self.catalog.commands[n].read_only]
        edit_tools = [t for t in EDIT_TOOLS if not self.read_only or t.name in READ_ONLY_EDIT_TOOLS]
        self.tools = GENERIC_TOOLS + INSIGHT_TOOLS + edit_tools + [
            command_tool(self.catalog, self.catalog.commands[n]) for n in exposed
        ]
        self.exposed = set(exposed)
        self.insights = Insights(self.client)
        self.edits = Edits(self.client, self.insights)
        self.tapir_version: str | None = None
        self.unsupported: set[str] = set()
        self.server = Server(
            "archicad-tapir",
            version=__version__,
            instructions=INSTRUCTIONS,
            on_list_tools=self.list_tools,
            on_call_tool=self.call_tool,
        )

    async def detect_tapir(self) -> str | None:
        """Reads the installed Tapir version once, to hide commands it lacks."""
        if self.tapir_version is None:
            try:
                response = await self.client.run_tapir_command("GetAddOnVersion")
            except (ArchicadError, OSError):
                return None
            self.tapir_version = (response or {}).get("version")
            self.unsupported = set(self.catalog.unsupported_by(self.tapir_version))
        return self.tapir_version

    async def list_tools(self, ctx: Any, params: Any) -> types.ListToolsResult:
        await self.detect_tapir()
        return types.ListToolsResult(tools=[t for t in self.tools if t.name not in self.unsupported])

    async def call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            return await self.dispatch(params.name, params.arguments or {})
        except ArchicadError as error:
            return text_result(str(error), self.max_chars, is_error=True)
        except (OSError, ValueError) as error:
            return text_result(
                f"Lost the connection to Archicad ({error.__class__.__name__}: {error}). "
                "Archicad may be busy or closed; retry, or run archicad_status.",
                self.max_chars,
                is_error=True,
            )

    async def dispatch(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        if name == "archicad_status":
            return await self.status()
        if name == "archicad_element_images":
            return await self.element_images(args)
        if name in SCHEMAS:
            return text_result(await self.insights.run(name, args), self.max_chars)
        if name in EDIT_SCHEMAS:
            if self.read_only and name not in READ_ONLY_EDIT_TOOLS:
                return text_result(f"Read-only mode: {name} changes the model.", self.max_chars, is_error=True)
            return text_result(await self.edits.run(name, args), self.max_chars)
        if name == "tapir_list_commands":
            commands = self.catalog.search(args.get("group"), args.get("search"))
            lines = [
                f"{c.name} [{c.group}{', read-only' if c.read_only else ''}]: {c.description}" for c in commands
            ]
            return text_result("\n".join(lines) or "No matching command.", self.max_chars)
        if name == "tapir_describe_command":
            command = self.catalog.commands.get(args.get("command", ""))
            if command is None:
                return text_result(f"Unknown Tapir command {args.get('command')!r}.", self.max_chars, is_error=True)
            return text_result(
                {
                    "name": command.name,
                    "group": command.group,
                    "description": command.description,
                    "readOnly": command.read_only,
                    "inputSchema": self.catalog.input_schema(command.name),
                    "outputSchema": self.catalog.output_schema(command.name),
                },
                self.max_chars,
            )
        if name == "tapir_run_command":
            return await self.run_tapir(args.get("command", ""), args.get("parameters") or {})
        if name == "archicad_run_api_command":
            command = args.get("command", "")
            if not command.startswith("API."):
                return text_result("Official commands start with 'API.'.", self.max_chars, is_error=True)
            if self.read_only and not command.startswith(("API.Get", "API.IsAlive")):
                return text_result("Read-only mode: only API.Get* commands are allowed.", self.max_chars, is_error=True)
            result = await self.client.run_command(command, args.get("parameters") or {})
            return text_result(result, self.max_chars)
        if name in self.exposed:
            return await self.run_tapir(name, args)
        return text_result(f"Unknown tool {name!r}.", self.max_chars, is_error=True)

    async def element_images(self, args: dict[str, Any]) -> types.CallToolResult:
        content: list[Any] = []
        for image in await self.insights.element_images(args):
            if "data" in image:
                content.append(types.TextContent(text=image["caption"]))
                content.append(types.ImageContent(data=image["data"], mime_type=image["mimeType"]))
            else:
                content.append(types.TextContent(text=f"{image['caption']}: {image['error']}"))
        if not content:
            content.append(types.TextContent(text="No elements: select some in Archicad or pass elements/elementType."))
        return types.CallToolResult(content=content, is_error=not any(isinstance(c, types.ImageContent) for c in content))

    async def run_tapir(self, name: str, parameters: dict[str, Any]) -> types.CallToolResult:
        command = self.catalog.commands.get(name)
        if command is None:
            return text_result(
                f"Unknown Tapir command {name!r}. Use tapir_list_commands to find the right one.",
                self.max_chars,
                is_error=True,
            )
        if self.read_only and not command.read_only:
            return text_result(f"Read-only mode: {name} may change the model.", self.max_chars, is_error=True)
        errors = self.catalog.validate(name, parameters)
        if errors:
            return text_result(
                f"Invalid parameters for {name}:\n" + "\n".join(errors)
                + "\nUse tapir_describe_command to see the schema.",
                self.max_chars,
                is_error=True,
            )
        await self.detect_tapir()
        if name in self.unsupported:
            return text_result(
                f"{name} needs Tapir {command.version} or newer; the installed add-on is {self.tapir_version}. "
                + UPDATE_TAPIR_HINT,
                self.max_chars,
                is_error=True,
            )
        try:
            result = await self.client.run_tapir_command(name, parameters)
        except ArchicadError as error:
            if "(code 4010)" in str(error):
                raise ArchicadError(f"{error}. The installed Tapir add-on lacks this command. {UPDATE_TAPIR_HINT}") from error
            raise
        return text_result(result if result is not None else {"succeeded": True}, self.max_chars)

    async def status(self) -> types.CallToolResult:
        instances = await anyio.to_thread.run_sync(self.client.find_instances)
        report: dict[str, Any] = {"instances": instances, "tapirCatalogVersion": self.catalog.tapir_version}
        if not instances:
            report["hint"] = "No Archicad found. Open Archicad with a project (ports 19723-19744 are scanned)."
            return text_result(report, self.max_chars, is_error=True)
        if self.client.fixed_port is None and self.client.port not in [i["port"] for i in instances]:
            self.client.port = instances[0]["port"]
        report["connectedPort"] = self.client.port
        self.tapir_version = None
        try:
            report["tapirAddOnVersion"] = (await self.client.run_tapir_command("GetAddOnVersion")).get("version")
            await self.detect_tapir()
            if self.unsupported:
                report["commandsNeedingNewerTapir"] = sorted(self.unsupported)
                report["hint"] = UPDATE_TAPIR_HINT
        except ArchicadError as error:
            report["tapirAddOnVersion"] = None
            report["hint"] = f"Tapir add-on does not answer ({error}). Install it: https://github.com/ENZYME-APD/tapir-archicad-automation#installation"
        return text_result(report, self.max_chars)

    async def run_stdio(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(read_stream, write_stream, self.server.create_initialization_options())


def main() -> None:
    anyio.run(ArchicadMCP().run_stdio)


if __name__ == "__main__":
    main()
