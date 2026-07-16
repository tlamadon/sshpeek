"""Reverse proxy for declared HTTP services.

Each service in sshpeek.yaml gets a stable URL:

    http://<service>.<host>.localhost:<sshpeek port>/

Browsers resolve *.localhost to 127.0.0.1 natively, so no /etc/hosts edits.
Under the hood every service is backed by an (internal, ephemeral) SSH local
forward from the pool; the proxy just relays to 127.0.0.1:<forwarded port>.
Proxying to a real local port keeps things simple and gives WebSockets and
streaming for free -- no hand-rolled HTTP over SSH channels.

Path rewriting is deliberately absent: because every service lives on its own
(sub)domain at path /, apps that generate absolute paths (Jupyter, Grafana,
...) work unmodified, and cookies/origins stay isolated per service.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.websockets import WebSocket
from websockets.asyncio.client import connect as ws_connect

from .config import Config, HostSpec, HttpService
from .pool import SSHPool

log = logging.getLogger("sshpeek.proxy")

# Hop-by-hop headers are between us and each peer, never forwarded.
HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host",
}


def service_fid(host: str, service: str) -> str:
    return f"http:{host}:{service}"


class ProxyRouter:
    """ASGI wrapper: routes *.localhost hosts to the proxy, everything else
    to the inner app."""

    def __init__(self, app, cfg: Config, pool: SSHPool) -> None:
        self.app = app
        self.cfg = cfg
        self.pool = pool
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=10), follow_redirects=False
        )

    # -- routing -----------------------------------------------------------

    def _match(self, scope) -> tuple[HostSpec, HttpService] | None:
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        hostname = (headers.get("host") or "").split(":")[0].lower()
        if not hostname.endswith(".localhost"):
            return None
        labels = hostname[: -len(".localhost")]
        if "." not in labels:
            return None
        service, halias = labels.split(".", 1)
        hs = self.cfg.hosts.get(halias)
        if hs is None:
            return None
        for s in hs.http:
            if s.name == service:
                return hs, s
        return None

    async def _local_port(self, hs: HostSpec, s: HttpService) -> int:
        f = await self.pool.add_forward(
            hs.name, s.remote_port, s.remote_host,
            fid=service_fid(hs.name, s.name), internal=True, declared=True,
        )
        if f.local_port is None:
            raise OSError("forward has no bound port")
        return f.local_port

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            m = self._match(scope)
            if m is not None:
                if scope["type"] == "http":
                    return await self._http(scope, receive, send, *m)
                return await self._ws(scope, receive, send, *m)
        await self.app(scope, receive, send)

    # -- http --------------------------------------------------------------

    async def _http(self, scope, receive, send, hs: HostSpec, s: HttpService):
        request = Request(scope, receive)
        try:
            port = await self._local_port(hs, s)
        except Exception as e:  # noqa: BLE001
            resp = PlainTextResponse(
                f"sshpeek: cannot reach {s.name} on {hs.name}: {e}", status_code=502
            )
            return await resp(scope, receive, send)

        url = f"http://127.0.0.1:{port}{scope['path']}"
        if scope.get("query_string"):
            url += "?" + scope["query_string"].decode()
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP]
        req = self.client.build_request(
            request.method, url, headers=headers, content=request.stream()
        )
        try:
            r = await self.client.send(req, stream=True)
        except httpx.HTTPError as e:
            resp = PlainTextResponse(
                f"sshpeek: {s.name} on {hs.name} not responding: {e}", status_code=502
            )
            return await resp(scope, receive, send)

        resp = StreamingResponse(
            r.aiter_raw(), status_code=r.status_code, background=BackgroundTask(r.aclose)
        )
        # Bypass Starlette's dict headers to preserve duplicates (Set-Cookie)
        # and pass raw bytes through untouched (Content-Encoding intact).
        resp.raw_headers = [
            (k.encode(), v.encode())
            for k, v in r.headers.multi_items()
            if k.lower() not in HOP
        ]
        await resp(scope, receive, send)

    # -- websocket ---------------------------------------------------------

    async def _ws(self, scope, receive, send, hs: HostSpec, s: HttpService):
        ws = WebSocket(scope, receive, send)
        try:
            port = await self._local_port(hs, s)
        except Exception as e:  # noqa: BLE001
            log.warning("ws: cannot reach %s on %s: %s", s.name, hs.name, e)
            await ws.close(code=1011)
            return

        url = f"ws://127.0.0.1:{port}{scope['path']}"
        if scope.get("query_string"):
            url += "?" + scope["query_string"].decode()
        fwd_headers = {}
        for k, v in scope.get("headers") or []:
            if k.decode().lower() in ("cookie", "authorization"):
                fwd_headers[k.decode()] = v.decode()

        try:
            upstream = await ws_connect(
                url,
                subprotocols=scope.get("subprotocols") or None,
                additional_headers=fwd_headers,
                max_size=None,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("ws upstream connect failed for %s: %s", url, e)
            await ws.close(code=1011)
            return

        await ws.accept(subprotocol=upstream.subprotocol)

        async def client_to_upstream():
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("text") is not None:
                        await upstream.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await upstream.send(msg["bytes"])
            except Exception:  # noqa: BLE001
                return

        async def upstream_to_client():
            try:
                async for m in upstream:
                    if isinstance(m, str):
                        await ws.send_text(m)
                    else:
                        await ws.send_bytes(m)
            except Exception:  # noqa: BLE001
                return

        t1 = asyncio.create_task(client_to_upstream())
        t2 = asyncio.create_task(upstream_to_client())
        _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        for closer in (upstream.close(), ws.close()):
            try:
                await closer
            except Exception:  # noqa: BLE001
                pass
