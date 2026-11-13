---
name: archicad
description: Work with an open Archicad project through the archicad-tapir MCP server - inspect the model, take quantities, audit model quality, renumber IDs, classify elements, create rooms, slabs and terrain, move or rotate elements, and undo those changes. Use whenever the user mentions Archicad, a BIM model they have open, Tapir, or asks about walls, slabs, zones, stories, layers, quantities, element IDs or classifications in their project.
---

# Working with Archicad through the archicad-tapir MCP server

The `archicad` MCP server talks to a running Archicad (25-29) through its JSON API and the Tapir add-on. Everything happens in the user's live project, so treat it like editing someone's real drawing set: look first, change only what was asked, and keep every change reversible.

## Units and identifiers

- Lengths in **meters**, areas in m², volumes in m³. Raw Tapir/API commands take angles in **radians**; `archicad_rotate_elements` takes **degrees** (counter-clockwise positive).
- Elements are identified as `{"elementId": {"guid": "..."}}`. Get guids from `archicad_list_elements`, `GetSelectedElements`, `GetElementsByType` or `FilterElements`, never invent them.
- Stories are addressed by `floorIndex` (from `GetStories` or `archicad_model_summary`), not by name.
- Answer in the user's language. Archicad property and type names stay in English (`Wall`, `Slab`, `Zone`, `General_GrossVolume`).

## Start of every session

1. If anything is uncertain about the connection, call `archicad_status`. It lists running Archicad instances and whether Tapir answers. If nothing answers, tell the user to open Archicad with a project and install Tapir; don't keep retrying.
2. Call `archicad_model_summary` once to learn stories, element counts by type/story/layer, zone areas and the current selection. Reuse that knowledge instead of calling it again for every question.

## Choosing the right tool

Prefer the high-level `archicad_*` tools: they answer in one call with compact output, and the ones that change the model are journaled so `archicad_undo` can revert them.

| The user wants to... | Use |
| --- | --- |
| Understand the project | `archicad_model_summary` |
| Find elements (by type, selection, list) | `archicad_list_elements` |
| See what elements look like | `archicad_element_images` (3D, 2D or Section; zone floor-plan clips) |
| Quantities / take-off / "how many m² of..." | `archicad_quantities` (`groupBy` type/story/layer/element, `format: "csv"` for spreadsheets) |
| Audit model quality | `archicad_check_model` |
| Fix or renumber IDs | `archicad_set_element_ids` (`fixDuplicates`, `numbering`, or explicit `ids`) |
| Classify elements in bulk | `archicad_classification_items`, then `archicad_classify_elements` |
| Draw a room (walls + floor + zone) | `archicad_create_room` |
| Site blocks, platforms, masses | `archicad_create_slabs`, `archicad_set_slab_levels` |
| Terrain | `archicad_create_meshes`, `archicad_set_mesh`, `archicad_create_surface_morph` (paths draped on terrain) |
| Move / rotate | `archicad_move_elements`, `archicad_rotate_elements` |
| Revert | `archicad_history`, then `archicad_undo` |

Only drop to raw commands when no high-level tool covers the task:

- The curated Tapir tools (`GetDetailsOfElements`, `GetPropertyValuesOfElements`, `SetGDLParametersOfElements`, `CreateWalls`, `CreateObjects`, `DeleteElements`, `CreateIssue`...) are exposed directly.
- For anything else: `tapir_list_commands` (filter by group or text) → `tapir_describe_command` (read the schema) → `tapir_run_command`. Official `API.*` commands go through `archicad_run_api_command`.
- With `GetDetailsOfElements`, always pass `fields` so the response stays small.
- Changes made through raw commands are **not** in the undo journal. Say so before using them for edits.

## Rules for changing the model

1. **Scope first.** Before an edit, find out exactly which elements it touches (`archicad_list_elements`, or the selection). If the user said "these" or "the selected ones", use `selectedOnly: true`.
2. **Dry run anything non-trivial.** Every journaled edit tool accepts `dryRun: true`. Use it when more than a handful of elements change, when the scope came from a filter, or when the user didn't spell out the exact elements; show the result and ask before applying.
3. **Ask before deleting** (`DeleteElements`) or making large changes, and list what will go.
4. **Never call `SetStories`.** It rebuilds the story structure and has deleted elements in a real project. If the user wants to change stories, tell them to do it in Archicad's Story Settings.
5. **Mind non-journaled tools.** `archicad_apply_dimension_favorite` applies a favorite to *every* dimension in the project and cannot be reverted with `archicad_undo`; confirm with the user first and remind them that Archicad's own Undo is the way back.
6. **Locked layers.** Archicad silently leaves elements on locked layers in place. The move/rotate/mesh tools handle this; if you use raw commands and "success" doesn't match what the user sees, check layer locks with `GetLayers`.
7. **Report what changed** after each edit: how many elements, which ones, and that `archicad_undo` can revert it.

## Undo

Archicad can't group several API actions into one Cmd/Ctrl+Z, so the server keeps its own journal (`~/.archicad-mcp/journal.json`), tied to the project.

- `archicad_history` lists revertible actions, newest first.
- `archicad_undo` with `steps: n` reverts the last n: deletes what they created and restores what they changed.
- It survives restarts of Claude, and only acts on the project the action belongs to.

## Read-only mode

If the server runs with `ARCHICAD_MCP_READ_ONLY=1`, edit tools are refused. Don't try to work around it with raw commands; tell the user the server is read-only and offer the analysis you can still do.

## Common workflows

**"What's in my model?"** → `archicad_model_summary`, then summarize stories, main element types and total zone area in a few lines.

**Quantity take-off** → `archicad_quantities` with the right `elementType` and `groupBy`. Add `extraProperties` (e.g. `General_GrossVolume`) when the default totals aren't enough. Use `format: "csv"` when the user wants to paste into a spreadsheet. Note that values come from Archicad's built-in properties, so they follow the model's own calculation rules.

**Model audit before issuing drawings** → `archicad_check_model` (all checks), group findings by severity (duplicate geometry and duplicate IDs first, then unclassified, then cosmetic), then offer fixes: `archicad_set_element_ids` with `fixDuplicates` (dry run first), `archicad_classify_elements` for unclassified elements. Duplicate geometry needs the user's judgement; highlight them with `HighlightElements` instead of deleting.

**Renumber doors/furniture/rooms** → `archicad_set_element_ids` with `numbering: {"prefix": "D-", "digits": 3}` and the right `elementType`, dry run, confirm, apply.

**Bulk classification** → `archicad_classification_items` with `search` to find the item IDs, then `archicad_classify_elements` with rules by `elementType` and/or `layer`. By default it only touches unclassified elements; set `overwrite: true` only when the user asks to reclassify.

**Sketch a room** → `archicad_create_room` with the outline as wall axes, in meters. Wall height defaults to the story height. The zone is placed automatically only on the story currently open in Archicad; on other stories it is drawn from the inner wall outline.

**Site and terrain** → masses with `archicad_create_slabs` (exact `topLevel` and `thickness`, on a named layer), ground with `archicad_create_meshes` (per-point `z` relative to `level`), paths with `archicad_create_surface_morph` plus a `buildingMaterial`, holes in the terrain with `archicad_set_mesh`.

**Inspect a specific element** → get its guid from the selection, then `GetDetailsOfElements` with only the `fields` needed, `GetPropertyValuesOfElements` for properties, and `archicad_element_images` if a picture helps.
