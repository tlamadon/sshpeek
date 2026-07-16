"""S3 sources.

Buckets declared under `s3:` in sshpeek.yaml show up as browsable sources
next to the SSH hosts. Object keys are presented as paths: "directories"
are the usual `/`-delimited common prefixes. Credentials come from the
normal boto3 chain (env vars, ~/.aws profiles, SSO, instance roles).

boto3 is an optional dependency (`pip install sshpeek[s3]`) and blocking,
so it is imported lazily and every call runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging

from .config import S3Spec

log = logging.getLogger("sshpeek.s3")


class S3Error(Exception):
    """Normalized error for anything that goes wrong talking to S3."""


class S3Source:
    def __init__(self, spec: S3Spec) -> None:
        self.spec = spec
        self.connected = False  # true after the first successful call
        self._client = None

    # -- blocking helpers (always called via asyncio.to_thread) -------------

    def _client_or_raise(self):
        if self._client is None:
            try:
                import boto3
            except ImportError as e:
                raise S3Error(
                    "boto3 is not installed -- pip install 'sshpeek[s3]'"
                ) from e
            session = boto3.session.Session(
                profile_name=self.spec.profile, region_name=self.spec.region
            )
            self._client = session.client("s3", endpoint_url=self.spec.endpoint)
        return self._client

    def _key(self, path: str) -> str:
        """Map a browser path like /runs/2024 onto a full object key."""
        return self.spec.prefix + path.strip("/")

    def _listdir(self, path: str) -> list[dict]:
        c = self._client_or_raise()
        key = self._key(path)
        pfx = key + "/" if key else ""
        dirs: list[dict] = []
        files: list[dict] = []
        for page in c.get_paginator("list_objects_v2").paginate(
            Bucket=self.spec.bucket, Prefix=pfx, Delimiter="/"
        ):
            for p in page.get("CommonPrefixes", []):
                name = p["Prefix"][len(pfx):].rstrip("/")
                dirs.append({"name": name, "dir": True, "size": None, "mtime": None})
            for o in page.get("Contents", []):
                if o["Key"] == pfx:  # the "directory marker" object itself
                    continue
                files.append(
                    {
                        "name": o["Key"][len(pfx):],
                        "dir": False,
                        "size": o["Size"],
                        "mtime": o["LastModified"].timestamp(),
                    }
                )
        self.connected = True
        out = sorted(dirs, key=lambda e: e["name"].lower())
        out += sorted(files, key=lambda e: e["name"].lower())
        return out

    def _stat(self, path: str) -> tuple[float, int]:
        c = self._client_or_raise()
        head = c.head_object(Bucket=self.spec.bucket, Key=self._key(path))
        self.connected = True
        return head["LastModified"].timestamp(), head["ContentLength"]

    def _open(self, path: str):
        c = self._client_or_raise()
        obj = c.get_object(Bucket=self.spec.bucket, Key=self._key(path))
        self.connected = True
        return obj["ContentLength"], obj["Body"]

    # -- async surface used by the endpoints ---------------------------------

    async def listdir(self, path: str) -> list[dict]:
        return await self._call(self._listdir, path)

    async def stat(self, path: str) -> tuple[float, int]:
        return await self._call(self._stat, path)

    async def open(self, path: str):
        """Return (size, blocking StreamingBody); read it with to_thread."""
        return await self._call(self._open, path)

    async def _call(self, fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except S3Error:
            raise
        except Exception as e:  # noqa: BLE001 - boto raises a small zoo
            raise S3Error(f"{self.spec.bucket}: {e}") from e
