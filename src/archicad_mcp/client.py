"""HTTP client for the Archicad JSON API and the Tapir add-on commands.

Archicad listens on the first free port between 19723 and 19744 and accepts
POST requests with a body of {"command": ..., "parameters": ...}. Tapir
commands are reached through the official API.ExecuteAddOnCommand command.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_HOST = "127.0.0.1"
PORT_RANGE = range(19723, 19745)
TAPIR_NAMESPACE = "TapirCommand"


class ArchicadError(Exception):
    """Raised when Archicad is unreachable or a command reports an error."""


class ArchicadClient:
    def __init__(self, host: str | None = None, port: int | None = None, timeout: float | None = None) -> None:
        self.host = host or os.environ.get("ARCHICAD_HOST", DEFAULT_HOST)
        env_port = os.environ.get("ARCHICAD_PORT")
        self.fixed_port = port or (int(env_port) if env_port else None)
        self.port: int | None = self.fixed_port
        self.timeout = timeout or float(os.environ.get("ARCHICAD_TIMEOUT", "120"))

    # -- low level ---------------------------------------------------------

    def _post(self, port: int, command: str, parameters: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        body = json.dumps({"command": command, "parameters": parameters or {}}).encode("utf8")
        request = urllib.request.Request(
            f"http://{self.host}:{port}", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf8"))

    def find_instances(self) -> list[dict[str, Any]]:
        """Returns every running Archicad instance with its port and product info."""
        ports = [self.fixed_port] if self.fixed_port else list(PORT_RANGE)
        instances = []
        for port in ports:
            try:
                alive = self._post(port, "API.IsAlive", None, timeout=0.5)
            except (OSError, ValueError):
                continue
            if not alive.get("succeeded"):
                continue
            info: dict[str, Any] = {"port": port}
            try:
                product = self._post(port, "API.GetProductInfo", None, timeout=5)
                info.update(product.get("result", {}))
            except (OSError, ValueError):
                pass
            instances.append(info)
        return instances

    def _resolve_port(self) -> int:
        if self.port is not None:
            return self.port
        instances = self.find_instances()
        if not instances:
            where = f"port {self.fixed_port}" if self.fixed_port else "ports 19723-19744"
            raise ArchicadError(
                f"No running Archicad found on {self.host} ({where}). "
                "Open Archicad with a project, or set ARCHICAD_PORT."
            )
        self.port = instances[0]["port"]
        return self.port

    def _run_sync(self, command: str, parameters: dict[str, Any] | None) -> Any:
        port = self._resolve_port()
        try:
            response = self._post(port, command, parameters, self.timeout)
        except urllib.error.URLError as error:
            if self.fixed_port is None:
                # Archicad may have been restarted on another port: look again once.
                self.port = None
                port = self._resolve_port()
                response = self._post(port, command, parameters, self.timeout)
            else:
                raise ArchicadError(f"Cannot reach Archicad on port {port}: {error.reason}") from error
        if not response.get("succeeded", False):
            error = response.get("error", {})
            raise ArchicadError(f"{command} failed: {error.get('message', error)} (code {error.get('code')})")
        return response.get("result")

    # -- public API --------------------------------------------------------

    async def run_command(self, command: str, parameters: dict[str, Any] | None = None) -> Any:
        """Runs an official Archicad JSON command, such as API.GetElementsByType."""
        return await asyncio.to_thread(self._run_sync, command, parameters)

    async def run_tapir_command(self, command: str, parameters: dict[str, Any] | None = None) -> Any:
        """Runs a Tapir add-on command and returns its response."""
        result = await self.run_command(
            "API.ExecuteAddOnCommand",
            {
                "addOnCommandId": {"commandNamespace": TAPIR_NAMESPACE, "commandName": command},
                "addOnCommandParameters": parameters or {},
            },
        )
        response = (result or {}).get("addOnCommandResponse")
        if isinstance(response, dict) and isinstance(response.get("error"), dict) and len(response) == 1:
            error = response["error"]
            raise ArchicadError(f"{command} failed: {error.get('message', error)} (code {error.get('code')})")
        return response
