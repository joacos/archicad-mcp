"""End-to-end tests against a fake Archicad HTTP server."""

import copy
import json
import sys
import uuid
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from mcp import Client, StdioServerParameters

from archicad_mcp.catalog import Catalog
from archicad_mcp.client import ArchicadClient
from archicad_mcp.server import ArchicadMCP

GUID = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
WALLS = [f"00000000-0000-0000-0000-00000000000{i}" for i in range(1, 4)]
ZONE = "00000000-0000-0000-0000-00000000000Z"
ELEMENTS = {WALLS[0]: ("Wall", "W-1", 0), WALLS[1]: ("Wall", "W-1", 0), WALLS[2]: ("Wall", "", 1), ZONE: ("Zone", "Z-1", 0)}
LAYER_GUID = "11111111-1111-1111-1111-111111111111"
STORIES = [
    {"index": 0, "name": "Planta baja", "level": 0, "height": 3, "dispOnSections": True},
    {"index": 1, "name": "", "level": 3, "height": 3, "dispOnSections": True},
    {"index": 2, "name": "Techo", "level": 6, "dispOnSections": True},
]
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
SLABS: dict = {}
MESHES: dict = {}
MORPHS: dict = {}
MOVES: list = []
ROTATIONS: list = []
OFFSETS: dict = {}
LAYERS: list = []
LAYER_OF: dict = {}
MODIFIED_SLABS: list = []
ZONE_GEOMETRIES: list = []
CLASSIFIED: dict = {ZONE: "X"}
CLASSIFICATION_TREE = [
    {"classificationItem": {"classificationItemId": {"guid": "I-SITE"}, "id": "Site", "children": [
        {"classificationItem": {"classificationItemId": {"guid": "I-WALL"}, "id": "Wall"}}]}},
    {"classificationItem": {"classificationItemId": {"guid": "I-X"}, "id": "Other"}},
]
INITIAL = copy.deepcopy((ELEMENTS, STORIES))
CREATED_TYPES = {"CreateWalls": ("Wall", "wallsData"), "CreateSlabs": ("Slab", "slabsData"), "CreateZones": ("Zone", "zonesData"),
                 "CreateMeshes": ("Mesh", "meshesData"), "CreateMorphs": ("Morph", "morphsData")}
PROPERTY_VALUES = {"General_3DLength": ("length", 4.0), "General_Height": ("length", 3.0), "Zone_CalculatedArea": ("area", 20.0)}


def fake_tapir(name, params):
    guids = [e["elementId"]["guid"] for e in params.get("elements", [])]
    if name == "GetAllElements":
        return {"elements": [{"elementId": {"guid": g}} for g in ELEMENTS]}
    if name == "GetElementsByType":
        return {"elements": [{"elementId": {"guid": g}} for g, e in ELEMENTS.items() if e[0] == params["elementType"]]}
    if name == "GetDetailsOfElements":
        return {"detailsOfElements": [
            dict(zip(("type", "id", "floorIndex"), ELEMENTS.get(g, ("Object", "", 0))), layerIndex=LAYER_OF.get(g, 1),
                 **({"details": {"referencePlaneLocation": "Top", "zCoordinate": SLABS[g][0], "thickness": SLABS[g][1]}}
                    if g in SLABS else {"details": dict(MESHES[g])} if g in MESHES else {}))
            if g in ELEMENTS or g == GUID
            else {"error": {"code": -2130313115, "message": "Failed to get the details of element"}}
            for g in guids
        ]}
    if name == "GetStories":
        return {"stories": copy.deepcopy(STORIES)}
    if name == "SetStories":
        for story, settings in zip(STORIES, params["stories"]):
            story["name"] = settings["name"]
        return {"success": True}
    if name == "GetElementPreviewImage":
        return {"previewImage": PNG}
    if name == "GetRoomImage":
        return {"roomImage": PNG}
    if name == "RotateElements":
        ROTATIONS.extend(params["elementsWithRotations"])
        for item in params["elementsWithRotations"]:
            g = item["elementId"]["guid"]
            dx, dy = OFFSETS.get(g, (0, 0))
            OFFSETS[g] = (dx + 0.5, dy)  # any visible change of the box
        return {"executionResults": [{"success": True} for _ in params["elementsWithRotations"]]}
    if name == "MoveElements":
        MOVES.extend(params["elementsWithMoveVectors"])
        for item in params["elementsWithMoveVectors"]:
            g, v = item["elementId"]["guid"], item["moveVector"]
            dx, dy = OFFSETS.get(g, (0, 0))
            OFFSETS[g] = (dx + v["x"], dy + v["y"])
        return {"executionResults": [{"success": True} for _ in params["elementsWithMoveVectors"]]}
    if name == "ModifyMeshes":
        for item in params["meshesData"]:
            MESHES[item["elementId"]["guid"]].update(item["meshData"])
        return {"executionResults": [{"success": True} for _ in params["meshesData"]]}
    if name == "ModifySlabs":
        MODIFIED_SLABS.extend(params["slabsWithDetails"])
        for item in params["slabsWithDetails"]:
            g = item["elementId"]["guid"]
            top, thickness = SLABS.get(g, (0, 0.2))
            SLABS[g] = (item.get("zCoordinate", top), item.get("thickness", thickness))
        return {"executionResults": [{"success": True} for _ in params["slabsWithDetails"]]}
    if name in CREATED_TYPES:
        element_type, key = CREATED_TYPES[name]
        created = []
        for item in params[key]:
            if element_type == "Zone" and "referencePosition" in item["geometry"] and item["floorIndex"] != 0:
                # Like Archicad: automatic zones only find the walls of the story on screen.
                created.append({"error": {"code": -2130313215, "message": "Failed to create new Zone"}})
                ZONE_GEOMETRIES.append(item["geometry"])
                continue
            ZONE_GEOMETRIES.append(item.get("geometry"))
            guid = str(uuid.uuid4()).upper()
            ELEMENTS[guid] = (element_type, "", 0)
            if element_type == "Slab":
                SLABS[guid] = (item["level"], item.get("thickness", 0.2))
            if element_type == "Mesh":
                MESHES[guid] = dict(item)
            if element_type == "Morph":
                MORPHS[guid] = item
            created.append({"elementId": {"guid": guid}})
        return {"elements": created}
    if name == "DeleteElements":
        for guid in guids:
            ELEMENTS.pop(guid, None)
        return {"executionResults": [{"success": True} for _ in guids]}
    if name == "GetAttributesByType":
        if params["attributeType"] == "BuildingMaterial":
            return {"attributes": [{"attributeId": {"guid": "MAT-GRAVEL"}, "index": 7, "name": "Gravel"}]}
        return {"attributes": copy.deepcopy(LAYERS)}
    if name == "CreateLayers":
        for item in params["layerDataArray"]:
            LAYERS.append({"attributeId": {"guid": str(uuid.uuid4()).upper()}, "index": len(LAYERS) + 1, "name": item["name"]})
        return {"attributeIds": [{"attributeId": LAYERS[-1]["attributeId"]}]}
    if name == "SetDetailsOfElements":
        for item in params["elementsWithDetails"]:
            LAYER_OF[item["elementId"]["guid"]] = item["details"]["layerIndex"]
        return {"executionResults": [{"success": True} for _ in params["elementsWithDetails"]]}
    if name == "GetLayers":
        return {"layers": [{"attributeId": {"guid": LAYER_GUID}, "isHidden": True, "isLocked": False}]}
    if name == "Get3DBoundingBoxes":
        box = {"xMin": 0, "xMax": 4, "yMin": 0, "yMax": 0.2, "zMin": 0, "zMax": 3}
        def moved(g):
            dx, dy = OFFSETS.get(g, (0, 0))
            z = {"zMin": SLABS[g][0] - SLABS[g][1], "zMax": SLABS[g][0]} if g in SLABS else {}
            if g in MESHES:
                top = max([p.get("z", 0) for p in MESHES[g].get("polygonCoordinates") or [{"z": 0}]])
                z = {"zMin": MESHES[g]["level"] - MESHES[g]["skirtLevel"], "zMax": MESHES[g]["level"] + top}
            return dict(box, xMin=box["xMin"] + dx, xMax=4 + (g == WALLS[2]) + dx, yMin=box["yMin"] + dy, yMax=box["yMax"] + dy, **z)
        return {"boundingBoxes3D": [{"boundingBox3D": moved(g)} for g in guids]}
    if name == "SetClassificationsOfElements":
        for item in params["elementClassifications"]:
            CLASSIFIED.pop(item["elementId"]["guid"], None)
            if "classificationItemId" in item["classificationId"]:
                CLASSIFIED[item["elementId"]["guid"]] = item["classificationId"]["classificationItemId"]["guid"]
        return {"executionResults": [{"success": True} for _ in params["elementClassifications"]]}
    if name == "GetProjectInfo":
        return {"projectName": "Demo", "isTeamwork": False, "isUntitled": False}
    return {
        "GetAddOnVersion": {"version": FakeArchicad.tapir_version},
        "GetSelectedElements": {"elements": [{"elementId": {"guid": GUID}}]},
    }.get(name, {})


def fake_api(command, params):
    if command == "API.GetPropertyIds":
        return {"properties": [{"propertyId": {"guid": p["nonLocalizedName"]}} for p in params["properties"]]}
    if command == "API.GetPropertyValuesOfElements":
        names = [p["propertyId"]["guid"] for p in params["properties"]]
        return {"propertyValuesForElements": [
            {"propertyValues": [
                {"propertyValue": {"type": PROPERTY_VALUES[n][0], "status": "normal", "value": PROPERTY_VALUES[n][1]}}
                if n in PROPERTY_VALUES else {"propertyValue": {"type": "volume", "status": "notAvailable"}}
                for n in names
            ]}
            for _ in params["elements"]
        ]}
    if command == "API.SetPropertyValuesOfElements":
        if any({"type", "status", "value"} - set(i["propertyValue"]) for i in params["elementPropertyValues"]):
            return {"error": "schema"}  # like Archicad: rejected by the JSON schema (oneOf)
        for item in params["elementPropertyValues"]:
            guid = item["elementId"]["guid"]
            ELEMENTS[guid] = (ELEMENTS[guid][0], item["propertyValue"]["value"], ELEMENTS[guid][2])
        return {"executionResults": [{"success": True} for _ in params["elementPropertyValues"]]}
    if command == "API.GetAllClassificationSystems":
        return {"classificationSystems": [{"classificationSystemId": {"guid": "CS"}, "name": "Clasificación"}]}
    if command == "API.GetAllClassificationsInSystem":
        return {"classificationItems": CLASSIFICATION_TREE}
    if command == "API.GetClassificationsOfElements":
        return {"elementClassifications": [
            {"classificationIds": [{"classificationId": dict(
                {"classificationSystemId": {"guid": "CS"}},
                **({"classificationItemId": {"guid": CLASSIFIED[e["elementId"]["guid"]]}} if e["elementId"]["guid"] in CLASSIFIED else {}),
            )}]}
            for e in params["elements"]
        ]}
    return None


class FakeArchicad(BaseHTTPRequestHandler):
    received: list = []
    tapir_version = "1.6.1"

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeArchicad.received.append(request)
        command, params = request["command"], request["parameters"]
        if command == "API.IsAlive":
            reply = {"succeeded": True, "result": {"isAlive": True}}
        elif command == "API.GetProductInfo":
            reply = {"succeeded": True, "result": {"version": 29, "buildNumber": 3000, "languageCode": "INT"}}
        elif command == "API.ExecuteAddOnCommand":
            name = params["addOnCommandId"]["commandName"]
            response = fake_tapir(name, params.get("addOnCommandParameters", {}))
            reply = {"succeeded": True, "result": {"addOnCommandResponse": response}}
        elif fake_api(command, params) == {"error": "schema"}:
            reply = {"succeeded": False, "error": {"code": 4002, "message": "Invalid command parameters"}}
        elif fake_api(command, params) is not None:
            reply = {"succeeded": True, "result": fake_api(command, params)}
        else:
            reply = {"succeeded": False, "error": {"code": 4001, "message": f"Unknown command {command}"}}
        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def archicad_port(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHICAD_MCP_JOURNAL", str(tmp_path / "journal.json"))
    ELEMENTS.clear()
    ELEMENTS.update(copy.deepcopy(INITIAL[0]))
    STORIES[:] = copy.deepcopy(INITIAL[1])
    MODIFIED_SLABS.clear()
    LAYERS[:] = [{"attributeId": {"guid": LAYER_GUID}, "index": 1, "name": "Muros"}]
    LAYER_OF.clear()
    MOVES.clear()
    SLABS.clear()
    MESHES.clear()
    MORPHS.clear()
    ROTATIONS.clear()
    OFFSETS.clear()
    ZONE_GEOMETRIES.clear()
    CLASSIFIED.clear()
    CLASSIFIED[ZONE] = "X"
    FakeArchicad.received = []
    FakeArchicad.tapir_version = "1.6.1"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeArchicad)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


@pytest.fixture
def anyio_backend():
    return "asyncio"


def text(result):
    return result.content[0].text


def make_server(port, monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return ArchicadMCP(client=ArchicadClient(port=port)).server


@pytest.mark.anyio
async def test_lists_curated_tools_with_tapir_schemas(archicad_port, monkeypatch):
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert {"archicad_status", "tapir_run_command", "CreateWalls", "GetSelectedElements"} <= set(tools)
    assert tools["GetSelectedElements"].annotations.read_only_hint is True
    assert tools["DeleteElements"].annotations.destructive_hint is True
    assert "$defs" in tools["CreateWalls"].input_schema


@pytest.mark.anyio
async def test_status_reports_version(archicad_port, monkeypatch):
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        result = await client.call_tool("archicad_status", {})
    report = json.loads(text(result))
    assert report["instances"][0]["version"] == 29
    assert report["tapirAddOnVersion"] == "1.6.1"


@pytest.mark.anyio
async def test_curated_tool_wraps_execute_addon_command(archicad_port, monkeypatch):
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        result = await client.call_tool("GetSelectedElements", {})
    assert GUID in text(result)
    sent = FakeArchicad.received[-1]
    assert sent["command"] == "API.ExecuteAddOnCommand"
    assert sent["parameters"]["addOnCommandId"] == {"commandNamespace": "TapirCommand", "commandName": "GetSelectedElements"}


@pytest.mark.anyio
async def test_invalid_parameters_never_reach_archicad(archicad_port, monkeypatch):
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        result = await client.call_tool("tapir_run_command", {"command": "DeleteElements", "parameters": {"elements": "x"}})
    assert result.is_error
    assert "Invalid parameters" in text(result)
    assert not FakeArchicad.received


@pytest.mark.anyio
async def test_read_only_mode_blocks_changes(archicad_port, monkeypatch):
    server = make_server(archicad_port, monkeypatch, ARCHICAD_MCP_READ_ONLY="1")
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool(
            "tapir_run_command",
            {"command": "DeleteElements", "parameters": {"elements": [{"elementId": {"guid": GUID}}]}},
        )
    assert "DeleteElements" not in names and "GetSelectedElements" in names
    assert result.is_error and "Read-only" in text(result)


@pytest.mark.anyio
async def test_official_api_errors_are_reported(archicad_port, monkeypatch):
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        result = await client.call_tool("archicad_run_api_command", {"command": "API.DoesNotExist"})
    assert result.is_error and "Unknown command" in text(result)


@pytest.mark.anyio
async def test_stdio_process(archicad_port):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "archicad_mcp"], env={"ARCHICAD_PORT": str(archicad_port)}
    )
    async with Client(params) as client:
        result = await client.call_tool("tapir_list_commands", {"search": "wall"})
        status = await client.call_tool("archicad_status", {})
    assert "CreateWalls" in text(result)
    assert json.loads(text(status))["connectedPort"] == archicad_port


def test_every_command_schema_is_valid():
    import jsonschema

    catalog = Catalog.load()
    for name in catalog.commands:
        jsonschema.Draft202012Validator.check_schema(catalog.input_schema(name))
        assert catalog.input_schema(name)["type"] == "object"


@pytest.mark.anyio
async def test_commands_newer_than_installed_tapir_are_hidden(archicad_port, monkeypatch):
    FakeArchicad.tapir_version = "1.5.9"
    async with Client(make_server(archicad_port, monkeypatch, ARCHICAD_MCP_TOOLS="all")) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("tapir_run_command", {"command": "GetIFCExportTranslators", "parameters": {}})
        status = json.loads(text(await client.call_tool("archicad_status", {})))
    assert "GetIFCExportTranslators" not in names and "GetAllElements" in names
    assert result.is_error and "1.6.0" in text(result)
    assert "GetIFCExportTranslators" in status["commandsNeedingNewerTapir"]


@pytest.mark.anyio
async def test_model_summary(archicad_port, monkeypatch):
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        summary = json.loads(text(await client.call_tool("archicad_model_summary", {})))
    assert summary["elementCount"] == 4
    assert summary["byType"] == {"Wall": 3, "Zone": 1}
    assert summary["zones"] == {"count": 1, "totalCalculatedArea_m2": 20.0}
    assert [s["elements"] for s in summary["stories"]] == [3, 1, 0]
    assert summary["byLayer"] == {"Muros": 4}


@pytest.mark.anyio
async def test_list_elements_is_compact(archicad_port, monkeypatch):
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        result = json.loads(text(await client.call_tool("archicad_list_elements", {"elementType": "Wall", "limit": 2})))
    assert result["total"] == 3 and len(result["elements"]) == 2
    assert result["elements"][0] == {"guid": WALLS[0], "type": "Wall", "id": "W-1", "story": "Planta baja", "layer": "Muros"}
    sent = [r for r in FakeArchicad.received if r["parameters"].get("addOnCommandId", {}).get("commandName") == "GetDetailsOfElements"]
    assert sent[0]["parameters"]["addOnCommandParameters"]["fields"] == ["type", "id", "floorIndex", "layerIndex"]


@pytest.mark.anyio
async def test_quantities_sum_only_additive_values(archicad_port, monkeypatch):
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        by_type = json.loads(text(await client.call_tool("archicad_quantities", {})))
        csv_text = text(await client.call_tool("archicad_quantities", {"groupBy": "story", "format": "csv"}))
    walls = next(r for r in by_type["rows"] if r["type"] == "Wall")
    assert walls["count"] == 3 and walls["General_3DLength"] == 12.0
    assert "General_Height" not in walls
    assert by_type["units"]["Zone_CalculatedArea"] == "m²"
    assert csv_text.splitlines()[0].startswith("type,story,count")
    assert "Wall,Planta baja,2,8.0" in csv_text


@pytest.mark.anyio
async def test_check_model(archicad_port, monkeypatch):
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        report = json.loads(text(await client.call_tool("archicad_check_model", {})))
    assert report["summary"] == {
        "missing_id": 1,
        "duplicate_id": 1,
        "unclassified": 3,
        "duplicate_geometry": 1,
        "hidden_or_locked_layer": 4,
        "zero_area_zone": 0,
        "unnamed_story": 1,
        "empty_story": 1,
    }
    assert report["issues"]["duplicate_id"][0]["id"] == "W-1"
    assert report["issues"]["empty_story"][0]["name"] == "Techo"


def call_json(result):
    assert not result.is_error, text(result)
    return json.loads(text(result))


@pytest.mark.anyio
async def test_fix_duplicate_ids_and_undo(archicad_port, monkeypatch):
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        preview = call_json(await client.call_tool("archicad_set_element_ids", {"fixDuplicates": True, "dryRun": True}))
        assert ELEMENTS[WALLS[1]][1] == "W-1"
        done = call_json(await client.call_tool("archicad_set_element_ids", {"fixDuplicates": True}))
        assert preview["changes"] == done["changes"] == [{"guid": WALLS[1], "type": "Wall", "from": "W-1", "to": "W-1-2"}]
        assert ELEMENTS[WALLS[1]][1] == "W-1-2"
        undone = call_json(await client.call_tool("archicad_undo", {}))
    assert undone["reverted"][0]["summary"] == "Changed 1 element IDs"
    assert ELEMENTS[WALLS[1]][1] == "W-1"


@pytest.mark.anyio
async def test_numbering_ids(archicad_port, monkeypatch):
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        call_json(await client.call_tool("archicad_set_element_ids", {"elementType": "Wall", "numbering": {"prefix": "M-", "digits": 2}}))
    assert [ELEMENTS[w][1] for w in WALLS] == ["M-01", "M-02", "M-03"]
    assert ELEMENTS[ZONE][1] == "Z-1"


@pytest.mark.anyio
async def test_create_room_and_undo(archicad_port, monkeypatch):
    square = [{"x": 0, "y": 0}, {"x": 5, "y": 0}, {"x": 5, "y": 4}, {"x": 0, "y": 4}]
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        plan = call_json(await client.call_tool("archicad_create_room", {"polygon": square, "zoneName": "Living", "dryRun": True}))
        assert len(plan["plan"]["walls"]) == 4 and plan["plan"]["walls"][3]["endCoordinate"] == {"x": 0, "y": 0}
        assert plan["plan"]["zone"]["geometry"] == {"referencePosition": {"x": 2.5, "y": 2.0}}
        assert len(ELEMENTS) == 4
        room = call_json(await client.call_tool("archicad_create_room", {"polygon": square, "zoneName": "Living"}))
        assert len(room["created"]) == 6 and len(ELEMENTS) == 10
        assert room["zonePlacement"] == "automatic"
        assert MODIFIED_SLABS[0]["structureType"] == "Basic" and MODIFIED_SLABS[0]["thickness"] == 0.2
        history = call_json(await client.call_tool("archicad_history", {}))
        assert history[0]["summary"] == "Created room Living (6 elements) on story 0"
        call_json(await client.call_tool("archicad_undo", {}))
        assert len(ELEMENTS) == 4
        assert call_json(await client.call_tool("archicad_history", {})) == []


@pytest.mark.anyio
async def test_read_only_mode_hides_edit_tools(archicad_port, monkeypatch):
    server = make_server(archicad_port, monkeypatch, ARCHICAD_MCP_READ_ONLY="1")
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("archicad_undo", {})
    assert "archicad_history" in names and "archicad_create_room" not in names
    assert result.is_error and "Read-only" in text(result)


@pytest.mark.anyio
async def test_room_on_another_story_uses_inner_outline(archicad_port, monkeypatch):
    square = [{"x": 0, "y": 0}, {"x": 5, "y": 0}, {"x": 5, "y": 4}, {"x": 0, "y": 4}]
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        room = call_json(await client.call_tool(
            "archicad_create_room",
            {"polygon": square, "floorIndex": 1, "zoneName": "Estar", "wallThickness": 0.3, "slabStructure": "default"},
        ))
    assert len(room["created"]) == 6
    assert room["zonePlacement"].startswith("inner outline of the walls")
    assert ZONE_GEOMETRIES[-1] == {"polygonCoordinates": [
        {"x": 0.15, "y": 0.15}, {"x": 4.85, "y": 0.15}, {"x": 4.85, "y": 3.85}, {"x": 0.15, "y": 3.85}
    ]}
    assert MODIFIED_SLABS == []


@pytest.mark.anyio
async def test_element_images(archicad_port, monkeypatch):
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        result = await client.call_tool("archicad_element_images", {"elements": [
            {"elementId": {"guid": WALLS[0]}}, {"elementId": {"guid": ZONE}}
        ]})
    kinds = [c.type for c in result.content]
    assert kinds == ["text", "image", "text", "image"] and not result.is_error
    assert result.content[0].text.startswith("Wall W-1 — Planta baja, Muros")
    sent = [r["parameters"]["addOnCommandId"]["commandName"] for r in FakeArchicad.received if r["command"] == "API.ExecuteAddOnCommand"]
    assert "GetElementPreviewImage" in sent and "GetRoomImage" in sent


@pytest.mark.anyio
async def test_create_slabs_on_new_layer_and_undo(archicad_port, monkeypatch):
    square = [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 8}, {"x": 0, "y": 8}]
    slabs = [{"polygon": square}, {"polygon": [{"x": p["x"] + 20, "y": p["y"]} for p in square],
                                    "holes": [[{"x": 22, "y": 2}, {"x": 24, "y": 2}, {"x": 24, "y": 4}]]}]
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        plan = call_json(await client.call_tool("archicad_create_slabs", {"slabs": slabs, "topLevel": 5.15, "thickness": 5, "layer": "EMPLAZAMIENTO", "dryRun": True}))
        assert plan["bottomLevel"] == 0.15 and len(ELEMENTS) == 4 and len(LAYERS) == 1
        done = call_json(await client.call_tool("archicad_create_slabs", {"slabs": slabs, "topLevel": 5.15, "thickness": 5, "layer": "EMPLAZAMIENTO"}))
        assert done["created"] == 2 and "created layer 'EMPLAZAMIENTO'" in done["layerNote"]
        assert {LAYER_OF[e["elementId"]["guid"]] for e in done["elements"]} == {2}
        assert [m["thickness"] for m in MODIFIED_SLABS] == [5, 5]
        again = call_json(await client.call_tool("archicad_create_slabs", {"slabs": slabs[:1], "layer": "EMPLAZAMIENTO"}))
        assert "existing layer" in again["layerNote"] and len(LAYERS) == 2
        call_json(await client.call_tool("archicad_undo", {"steps": 2}))
    assert len(ELEMENTS) == 4


@pytest.mark.anyio
async def test_move_elements_and_undo(archicad_port, monkeypatch):
    walls = [{"elementId": {"guid": g}} for g in WALLS[:2]]
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        refused = await client.call_tool("archicad_move_elements", {"vector": {"x": 1, "y": 0}})
        assert refused.is_error
        done = call_json(await client.call_tool("archicad_move_elements", {"elements": walls, "vector": {"x": -2.2, "y": 2.75}}))
        assert done["moved"] == 2 and MOVES[0]["moveVector"] == {"x": -2.2, "y": 2.75, "z": 0}
        assert done["problems"] is None
        undone = call_json(await client.call_tool("archicad_undo", {}))
        assert undone["reverted"][0]["problems"] is None
    assert MOVES[-1]["moveVector"] == {"x": 2.2, "y": -2.75, "z": 0} and len(MOVES) == 4


@pytest.mark.anyio
async def test_rotate_elements_and_undo(archicad_port, monkeypatch):
    walls = [{"elementId": {"guid": g}} for g in WALLS[:2]]
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        done = call_json(await client.call_tool("archicad_rotate_elements", {"elements": walls, "origin": {"x": 1, "y": 2}, "degrees": 90}))
        assert done["rotated"] == 2 and done["problems"] is None
        rot = ROTATIONS[0]["rotation"]
        assert rot["origin"] == {"x": 1, "y": 2} and rot["beginPoint"] == {"x": 11, "y": 2}
        assert abs(rot["endPoint"]["x"] - 1) < 1e-9 and abs(rot["endPoint"]["y"] - 12) < 1e-9
        call_json(await client.call_tool("archicad_undo", {}))
    back = ROTATIONS[-1]["rotation"]["endPoint"]
    assert abs(back["x"] - 1) < 1e-9 and abs(back["y"] + 8) < 1e-9


@pytest.mark.anyio
async def test_undo_skips_elements_deleted_meanwhile(archicad_port, monkeypatch):
    square = [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 8}, {"x": 0, "y": 8}]
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        done = call_json(await client.call_tool("archicad_create_slabs", {"slabs": [{"polygon": square}] * 3}))
        gone = done["elements"][0]["elementId"]["guid"]
        ELEMENTS.pop(gone)  # e.g. removed with Archicad's own Undo
        deletes_before = sum(1 for r in FakeArchicad.received if r["parameters"].get("addOnCommandId", {}).get("commandName") == "DeleteElements")
        undone = call_json(await client.call_tool("archicad_undo", {}))
    item = undone["reverted"][0]
    assert item["problems"] is None and item["alreadyGone"] == 1
    sent = [r["parameters"]["addOnCommandParameters"]["elements"] for r in FakeArchicad.received
            if r["parameters"].get("addOnCommandId", {}).get("commandName") == "DeleteElements"][deletes_before:]
    assert len(sent) == 1 and gone not in [e["elementId"]["guid"] for e in sent[0]] and len(sent[0]) == 2
    assert len(ELEMENTS) == 4


@pytest.mark.anyio
async def test_set_slab_levels_on_layer_and_undo(archicad_port, monkeypatch):
    square = [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 8}, {"x": 0, "y": 8}]
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        vol = call_json(await client.call_tool("archicad_create_slabs", {"slabs": [{"polygon": square}] * 2, "topLevel": 5.15, "thickness": 5, "layer": "Volumenes"}))
        call_json(await client.call_tool("archicad_create_slabs", {"slabs": [{"polygon": square}], "topLevel": 0.15, "thickness": 0.15, "layer": "Manzanas"}))
        plan = call_json(await client.call_tool("archicad_set_slab_levels", {"layer": "Volumenes", "thickness": 2.5, "dryRun": True}))
        assert plan["slabs"] == 2 and plan["to"] == [[0.15, 2.65]]
        done = call_json(await client.call_tool("archicad_set_slab_levels", {"layer": "Volumenes", "thickness": 2.5}))
        assert done["problems"] is None
        ids = [e["elementId"]["guid"] for e in vol["elements"]]
        assert all(abs(SLABS[g][0] - 2.65) < 1e-9 and SLABS[g][1] == 2.5 for g in ids)
        assert sorted(v for g, v in SLABS.items() if g not in ids) == [(0.15, 0.15)]
        call_json(await client.call_tool("archicad_undo", {}))
    assert all(SLABS[g] == (5.15, 5) for g in ids)


@pytest.mark.anyio
async def test_set_mesh_level_holes_and_undo(archicad_port, monkeypatch):
    mesh = "00000000-0000-0000-0000-0000000000AA"
    ELEMENTS[mesh] = ("Mesh", "TR-1", 0)
    outline = [{"x": 0, "y": 0, "z": 0}, {"x": 5, "y": 0, "z": 0}, {"x": 5, "y": 5, "z": 0}]
    MESHES[mesh] = {"level": 1, "skirtLevel": 1, "holes": [], "polygonCoordinates": outline}
    hole = [{"x": 1, "y": 1}, {"x": 2, "y": 1}, {"x": 2, "y": 2}]
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        plan = call_json(await client.call_tool("archicad_set_mesh", {"guid": mesh, "level": 0.1, "skirtLevel": 0.4, "holes": [hole], "dryRun": True}))
        assert plan["change"]["holes"] == 1 and MESHES[mesh]["level"] == 1
        done = call_json(await client.call_tool("archicad_set_mesh", {"guid": mesh, "level": 0.1, "skirtLevel": 0.4, "holes": [hole]}))
        assert done["problems"] is None and done["z"] == [-0.3, 0.1] and done["previousZ"] == [0, 1]
        assert MESHES[mesh]["holes"][0]["polygonCoordinates"][0] == {"x": 1, "y": 1, "z": 0}
        sent = [r["parameters"]["addOnCommandParameters"] for r in FakeArchicad.received
                if r["parameters"].get("addOnCommandId", {}).get("commandName") == "ModifyMeshes"][-1]
        assert sent["meshesData"][0]["meshData"]["polygonCoordinates"] == outline  # Tapir needs it to apply holes
        bad = await client.call_tool("archicad_set_mesh", {"guid": WALLS[0], "level": 0})
        assert bad.is_error and "not a Mesh" in text(bad)
        call_json(await client.call_tool("archicad_undo", {}))
    assert MESHES[mesh]["level"] == 1 and MESHES[mesh]["skirtLevel"] == 1 and MESHES[mesh]["holes"] == []


@pytest.mark.anyio
async def test_create_meshes_with_heights_and_undo(archicad_port, monkeypatch):
    ground = [{"x": 0, "y": 0, "z": 0}, {"x": 10, "y": 0, "z": 0.4}, {"x": 10, "y": 8, "z": 0.9}, {"x": 0, "y": 8, "z": 0.5}]
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        done = call_json(await client.call_tool("archicad_create_meshes", {
            "meshes": [{"polygon": ground, "sublines": [[{"x": 5, "y": 4, "z": 0.6}, {"x": 5.01, "y": 4, "z": 0.6}]]}],
            "skirtLevel": 0.5, "layer": "PLAZA - Terreno"}))
        assert done["created"] == 1 and done["surfaceZ"] == [0, 0.9] and done["z"] == [[-0.5, 0.9]]
        g = done["elements"][0]["elementId"]["guid"]
        assert MESHES[g]["skirtType"] == "SolidBodyWithSkirt" and MESHES[g]["sublines"][0]["coordinates"][0]["z"] == 0.6
        assert LAYER_OF[g] == 2
        changed = call_json(await client.call_tool("archicad_set_mesh", {"guid": g, "sublines": [[{"x": 1, "y": 1, "z": 0.2}, {"x": 1.01, "y": 1, "z": 0.2}]]}))
        assert changed["problems"] is None and MESHES[g]["polygonCoordinates"] == ground
        call_json(await client.call_tool("archicad_undo", {"steps": 2}))
    assert g not in ELEMENTS


@pytest.mark.anyio
async def test_create_surface_morph_faces_up_with_material(archicad_port, monkeypatch):
    verts = [{"x": 0, "y": 0, "z": 0.1}, {"x": 1, "y": 0, "z": 0.2}, {"x": 0, "y": 1, "z": 0.3}, {"x": 1, "y": 1, "z": 0.4}]
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        bad = await client.call_tool("archicad_create_surface_morph", {"vertices": verts, "triangles": [[0, 1, 2]], "buildingMaterial": "Lava"})
        assert bad.is_error and "No building material" in text(bad)
        done = call_json(await client.call_tool("archicad_create_surface_morph", {
            "vertices": verts, "triangles": [[0, 1, 2], [1, 2, 3]], "buildingMaterial": "Gravel", "layer": "PLAZA - Sendero"}))
        assert done["faces"] == 2 and done["problems"] is None
        g = done["elements"][0]["elementId"]["guid"]
        body = MORPHS[g]["body"]
        assert MORPHS[g]["buildingMaterialId"] == {"guid": "MAT-GRAVEL"} and body["bodyType"] == "Surface"
        assert body["polygons"][1]["vertexIds"] == [1, 3, 2]  # flipped to face up
        call_json(await client.call_tool("archicad_undo", {}))
    assert g not in ELEMENTS


@pytest.mark.anyio
async def test_solid_morph_keeps_given_faces(archicad_port, monkeypatch):
    v = [{"x": 0, "y": 0, "z": 1}, {"x": 1, "y": 0, "z": 1}, {"x": 0, "y": 1, "z": 1},
         {"x": 0, "y": 0, "z": 0}, {"x": 1, "y": 0, "z": 0}, {"x": 0, "y": 1, "z": 0}]
    sides = [[0, 3, 4, 1], [1, 4, 5, 2], [2, 5, 3, 0], [3, 5, 4]]
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        done = call_json(await client.call_tool("archicad_create_surface_morph", {
            "vertices": v, "triangles": [[0, 1, 2]], "faces": sides, "solid": True, "buildingMaterial": "Gravel"}))
    body = MORPHS[done["elements"][0]["elementId"]["guid"]]["body"]
    assert body["bodyType"] == "Solid" and [f["vertexIds"] for f in body["polygons"]][1:] == sides


@pytest.mark.anyio
async def test_classify_elements_by_rule_and_undo(archicad_port, monkeypatch):
    async with Client(make_server(archicad_port, monkeypatch)) as client:
        items = call_json(await client.call_tool("archicad_classification_items", {"search": "wall"}))
        assert items["items"] == ["Site/Wall"]
        rules = [{"item": "Site/Wall", "elementType": "Wall"}]
        preview = call_json(await client.call_tool("archicad_classify_elements", {"rules": rules, "dryRun": True}))
        assert preview["matched"] == 3 and preview["byItem"] == {"Site/Wall": 3} and not CLASSIFIED.get(WALLS[0])
        done = call_json(await client.call_tool("archicad_classify_elements", {"rules": rules}))
        assert done["matched"] == 3 and {CLASSIFIED[w] for w in WALLS} == {"I-WALL"}
        again = call_json(await client.call_tool("archicad_classify_elements", {"rules": rules}))
        assert again["matched"] == 0
        # The zone is already classified: untouched unless overwrite is set.
        zone_rule = [{"item": "Wall", "elementType": "Zone"}]
        assert call_json(await client.call_tool("archicad_classify_elements", {"rules": zone_rule}))["matched"] == 0
        assert call_json(await client.call_tool("archicad_classify_elements", {"rules": zone_rule, "overwrite": True}))["matched"] == 1
        await client.call_tool("archicad_undo", {"steps": 2})
        assert CLASSIFIED == {ZONE: "X"}
        bad = await client.call_tool("archicad_classify_elements", {"rules": [{"item": "Nope"}]})
        assert bad.is_error
