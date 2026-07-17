"""HTTP API + static UI.

Endpoints
---------
GET  /                      UI: host picker, file browser, tunnels, services
GET  /view?host=&path=      UI: live PDF/image viewer (auto-refresh on change)
GET  /api/hosts             configured + connected hosts and s3 sources
GET  /api/services          declared HTTP services with their proxy URLs
GET  /api/ls?host=&path=    directory listing (path defaults to remote home)
GET  /api/file?host=&path=  stream file bytes (add &dl=1 to force download)
GET  /api/events?host=&path= SSE: emits when the file's (mtime, size) changes
                             and the size has been stable for one poll interval
GET  /api/views             open live views (one per /api/events stream)
DELETE /api/views/{id}      detach a live view (its tab closes itself)
GET  /api/forwards          list port forwards
POST /api/forwards          {"host": ..., "port": ..., "remote_host": "localhost"}
DELETE /api/forwards/{id}   drop a forward

Everything is behind cookie/token auth (see auth.py) and a Host-header
allowlist (see proxy.py) unless listen.auth is set to none.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import mimetypes
import secrets
import stat as statmod
import time
from pathlib import Path, PurePosixPath

import asyncssh
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .auth import AuthGate
from .config import load_config
from .pool import SSHPool
from .proxy import ProxyRouter, service_fid
from .s3 import S3Error, S3Source

log = logging.getLogger("sshpeek.app")

pool = SSHPool()
CONFIG = load_config()
S3 = {name: S3Source(spec) for name, spec in CONFIG.s3.items()}
STATIC = Path(__file__).parent / "static"

# Auth secret: permanent password from the config, else a per-run token
# (printed with the startup URL). listen.auth: none disables.
SECRET = (CONFIG.password or secrets.token_urlsafe(24)) if CONFIG.auth_enabled else None

CHUNK = 256 * 1024

# Live viewer connections: one entry per open /api/events stream.
WATCHERS: dict[int, dict] = {}
_watcher_seq = itertools.count(1)

# Extensions we serve as text/plain when mimetypes has no (browser-friendly)
# answer, so "peeking" at them renders in the browser instead of downloading.
TEXT_EXT = {
    ".log", ".out", ".err", ".toml", ".nix", ".yml", ".yaml", ".jl", ".tex",
    ".bib", ".sty", ".cls", ".sh", ".zsh", ".conf", ".ini", ".service",
    ".gitignore", ".sbatch", ".slurm", ".env", ".lock", ".sql", ".r", ".R",
}


def err(e: object, code: int = 502) -> JSONResponse:
    return JSONResponse({"error": str(e)}, status_code=code)


def guess_type(path: str, dl: bool) -> str:
    name = PurePosixPath(path).name
    suffix = PurePosixPath(path).suffix.lower()
    ctype = mimetypes.guess_type(name)[0]
    # When peeking (not downloading), render known text-ish extensions and
    # extensionless files (README, Makefile, ...) inline in the browser.
    if not dl and (suffix in TEXT_EXT or (ctype is None and "." not in name)):
        return "text/plain; charset=utf-8"
    return ctype or "application/octet-stream"


# ---------------------------------------------------------------- pages ----

async def index(request: Request) -> FileResponse:
    return FileResponse(STATIC / "index.html")


async def view(request: Request) -> FileResponse:
    return FileResponse(STATIC / "viewer.html")


async def favicon(request: Request) -> FileResponse:
    return FileResponse(STATIC / "favicon.svg")


# ------------------------------------------------------------------ api ----

async def hosts(request: Request) -> JSONResponse:
    live = pool.hosts()
    names = list(dict.fromkeys(list(CONFIG.hosts) + list(live)))
    out = [{"name": n, "connected": live.get(n, False), "kind": "ssh"} for n in names]
    out += [{"name": n, "connected": s.connected, "kind": "s3"} for n, s in S3.items()]
    return JSONResponse(out)


async def services(request: Request) -> JSONResponse:
    out = []
    for hs in CONFIG.hosts.values():
        st = pool.peek_state(hs.name)
        for s in hs.http:
            f = st.forwards.get(service_fid(hs.name, s.name)) if st else None
            up = bool(st and not st.closed and f and f.local_port)
            url = f"http://{s.name}.{hs.name}.localhost:{CONFIG.listen_port}/"
            if SECRET:
                # Service origins have their own cookie jars; the token in the
                # link blesses each one on first visit (proxy sets the cookie
                # and redirects to the clean URL).
                url += f"?sshpeek_token={SECRET}"
            out.append(
                {
                    "name": s.name,
                    "host": hs.name,
                    "target": f"{s.remote_host}:{s.remote_port}",
                    "url": url,
                    "up": up,
                }
            )
    return JSONResponse(out)


async def ls(request: Request) -> JSONResponse:
    host = request.query_params.get("host")
    path = request.query_params.get("path") or "."
    if not host:
        return err("missing ?host=", 400)
    src = S3.get(host)
    if src is not None:
        cwd = "/" if path in (".", "", "/") else "/" + path.strip("/")
        try:
            entries = await src.listdir(cwd)
            return JSONResponse({"host": host, "path": cwd, "entries": entries})
        except S3Error as e:
            return err(e)
    try:
        st = await pool.get(host)
        cwd = await st.sftp.realpath(path)
        entries = []
        for name in await st.sftp.readdir(cwd):
            if name.filename in (".", ".."):
                continue
            a = name.attrs
            is_dir = a.permissions is not None and statmod.S_ISDIR(a.permissions)
            entries.append(
                {
                    "name": name.filename,
                    "dir": is_dir,
                    "size": a.size,
                    "mtime": a.mtime,
                }
            )
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return JSONResponse({"host": host, "path": cwd, "entries": entries})
    except (OSError, asyncssh.Error) as e:
        return err(e)


async def file_(request: Request) -> Response:
    host = request.query_params.get("host")
    path = request.query_params.get("path")
    dl = bool(request.query_params.get("dl"))
    if not host or not path:
        return err("missing ?host= or ?path=", 400)
    src = S3.get(host)
    if src is not None:
        try:
            size, stream = await src.open(path)
        except S3Error as e:
            return err(e)

        async def s3body():
            try:
                while True:
                    chunk = await asyncio.to_thread(stream.read, CHUNK)
                    if not chunk:
                        break
                    yield chunk
            except Exception as e:  # noqa: BLE001 - died mid-stream
                log.warning("stream of %s:%s aborted: %s", host, path, e)
            finally:
                with contextlib.suppress(Exception):
                    stream.close()

        headers = {"Content-Length": str(size)}
        if dl:
            headers["Content-Disposition"] = (
                f'attachment; filename="{PurePosixPath(path).name}"'
            )
        return StreamingResponse(s3body(), media_type=guess_type(path, dl), headers=headers)
    try:
        st = await pool.get(host)
        attrs = await st.sftp.stat(path)
        f = await st.sftp.open(path, "rb")
    except (OSError, asyncssh.Error) as e:
        return err(e)

    async def body():
        try:
            while True:
                chunk = await f.read(CHUNK)
                if not chunk:
                    break
                yield chunk
        except (OSError, asyncssh.Error) as e:  # connection died mid-stream
            log.warning("stream of %s:%s aborted: %s", host, path, e)
        finally:
            try:
                await f.close()
            except Exception:  # noqa: BLE001
                pass

    headers = {}
    if attrs.size is not None:
        headers["Content-Length"] = str(attrs.size)
    if dl:
        headers["Content-Disposition"] = (
            f'attachment; filename="{PurePosixPath(path).name}"'
        )
    return StreamingResponse(body(), media_type=guess_type(path, dl), headers=headers)


async def events(request: Request) -> Response:
    """SSE stream of change events for one remote file.

    A change is only emitted once (mtime, size) differs from the last emitted
    version AND is identical across two consecutive polls -- the stable-size
    debounce that avoids firing while latexmk & co. are mid-write.
    """
    host = request.query_params.get("host")
    path = request.query_params.get("path")
    try:
        interval = min(max(float(request.query_params.get("interval", "2")), 0.5), 60)
    except ValueError:
        interval = 2.0
    if not host or not path:
        return err("missing ?host= or ?path=", 400)
    src = S3.get(host)

    async def gen():
        wid = next(_watcher_seq)
        close = asyncio.Event()
        WATCHERS[wid] = {"id": wid, "host": host, "path": path,
                         "started": time.time(), "close": close}
        try:
            last_emit: tuple | None = None
            prev: tuple | None = None
            first = True
            while True:
                cur: tuple | None = None
                try:
                    if src is not None:
                        cur = await src.stat(path)
                    else:
                        st = await pool.get(host)
                        a = await st.sftp.stat(path)
                        cur = (a.mtime, a.size)
                except (OSError, asyncssh.Error, S3Error) as e:
                    log.debug("stat %s:%s failed: %s", host, path, e)
                if cur is not None and first:
                    first = False
                    last_emit = cur
                    payload = {"type": "init", "mtime": cur[0], "size": cur[1]}
                    yield f"data: {json.dumps(payload)}\n\n"
                elif cur is not None and cur == prev and cur != last_emit:
                    last_emit = cur
                    payload = {"type": "change", "mtime": cur[0], "size": cur[1]}
                    yield f"data: {json.dumps(payload)}\n\n"
                else:
                    # Comment line: ignored by EventSource, but forces a write so
                    # a vanished client cancels this generator promptly.
                    yield ": ping\n\n"
                prev = cur
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(close.wait(), interval)
                if close.is_set():
                    yield 'data: {"type": "bye"}\n\n'
                    return
        finally:
            WATCHERS.pop(wid, None)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def views_get(request: Request) -> JSONResponse:
    return JSONResponse([
        {"id": w["id"], "host": w["host"], "path": w["path"], "started": w["started"]}
        for w in WATCHERS.values()
    ])


async def views_delete(request: Request) -> JSONResponse:
    w = WATCHERS.get(request.path_params["vid"])
    if w is None:
        return err("no such live view", 404)
    w["close"].set()
    return JSONResponse({"ok": True})


async def forwards_get(request: Request) -> JSONResponse:
    out = []
    for f, up in pool.all_forwards():
        if f.internal:
            continue  # HTTP service backings show up under /api/services
        out.append(
            {
                "id": f.id,
                "host": f.host,
                "remote_host": f.remote_host,
                "remote_port": f.remote_port,
                "local_port": f.local_port,
                "declared": f.declared,
                "up": up,
            }
        )
    return JSONResponse(out)


async def forwards_post(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return err("body must be JSON", 400)
    host = data.get("host")
    port = data.get("port")
    remote_host = data.get("remote_host") or "localhost"
    local = data.get("local") or 0
    if not host or not port:
        return err('need {"host": ..., "port": ...}', 400)
    try:
        f = await pool.add_forward(host, int(port), remote_host, desired_local=int(local))
    except (OSError, asyncssh.Error, ValueError, asyncio.TimeoutError) as e:
        return err(e)
    return JSONResponse(
        {"id": f.id, "local_port": f.local_port, "url": f"http://127.0.0.1:{f.local_port}"}
    )


async def forwards_delete(request: Request) -> JSONResponse:
    fid = request.path_params["fid"]
    host = fid.split(":", 1)[0]
    ok = await pool.remove_forward(host, fid)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


async def _ensure_host(hs) -> None:
    for t in hs.tunnels:
        try:
            await pool.add_forward(
                hs.name, t.remote_port, t.remote_host,
                desired_local=t.local, declared=True,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("tunnel %s:%s on %s: %s", t.remote_host, t.remote_port, hs.name, e)
    for s in hs.http:
        try:
            await pool.add_forward(
                hs.name, s.remote_port, s.remote_host,
                fid=service_fid(hs.name, s.name), internal=True, declared=True,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("service %s on %s: %s", s.name, hs.name, e)


async def _ensure_loop() -> None:
    """Keep declared tunnels and services alive; self-heals after drops."""
    while True:
        targets = [hs for hs in CONFIG.hosts.values() if hs.tunnels or hs.http]
        if targets:
            await asyncio.gather(*(_ensure_host(hs) for hs in targets), return_exceptions=True)
        await asyncio.sleep(20)


routes = [
    Route("/", index),
    Route("/view", view),
    Route("/favicon.svg", favicon),
    Route("/api/hosts", hosts),
    Route("/api/services", services),
    Route("/api/ls", ls),
    Route("/api/file", file_),
    Route("/api/events", events),
    Route("/api/views", views_get),
    Route("/api/views/{vid:int}", views_delete, methods=["DELETE"]),
    Route("/api/forwards", forwards_get, methods=["GET"]),
    Route("/api/forwards", forwards_post, methods=["POST"]),
    Route("/api/forwards/{fid:path}", forwards_delete, methods=["DELETE"]),
]


@contextlib.asynccontextmanager
async def lifespan(_app: Starlette):
    if SECRET and not CONFIG.password:
        print(f"sshpeek: open http://{CONFIG.listen_host}:{CONFIG.listen_port}/?token={SECRET}",
              flush=True)
    elif SECRET:
        print("sshpeek: password auth on (listen.password in sshpeek.yaml)", flush=True)
    else:
        print("sshpeek: auth disabled (listen.auth: none)", flush=True)
    ensure_task = asyncio.create_task(_ensure_loop())
    yield
    ensure_task.cancel()
    await app.client.aclose()
    await pool.close()


inner = Starlette(routes=routes, lifespan=lifespan)
app = ProxyRouter(AuthGate(inner, SECRET), CONFIG, pool, secret=SECRET)
