"""A throwaway HTTP server that records signed webhook POSTs.

Deliberately a real socket rather than a mocked client: the signature is computed over the
exact bytes on the wire, so verifying it anywhere else would not prove the thing that
matters to an integrator.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field


@dataclass
class Received:
    headers: dict[str, str]
    body: bytes

    @property
    def event(self) -> dict:
        return json.loads(self.body)


@dataclass
class WebhookReceiver:
    status_code: int = 200
    received: list[Received] = field(default_factory=list)
    _server: asyncio.AbstractServer | None = None
    port: int = 0

    async def start(self) -> "WebhookReceiver":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}/hook"

    def events_of_type(self, event_type: str) -> list[Received]:
        return [r for r in self.received if r.event.get("type") == event_type]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()

            length = int(headers.get("content-length", "0") or 0)
            body = await reader.readexactly(length) if length else b""
            self.received.append(Received(headers=headers, body=body))

            writer.write(
                b"HTTP/1.1 %d OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                % self.status_code
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
