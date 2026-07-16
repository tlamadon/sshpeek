"""YAML configuration.

sshpeek still works with zero config (type any ~/.ssh/config alias in the UI),
but hosts, tunnels and HTTP services can be declared in a sshpeek.yaml, looked
up in this order:

    $SSHPEEK_CONFIG
    ./sshpeek.yaml
    ~/.config/sshpeek/sshpeek.yaml

Example:

    listen:
      host: 127.0.0.1
      port: 8642

    hosts:
      mercury:
        tunnels:
          - 5432                      # 127.0.0.1:5432 -> mercury localhost:5432
          - local: 18888
            remote: localhost:8888    # 127.0.0.1:18888 -> mercury localhost:8888
        http:
          jupyter: 8888               # http://jupyter.mercury.localhost:8642/
          mlflow: localhost:5000
      homebox:
        http:
          grafana: 3000
      internultra:                    # no tunnels: just a host chip in the UI

Tunnels are raw TCP binds on 127.0.0.1 (databases, anything). HTTP services
are reverse-proxied by sshpeek itself under <name>.<host>.localhost, which
browsers resolve to 127.0.0.1 natively.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("sshpeek.config")


@dataclass
class Tunnel:
    local: int
    remote_host: str
    remote_port: int


@dataclass
class HttpService:
    name: str
    remote_host: str
    remote_port: int


@dataclass
class HostSpec:
    name: str
    tunnels: list[Tunnel] = field(default_factory=list)
    http: list[HttpService] = field(default_factory=list)


@dataclass
class Config:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8642
    hosts: dict[str, HostSpec] = field(default_factory=dict)
    path: Path | None = None


def _parse_target(v: object, default_host: str = "localhost") -> tuple[str, int]:
    """Accept 8888, "8888" or "somehost:8888"."""
    if isinstance(v, int):
        return default_host, v
    s = str(v)
    if ":" in s:
        h, p = s.rsplit(":", 1)
        return h, int(p)
    return default_host, int(s)


def _parse(data: dict, path: Path) -> Config:
    cfg = Config(path=path)
    listen = data.get("listen") or {}
    cfg.listen_host = str(listen.get("host", cfg.listen_host))
    cfg.listen_port = int(listen.get("port", cfg.listen_port))

    for name, spec in (data.get("hosts") or {}).items():
        hs = HostSpec(name=str(name))
        spec = spec or {}
        for t in spec.get("tunnels") or []:
            if isinstance(t, dict):
                rh, rp = _parse_target(t.get("remote"))
                local = int(t.get("local", rp))
            else:
                rh, rp = _parse_target(t)
                local = rp
            hs.tunnels.append(Tunnel(local=local, remote_host=rh, remote_port=rp))
        for sname, target in (spec.get("http") or {}).items():
            rh, rp = _parse_target(target)
            hs.http.append(HttpService(name=str(sname), remote_host=rh, remote_port=rp))
        cfg.hosts[hs.name] = hs
    return cfg


def load_config() -> Config:
    candidates: list[Path] = []
    if os.environ.get("SSHPEEK_CONFIG"):
        candidates.append(Path(os.environ["SSHPEEK_CONFIG"]))
    candidates += [Path("sshpeek.yaml"), Path.home() / ".config" / "sshpeek" / "sshpeek.yaml"]
    for p in candidates:
        try:
            if p.exists():
                cfg = _parse(yaml.safe_load(p.read_text()) or {}, p)
                log.info("loaded config from %s (%d hosts)", p, len(cfg.hosts))
                return cfg
        except (OSError, yaml.YAMLError, ValueError, TypeError) as e:
            log.warning("could not read %s: %s", p, e)
    return Config()
