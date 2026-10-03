"""Builds src/archicad_mcp/data/tapir_commands.json from a Tapir checkout.

The Tapir repository publishes every command with its JSON schemas in
docs/archicad-addon/command_definitions.js and common_schema_definitions.js.
This script converts those two JavaScript files into one JSON catalog that
the MCP server loads at startup.

Usage: python scripts/build_catalog.py <path-to-tapir-archicad-automation>
"""

import json
import re
import subprocess
import sys
from pathlib import Path


def load_js_json(path: Path):
    text = path.read_text(encoding="utf8")
    text = text[text.index("=") + 1 :].strip()
    return json.loads(text.rstrip(";"))


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    tapir = Path(sys.argv[1])
    docs = tapir / "docs" / "archicad-addon"
    groups = load_js_json(docs / "command_definitions.js")
    definitions = load_js_json(docs / "common_schema_definitions.js")

    version_file = (tapir / "archicad-addon" / "Sources" / "AddOnVersion.hpp").read_text()
    tapir_version = re.search(r'ADDON_VERSION\s+"([^"]+)"', version_file).group(1)
    try:
        commit = subprocess.check_output(["git", "-C", str(tapir), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        commit = None

    catalog = {
        "tapirVersion": tapir_version,
        "sourceCommit": commit,
        "groups": [
            {
                "name": group["name"],
                "commands": [
                    {
                        "name": c["name"],
                        "version": c.get("version"),
                        "description": c.get("description", ""),
                        "inputSchema": c.get("inputScheme"),
                        "outputSchema": c.get("outputScheme"),
                    }
                    for c in group["commands"]
                ],
            }
            for group in groups
            if group["name"] != "Developer Commands"
        ],
        "definitions": definitions,
    }
    out = Path(__file__).resolve().parent.parent / "src" / "archicad_mcp" / "data" / "tapir_commands.json"
    out.write_text(json.dumps(catalog, separators=(",", ":")), encoding="utf8")
    count = sum(len(g["commands"]) for g in catalog["groups"])
    print(f"Wrote {count} commands (Tapir {tapir_version}) to {out}")


if __name__ == "__main__":
    main()
