---
name: archicad
description: Work with a running Archicad through the archicad MCP server (Graphisoft JSON API + Tapir add-on). Use when the user wants to read, quantify, check, create or change elements of their Archicad model - walls, slabs, zones, meshes, element IDs, classifications, properties, quantity take-offs, model quality checks or the current selection.
---

# Archicad

This skill guides the use of the `archicad` MCP server's tools. If tools such as `status` or `model_summary` from the `archicad` server are not available, ask the user to install it (see https://github.com/joacos/archicad-mcp) and to have Archicad open with the Tapir add-on.

## Before you start

1. `status`: checks that Archicad answers and which Tapir version is installed. If it fails: Archicad is closed, Tapir is not installed, or several Archicads are open (set `ARCHICAD_PORT`).
2. `model_summary`: stories, element counts by type/story/layer, zones and the current selection. Use it to get oriented before anything else.

## Reading the model (prefer these tools)

| Need | Tool |
| --- | --- |
| List elements (guid, type, ID, story, layer) | `list_elements` |
| Quantity take-off (lengths, areas, volumes) as JSON or CSV | `quantities` |
| Quality checks (empty/duplicate IDs, unclassified, duplicates, hidden layers, zones without area) | `check_model` |
| Available classification items | `classification_items` |
| Element images | `element_images` |
| What the user has selected | `GetSelectedElements` |

They answer in one call with compact results; use them before raw commands. With `GetDetailsOfElements`, always pass `fields` to get only what you need.

## Changing the model (undoable)

These tools accept `dryRun: true` and are recorded in a journal that `undo` reverts, even after the client restarts:

`set_element_ids`, `create_room`, `create_slabs`, `set_slab_levels`, `create_meshes`, `set_mesh`, `create_surface_morph`, `move_elements`, `rotate_elements`, `classify_elements`, `apply_dimension_favorite`.

- `history` lists what can be reverted; `undo` reverts it.
- For large changes: run with `dryRun: true` first, show the result to the user and ask for confirmation.
- Prefer these tools over `CreateWalls`, `MoveElements`, etc.: raw commands cannot be reverted with `undo`.
- Column and beam segment IDs cannot be changed through the API; `set_element_ids` skips them and lists them under `skipped`.

## Rules

- **Never** use `SetStories`: it rebuilds the story structure and has deleted elements in a real project.
- Ask before deleting elements (`DeleteElements`) or making large changes.
- Units: lengths in meters, angles in radians.
- Elements are identified as `{"elementId": {"guid": "..."}}`. Get them from `GetSelectedElements`, `GetElementsByType`, `FilterElements` or `list_elements`, and pass them to detail, property, classification or modification commands.
- `create_room` places the zone automatically only on the story open on screen; on other stories it draws it along the inner outline of the walls.

## Commands without their own tool

- `tapir_list_commands` (filter by group or text) → `tapir_describe_command` (input/output schema) → `tapir_run_command` (validates before sending).
- Official `API.*` commands: `run_api_command`.

## Typical flows

- **"What do I have selected?"** → `GetSelectedElements` → `list_elements` or `GetDetailsOfElements` with `fields`.
- **Take-off** → `quantities` by type, grouped by story or layer; ask for CSV if the user wants a spreadsheet.
- **Clean up IDs** → `check_model` → `set_element_ids` with `fixDuplicates` or `numbering` (e.g. prefix `FU-`), in `dryRun` first.
- **Create a room** → `create_room` with the outline, wall height and slab thickness; `dryRun` first if anything is unclear.
- Reply in the user's language.
