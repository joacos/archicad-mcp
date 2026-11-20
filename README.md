# Archicad MCP

Servidor MCP que permite a Claude (u otro cliente MCP) trabajar con un Archicad abierto, usando la API JSON oficial de Graphisoft y los ~780 comandos del add-on [Tapir](https://github.com/ENZYME-APD/tapir-archicad-automation).

## Requisitos

1. **Archicad 25 a 30** (Windows o macOS) con un proyecto abierto.
2. **Add-on Tapir** instalado: descargá el [instalador para Windows](https://github.com/ENZYME-APD/tapir-archicad-automation/releases/latest/download/TapirInstaller_Win.exe) o [para macOS](https://github.com/ENZYME-APD/tapir-archicad-automation/releases/latest/download/TapirInstaller_Mac.zip) y reiniciá Archicad.
3. **[uv](https://docs.astral.sh/uv/getting-started/installation/)**: baja Python y el servidor solo, no hace falta instalar nada más.

## Instalación

Todos los clientes usan la misma configuración. El servidor le pasa a Claude sus instrucciones (qué tools usar, cuáles se pueden revertir y qué evitar), así que no hay skill aparte que instalar.

```json
{
  "mcpServers": {
    "archicad": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/joacos/archicad-mcp", "archicad-mcp"]
    }
  }
}
```

- **Claude Desktop**: Configuración > Desarrollador > Editar configuración (`claude_desktop_config.json`). Si Claude Desktop no encuentra `uvx`, poné la ruta completa (`which uvx` en macOS, `where uvx` en Windows).
- **Antigravity**: menú `...` del panel del agente > MCP Servers > Manage MCP Servers > View raw config (`~/.gemini/antigravity/mcp_config.json`).
- **Claude Code**, como plugin:

  ```
  /plugin marketplace add joacos/archicad-mcp
  /plugin install archicad@archicad-mcp
  ```

  o a mano: `claude mcp add archicad -- uvx --from git+https://github.com/joacos/archicad-mcp archicad-mcp`. Usá una de las dos, no ambas.

Si tenías la versión anterior con el nombre `archicad-tapir`, desinstalala primero: `/plugin uninstall archicad-tapir@archicad-mcp`. Si subiste el skill `archicad` a claude.ai o lo copiaste en Antigravity, borralo: desde la 0.4.0 esas instrucciones vienen con el servidor.

Reiniciá el cliente y pedile, por ejemplo: "¿Qué elementos tengo seleccionados en Archicad?" o "Creá cuatro muros de 3 m de alto formando un rectángulo de 5 × 4 m". Si algo no anda, pedile que ejecute `status`.

## Herramientas que expone

| Tool | Qué hace |
| --- | --- |
| `status` | Busca los Archicad abiertos (puertos 19723-19744), verifica que Tapir responda y avisa qué comandos necesitan una versión más nueva del add-on. |
| `model_summary` | Resumen del proyecto en una llamada: pisos, cantidad de elementos por tipo, piso y capa, superficie de zonas y selección actual. |
| `list_elements` | Lista compacta de elementos (guid, tipo, ID, piso, capa), mucho más liviana que `GetDetailsOfElements`. |
| `quantities` | Cómputo métrico con las propiedades de Archicad: largos, superficies, volúmenes y cantidades, por tipo y opcionalmente por piso o capa, en JSON o CSV. |
| `check_model` | Chequeos de calidad: IDs vacíos o repetidos, elementos sin clasificar, elementos probablemente duplicados, capas ocultas o bloqueadas, zonas sin superficie, pisos sin nombre o vacíos. |
| `set_element_ids` | Cambia IDs de elementos: valores explícitos, sufijos únicos para IDs repetidos (`fixDuplicates`) o numeración con prefijo (`numbering`, por ejemplo FU-001). |
| `create_room` | Crea una habitación desde un contorno: muros sobre cada lado, losa de piso y zona. La losa se crea básica con el espesor pedido (`slabStructure: "default"` conserva la estructura de la herramienta Losa). La zona se ubica automáticamente; como Archicad solo lo logra en el piso abierto en pantalla, en otros pisos se dibuja por el contorno interior de los muros. |
| `create_slabs`, `set_slab_levels`, `create_meshes`, `set_mesh`, `create_surface_morph`, `move_elements`, `rotate_elements`, `classify_elements`, `apply_dimension_favorite` | Crean losas, mallas y morphs, cambian niveles, mueven, rotan, clasifican y aplican favoritos de cota; todas aceptan `dryRun` y se pueden revertir. |
| `classification_items` / `element_images` | Lista los ítems de clasificación disponibles y genera imágenes de elementos. |
| `undo` / `history` | Revierte las últimas acciones de las tools que modifican el modelo (borra lo creado y restaura lo cambiado) y lista lo que se puede revertir. |
| `tapir_list_commands` | Lista los comandos de Tapir, filtrando por grupo o texto. |
| `tapir_describe_command` | Devuelve el JSON Schema de entrada y salida de un comando. |
| `tapir_run_command` | Ejecuta cualquier comando de Tapir, validando los parámetros antes de enviarlos. |
| `run_api_command` | Ejecuta cualquier comando oficial `API.*`. |
| 30 comandos de Tapir como tools propias | Los más usados (`GetSelectedElements`, `GetElementsByType`, `GetPropertyValuesOfElements`, `CreateWalls`, `CreateSlabs`, `MoveElements`, `DeleteElements`, etc.), con el schema exacto de Tapir. |

Cada tool va marcada como de solo lectura o destructiva, para que el cliente pida confirmación donde corresponde.

Las tools que modifican el modelo aceptan `dryRun: true` para mostrar qué harían sin tocar nada. Archicad no permite agrupar varias acciones en un solo Cmd+Z, así que cada cambio queda registrado en `~/.archicad-mcp/journal.json` (configurable con `ARCHICAD_MCP_JOURNAL`) y `undo` lo revierte, aunque se haya reiniciado Claude. Cada registro guarda el proyecto al que pertenece, así que solo se revierte en el proyecto correcto.

Las cuatro tools `model_summary`, `list_elements`, `quantities` y `check_model` solo leen el modelo, así que también están disponibles en modo solo lectura. Al conectarse, el servidor lee la versión instalada de Tapir y oculta los comandos que esa versión todavía no tiene.

## Opciones (variables de entorno)

| Variable | Valor por defecto | Uso |
| --- | --- | --- |
| `ARCHICAD_MCP_TOOLS` | `curated` | `all` expone los ~250 comandos como tools; `none` deja solo las genéricas. |
| `ARCHICAD_MCP_READ_ONLY` | apagado | Con `1`, rechaza todo comando que pueda modificar el modelo. |
| `ARCHICAD_PORT` | autodetección | Fija el puerto si tenés varios Archicad abiertos. |
| `ARCHICAD_HOST` | `127.0.0.1` | Host de Archicad. |
| `ARCHICAD_TIMEOUT` | `120` | Segundos de espera por comando. |
| `ARCHICAD_MCP_MAX_CHARS` | `100000` | Largo máximo de cada respuesta; lo que exceda se recorta. |

Ejemplo:

```json
"archicad": {
  "command": "uvx",
  "args": ["--from", "git+https://github.com/joacos/archicad-mcp", "archicad-mcp"],
  "env": { "ARCHICAD_MCP_READ_ONLY": "1" }
}
```

## Actualizar el catálogo de Tapir

Los schemas vienen de la documentación de Tapir (versión 2.0.0). Cuando salga una versión nueva:

```bash
git clone --depth 1 https://github.com/ENZYME-APD/tapir-archicad-automation
python scripts/build_catalog.py tapir-archicad-automation
```

## Tests

```bash
python -m pip install -e ".[test]"
python -m pytest
```

Los tests levantan un Archicad simulado por HTTP, así que no necesitan Archicad.
