# archicad-tapir-mcp

Servidor MCP que permite a Claude (u otro cliente MCP) trabajar con un Archicad abierto, usando la API JSON oficial de Graphisoft y los ~250 comandos del add-on [Tapir](https://github.com/ENZYME-APD/tapir-archicad-automation).

## Requisitos

1. **Archicad 25 a 29** (Windows o macOS) con un proyecto abierto.
2. **Add-on Tapir** instalado: descargá el [instalador para Windows](https://github.com/ENZYME-APD/tapir-archicad-automation/releases/latest/download/TapirInstaller_Win.exe) o [para macOS](https://github.com/ENZYME-APD/tapir-archicad-automation/releases/latest/download/TapirInstaller_Mac.zip) y reiniciá Archicad.
3. **Python 3.10 o superior**.

## Instalación

Desde la carpeta de este proyecto:

```bash
python -m pip install .
```

## Instalación como plugin de Claude

En Claude Code, sin instalar nada a mano (requiere [uv](https://docs.astral.sh/uv/)):

```
/plugin marketplace add joacos/archicad-mcp
/plugin install archicad-tapir@archicad-mcp
```

El plugin arranca el servidor con `uvx` desde este repositorio. Si ya lo registraste a mano como `archicad`, quitá esa entrada para no tenerlo duplicado.

## Configuración en Claude Desktop

Editá `claude_desktop_config.json` (Configuración > Desarrollador > Editar configuración) y agregá:

```json
{
  "mcpServers": {
    "archicad": {
      "command": "python",
      "args": ["-m", "archicad_mcp"]
    }
  }
}
```

En macOS el comando suele ser `python3`, o la ruta del entorno virtual donde lo instalaste (por ejemplo `/ruta/al/proyecto/.venv/bin/python`). En Windows, si `python` no está en el PATH, usá la ruta completa, por ejemplo `"C:\\Users\\TuUsuario\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"`.

En Claude Code: `claude mcp add archicad -- python -m archicad_mcp`.

Reiniciá Claude y pedile, por ejemplo: "¿Qué elementos tengo seleccionados en Archicad?" o "Creá cuatro muros de 3 m de alto formando un rectángulo de 5 × 4 m". Si algo no anda, pedile que ejecute `archicad_status`.

## Herramientas que expone

| Tool | Qué hace |
| --- | --- |
| `archicad_status` | Busca los Archicad abiertos (puertos 19723-19744), verifica que Tapir responda y avisa qué comandos necesitan una versión más nueva del add-on. |
| `archicad_model_summary` | Resumen del proyecto en una llamada: pisos, cantidad de elementos por tipo, piso y capa, superficie de zonas y selección actual. |
| `archicad_list_elements` | Lista compacta de elementos (guid, tipo, ID, piso, capa), mucho más liviana que `GetDetailsOfElements`. |
| `archicad_quantities` | Cómputo métrico con las propiedades de Archicad: largos, superficies, volúmenes y cantidades, por tipo y opcionalmente por piso o capa, en JSON o CSV. |
| `archicad_check_model` | Chequeos de calidad: IDs vacíos o repetidos, elementos sin clasificar, elementos probablemente duplicados, capas ocultas o bloqueadas, zonas sin superficie, pisos sin nombre o vacíos. |
| `archicad_set_element_ids` | Cambia IDs de elementos: valores explícitos, sufijos únicos para IDs repetidos (`fixDuplicates`) o numeración con prefijo (`numbering`, por ejemplo FU-001). |
| `archicad_create_room` | Crea una habitación desde un contorno: muros sobre cada lado, losa de piso y zona. La losa se crea básica con el espesor pedido (`slabStructure: "default"` conserva la estructura de la herramienta Losa). La zona se ubica automáticamente; como Archicad solo lo logra en el piso abierto en pantalla, en otros pisos se dibuja por el contorno interior de los muros. |
| `archicad_undo` / `archicad_history` | Revierte las últimas acciones de las tres tools anteriores (borra lo creado y restaura lo cambiado) y lista lo que se puede revertir. |
| `tapir_list_commands` | Lista los comandos de Tapir, filtrando por grupo o texto. |
| `tapir_describe_command` | Devuelve el JSON Schema de entrada y salida de un comando. |
| `tapir_run_command` | Ejecuta cualquier comando de Tapir, validando los parámetros antes de enviarlos. |
| `archicad_run_api_command` | Ejecuta cualquier comando oficial `API.*`. |
| 30 comandos de Tapir como tools propias | Los más usados (`GetSelectedElements`, `GetElementsByType`, `GetPropertyValuesOfElements`, `CreateWalls`, `CreateSlabs`, `MoveElements`, `DeleteElements`, etc.), con el schema exacto de Tapir. |

Cada tool va marcada como de solo lectura o destructiva, para que el cliente pida confirmación donde corresponde.

Las tools que modifican el modelo aceptan `dryRun: true` para mostrar qué harían sin tocar nada. Archicad no permite agrupar varias acciones en un solo Cmd+Z, así que cada cambio queda registrado en `~/.archicad-mcp/journal.json` (configurable con `ARCHICAD_MCP_JOURNAL`) y `archicad_undo` lo revierte, aunque se haya reiniciado Claude. Cada registro guarda el proyecto al que pertenece, así que solo se revierte en el proyecto correcto.

Las cuatro tools `archicad_model_summary`, `archicad_list_elements`, `archicad_quantities` y `archicad_check_model` solo leen el modelo, así que también están disponibles en modo solo lectura. Al conectarse, el servidor lee la versión instalada de Tapir y oculta los comandos que esa versión todavía no tiene.

## Opciones (variables de entorno)

| Variable | Valor por defecto | Uso |
| --- | --- | --- |
| `ARCHICAD_MCP_TOOLS` | `curated` | `all` expone los ~250 comandos como tools; `none` deja solo las genéricas. |
| `ARCHICAD_MCP_READ_ONLY` | apagado | Con `1`, rechaza todo comando que pueda modificar el modelo. |
| `ARCHICAD_PORT` | autodetección | Fija el puerto si tenés varios Archicad abiertos. |
| `ARCHICAD_HOST` | `127.0.0.1` | Host de Archicad. |
| `ARCHICAD_TIMEOUT` | `120` | Segundos de espera por comando. |
| `ARCHICAD_MCP_MAX_CHARS` | `100000` | Largo máximo de cada respuesta; lo que exceda se recorta. |

Ejemplo en Claude Desktop:

```json
"archicad": {
  "command": "python",
  "args": ["-m", "archicad_mcp"],
  "env": { "ARCHICAD_MCP_READ_ONLY": "1" }
}
```

## Actualizar el catálogo de Tapir

Los schemas vienen de la documentación de Tapir (versión 1.6.1). Cuando salga una versión nueva:

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
