"""FPP targets, logins from an env file, and the upload flow (no network: httpx.request is faked)."""

from __future__ import annotations

import json

import httpx
import pytest

from xlights_mcp.config import FPPConfig
from xlights_mcp.fpp import client, upload

SECRET = "s3cret-pw"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("XLIGHTS_FPP_ENV", raising=False)
    env = tmp_path / "fpp.env"
    env.write_text(f"# logins\nFPP_USER=admin\nFPP_PASS_RADIO={SECRET}\n", encoding="utf-8")
    return FPPConfig(targets={"radio": "10.0.0.5", "matrix": "10.0.0.6"}, env_file=str(env))


def test_resolve_by_name_ip_and_default(cfg):
    assert cfg.resolve("Radio").host == "10.0.0.5"
    assert cfg.resolve("radio").name == "radio"
    assert cfg.resolve("10.0.0.6").name == "matrix"  # raw IP of a known target keeps its login name
    assert cfg.resolve("10.9.9.9").name == ""
    assert cfg.resolve(None).host == "fpp.local"
    with pytest.raises(ValueError, match="Known targets: matrix, radio"):
        cfg.resolve("nosuch")


def test_login_only_where_configured_and_never_dumped(cfg):
    radio, matrix = cfg.resolve("radio"), cfg.resolve("matrix")
    assert isinstance(radio.auth(), httpx.BasicAuth)
    assert matrix.auth() is None
    dumped = json.dumps(radio.model_dump(mode="json")) + repr(radio) + str(radio)
    assert SECRET not in dumped


def test_env_var_overrides_env_file(cfg, tmp_path, monkeypatch):
    other = tmp_path / "other.env"
    other.write_text("FPP_PASS_MATRIX=x\n", encoding="utf-8")
    monkeypatch.setenv("XLIGHTS_FPP_ENV", str(other))
    assert cfg.resolve("matrix").auth() is not None
    assert cfg.resolve("radio").auth() is None


class FakeFPP:
    """Records requests; answers like FPP."""

    def __init__(self, playing="", status_code=200):
        self.calls, self.playing, self.status_code = [], playing, status_code

    def __call__(self, method, url, auth=None, timeout=None, **kw):
        req = httpx.Request(method, url)
        self.calls.append((method, url, auth, kw))
        if self.status_code != 200:
            return httpx.Response(self.status_code, request=req)
        if url.endswith("/api/fppd/status"):
            body = {"status_name": "playing" if self.playing else "idle", "current_sequence": self.playing}
        elif url.endswith("/meta"):
            body = {"NumFrames": 400, "ChannelCount": 1000, "StepTime": 25}
        else:
            body = {"Status": "OK"}
        return httpx.Response(200, json=body, request=req)


def test_upload_sends_login_and_verifies(cfg, tmp_path, monkeypatch):
    fake = FakeFPP()
    monkeypatch.setattr(httpx, "request", fake)
    fseq = tmp_path / "My Song.fseq"
    fseq.write_bytes(b"PSEQ" + b"\0" * 60)
    out = upload.upload_sequence(cfg.resolve("radio"), fseq)
    assert out["success"] and out["fseq"]["verified"] and out["fseq"]["frames"] == 400
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert posts[0][1] == "http://10.0.0.5:80/api/sequence/My%20Song.fseq"
    assert posts[0][3]["content"] == fseq.read_bytes()
    assert all(isinstance(c[2], httpx.BasicAuth) for c in fake.calls)
    assert SECRET not in json.dumps(out)


def test_upload_refuses_while_playing(cfg, tmp_path, monkeypatch):
    fake = FakeFPP(playing="Song.fseq")
    monkeypatch.setattr(httpx, "request", fake)
    fseq = tmp_path / "Song.fseq"
    fseq.write_bytes(b"PSEQ")
    assert "playing" in upload.upload_sequence(cfg.resolve("radio"), fseq)["error"]
    assert not [c for c in fake.calls if c[0] == "POST"]
    assert upload.upload_sequence(cfg.resolve("radio"), fseq, force=True)["success"]


def test_401_explains_without_leaking(cfg, monkeypatch):
    monkeypatch.setattr(httpx, "request", FakeFPP(status_code=401))
    err = client.get_fpp_status(cfg.resolve("radio"))["error"]
    assert "rejected" in err and SECRET not in err
    err = client.get_fpp_status(cfg.resolve("matrix"))["error"]
    assert "no password configured" in err


def test_status_redacts_wifi_key(cfg, monkeypatch):
    status = {"interfaces": [{"ifname": "wlan0", "config": {"SSID": "Home", "PSK": "wifi-key-123", "ADDRESS": "1.2.3.4"}}],
              "wifi_password": "pw2", "empty": {"PSK": ""}}

    def fake(method, url, auth=None, timeout=None, **kw):
        return httpx.Response(200, json=status, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake)
    out = client.get_fpp_status(cfg.resolve("radio"))
    dumped = json.dumps(out)
    assert "wifi-key-123" not in dumped and "pw2" not in dumped
    conf = out["status"]["interfaces"][0]["config"]
    assert conf["PSK"] == client.REDACTED and conf["SSID"] == "Home" and conf["ADDRESS"] == "1.2.3.4"
    assert out["status"]["empty"]["PSK"] == ""


def test_logs_list_and_tail(cfg, monkeypatch):
    log = "\n".join([f"line {i}" for i in range(10)] + ['wpa psk="abc123" ok', "ERROR boom", "Password: hunter2"])

    def fake(method, url, auth=None, timeout=None, **kw):
        req = httpx.Request(method, url)
        if url.endswith("/api/files/logs"):
            return httpx.Response(200, json={"files": [{"name": "fppd.log", "sizeBytes": 9, "mtime": "now"}]}, request=req)
        if url.endswith("/api/files/crashes"):
            return httpx.Response(404, request=req)
        if url.endswith("/api/file/Logs/var/log/syslog"):
            return httpx.Response(200, text="boot ok", request=req)
        if url.endswith("/api/file/Logs/cut.log"):
            return httpx.Response(200, content=b"before\n\0\0\0after\n", request=req)
        if url.endswith("/api/file/Logs/gone.log"):
            return httpx.Response(200, text="File /home/fpp/media/logs/gone.log does not exist.", request=req)
        assert url.endswith("/api/file/Logs/fppd.log")
        return httpx.Response(200, text=log, request=req)

    monkeypatch.setattr(httpx, "request", fake)
    radio = cfg.resolve("radio")
    listed = client.list_logs(radio)
    assert listed["logs"] == [{"name": "fppd.log", "bytes": 9, "modified": "now"}] and listed["crash_reports"] == []
    out = client.read_log(radio, "fppd.log", lines=3)
    assert out["total_lines"] == 13 and out["shown_lines"] == 3
    assert "abc123" not in out["text"] and "hunter2" not in out["text"] and "ERROR boom" in out["text"]
    out = client.read_log(radio, "fppd.log", grep="error|line 9")
    assert out["matching_lines"] == 2 and out["text"] == "line 9\nERROR boom"
    assert "error" in client.read_log(radio, "../config/settings")
    assert "error" in client.read_log(radio, "/etc/shadow")
    assert "does not exist" in client.read_log(radio, "gone.log")["error"]
    assert client.read_log(radio, "/var/log/syslog")["text"] == "boot ok"
    assert client.read_log(radio, "cut.log")["text"] == "before\n[3 NUL bytes: unclean power-off?]\nafter"
