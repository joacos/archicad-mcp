"""High-level, read-only tools built on several Tapir and JSON API commands.

They answer common questions in one call (what is in the model, how much of
it, what is wrong with it) and return compact rows instead of the full
element details, which keeps responses small.
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from typing import Any

from .client import ArchicadClient, ArchicadError

CHUNK = 500

# Built-in properties reported per element type, as (name, additive). Additive
# values are summed in the totals; the others (heights, thicknesses) are not.
QUANTITY_PROPERTIES: dict[str, list[tuple[str, bool]]] = {
    "Wall": [
        ("General_3DLength", True),
        ("General_Height", False),
        ("General_Thickness", False),
        ("Wall_NetInsideSurfaceArea", True),
        ("Wall_NetOutsideSurfaceArea", True),
        ("General_NetVolume", True),
    ],
    "Slab": [
        ("General_NetTopSurfaceArea", True),
        ("General_FloorPlanPerimeter", True),
        ("General_Thickness", False),
        ("General_NetVolume", True),
    ],
    "Roof": [("Roof_GrossTopSurfaceArea", True), ("General_Thickness", False), ("General_NetVolume", True)],
    "Shell": [("Shell_NetSurfaceAreaOfReferenceSide", True), ("General_NetVolume", True)],
    "Column": [("General_Height", True), ("General_NetVolume", True)],
    "Beam": [("General_3DLength", True), ("General_NetVolume", True)],
    "Zone": [("Zone_CalculatedArea", True), ("Zone_NetArea", True), ("Zone_Perimeter", True)],
    "Window": [("WindowDoor_SurfaceArea", True)],
    "Door": [("WindowDoor_SurfaceArea", True)],
    "Skylight": [("Skylight_OpeningArea", True)],
    "CurtainWall": [("CurtainWall_Length", True), ("CurtainWall_SurfaceAreaOfPanels", True)],
    "Railing": [("Railing_ReferenceLine2DLength", True)],
    "Mesh": [("General_SurfaceArea", True), ("General_NetVolume", True)],
    "Morph": [("General_SurfaceArea", True), ("General_NetVolume", True)],
}
DEFAULT_QUANTITIES = [("General_NetVolume", True)]
UNITS = {"length": "m", "area": "m²", "volume": "m³", "angle": "rad"}

CHECKS = {
    "missing_id": "Elements with an empty ID.",
    "duplicate_id": "IDs used by more than one element.",
    "unclassified": "Elements without a classification in any system.",
    "duplicate_geometry": "Elements of the same type with an identical 3D bounding box (probable duplicates).",
    "hidden_or_locked_layer": "Elements on hidden or locked layers.",
    "zero_area_zone": "Zones whose calculated area is zero or not available.",
    "unnamed_story": "Stories without a name.",
    "empty_story": "Stories without any element.",
}

SCOPE_PROPERTIES = {
    "elementType": {
        "type": "string",
        "description": "Only elements of this type, e.g. 'Wall', 'Slab', 'Zone', 'Object', 'Window'.",
    },
    "selectedOnly": {"type": "boolean", "description": "Only the elements selected in Archicad.", "default": False},
    "elements": {
        "type": "array",
        "description": "Explicit elements, as returned by other tools: [{\"elementId\": {\"guid\": \"...\"}}].",
        "items": {
            "type": "object",
            "properties": {
                "elementId": {"type": "object", "properties": {"guid": {"type": "string"}}, "required": ["guid"]}
            },
            "required": ["elementId"],
        },
    },
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "archicad_model_summary": {"type": "object", "properties": {}, "additionalProperties": False},
    "archicad_list_elements": {
        "type": "object",
        "properties": {
            **SCOPE_PROPERTIES,
            "limit": {"type": "integer", "minimum": 1, "default": 200, "description": "Maximum rows returned."},
        },
        "additionalProperties": False,
    },
    "archicad_quantities": {
        "type": "object",
        "properties": {
            **SCOPE_PROPERTIES,
            "groupBy": {
                "type": "string",
                "enum": ["type", "story", "layer", "element"],
                "default": "type",
                "description": "Group totals by element type (always), plus story or layer; 'element' lists every element.",
            },
            "extraProperties": {
                "type": "array",
                "items": {"type": "string"},
                "description": "More built-in properties by non-localized name, e.g. 'General_GrossVolume'.",
            },
            "format": {"type": "string", "enum": ["json", "csv"], "default": "json"},
        },
        "additionalProperties": False,
    },
    "archicad_check_model": {
        "type": "object",
        "properties": {
            **SCOPE_PROPERTIES,
            "checks": {
                "type": "array",
                "items": {"type": "string", "enum": list(CHECKS)},
                "description": "Checks to run; all when omitted. "
                + " ".join(f"{name}: {text}" for name, text in CHECKS.items()),
            },
            "maxPerCheck": {"type": "integer", "minimum": 1, "default": 50},
        },
        "additionalProperties": False,
    },
    "archicad_element_images": {
        "type": "object",
        "properties": {
            **SCOPE_PROPERTIES,
            "imageType": {"type": "string", "enum": ["3D", "2D", "Section"], "default": "3D"},
            "size": {"type": "integer", "minimum": 64, "maximum": 1024, "default": 256, "description": "Pixels per side."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
        },
        "additionalProperties": False,
    },
}

DESCRIPTIONS = {
    "archicad_model_summary": (
        "One-call overview of the open project: project info, stories, element counts by type, story and "
        "layer, zone areas and the current selection. Start here to understand a model."
    ),
    "archicad_list_elements": (
        "Compact list of elements (guid, type, ID, story name, layer name). Much smaller than "
        "GetDetailsOfElements; use it to find elements before acting on them."
    ),
    "archicad_quantities": (
        "Quantity take-off from Archicad's built-in properties: lengths (m), areas (m²), volumes (m³) and "
        "counts, totalled by element type and optionally by story or layer, as JSON or CSV."
    ),
    "archicad_element_images": (
        "Returns images of elements so you can see them: a 3D, 2D or section preview of each element, and "
        "a floor plan clip of each zone. Defaults to the selection when no scope is given."
    ),
    "archicad_check_model": (
        "Model quality checks: missing or duplicate IDs, unclassified elements, probable duplicate "
        "elements, elements on hidden or locked layers, zones without area, unnamed or empty stories."
    ),
}


def chunks(items: list[Any], size: int = CHUNK) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class Insights:
    def __init__(self, client: ArchicadClient) -> None:
        self.client = client
        self._property_ids: dict[str, dict[str, Any] | None] = {}

    async def run(self, name: str, args: dict[str, Any]) -> Any:
        if name == "archicad_model_summary":
            return await self.model_summary()
        if name == "archicad_list_elements":
            return await self.list_elements(args)
        if name == "archicad_quantities":
            return await self.quantities(args)
        if name == "archicad_check_model":
            return await self.check_model(args)
        raise KeyError(name)

    # -- building blocks ---------------------------------------------------

    async def resolve_scope(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        if args.get("elements"):
            return args["elements"]
        if args.get("selectedOnly"):
            return (await self.client.run_tapir_command("GetSelectedElements")).get("elements", [])
        if args.get("elementType"):
            result = await self.client.run_tapir_command("GetElementsByType", {"elementType": args["elementType"]})
            return result.get("elements", [])
        return (await self.client.run_tapir_command("GetAllElements")).get("elements", [])

    async def stories(self) -> tuple[dict[int, str], list[dict[str, Any]]]:
        stories = (await self.client.run_tapir_command("GetStories")).get("stories", [])
        names = {s["index"]: s.get("name") or f"Story {s['index']} ({s.get('level', 0):g} m)" for s in stories}
        return names, stories

    async def layers(self) -> dict[int, dict[str, Any]]:
        result = await self.client.run_tapir_command("GetAttributesByType", {"attributeType": "Layer"})
        return {a["index"]: a for a in result.get("attributes", []) if "index" in a}

    async def details(self, elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Type, ID, story and layer of each element, in input order."""
        rows = []
        for part in chunks(elements):
            result = await self.client.run_tapir_command(
                "GetDetailsOfElements", {"elements": part, "fields": ["type", "id", "floorIndex", "layerIndex"]}
            )
            rows += result.get("detailsOfElements", [])
        return rows

    async def rows(self, elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        story_names, _ = await self.stories()
        layers = await self.layers()
        rows = []
        for element, detail in zip(elements, await self.details(elements)):
            if "error" in detail:
                rows.append({"guid": element["elementId"]["guid"], "error": detail["error"].get("message")})
                continue
            layer = layers.get(detail.get("layerIndex"), {})
            rows.append(
                {
                    "guid": element["elementId"]["guid"],
                    "type": detail.get("type"),
                    "id": detail.get("id", ""),
                    "story": story_names.get(detail.get("floorIndex"), detail.get("floorIndex")),
                    "layer": layer.get("name", detail.get("layerIndex")),
                    "_floorIndex": detail.get("floorIndex"),
                    "_layerIndex": detail.get("layerIndex"),
                }
            )
        return rows

    async def property_ids(self, names: list[str]) -> dict[str, dict[str, Any]]:
        missing = [n for n in names if n not in self._property_ids]
        if missing:
            result = await self.client.run_command(
                "API.GetPropertyIds",
                {"properties": [{"type": "BuiltIn", "nonLocalizedName": n} for n in missing]},
            )
            for name, item in zip(missing, result.get("properties", [])):
                self._property_ids[name] = item.get("propertyId")
        return {n: self._property_ids[n] for n in names if self._property_ids.get(n)}

    async def property_values(
        self, elements: list[dict[str, Any]], names: list[str]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Per element {name: value}, plus the unit type of each property seen."""
        ids = await self.property_ids(names)
        known = [n for n in names if n in ids]
        values: list[dict[str, Any]] = []
        kinds: dict[str, str] = {}
        if not known or not elements:
            return [{} for _ in elements], kinds
        properties = [{"propertyId": ids[n]} for n in known]
        for part in chunks(elements):
            result = await self.client.run_command(
                "API.GetPropertyValuesOfElements", {"elements": part, "properties": properties}
            )
            for item in result.get("propertyValuesForElements", []):
                row: dict[str, Any] = {}
                for name, value in zip(known, item.get("propertyValues", [])):
                    pv = value.get("propertyValue", {})
                    if pv.get("status") == "normal":
                        row[name] = pv.get("value")
                        kinds.setdefault(name, pv.get("type", ""))
                values.append(row)
        return values, kinds

    # -- tools -------------------------------------------------------------

    async def element_images(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        """[{caption, data (base64), mimeType}] or [{caption, error}] per element."""
        scoped = any(args.get(k) for k in ("elements", "selectedOnly", "elementType"))
        elements = await self.resolve_scope(args if scoped else {"selectedOnly": True})
        size = args.get("size", 256)
        images = []
        for row in await self.rows(elements[: args.get("limit", 6)]):
            if "error" in row:
                images.append({"caption": row["guid"], "error": row["error"]})
                continue
            caption = f"{row['type']} {row['id'] or '(no ID)'} — {row['story']}, {row['layer']} ({row['guid']})"
            try:
                if row["type"] == "Zone":
                    result = await self.client.run_tapir_command(
                        "GetRoomImage", {"zoneId": {"guid": row["guid"]}, "width": size, "height": size}
                    )
                    data = result.get("roomImage")
                else:
                    result = await self.client.run_tapir_command(
                        "GetElementPreviewImage",
                        {
                            "elementId": {"guid": row["guid"]},
                            "imageType": args.get("imageType", "3D"),
                            "width": size,
                            "height": size,
                        },
                    )
                    data = result.get("previewImage")
            except ArchicadError as error:
                images.append({"caption": caption, "error": str(error)})
                continue
            images.append({"caption": caption, "data": data, "mimeType": "image/png"} if data else {"caption": caption, "error": "No image returned."})
        return images

    async def model_summary(self) -> dict[str, Any]:
        project = await self.client.run_tapir_command("GetProjectInfo")
        story_names, stories = await self.stories()
        elements = (await self.client.run_tapir_command("GetAllElements")).get("elements", [])
        selected = (await self.client.run_tapir_command("GetSelectedElements")).get("elements", [])
        rows = await self.rows(elements)
        by_story: dict[str, Counter] = defaultdict(Counter)
        for row in rows:
            by_story[str(row.get("story"))][row.get("type")] += 1
        zones = [{"elementId": {"guid": r["guid"]}} for r in rows if r.get("type") == "Zone"]
        zone_area = None
        if zones:
            values, _ = await self.property_values(zones, ["Zone_CalculatedArea"])
            zone_area = round(sum(v.get("Zone_CalculatedArea") or 0 for v in values), 3)
        return {
            "project": {k: project.get(k) for k in ("projectName", "projectLocation", "isTeamwork", "isUntitled")},
            "stories": [
                {
                    "index": s["index"],
                    "name": story_names[s["index"]],
                    "level": s.get("level"),
                    "height": s.get("height"),
                    "elements": sum(by_story.get(story_names[s["index"]], Counter()).values()),
                }
                for s in stories
            ],
            "elementCount": len(rows),
            "byType": dict(Counter(r.get("type") for r in rows).most_common()),
            "byStory": {story: dict(counts.most_common()) for story, counts in by_story.items()},
            "byLayer": dict(Counter(str(r.get("layer")) for r in rows).most_common(25)),
            "zones": {"count": len(zones), "totalCalculatedArea_m2": zone_area},
            "selection": {"count": len(selected), "byType": dict(Counter(r.get("type") for r in await self.rows(selected)))}
            if selected
            else {"count": 0},
        }

    async def list_elements(self, args: dict[str, Any]) -> dict[str, Any]:
        elements = await self.resolve_scope(args)
        limit = args.get("limit", 200)
        rows = await self.rows(elements[:limit])
        result: dict[str, Any] = {
            "total": len(elements),
            "elements": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
        }
        if len(elements) > limit:
            result["note"] = f"Showing {limit} of {len(elements)}. Raise limit or narrow with elementType."
        return result

    async def quantities(self, args: dict[str, Any]) -> Any:
        elements = await self.resolve_scope(args)
        rows = await self.rows(elements)
        group_by = args.get("groupBy", "type")
        extra = [(n, True) for n in args.get("extraProperties", [])]
        by_type: dict[str, list[int]] = defaultdict(list)
        for i, row in enumerate(rows):
            if "error" not in row:
                by_type[row["type"]].append(i)

        per_element: list[dict[str, Any]] = [{} for _ in rows]
        additive: dict[str, bool] = {}
        kinds: dict[str, str] = {}
        for type_name, indexes in by_type.items():
            spec = QUANTITY_PROPERTIES.get(type_name, DEFAULT_QUANTITIES) + extra
            additive.update({n: a for n, a in spec})
            values, seen = await self.property_values(
                [{"elementId": {"guid": rows[i]["guid"]}} for i in indexes], [n for n, _ in spec]
            )
            kinds.update(seen)
            for i, value in zip(indexes, values):
                per_element[i] = value
        for name in kinds:
            if kinds[name] not in UNITS:
                additive[name] = False

        if group_by == "element":
            table = [
                {**{k: v for k, v in row.items() if not k.startswith("_")}, **per_element[i]}
                for i, row in enumerate(rows)
                if "error" not in row
            ]
        else:
            groups: dict[tuple, dict[str, Any]] = {}
            for i, row in enumerate(rows):
                if "error" in row:
                    continue
                key = (row["type"],) + ((row[group_by],) if group_by in ("story", "layer") else ())
                group = groups.setdefault(
                    key,
                    {"type": row["type"], **({group_by: row[group_by]} if len(key) > 1 else {}), "count": 0},
                )
                group["count"] += 1
                for name, value in per_element[i].items():
                    if additive.get(name) and isinstance(value, (int, float)):
                        group[name] = group.get(name, 0) + value
            table = sorted(groups.values(), key=lambda g: (str(g["type"]), str(g.get(group_by, ""))))
            for group in table:
                for name, value in group.items():
                    if isinstance(value, float):
                        group[name] = round(value, 3)

        units = {name: UNITS.get(kind, kind) for name, kind in kinds.items()}
        if args.get("format") == "csv":
            columns: list[str] = []
            for item in table:
                columns += [c for c in item if c not in columns]
            out = io.StringIO()
            writer = csv.DictWriter(out, fieldnames=columns)
            writer.writeheader()
            writer.writerows(table)
            return out.getvalue()
        return {
            "elementCount": len(rows),
            "groupBy": group_by,
            "units": units,
            "rows": table,
            "note": "Totals only sum lengths, areas and volumes; heights and thicknesses appear per element "
            "with groupBy='element'.",
        }

    async def check_model(self, args: dict[str, Any]) -> dict[str, Any]:
        wanted = args.get("checks") or list(CHECKS)
        limit = args.get("maxPerCheck", 50)
        elements = await self.resolve_scope(args)
        rows = [r for r in await self.rows(elements) if "error" not in r]
        story_names, stories = await self.stories()
        issues: dict[str, list[Any]] = {}

        def public(row: dict[str, Any], **extra: Any) -> dict[str, Any]:
            return {**{k: v for k, v in row.items() if not k.startswith("_")}, **extra}

        if "missing_id" in wanted:
            issues["missing_id"] = [public(r) for r in rows if not str(r.get("id", "")).strip()]
        if "duplicate_id" in wanted:
            by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for r in rows:
                if str(r.get("id", "")).strip():
                    by_id[r["id"]].append(r)
            issues["duplicate_id"] = [
                {"id": id_, "count": len(rs), "elements": [public(r) for r in rs[:10]]}
                for id_, rs in by_id.items()
                if len(rs) > 1
            ]
        if "unclassified" in wanted and rows:
            systems = (await self.client.run_command("API.GetAllClassificationSystems")).get("classificationSystems", [])
            if systems:
                system_ids = [{"classificationSystemId": s["classificationSystemId"]} for s in systems]
                unclassified = []
                for part in chunks(rows):
                    result = await self.client.run_command(
                        "API.GetClassificationsOfElements",
                        {"elements": [{"elementId": {"guid": r["guid"]}} for r in part], "classificationSystemIds": system_ids},
                    )
                    for r, item in zip(part, result.get("elementClassifications", [])):
                        ids = item.get("classificationIds", [])
                        if not any("classificationItemId" in c.get("classificationId", {}) for c in ids):
                            unclassified.append(public(r))
                issues["unclassified"] = unclassified
        if "duplicate_geometry" in wanted and rows:
            boxes: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
            for part in chunks(rows):
                result = await self.client.run_tapir_command(
                    "Get3DBoundingBoxes", {"elements": [{"elementId": {"guid": r["guid"]}} for r in part]}
                )
                for r, item in zip(part, result.get("boundingBoxes3D", [])):
                    box = item.get("boundingBox3D")
                    if box:
                        key = (r["type"],) + tuple(round(box[k], 3) for k in ("xMin", "xMax", "yMin", "yMax", "zMin", "zMax"))
                        boxes[key].append(r)
            issues["duplicate_geometry"] = [
                {"type": key[0], "count": len(rs), "elements": [public(r) for r in rs[:10]]}
                for key, rs in boxes.items()
                if len(rs) > 1
            ]
        if "hidden_or_locked_layer" in wanted and rows:
            layers = await self.layers()
            used = sorted({r["_layerIndex"] for r in rows if r.get("_layerIndex") in layers})
            flags: dict[int, dict[str, Any]] = {}
            if used:
                result = await self.client.run_tapir_command(
                    "GetLayers",
                    {
                        "attributeIds": [{"attributeId": layers[i]["attributeId"]} for i in used],
                        "fields": ["isHidden", "isLocked"],
                    },
                )
                flags = {i: item for i, item in zip(used, result.get("layers", []))}
            issues["hidden_or_locked_layer"] = [
                public(r, hidden=flags[r["_layerIndex"]].get("isHidden"), locked=flags[r["_layerIndex"]].get("isLocked"))
                for r in rows
                if r.get("_layerIndex") in flags
                and (flags[r["_layerIndex"]].get("isHidden") or flags[r["_layerIndex"]].get("isLocked"))
            ]
        if "zero_area_zone" in wanted:
            zones = [r for r in rows if r.get("type") == "Zone"]
            values, _ = await self.property_values(
                [{"elementId": {"guid": r["guid"]}} for r in zones], ["Zone_CalculatedArea"]
            )
            issues["zero_area_zone"] = [
                public(r) for r, v in zip(zones, values) if not (v.get("Zone_CalculatedArea") or 0) > 0
            ]
        if "unnamed_story" in wanted:
            issues["unnamed_story"] = [
                {"index": s["index"], "level": s.get("level"), "label": story_names[s["index"]]}
                for s in stories
                if not str(s.get("name", "")).strip()
            ]
        if "empty_story" in wanted and not (args.get("elements") or args.get("selectedOnly") or args.get("elementType")):
            used_stories = {r.get("_floorIndex") for r in rows}
            issues["empty_story"] = [
                {"index": s["index"], "name": story_names[s["index"]], "level": s.get("level")}
                for s in stories
                if s["index"] not in used_stories
            ]

        return {
            "checkedElements": len(rows),
            "summary": {name: len(found) for name, found in issues.items()},
            "issues": {
                name: found[:limit] + ([f"... {len(found) - limit} more"] if len(found) > limit else [])
                for name, found in issues.items()
                if found
            },
        }

