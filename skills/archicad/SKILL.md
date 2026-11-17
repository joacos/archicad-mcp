---
name: archicad
description: Trabaja con un Archicad abierto a través del servidor MCP archicad-tapir (API JSON de Graphisoft + add-on Tapir). Usar cuando el usuario pida consultar, computar, revisar, crear o modificar elementos de su modelo de Archicad - muros, losas, zonas, mallas, IDs, clasificaciones, propiedades, cómputo métrico, chequeo de calidad o selección actual.
---

# Archicad (MCP archicad-tapir)

Este skill guía el uso de las tools del servidor MCP `archicad-tapir-mcp`. Si las tools `archicad_*` / `tapir_*` no están disponibles, pedile al usuario que instale el servidor (ver README del repo: https://github.com/joacos/archicad-mcp) y que tenga Archicad abierto con el add-on Tapir.

## Antes de empezar

1. `archicad_status`: confirma que Archicad responde y qué versión de Tapir hay. Si falla: Archicad cerrado, Tapir no instalado, o varios Archicad abiertos (fijar `ARCHICAD_PORT`).
2. `archicad_model_summary`: pisos, cantidad de elementos por tipo/piso/capa, zonas y selección actual. Usalo para orientarte antes de cualquier otra cosa.

## Leer el modelo (preferir estas tools)

| Necesidad | Tool |
| --- | --- |
| Listar elementos (guid, tipo, ID, piso, capa) | `archicad_list_elements` |
| Cómputo métrico (largos, superficies, volúmenes) en JSON o CSV | `archicad_quantities` |
| Chequeo de calidad (IDs vacíos/repetidos, sin clasificar, duplicados, capas ocultas, zonas sin superficie) | `archicad_check_model` |
| Ítems de clasificación disponibles | `archicad_classification_items` |
| Imágenes de elementos | `archicad_element_images` |
| Lo seleccionado por el usuario | `GetSelectedElements` |

Responden en una sola llamada y con resultados compactos; usalas antes que comandos crudos. Con `GetDetailsOfElements`, pasá siempre `fields` para traer solo lo necesario.

## Modificar el modelo (reversibles)

Estas tools aceptan `dryRun: true` y quedan registradas en un journal que `archicad_undo` revierte (aunque se reinicie el cliente):

`archicad_set_element_ids`, `archicad_create_room`, `archicad_create_slabs`, `archicad_set_slab_levels`, `archicad_create_meshes`, `archicad_set_mesh`, `archicad_create_surface_morph`, `archicad_move_elements`, `archicad_rotate_elements`, `archicad_classify_elements`, `archicad_apply_dimension_favorite`.

- `archicad_history` lista lo que se puede revertir; `archicad_undo` lo revierte.
- Para cambios grandes: primero `dryRun: true`, mostrale el resultado al usuario y pedí confirmación.
- Preferí estas tools a `CreateWalls`, `MoveElements`, etc.: los comandos crudos no se pueden revertir con `archicad_undo`.

## Reglas

- **Nunca** uses `SetStories`: reconstruye la estructura de pisos y borró elementos en un proyecto real.
- Preguntá antes de borrar elementos (`DeleteElements`) o de hacer cambios masivos.
- Unidades: largos en metros, ángulos en radianes.
- Los elementos se identifican como `{"elementId": {"guid": "..."}}`. Obtenelos con `GetSelectedElements`, `GetElementsByType`, `FilterElements` o `archicad_list_elements`, y pasalos a los comandos de detalle, propiedades, clasificación o modificación.
- `archicad_create_room` ubica la zona automáticamente solo en el piso abierto en pantalla; en otros pisos la dibuja por el contorno interior de los muros.

## Comandos que no tienen tool propia

- `tapir_list_commands` (filtrar por grupo o texto) → `tapir_describe_command` (schema de entrada/salida) → `tapir_run_command` (valida antes de enviar).
- Comandos oficiales `API.*`: `archicad_run_api_command`.

## Flujos típicos

- **"¿Qué tengo seleccionado?"** → `GetSelectedElements` → `archicad_list_elements` o `GetDetailsOfElements` con `fields`.
- **Cómputo** → `archicad_quantities` por tipo, agrupando por piso o capa; pedí CSV si el usuario lo quiere en planilla.
- **Limpiar IDs** → `archicad_check_model` → `archicad_set_element_ids` con `fixDuplicates` o `numbering` (ej. prefijo `FU-`), primero en `dryRun`.
- **Crear una habitación** → `archicad_create_room` con el contorno, alto de muros y espesor de losa; `dryRun` primero si no está claro.
