"""Smoke tests that need no SSH or network."""
from pathlib import Path

from sshpeek.config import _parse, _parse_target
from sshpeek.app import guess_type
from sshpeek.proxy import service_fid


def test_parse_target():
    assert _parse_target(8888) == ("localhost", 8888)
    assert _parse_target("8888") == ("localhost", 8888)
    assert _parse_target("db.internal:5432") == ("db.internal", 5432)


def test_parse_config():
    cfg = _parse(
        {
            "listen": {"port": 9000},
            "hosts": {
                "mercury": {
                    "tunnels": [8888, {"local": 15432, "remote": "db:5432"}],
                    "http": {"jupyter": 8888, "mlflow": "localhost:5000"},
                },
                "bare": None,
            },
        },
        Path("test.yaml"),
    )
    assert cfg.listen_port == 9000
    m = cfg.hosts["mercury"]
    assert m.tunnels[0].local == 8888 and m.tunnels[0].remote_port == 8888
    assert m.tunnels[1].local == 15432 and m.tunnels[1].remote_host == "db"
    assert {s.name for s in m.http} == {"jupyter", "mlflow"}
    assert cfg.hosts["bare"].tunnels == [] and cfg.hosts["bare"].http == []


def test_parse_s3_config():
    cfg = _parse(
        {
            "s3": {
                "data": "my-bucket",
                "results": {
                    "bucket": "my-results",
                    "prefix": "/runs/",
                    "profile": "work",
                },
            }
        },
        Path("test.yaml"),
    )
    assert cfg.s3["data"].bucket == "my-bucket" and cfg.s3["data"].prefix == ""
    r = cfg.s3["results"]
    assert r.bucket == "my-results" and r.prefix == "runs/" and r.profile == "work"


def test_s3_key_mapping():
    from sshpeek.config import S3Spec
    from sshpeek.s3 import S3Source

    src = S3Source(S3Spec(name="x", bucket="b", prefix="runs/"))
    assert src._key("/") == "runs/"
    assert src._key("/2024/plot.pdf") == "runs/2024/plot.pdf"
    bare = S3Source(S3Spec(name="y", bucket="b"))
    assert bare._key("/") == ""
    assert bare._key("/a/b") == "a/b"


def test_guess_type():
    assert guess_type("a/paper.tex", dl=False).startswith("text/plain")
    assert guess_type("a/README", dl=False).startswith("text/plain")
    assert guess_type("a/job.out", dl=False).startswith("text/plain")
    assert guess_type("a/blob.bin", dl=False) == "application/octet-stream"
    assert guess_type("a/doc.pdf", dl=False) == "application/pdf"


def test_service_fid():
    assert service_fid("mercury", "jupyter") == "http:mercury:jupyter"


def test_ui_serves():
    from starlette.testclient import TestClient
    from sshpeek.app import app

    with TestClient(app) as c:
        assert "sshpeek" in c.get("/").text
        assert c.get("/view").status_code == 200
        assert c.get("/api/forwards").json() == []
        assert c.get("/api/ls").status_code == 400
