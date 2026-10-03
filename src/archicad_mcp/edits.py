"""High-level tools that change the model, with a journal to revert them.

Archicad has no API to group several commands into one undo step, so every
change made here is recorded in a journal (a JSON file) together with what
is needed to revert it: the guids of created elements, or the previous IDs
and story names. archicad_undo replays that journal backwards.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

from .client import ArchicadClient, ArchicadError
from .insights import SCOPE_PROPERTIES, Insights, chunks

JOURNAL_LIMIT = 50
DRY_RUN = {
    "type": "boolean",
    "default": False,
    "description": "Only return what would change, without touching the model.",
}
POINT = {
    "type": "object",
    "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
    "required": ["x", "y"],
    "additionalProperties": False,
}

POINT3 = {
    "type": "object",
    "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number", "default": 0}},
    "required": ["x", "y"],
    "additionalProperties": False,
}

EDIT_SCHEMAS: dict[str, dict[str, Any]] = {
    "archicad_set_element_ids": {
        "type": "object",
        "properties": {
            **SCOPE_PROPERTIES,
            "ids": {
                "type": "array",
                "description": "Explicit new IDs.",
                "items": {
                    "type": "object",
                    "properties": {"guid": {"type": "string"}, "id": {"type": "string"}},
                    "required": ["guid", "id"],
                    "additionalProperties": False,
                },
            },
            "fixDuplicates": {
                "type": "boolean",
                "description": "Give a unique ID to every repeated one in scope: the first keeps it, the others "
                "get a -2, -3... suffix. Empty IDs are left alone.",
            },
            "numbering": {
                "type": "object",
                "description": "Renumber the elements in scope as prefix + zero-padded number, e.g. FU-001.",
                "properties": {
                    "prefix": {"type": "string"},
                    "start": {"type": "integer", "default": 1},
                    "digits": {"type": "integer", "minimum": 1, "default": 3},
                },
                "required": ["prefix"],
                "additionalProperties": False,
            },
            "dryRun": DRY_RUN,
        },
        "additionalProperties": False,
    },
    "archicad_classification_items": {
        "type": "object",
        "properties": {
            "system": {"type": "string", "description": "Part of the classification system name; the first one by default."},
            "search": {"type": "string", "description": "Text to look for in the item ID or its path."},
        },
        "additionalProperties": False,
    },
    "archicad_classify_elements": {
        "type": "object",
        "properties": {
            **SCOPE_PROPERTIES,
            "system": {"type": "string", "description": "Part of the classification system name; the first one by default."},
            "rules": {
                "type": "array",
                "minItems": 1,
                "description": "First matching rule wins for each element. A rule without elementType and layer matches everything.",
                "items": {
                    "type": "object",
                    "properties": {
                        "item": {
                            "type": "string",
                            "description": "Classification item ID (e.g. 'Terrain') or full path (e.g. 'Site/Massing/Morph'); see archicad_classification_items.",
                        },
                        "elementType": {"type": "string", "description": "Match elements of this type, e.g. 'Slab'."},
                        "layer": {"type": "string", "description": "Match elements on the layer with exactly this name."},
                    },
                    "required": ["item"],
                    "additionalProperties": False,
                },
            },
            "overwrite": {
                "type": "boolean",
                "default": False,
                "description": "Also reclassify elements that already have a classification in this system.",
            },
            "dryRun": DRY_RUN,
        },
        "required": ["rules"],
        "additionalProperties": False,
    },
    "archicad_create_room": {
        "type": "object",
        "properties": {
            "polygon": {
                "type": "array",
                "items": POINT,
                "minItems": 3,
                "description": "Room outline in meters, as the axis of the walls, in order (clockwise or not).",
            },
            "floorIndex": {"type": "integer", "default": 0, "description": "Story index from GetStories."},
            "walls": {"type": "boolean", "default": True},
            "wallHeight": {"type": "number", "exclusiveMinimum": 0, "description": "Defaults to the story height."},
            "wallThickness": {"type": "number", "exclusiveMinimum": 0, "default": 0.2},
            "slab": {"type": "boolean", "default": True, "description": "Floor slab with its top at the story level."},
            "slabThickness": {"type": "number", "exclusiveMinimum": 0, "default": 0.2},
            "slabStructure": {
                "type": "string",
                "enum": ["basic", "default"],
                "default": "basic",
                "description": "'basic' makes a homogeneous slab of slabThickness; 'default' keeps the slab tool's "
                "default structure (often a composite with its own fixed thickness).",
            },
            "zoneName": {"type": "string", "description": "Creates a zone with this name when given."},
            "zoneNumber": {"type": "string", "default": ""},
            "dryRun": DRY_RUN,
        },
        "required": ["polygon"],
        "additionalProperties": False,
    },
    "archicad_create_slabs": {
        "type": "object",
        "properties": {
            "slabs": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "polygon": {"type": "array", "items": POINT, "minItems": 3},
                        "holes": {"type": "array", "items": {"type": "array", "items": POINT, "minItems": 3}},
                    },
                    "required": ["polygon"],
                    "additionalProperties": False,
                },
                "description": "Slab outlines in meters, optionally with holes.",
            },
            "floorIndex": {"type": "integer", "default": 0},
            "topLevel": {"type": "number", "description": "Absolute height of the top face; defaults to the story level."},
            "thickness": {"type": "number", "exclusiveMinimum": 0, "default": 0.2},
            "layer": {"type": "string", "description": "Layer name; created when it does not exist."},
            "dryRun": DRY_RUN,
        },
        "required": ["slabs"],
        "additionalProperties": False,
    },
    "archicad_move_elements": {
        "type": "object",
        "properties": {
            **SCOPE_PROPERTIES,
            "vector": {
                "type": "object",
                "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number", "default": 0}},
                "required": ["x", "y"],
                "additionalProperties": False,
            },
            "dryRun": DRY_RUN,
        },
        "required": ["vector"],
        "additionalProperties": False,
    },
    "archicad_rotate_elements": {
        "type": "object",
        "properties": {
            **SCOPE_PROPERTIES,
            "origin": {
                "type": "object",
                "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                "required": ["x", "y"],
                "additionalProperties": False,
            },
            "degrees": {"type": "number", "description": "Counter-clockwise angle; negative is clockwise."},
            "dryRun": DRY_RUN,
        },
        "required": ["origin", "degrees"],
        "additionalProperties": False,
    },
    "archicad_set_slab_levels": {
        "type": "object",
        "properties": {
            **SCOPE_PROPERTIES,
            "layer": {"type": "string", "description": "Only slabs on this layer (combined with the scope)."},
            "topLevel": {"type": "number", "description": "New absolute height of the top face."},
            "thickness": {"type": "number", "exclusiveMinimum": 0, "description": "New thickness."},
            "keepBottom": {
                "type": "boolean",
                "default": True,
                "description": "When only thickness is given, keep the bottom face where it is (top moves).",
            },
            "dryRun": DRY_RUN,
        },
        "additionalProperties": False,
    },
    "archicad_set_mesh": {
        "type": "object",
        "properties": {
            "guid": {"type": "string", "description": "The Mesh to change."},
            "level": {"type": "number", "description": "New reference level (height of a point with z = 0)."},
            "skirtLevel": {"type": "number", "description": "New skirt depth below the mesh surface."},
            "polygon": {
                "type": "array",
                "items": POINT3,
                "minItems": 3,
                "description": "Replaces the outline; z is the height of each point above the mesh level.",
            },
            "holes": {
                "type": "array",
                "items": {"type": "array", "items": POINT3, "minItems": 3},
                "description": "Replaces the mesh holes with these outlines (e.g. where a path goes).",
            },
            "sublines": {
                "type": "array",
                "items": {"type": "array", "items": POINT3, "minItems": 2},
                "description": "Replaces the interior level lines (survey points as 2-point lines, contours...).",
            },
            "dryRun": DRY_RUN,
        },
        "required": ["guid"],
        "additionalProperties": False,
    },
    "archicad_create_meshes": {
        "type": "object",
        "properties": {
            "meshes": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "polygon": {"type": "array", "items": POINT3, "minItems": 3},
                        "holes": {"type": "array", "items": {"type": "array", "items": POINT3, "minItems": 3}},
                        "sublines": {"type": "array", "items": {"type": "array", "items": POINT3, "minItems": 2}},
                    },
                    "required": ["polygon"],
                    "additionalProperties": False,
                },
                "description": "Terrain pieces; z of each point is relative to level.",
            },
            "floorIndex": {"type": "integer", "default": 0},
            "level": {"type": "number", "default": 0},
            "skirtLevel": {"type": "number", "default": 0.5, "description": "Depth of the solid body below level."},
            "layer": {"type": "string", "description": "Layer name; created when it does not exist."},
            "dryRun": DRY_RUN,
        },
        "required": ["meshes"],
        "additionalProperties": False,
    },
    "archicad_create_surface_morph": {
        "type": "object",
        "properties": {
            "vertices": {"type": "array", "items": POINT3, "minItems": 3, "description": "World coordinates."},
            "triangles": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "integer"}, "minItems": 3, "maxItems": 3},
                "description": "Top faces as vertex indices; orientation is fixed to face up.",
            },
            "faces": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "integer"}, "minItems": 3},
                "description": "More faces used as given (counter-clockwise seen from outside), e.g. sides and bottom of a solid.",
            },
            "solid": {"type": "boolean", "default": False, "description": "Declare a closed solid instead of a surface."},
            "buildingMaterial": {"type": "string", "description": "Building material name, e.g. 'Gravel' or 'Concrete'."},
            "layer": {"type": "string"},
            "dryRun": DRY_RUN,
        },
        "required": ["vertices"],
        "additionalProperties": False,
    },
    "archicad_undo": {
        "type": "object",
        "properties": {
            "steps": {"type": "integer", "minimum": 1, "default": 1, "description": "How many actions to revert."}
        },
        "additionalProperties": False,
    },
    "archicad_history": {"type": "object", "properties": {}, "additionalProperties": False},
}

EDIT_DESCRIPTIONS = {
    "archicad_set_element_ids": (
        "Changes element IDs: explicit values, unique suffixes for repeated IDs (fixDuplicates) or a "
        "prefix + number sequence (numbering). Revert with archicad_undo."
    ),
    "archicad_classification_items": (
        "Lists the items of a classification system with their path, to use in archicad_classify_elements."
    ),
    "archicad_classify_elements": (
        "Classifies many elements at once with rules by element type and/or layer (e.g. every Slab on layer "
        "'02 PLAZA - Pasto' as 'Terrain'), by default only those still unclassified. Revert with archicad_undo."
    ),
    "archicad_create_room": (
        "Creates a room from an outline: walls along its edges, a floor slab and a zone, on one story. "
        "Revert with archicad_undo."
    ),
    "archicad_create_slabs": (
        "Creates many homogeneous slabs at once (site blocks, platforms, masses) with an exact top level and "
        "thickness, optionally on a named layer. Revert with archicad_undo."
    ),
    "archicad_move_elements": (
        "Moves elements (explicit list, selection or a type) by a vector in meters. Revert with archicad_undo."
    ),
    "archicad_rotate_elements": (
        "Rotates elements in plan around a point by an angle in degrees. Revert with archicad_undo."
    ),
    "archicad_set_slab_levels": (
        "Changes the top level and/or thickness of slabs (e.g. the height of site masses), verifying the "
        "result in 3D. Revert with archicad_undo."
    ),
    "archicad_set_mesh": (
        "Changes a Mesh (terrain): its level, skirt depth and holes. Works on locked layers and checks the "
        "result in 3D. Revert with archicad_undo."
    ),
    "archicad_create_meshes": (
        "Creates terrain Meshes with heights per point (surveyed ground, paths, curbs), as solid bodies, "
        "optionally on a named layer. Revert with archicad_undo."
    ),
    "archicad_create_surface_morph": (
        "Creates a Morph surface from triangles (e.g. a path draped on terrain) with its own building "
        "material, so it reads differently from the mesh below. Revert with archicad_undo."
    ),
    "archicad_undo": (
        "Reverts the last actions made with archicad_set_element_ids, archicad_classify_elements, archicad_create_slabs, "
        "archicad_create_meshes, archicad_move_elements, archicad_rotate_elements, archicad_set_slab_levels, "
        "archicad_set_mesh, archicad_create_surface_morph or "
        "archicad_create_room in the open project: deletes what they created and restores what they changed."
    ),
    "archicad_history": "Lists the actions archicad_undo can revert in the open project, newest first.",
}
READ_ONLY_EDIT_TOOLS = {"archicad_history", "archicad_classification_items"}


def journal_path() -> Path:
    return Path(os.environ.get("ARCHICAD_MCP_JOURNAL") or Path.home() / ".archicad-mcp" / "journal.json")


def element(guid: str) -> dict[str, Any]:
    return {"elementId": {"guid": guid}}


def created_guids(result: Any) -> tuple[list[str], list[str]]:
    guids, errors = [], []
    for item in (result or {}).get("elements", []):
        if "elementId" in item:
            guids.append(item["elementId"]["guid"])
        else:
            errors.append(item.get("error", {}).get("message", str(item)))
    return guids, errors


def contains(polygon: list[dict[str, float]], x: float, y: float) -> bool:
    inside = False
    for a, b in zip(polygon, polygon[1:] + polygon[:1]):
        if (a["y"] > y) != (b["y"] > y) and x < a["x"] + (y - a["y"]) * (b["x"] - a["x"]) / (b["y"] - a["y"]):
            inside = not inside
    return inside


def inset(polygon: list[dict[str, float]], distance: float) -> list[dict[str, float]]:
    """The polygon with every edge moved inwards by distance (mitred corners)."""
    if distance <= 0:
        return polygon
    area = sum(a["x"] * b["y"] - b["x"] * a["y"] for a, b in zip(polygon, polygon[1:] + polygon[:1]))
    sign = 1 if area > 0 else -1  # inwards is left of each edge for counter-clockwise outlines
    lines = []
    for a, b in zip(polygon, polygon[1:] + polygon[:1]):
        dx, dy = b["x"] - a["x"], b["y"] - a["y"]
        length = (dx * dx + dy * dy) ** 0.5
        nx, ny = -dy / length * distance * sign, dx / length * distance * sign
        lines.append(((a["x"] + nx, a["y"] + ny), (dx, dy)))
    result = []
    for (p1, d1), (p2, d2) in zip(lines[-1:] + lines[:-1], lines):
        cross = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(cross) < 1e-12:  # collinear edges: keep the shifted point
            result.append({"x": p2[0], "y": p2[1]})
            continue
        t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / cross
        result.append({"x": round(p1[0] + t * d1[0], 9), "y": round(p1[1] + t * d1[1], 9)})
    return result


def inner_point(polygon: list[dict[str, float]]) -> dict[str, float]:
    """A point inside the polygon: its centroid, or near a vertex for concave shapes."""
    cx = sum(p["x"] for p in polygon) / len(polygon)
    cy = sum(p["y"] for p in polygon) / len(polygon)
    if contains(polygon, cx, cy):
        return {"x": cx, "y": cy}
    for a, b, c in zip(polygon, polygon[1:] + polygon[:1], polygon[2:] + polygon[:2]):
        x, y = (a["x"] + b["x"] + c["x"]) / 3, (a["y"] + b["y"] + c["y"]) / 3
        if contains(polygon, x, y):
            return {"x": x, "y": y}
    return {"x": cx, "y": cy}


class Edits:
    def __init__(self, client: ArchicadClient, insights: Insights) -> None:
        self.client = client
        self.insights = insights

    async def run(self, name: str, args: dict[str, Any]) -> Any:
        handlers = {
            "archicad_set_element_ids": self.set_element_ids,
            "archicad_classification_items": self.classification_items,
            "archicad_classify_elements": self.classify_elements,
            "archicad_create_room": self.create_room,
            "archicad_create_slabs": self.create_slabs,
            "archicad_move_elements": self.move_elements,
            "archicad_rotate_elements": self.rotate_elements,
            "archicad_set_slab_levels": self.set_slab_levels,
            "archicad_set_mesh": self.set_mesh,
            "archicad_create_meshes": self.create_meshes,
            "archicad_create_surface_morph": self.create_surface_morph,
        }
        if name in handlers:
            return await handlers[name](args)
        if name == "archicad_undo":
            return await self.undo(args.get("steps", 1))
        if name == "archicad_history":
            project = await self.project()
            return [
                {k: e[k] for k in ("id", "time", "tool", "summary")}
                for e in reversed(self.load_journal())
                if e["project"] == project
            ]
        raise KeyError(name)

    # -- journal -----------------------------------------------------------

    async def project(self) -> str:
        info = await self.client.run_tapir_command("GetProjectInfo")
        return info.get("projectPath") or info.get("projectName") or "untitled"

    def load_journal(self) -> list[dict[str, Any]]:
        try:
            return json.loads(journal_path().read_text(encoding="utf8"))
        except (OSError, ValueError):
            return []

    def save_journal(self, entries: list[dict[str, Any]]) -> None:
        path = journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries[-JOURNAL_LIMIT:], indent=1, ensure_ascii=False), encoding="utf8")

    async def record(self, tool: str, summary: str, undo: list[dict[str, Any]]) -> int:
        entries = self.load_journal()
        entry_id = (entries[-1]["id"] + 1) if entries else 1
        entries.append(
            {
                "id": entry_id,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "project": await self.project(),
                "tool": tool,
                "summary": summary,
                "undo": undo,
            }
        )
        self.save_journal(entries)
        return entry_id

    async def undo(self, steps: int) -> dict[str, Any]:
        project = await self.project()
        entries = self.load_journal()
        mine = [e for e in entries if e["project"] == project][-steps:]
        if not mine:
            return {"reverted": [], "note": "Nothing to undo in this project."}
        reverted = []
        for entry in reversed(mine):
            problems: list[str] = []
            gone: list[str] = []
            for op in reversed(entry["undo"]):
                op_problems, op_gone = await self.apply_undo(op)
                problems += op_problems
                gone += op_gone
            item = {"id": entry["id"], "summary": entry["summary"], "problems": problems or None}
            if gone:
                # Removed meanwhile (e.g. Archicad's own Undo or a manual delete): nothing to revert for them.
                item["alreadyGone"] = len(gone)
            reverted.append(item)
            entries.remove(entry)
            self.save_journal(entries)
        return {"reverted": reverted}

    async def existing(self, guids: list[str]) -> set[str]:
        """The guids among these that still exist in the model."""
        found: set[str] = set()
        for part in chunks(guids):
            result = await self.client.run_tapir_command(
                "GetDetailsOfElements", {"elements": [element(g) for g in part], "fields": ["type"]}
            )
            found |= {g for g, d in zip(part, (result or {}).get("detailsOfElements", [])) if "error" not in d}
        return found

    async def apply_undo(self, op: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Reverts one operation; returns (problems, guids that no longer exist)."""
        if op["op"] in ("delete", "move", "rotate", "setIds", "slabLevels", "classify"):
            if op["op"] in ("setIds", "slabLevels", "classify"):
                guids = [v["guid"] for v in op["values"]]
            else:
                guids = [e["elementId"]["guid"] for e in op["elements"]]
            alive = await self.existing(guids)
            gone = [g for g in guids if g not in alive]
            if not alive:
                return [], gone
            elements = [element(g) for g in guids if g in alive]
            if op["op"] == "delete":
                result = await self.client.run_tapir_command("DeleteElements", {"elements": elements})
                problems = [
                    r.get("error", {}).get("message", "not deleted")
                    for r in (result or {}).get("executionResults", [])
                    if not r.get("success")
                ]
            elif op["op"] == "move":
                problems = await self.move(elements, op["vector"])
            elif op["op"] == "rotate":
                problems = await self.rotate(elements, op["origin"], op["degrees"])
            elif op["op"] == "slabLevels":
                problems = await self.write_slab_levels([v for v in op["values"] if v["guid"] in alive])
            elif op["op"] == "classify":
                problems = await self.write_classifications(op["system"], [v for v in op["values"] if v["guid"] in alive])
            else:
                problems = await self.write_ids([v for v in op["values"] if v["guid"] in alive])
            return problems, gone
        if op["op"] == "meshData":
            alive = await self.existing([op["guid"]])
            if not alive:
                return [], [op["guid"]]
            data = dict(op["data"])
            if "holes" in data and "polygonCoordinates" not in data:
                # Older journal entries did not keep the outline, which Tapir needs to apply holes.
                result = await self.client.run_tapir_command(
                    "GetDetailsOfElements", {"elements": [element(op["guid"])], "fields": ["details"]}
                )
                det = (result or {}).get("detailsOfElements", [{}])[0].get("details", {})
                data.update({"polygonCoordinates": det.get("polygonCoordinates"), "polygonArcs": det.get("polygonArcs") or []})
            problems, _, _ = await self.transform(
                [element(op["guid"])], "ModifyMeshes", {"meshesData": [{"elementId": {"guid": op["guid"]}, "meshData": data}]}
            )
            return problems, []
        if op["op"] == "setStories":
            # SetStories rebuilds the story structure and can delete elements
            # (seen on a real project), so story changes are never replayed.
            return ["Story names are not reverted automatically; rename them in Archicad (Design > Story Settings)."], []
        return [f"Unknown undo operation {op['op']}"], []

    # -- element IDs -------------------------------------------------------

    async def write_ids(self, values: list[dict[str, str]]) -> list[str]:
        prop = (await self.insights.property_ids(["General_ElementID"]))["General_ElementID"]
        result = await self.client.run_command(
            "API.SetPropertyValuesOfElements",
            {
                "elementPropertyValues": [
                    {"elementId": {"guid": v["guid"]}, "propertyId": prop, "propertyValue": {"type": "string", "status": "normal", "value": v["id"]}}
                    for v in values
                ]
            },
        )
        return [
            f"{v['guid']}: {r.get('error', {}).get('message', 'failed')}"
            for v, r in zip(values, (result or {}).get("executionResults", []))
            if not r.get("success")
        ]

    async def set_element_ids(self, args: dict[str, Any]) -> dict[str, Any]:
        modes = [m for m in ("ids", "fixDuplicates", "numbering") if args.get(m)]
        if len(modes) != 1:
            raise ArchicadError("Give exactly one of ids, fixDuplicates or numbering.")
        if args.get("ids"):
            scope = [element(i["guid"]) for i in args["ids"]]
            rows = await self.insights.rows(scope)
            wanted = {i["guid"]: i["id"] for i in args["ids"]}
        else:
            rows = await self.insights.rows(await self.insights.resolve_scope(args))
            wanted = {}
            if args.get("fixDuplicates"):
                every = await self.insights.rows(await self.insights.resolve_scope({}))
                taken = {str(r.get("id", "")) for r in every}
                seen: set[str] = set()
                for row in rows:
                    current = str(row.get("id", ""))
                    if not current.strip():
                        continue
                    if current in seen:
                        n = 2
                        while f"{current}-{n}" in taken:
                            n += 1
                        wanted[row["guid"]] = f"{current}-{n}"
                        taken.add(wanted[row["guid"]])
                    seen.add(current)
            else:
                spec = args["numbering"]
                number = spec.get("start", 1)
                for row in rows:
                    if "error" not in row:
                        wanted[row["guid"]] = f"{spec['prefix']}{number:0{spec.get('digits', 3)}d}"
                        number += 1
        changes = [
            {"guid": r["guid"], "type": r.get("type"), "from": r.get("id", ""), "to": wanted[r["guid"]]}
            for r in rows
            if r["guid"] in wanted and "error" not in r and r.get("id", "") != wanted[r["guid"]]
        ]
        if args.get("dryRun") or not changes:
            return {"dryRun": bool(args.get("dryRun")), "changes": changes}
        problems = await self.write_ids([{"guid": c["guid"], "id": c["to"]} for c in changes])
        entry = await self.record(
            "archicad_set_element_ids",
            f"Changed {len(changes)} element IDs",
            [{"op": "setIds", "values": [{"guid": c["guid"], "id": c["from"]} for c in changes]}],
        )
        return {"changes": changes, "problems": problems or None, "undoId": entry}

    # -- classifications ---------------------------------------------------

    async def classification_system(self, name: str | None) -> str:
        systems = (await self.client.run_command("API.GetAllClassificationSystems")).get("classificationSystems", [])
        found = [s for s in systems if not name or name.lower() in s.get("name", "").lower()]
        if not found:
            raise ArchicadError(f"No classification system matches {name!r}; existing: {[s.get('name') for s in systems]}.")
        return found[0]["classificationSystemId"]["guid"]

    async def classification_entries(self, system: str) -> list[dict[str, str]]:
        """Every item of the system as {path, id, guid}."""
        tree = await self.client.run_command(
            "API.GetAllClassificationsInSystem", {"classificationSystemId": {"guid": system}}
        )
        entries: list[dict[str, str]] = []

        def walk(nodes: list[dict[str, Any]], parent: str) -> None:
            for node in nodes:
                item = node["classificationItem"]
                path = f"{parent}/{item['id']}" if parent else item["id"]
                entries.append({"path": path, "id": item["id"], "guid": item["classificationItemId"]["guid"]})
                walk(item.get("children", []), path)

        walk(tree.get("classificationItems", []), "")
        return entries

    async def classification_items(self, args: dict[str, Any]) -> dict[str, Any]:
        system = await self.classification_system(args.get("system"))
        text = (args.get("search") or "").lower()
        return {"items": [e["path"] for e in await self.classification_entries(system) if text in e["path"].lower()]}

    async def write_classifications(self, system: str, values: list[dict[str, Any]]) -> list[str]:
        """Sets each element's item (guid), or unclassifies it when the item is None."""
        problems: list[str] = []
        for part in chunks(values):
            result = await self.client.run_tapir_command(
                "SetClassificationsOfElements",
                {
                    "elementClassifications": [
                        {
                            "elementId": {"guid": v["guid"]},
                            "classificationId": {
                                "classificationSystemId": {"guid": system},
                                **({"classificationItemId": {"guid": v["item"]}} if v["item"] else {}),
                            },
                        }
                        for v in part
                    ]
                },
            )
            problems += [
                f"{v['guid']}: {r.get('error', {}).get('message', 'failed')}"
                for v, r in zip(part, (result or {}).get("executionResults", []))
                if not r.get("success")
            ]
        return problems

    async def classify_elements(self, args: dict[str, Any]) -> dict[str, Any]:
        system = await self.classification_system(args.get("system"))
        entries = await self.classification_entries(system)
        names: dict[str, str] = {}
        rules = []
        for rule in args["rules"]:
            wanted = rule["item"].lower()
            found = [e for e in entries if e["path"].lower() == wanted] or [e for e in entries if e["id"].lower() == wanted]
            if len(found) != 1:
                raise ArchicadError(
                    f"Item {rule['item']!r} "
                    + (f"is ambiguous: {[e['path'] for e in found]}." if found else "was not found; use archicad_classification_items.")
                )
            names[found[0]["guid"]] = found[0]["path"]
            rules.append({**rule, "guid": found[0]["guid"]})

        rows = [r for r in await self.insights.rows(await self.insights.resolve_scope(args)) if "error" not in r]
        current: dict[str, str | None] = {}
        for part in chunks(rows):
            result = await self.client.run_command(
                "API.GetClassificationsOfElements",
                {
                    "elements": [element(r["guid"]) for r in part],
                    "classificationSystemIds": [{"classificationSystemId": {"guid": system}}],
                },
            )
            for r, item in zip(part, result.get("elementClassifications", [])):
                ids = item.get("classificationIds", [])
                current[r["guid"]] = next(
                    (c["classificationId"]["classificationItemId"]["guid"] for c in ids if "classificationItemId" in c.get("classificationId", {})),
                    None,
                )

        changes = []
        for row in rows:
            rule = next(
                (
                    r
                    for r in rules
                    if (not r.get("elementType") or r["elementType"].lower() == str(row["type"]).lower())
                    and (not r.get("layer") or r["layer"].lower() == str(row["layer"]).lower())
                ),
                None,
            )
            before = current.get(row["guid"])
            if rule and before != rule["guid"] and (before is None or args.get("overwrite")):
                changes.append({"guid": row["guid"], "type": row["type"], "layer": row["layer"], "item": rule["guid"], "before": before})
        by_item: dict[str, int] = {}
        for c in changes:
            by_item[names[c["item"]]] = by_item.get(names[c["item"]], 0) + 1
        summary = {"matched": len(changes), "byItem": by_item, "checkedElements": len(rows)}
        if args.get("dryRun") or not changes:
            return {"dryRun": bool(args.get("dryRun")), **summary, "sample": [{k: c[k] for k in ("guid", "type", "layer")} for c in changes[:10]]}
        problems = await self.write_classifications(system, changes)
        entry = await self.record(
            "archicad_classify_elements",
            f"Classified {len(changes)} elements ({', '.join(f'{n} x {k}' for k, n in by_item.items())})",
            [{"op": "classify", "system": system, "values": [{"guid": c["guid"], "item": c["before"]} for c in changes]}],
        )
        return {**summary, "problems": problems or None, "undoId": entry}

    # -- rooms -------------------------------------------------------------

    async def create_room(self, args: dict[str, Any]) -> dict[str, Any]:
        polygon = [{"x": p["x"], "y": p["y"]} for p in args["polygon"]]
        if len(polygon) > 3 and polygon[0] == polygon[-1]:
            polygon.pop()
        if len(polygon) < 3:
            raise ArchicadError("The outline needs at least 3 distinct points.")
        _, stories = await self.insights.stories()
        floor = args.get("floorIndex", 0)
        story = next((s for s in stories if s["index"] == floor), None)
        if story is None:
            raise ArchicadError(f"No story with index {floor}; existing: {[s['index'] for s in stories]}.")

        plan: dict[str, Any] = {}
        if args.get("walls", True):
            height = args.get("wallHeight") or story.get("height") or 3.0
            plan["walls"] = [
                {
                    "begCoordinate": a,
                    "endCoordinate": b,
                    "floorIndex": floor,
                    "zCoordinate": 0,
                    "height": height,
                    "thickness": args.get("wallThickness", 0.2),
                    "referenceLineLocation": "Center",
                }
                for a, b in zip(polygon, polygon[1:] + polygon[:1])
            ]
        if args.get("slab", True):
            plan["slabStructure"] = args.get("slabStructure", "basic")
            plan["slab"] = {
                "level": story.get("level", 0),
                "floorIndex": floor,
                "thickness": args.get("slabThickness", 0.2),
                "referencePlaneLocation": "Top",
                "polygonCoordinates": polygon,
            }
        if args.get("zoneName"):
            plan["zone"] = {
                "floorIndex": floor,
                "name": args["zoneName"],
                "numberStr": args.get("zoneNumber", ""),
                "geometry": {"referencePosition": inner_point(polygon)},
            }
            # Used when automatic placement fails: Archicad only detects the walls of
            # the story shown in the floor plan window.
            wall_inset = args.get("wallThickness", 0.2) / 2 if "walls" in plan else 0
            plan["zoneFallbackOutline"] = inset(polygon, wall_inset)
        if args.get("dryRun"):
            return {"dryRun": True, "plan": plan}

        created: list[str] = []
        problems: list[str] = []
        placement = None
        try:
            if "walls" in plan:
                guids, errors = created_guids(
                    await self.client.run_tapir_command("CreateWalls", {"wallsData": plan["walls"]})
                )
                created += guids
                problems += [f"wall: {e}" for e in errors]
            if "slab" in plan:
                guids, errors = created_guids(
                    await self.client.run_tapir_command("CreateSlabs", {"slabsData": [plan["slab"]]})
                )
                created += guids
                problems += [f"slab: {e}" for e in errors]
                if guids and plan["slabStructure"] == "basic":
                    # CreateSlabs keeps the tool's default structure, whose composite
                    # thickness overrides the requested one; make it homogeneous.
                    result = await self.client.run_tapir_command(
                        "ModifySlabs",
                        {
                            "slabsWithDetails": [
                                {
                                    "elementId": {"guid": guids[0]},
                                    "structureType": "Basic",
                                    "thickness": plan["slab"]["thickness"],
                                }
                            ]
                        },
                    )
                    problems += [
                        f"slab thickness: {r.get('error', {}).get('message', 'not changed')}"
                        for r in (result or {}).get("executionResults", [])
                        if not r.get("success")
                    ]
            if "zone" in plan:
                guids, errors = created_guids(
                    await self.client.run_tapir_command("CreateZones", {"zonesData": [plan["zone"]]})
                )
                placement = "automatic"
                if errors:
                    placement = f"inner outline of the walls (automatic failed: {'; '.join(errors)})"
                    plan["zone"]["geometry"] = {"polygonCoordinates": plan["zoneFallbackOutline"]}
                    guids, errors = created_guids(
                        await self.client.run_tapir_command("CreateZones", {"zonesData": [plan["zone"]]})
                    )
                created += guids
                problems += [f"zone: {e}" for e in errors]
        finally:
            entry = None
            if created:
                entry = await self.record(
                    "archicad_create_room",
                    f"Created room{' ' + args['zoneName'] if args.get('zoneName') else ''} "
                    f"({len(created)} elements) on story {floor}",
                    [{"op": "delete", "elements": [element(g) for g in created]}],
                )
        result = {"created": [element(g) for g in created], "problems": problems or None, "undoId": entry}
        if placement:
            result["zonePlacement"] = placement
        return result

    # -- slabs -------------------------------------------------------------

    async def assign_layer(self, guids: list[str], index: int) -> list[str]:
        """Puts elements on a layer; new elements may land on a locked default layer, so unlock it meanwhile."""
        problems, _, _ = await self.transform(
            [element(g) for g in guids],
            "SetDetailsOfElements",
            {"elementsWithDetails": [{"elementId": {"guid": g}, "details": {"layerIndex": index}} for g in guids]},
        )
        return [f"layer: {p}" for p in problems]

    async def layer_index(self, name: str) -> tuple[int, bool]:
        """Index of the layer with this name, creating it when missing."""
        for index, layer in (await self.insights.layers()).items():
            if layer.get("name") == name:
                return index, False
        result = await self.client.run_tapir_command(
            "CreateLayers", {"layerDataArray": [{"name": name}], "overwriteExisting": False}
        )
        errors = [i["error"].get("message") for i in (result or {}).get("attributeIds", []) if "error" in i]
        if errors:
            raise ArchicadError(f"Cannot create layer {name!r}: {errors[0]}")
        for index, layer in (await self.insights.layers()).items():
            if layer.get("name") == name:
                return index, True
        raise ArchicadError(f"Layer {name!r} was not found after creating it.")

    async def create_slabs(self, args: dict[str, Any]) -> dict[str, Any]:
        _, stories = await self.insights.stories()
        floor = args.get("floorIndex", 0)
        story = next((s for s in stories if s["index"] == floor), None)
        if story is None:
            raise ArchicadError(f"No story with index {floor}; existing: {[s['index'] for s in stories]}.")
        top = args.get("topLevel", story.get("level", 0))
        thickness = args.get("thickness", 0.2)
        data = [
            {
                "level": top,
                "floorIndex": floor,
                "thickness": thickness,
                "referencePlaneLocation": "Top",
                "polygonCoordinates": slab["polygon"],
                **({"holes": [{"polygonOutline": h} for h in slab["holes"]]} if slab.get("holes") else {}),
            }
            for slab in args["slabs"]
        ]
        summary = {
            "slabs": len(data),
            "topLevel": top,
            "bottomLevel": round(top - thickness, 4),
            "layer": args.get("layer"),
            "floorIndex": floor,
        }
        if args.get("dryRun"):
            return {"dryRun": True, **summary}

        created: list[str] = []
        problems: list[str] = []
        layer_note = None
        try:
            for part in chunks(data, 50):
                guids, errors = created_guids(await self.client.run_tapir_command("CreateSlabs", {"slabsData": part}))
                created += guids
                problems += [f"slab: {e}" for e in errors]
            if created:
                # CreateSlabs keeps the tool's default structure; make them homogeneous.
                result = await self.client.run_tapir_command(
                    "ModifySlabs",
                    {
                        "slabsWithDetails": [
                            {"elementId": {"guid": g}, "structureType": "Basic", "thickness": thickness} for g in created
                        ]
                    },
                )
                problems += [
                    f"thickness: {r.get('error', {}).get('message', 'not changed')}"
                    for r in (result or {}).get("executionResults", [])
                    if not r.get("success")
                ]
            if created and args.get("layer"):
                index, new = await self.layer_index(args["layer"])
                layer_note = f"{'created' if new else 'existing'} layer {args['layer']!r} (index {index})"
                problems += await self.assign_layer(created, index)
        finally:
            entry = None
            if created:
                entry = await self.record(
                    "archicad_create_slabs",
                    f"Created {len(created)} slabs"
                    + (f" on layer {args['layer']}" if args.get("layer") else "")
                    + f" (top {top:g} m, {thickness:g} m thick)",
                    [{"op": "delete", "elements": [element(g) for g in created]}],
                )
        return {
            **summary,
            "created": len(created),
            "elements": [element(g) for g in created],
            "layerNote": layer_note,
            "problems": problems or None,
            "undoId": entry,
        }

    # -- moving ------------------------------------------------------------

    async def set_layer_locks(self, layers: list[dict[str, Any]], locked: bool) -> None:
        """Rewrites layers with the given lock state, keeping their other settings."""
        await self.client.run_tapir_command(
            "CreateLayers",
            {
                "layerDataArray": [
                    {
                        "attributeId": layer["attributeId"],
                        "name": layer["name"],
                        "isHidden": layer.get("isHidden", False),
                        "isLocked": locked,
                        "isWireframe": layer.get("isWireframe", False),
                        "intersectionGroupNr": layer.get("intersectionGroupNr", 1),
                    }
                    for layer in layers
                ],
                "overwriteExisting": True,
            },
        )

    async def transform(self, elements: list[dict[str, Any]], command: str, parameters: dict[str, Any]) -> tuple[list[str], list, list]:
        """Runs MoveElements or RotateElements with locked layers unlocked meanwhile.

        Archicad silently keeps elements on locked layers in place (Tapir still
        reports success), so those layers are unlocked for the edit and locked again.
        Returns the problems and the 3D boxes before and after.
        """
        rows = await self.insights.rows(elements)
        used = sorted({r["_layerIndex"] for r in rows if "_layerIndex" in r})
        layers = await self.insights.layers()
        locked: list[dict[str, Any]] = []
        if used:
            result = await self.client.run_tapir_command(
                "GetLayers", {"attributeIds": [{"attributeId": layers[i]["attributeId"]} for i in used if i in layers]}
            )
            locked = [l for l in (result or {}).get("layers", []) if l.get("isLocked")]
        boxes = lambda r: [b.get("boundingBox3D") for b in (r or {}).get("boundingBoxes3D", [])]
        before = boxes(await self.client.run_tapir_command("Get3DBoundingBoxes", {"elements": elements}))
        if locked:
            await self.set_layer_locks(locked, False)
        try:
            result = await self.client.run_tapir_command(command, parameters)
        finally:
            if locked:
                await self.set_layer_locks(locked, True)
        problems = [
            f"{e['elementId']['guid']}: {r.get('error', {}).get('message', 'not changed')}"
            for e, r in zip(elements, (result or {}).get("executionResults", []))
            if not r.get("success")
        ]
        after = boxes(await self.client.run_tapir_command("Get3DBoundingBoxes", {"elements": elements}))
        return problems, before, after

    async def move(self, elements: list[dict[str, Any]], vector: dict[str, float]) -> list[str]:
        """Moves elements and checks that they really moved."""
        problems, before, after = await self.transform(
            elements, "MoveElements", {"elementsWithMoveVectors": [{**e, "moveVector": vector} for e in elements]}
        )
        for e, b0, b1 in zip(elements, before, after):
            if b0 and b1 and abs((b1["xMin"] - b0["xMin"]) - vector["x"]) + abs((b1["yMin"] - b0["yMin"]) - vector["y"]) > 0.2:
                problems.append(f"{e['elementId']['guid']}: did not move as asked")
        return problems

    async def rotate(self, elements: list[dict[str, Any]], origin: dict[str, float], degrees: float) -> list[str]:
        """Rotates elements around origin (counter-clockwise for positive degrees)."""
        a = math.radians(degrees)
        rotation = {
            "origin": origin,
            "beginPoint": {"x": origin["x"] + 10, "y": origin["y"]},
            "endPoint": {"x": origin["x"] + 10 * math.cos(a), "y": origin["y"] + 10 * math.sin(a)},
        }
        problems, before, after = await self.transform(
            elements, "RotateElements", {"elementsWithRotations": [{**e, "rotation": rotation} for e in elements]}
        )
        if abs(degrees) >= 0.1:
            for e, b0, b1 in zip(elements, before, after):
                if b0 and b1 and all(abs(b1[k] - b0[k]) < 1e-4 for k in ("xMin", "xMax", "yMin", "yMax")):
                    problems.append(f"{e['elementId']['guid']}: did not rotate")
        return problems

    async def scoped(self, args: dict[str, Any], verb: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Resolves the elements to move or rotate, with their display rows."""
        if not any(args.get(k) for k in ("elements", "selectedOnly", "elementType")):
            raise ArchicadError(f"Say what to {verb}: elements, selectedOnly or elementType.")
        elements = [element(e["elementId"]["guid"]) for e in await self.insights.resolve_scope(args)]
        rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in await self.insights.rows(elements)]
        return elements, rows

    async def rotate_elements(self, args: dict[str, Any]) -> dict[str, Any]:
        elements, rows = await self.scoped(args, "rotate")
        origin = {"x": args["origin"]["x"], "y": args["origin"]["y"]}
        degrees = args["degrees"]
        if args.get("dryRun") or not elements:
            return {"dryRun": bool(args.get("dryRun")), "origin": origin, "degrees": degrees, "elements": rows}
        problems = await self.rotate(elements, origin, degrees)
        entry = await self.record(
            "archicad_rotate_elements",
            f"Rotated {len(elements)} elements by {degrees:g}° around ({origin['x']:.2f}, {origin['y']:.2f})",
            [{"op": "rotate", "elements": elements, "origin": origin, "degrees": -degrees}],
        )
        return {"rotated": len(elements), "origin": origin, "degrees": degrees, "elements": rows, "problems": problems or None, "undoId": entry}

    async def move_elements(self, args: dict[str, Any]) -> dict[str, Any]:
        elements, rows = await self.scoped(args, "move")
        vector = {"x": args["vector"]["x"], "y": args["vector"]["y"], "z": args["vector"].get("z", 0)}
        if args.get("dryRun") or not elements:
            return {"dryRun": bool(args.get("dryRun")), "vector": vector, "elements": rows}
        problems = await self.move(elements, vector)
        entry = await self.record(
            "archicad_move_elements",
            f"Moved {len(elements)} elements by ({vector['x']:.3f}, {vector['y']:.3f}, {vector['z']:.3f}) m",
            [{"op": "move", "elements": elements, "vector": {k: -v for k, v in vector.items()}}],
        )
        return {"moved": len(elements), "vector": vector, "elements": rows, "problems": problems or None, "undoId": entry}

    # -- slab levels -------------------------------------------------------

    async def write_slab_levels(self, values: list[dict[str, Any]]) -> list[str]:
        """Sets top level and thickness of slabs and checks their 3D boxes afterwards."""
        problems: list[str] = []
        for part in chunks(values, 100):
            result = await self.client.run_tapir_command(
                "ModifySlabs",
                {
                    "slabsWithDetails": [
                        {"elementId": {"guid": v["guid"]}, "zCoordinate": v["top"], "thickness": v["thickness"]}
                        for v in part
                    ]
                },
            )
            problems += [
                f"{v['guid']}: {r.get('error', {}).get('message', 'not changed')}"
                for v, r in zip(part, (result or {}).get("executionResults", []))
                if not r.get("success")
            ]
            boxes = await self.client.run_tapir_command("Get3DBoundingBoxes", {"elements": [element(v["guid"]) for v in part]})
            for v, b in zip(part, (boxes or {}).get("boundingBoxes3D", [])):
                box = b.get("boundingBox3D")
                if box and (abs(box["zMax"] - v["top"]) > 0.01 or abs(box["zMin"] - (v["top"] - v["thickness"])) > 0.01):
                    problems.append(f"{v['guid']}: ended at z {box['zMin']:.3f}..{box['zMax']:.3f}")
        return problems

    async def set_slab_levels(self, args: dict[str, Any]) -> dict[str, Any]:
        if "topLevel" not in args and "thickness" not in args:
            raise ArchicadError("Give topLevel, thickness or both.")
        scoped = any(args.get(k) for k in ("elements", "selectedOnly", "elementType"))
        elements = await self.insights.resolve_scope(args if scoped else {"elementType": "Slab"})
        details = []
        for part in chunks(elements):
            result = await self.client.run_tapir_command(
                "GetDetailsOfElements", {"elements": part, "fields": ["type", "layerIndex", "details"]}
            )
            details += result.get("detailsOfElements", [])
        layer_index = None
        if args.get("layer"):
            layer_index = next((i for i, l in (await self.insights.layers()).items() if l.get("name") == args["layer"]), None)
            if layer_index is None:
                raise ArchicadError(f"No layer named {args['layer']!r}.")
        old, new = [], []
        for e, d in zip(elements, details):
            if d.get("type") != "Slab" or (layer_index is not None and d.get("layerIndex") != layer_index):
                continue
            det = d.get("details", {})
            top, thickness = det.get("zCoordinate"), det.get("thickness")
            if det.get("referencePlaneLocation") != "Top" or top is None or thickness is None:
                continue  # only slabs referenced to their top face are handled
            new_thickness = args.get("thickness", thickness)
            if "topLevel" in args:
                new_top = args["topLevel"]
            elif args.get("keepBottom", True):
                new_top = top - thickness + new_thickness
            else:
                new_top = top
            guid = e["elementId"]["guid"]
            old.append({"guid": guid, "top": top, "thickness": thickness})
            new.append({"guid": guid, "top": round(new_top, 6), "thickness": new_thickness})
        summary = {
            "slabs": len(new),
            "from": sorted({(o["top"] - o["thickness"], o["top"]) for o in old}),
            "to": sorted({(round(n["top"] - n["thickness"], 6), n["top"]) for n in new}),
        }
        if args.get("dryRun") or not new:
            return {"dryRun": bool(args.get("dryRun")), **summary}
        problems = await self.write_slab_levels(new)
        entry = await self.record(
            "archicad_set_slab_levels",
            f"Changed {len(new)} slabs to z {summary['to'][0][0]:g}..{summary['to'][0][1]:g} m"
            + (f" on layer {args['layer']}" if args.get("layer") else ""),
            [{"op": "slabLevels", "values": old}],
        )
        return {**summary, "problems": problems or None, "undoId": entry}

    # -- meshes ------------------------------------------------------------

    async def set_mesh(self, args: dict[str, Any]) -> dict[str, Any]:
        guid = args["guid"]
        result = await self.client.run_tapir_command(
            "GetDetailsOfElements", {"elements": [element(guid)], "fields": ["type", "details"]}
        )
        info = (result or {}).get("detailsOfElements", [{}])[0]
        if info.get("type") != "Mesh":
            raise ArchicadError(f"{guid} is not a Mesh ({info.get('type') or info.get('error')}).")
        det = info.get("details", {})
        # Tapir only applies holes when the outline is sent in the same call.
        outline = {"polygonCoordinates": det.get("polygonCoordinates"), "polygonArcs": det.get("polygonArcs") or []}
        previous = {
            "level": det.get("level"),
            "skirtLevel": det.get("skirtLevel"),
            "holes": det.get("holes") or [],
            **outline,
        }
        p3 = lambda pts: [{"x": p["x"], "y": p["y"], "z": p.get("z", 0)} for p in pts]
        data: dict[str, Any] = {k: args[k] for k in ("level", "skirtLevel") if k in args}
        if "holes" in args:
            data["holes"] = [{"polygonCoordinates": p3(h)} for h in args["holes"]]
        if "sublines" in args:
            data["sublines"] = [{"coordinates": p3(l)} for l in args["sublines"]]
            previous["sublines"] = det.get("sublines") or []
        if "polygon" in args:
            data["polygonCoordinates"] = p3(args["polygon"])
            data["polygonArcs"] = []
        if "holes" in data or "sublines" in data:
            # Tapir only applies holes and level lines when the outline is sent in the same call.
            data.setdefault("polygonCoordinates", outline["polygonCoordinates"])
            data.setdefault("polygonArcs", outline["polygonArcs"])
        if not data:
            raise ArchicadError("Give level, skirtLevel or holes.")
        if args.get("dryRun"):
            shown = {k: v for k, v in data.items() if not k.startswith("polygon")}
            return {
                "dryRun": True,
                "previous": {"level": previous["level"], "skirtLevel": previous["skirtLevel"], "holes": len(previous["holes"])},
                "change": {**shown, **({"holes": len(data["holes"])} if "holes" in data else {})},
            }
        problems, before, after = await self.transform(
            [element(guid)], "ModifyMeshes", {"meshesData": [{"elementId": {"guid": guid}, "meshData": data}]}
        )
        b0, b1 = (before or [None])[0], (after or [None])[0]
        if b0 and b1 and all(abs(b1[k] - b0[k]) < 1e-4 for k in ("zMin", "zMax")) and ("level" in data or "skirtLevel" in data):
            problems.append("the mesh did not change height")
        if "holes" in data:
            check = await self.client.run_tapir_command(
                "GetDetailsOfElements", {"elements": [element(guid)], "fields": ["details"]}
            )
            got = len(((check or {}).get("detailsOfElements", [{}])[0].get("details", {}) or {}).get("holes") or [])
            if got != len(data["holes"]):
                problems.append(
                    f"asked for {len(data['holes'])} holes, the mesh has {got}: holes must lie strictly inside the outline"
                )
        entry = await self.record(
            "archicad_set_mesh",
            f"Changed mesh {guid[:8]}: "
            + ", ".join(f"{k} {v if k != 'holes' else len(v)}" for k, v in data.items() if not k.startswith("polygon")),
            [{"op": "meshData", "guid": guid, "data": previous}],
        )
        return {
            "z": [round(b1["zMin"], 4), round(b1["zMax"], 4)] if b1 else None,
            "previousZ": [round(b0["zMin"], 4), round(b0["zMax"], 4)] if b0 else None,
            "problems": problems or None,
            "undoId": entry,
        }

    async def create_meshes(self, args: dict[str, Any]) -> dict[str, Any]:
        p3 = lambda pts: [{"x": p["x"], "y": p["y"], "z": p.get("z", 0)} for p in pts]
        level, skirt = args.get("level", 0), args.get("skirtLevel", 0.5)
        data = [
            {
                "floorIndex": args.get("floorIndex", 0),
                "level": level,
                "skirtType": "SolidBodyWithSkirt",
                "skirtLevel": skirt,
                "polygonCoordinates": p3(m["polygon"]),
                **({"holes": [{"polygonCoordinates": p3(h)} for h in m["holes"]]} if m.get("holes") else {}),
                **({"sublines": [{"coordinates": p3(l)} for l in m["sublines"]]} if m.get("sublines") else {}),
            }
            for m in args["meshes"]
        ]
        zs = [p["z"] for d in data for p in d["polygonCoordinates"]]
        summary = {"meshes": len(data), "surfaceZ": [round(level + min(zs), 3), round(level + max(zs), 3)], "layer": args.get("layer")}
        if args.get("dryRun"):
            return {"dryRun": True, **summary}
        created: list[str] = []
        problems: list[str] = []
        layer_note = None
        try:
            for part in chunks(data, 50):
                guids, errors = created_guids(await self.client.run_tapir_command("CreateMeshes", {"meshesData": part}))
                created += guids
                problems += [f"mesh: {e}" for e in errors]
            if created and args.get("layer"):
                index, new = await self.layer_index(args["layer"])
                layer_note = f"{'created' if new else 'existing'} layer {args['layer']!r} (index {index})"
                problems += await self.assign_layer(created, index)
        finally:
            entry = None
            if created:
                entry = await self.record(
                    "archicad_create_meshes",
                    f"Created {len(created)} meshes" + (f" on layer {args['layer']}" if args.get("layer") else ""),
                    [{"op": "delete", "elements": [element(g) for g in created]}],
                )
        boxes = await self.client.run_tapir_command("Get3DBoundingBoxes", {"elements": [element(g) for g in created]}) if created else {}
        return {
            **summary,
            "created": len(created),
            "elements": [element(g) for g in created],
            "z": [[round(b["boundingBox3D"]["zMin"], 3), round(b["boundingBox3D"]["zMax"], 3)] for b in (boxes or {}).get("boundingBoxes3D", [])],
            "layerNote": layer_note,
            "problems": problems or None,
            "undoId": entry,
        }

    # -- morphs ------------------------------------------------------------

    async def create_surface_morph(self, args: dict[str, Any]) -> dict[str, Any]:
        verts = [{"x": v["x"], "y": v["y"], "z": v.get("z", 0)} for v in args["vertices"]]
        faces = []
        for tri in args.get("triangles", []):
            a, b, c = (verts[i] for i in tri)
            cross = (b["x"] - a["x"]) * (c["y"] - a["y"]) - (b["y"] - a["y"]) * (c["x"] - a["x"])
            faces.append({"vertexIds": list(tri) if cross >= 0 else [tri[0], tri[2], tri[1]]})  # counter-clockwise from above
        faces += [{"vertexIds": list(f)} for f in args.get("faces", [])]
        if not faces:
            raise ArchicadError("Give triangles and/or faces.")
        material_id = None
        if args.get("buildingMaterial"):
            result = await self.client.run_tapir_command("GetAttributesByType", {"attributeType": "BuildingMaterial"})
            material_id = next(
                (a["attributeId"] for a in result.get("attributes", []) if a.get("name") == args["buildingMaterial"]), None
            )
            if material_id is None:
                raise ArchicadError(f"No building material named {args['buildingMaterial']!r}.")
        summary = {"vertices": len(verts), "faces": len(faces), "z": [min(v["z"] for v in verts), max(v["z"] for v in verts)]}
        if args.get("dryRun"):
            return {"dryRun": True, **summary}
        data = {
            "basePoint": {"x": 0, "y": 0, "z": 0},
            "body": {"bodyType": "Solid" if args.get("solid") else "Surface", "vertices": verts, "polygons": faces},
            **({"buildingMaterialId": material_id} if material_id else {}),
        }
        guids, errors = created_guids(await self.client.run_tapir_command("CreateMorphs", {"morphsData": [data]}))
        problems = [f"morph: {e}" for e in errors]
        layer_note = None
        entry = None
        if guids:
            entry = await self.record(
                "archicad_create_surface_morph",
                f"Created {'solid' if args.get('solid') else 'surface'} morph ({len(faces)} faces)" + (f" on layer {args['layer']}" if args.get("layer") else ""),
                [{"op": "delete", "elements": [element(g) for g in guids]}],
            )
            if args.get("layer"):
                index, new = await self.layer_index(args["layer"])
                layer_note = f"{'created' if new else 'existing'} layer {args['layer']!r} (index {index})"
                problems += await self.assign_layer(guids, index)
        boxes = await self.client.run_tapir_command("Get3DBoundingBoxes", {"elements": [element(g) for g in guids]}) if guids else {}
        return {
            **summary,
            "elements": [element(g) for g in guids],
            "box": [b["boundingBox3D"] for b in (boxes or {}).get("boundingBoxes3D", [])],
            "layerNote": layer_note,
            "problems": problems or None,
            "undoId": entry,
        }
