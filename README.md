# sshpeek

A small local web UI for peeking at remote machines over SSH: browse and view
files, live-view PDFs and images that auto-refresh when they change remotely
(remote LaTeX builds, plotting pipelines), open port tunnels, reach named
remote web apps at stable `*.localhost` URLs, and browse S3 buckets alongside.

One multiplexed SSH connection per host, kept alive in the background.
Everything that works with plain `ssh <host>` works here: hosts, keys,
ProxyJump, agents all come from `~/.ssh/config` and your ssh-agent.

## Quickstart

```sh
pip install sshpeek              # or: uv tool install sshpeek
pip install 'sshpeek[s3]'        # with S3 support
sshpeek                          # → http://127.0.0.1:8642
```

Or straight from the repo without installing: `uv run sshpeek`.

Zero config works (type any `~/.ssh/config` alias in the host box), but the
point of sshpeek is the declarative config. Copy `sshpeek.example.yaml` to
`./sshpeek.yaml` or `~/.config/sshpeek/sshpeek.yaml` (or point `$SSHPEEK_CONFIG` /
`--config` at it):

```yaml
listen: { host: 127.0.0.1, port: 8642 }

hosts:
  mercury:
    tunnels:
      - 5432                      # 127.0.0.1:5432 -> mercury localhost:5432
      - local: 18888
        remote: localhost:8888
    http:
      jupyter: 8888               # http://jupyter.mercury.localhost:8642/
      mlflow: localhost:5000
  homebox:
    http:
      grafana: 3000
  internultra:                    # just a host chip for browsing

s3:
  data: my-bucket                 # chip "data" -> bucket "my-bucket"
  results:
    bucket: my-results-bucket
    prefix: runs/                 # browse only under this key prefix
    profile: work                 # ~/.aws profile; SSO works too
```

Declared tunnels and services are established at startup and re-ensured
every 20s, so they self-heal after connection drops or laptop sleep
(re-binding the same local ports).

## What it does

- **Files** — browse remote directories (sftp over the shared connection),
  view text/logs/PDFs inline, download anything. File-type icons, and a
  toggle for dotfiles (hidden by default).
- **Live views** — the `live` link next to any PDF or image opens a viewer
  that watches the file over SSE and re-renders when it changes, keeping
  your scroll position. PDF hyperlinks (URLs, citations, TOC) are clickable.
  Refreshes are debounced until the file size is stable across two polls,
  so a `latexmk` mid-write never renders garbage. A "Live views" panel
  lists every open view and can detach one remotely — its tab closes
  itself, handy for trimming after a long session.
- **S3 sources** — buckets declared under `s3:` appear next to the SSH
  hosts: same browsing, peeking, downloads and live views, with keys
  presented as paths. Credentials use the normal boto3 chain (env vars,
  profiles, SSO). Optional: `pip install 'sshpeek[s3]'`.
- **Tunnels** — raw TCP binds on `127.0.0.1` to any remote port through the
  SSH connection: databases, dashboards, anything. Declared in YAML (pinned)
  or opened ad hoc from the UI.
- **HTTP services** — named remote web apps proxied by sshpeek itself under
  `<name>.<host>.localhost:<port>`. Browsers resolve `*.localhost` to
  loopback natively, so the URLs are bookmarkable with no `/etc/hosts`
  edits. Each service lives at the root of its own origin, so apps that
  generate absolute paths (Jupyter, Grafana, ...) work unmodified, cookies
  stay isolated per service, and WebSockets are proxied too.

Tunnels vs. services: a tunnel gives you a local TCP port (use for anything
non-HTTP, or when the app itself must see `127.0.0.1`); a service gives you
a stable named URL through sshpeek's proxy. Both ride the same SSH connection.

## Notes

- Host keys are checked against `~/.ssh/known_hosts`; if you have never
  ssh'd to a host from this machine, do that once first.
- Binds to 127.0.0.1 by default, and the UI/API/proxied services require
  auth: a fresh token printed with the startup URL, or a permanent
  `listen.password` in sshpeek.yaml (so browser cookies survive restarts).
  `listen.auth: none` opts out. A Host-header allowlist blocks DNS-rebinding
  from hostile web pages. Caveat: raw TCP tunnels bind plain local ports —
  no HTTP auth applies there, so any local process can use an open tunnel.
- `*.localhost` resolution is native in Chrome/Firefox/Safari and
  systemd-resolved; curl needs `--resolve` or a hosts entry.
- `--port` / `--host` / `--config` / `-v` flags on the CLI.

## API

```
GET  /api/hosts                    configured + connected hosts
GET  /api/services                 declared HTTP services + proxy URLs
GET  /api/ls?host=&path=           directory listing (default: remote $HOME)
GET  /api/file?host=&path=[&dl=1]  stream file bytes
GET  /api/events?host=&path=       SSE change events (stable-size debounced)
GET  /api/views                    open live views (one per events stream)
DELETE /api/views/{id}             detach a live view (its tab closes itself)
GET  /api/forwards                 list tunnels
POST /api/forwards                 {"host": "mercury", "port": 8888, "local": 18888}
DELETE /api/forwards/{id}
```

## Roadmap

- Log tailing: `/api/file` with an offset param + incremental fetch,
  rendered as a live `<pre>` — cheap `tail -f` for Slurm `.out` files.
- Browser terminal: xterm.js over a WebSocket to an SSH PTY.
- Centralized deployment: containerize behind Caddy + Cloudflare Access;
  the subdomain proxy pattern carries over (real subdomains instead of
  `*.localhost`), tunnels become the piece that stays per-machine.
