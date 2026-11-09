"""Falcon Pi Player (FPP) REST API client."""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx

from xlights_mcp.config import FPPConfig

logger = logging.getLogger(__name__)

REDACTED = "[redacted]"
# Keys whose values are secrets (FPP's /system/status includes each interface's Wi-Fi PSK in plain text).
_SECRET_KEY = re.compile(r"(?i)^(psk|wpa_?passphrase|passphrase|pass|passwd|password|secret|.*_(psk|password|passwd|secret))$")
# The same secrets written as key=value / key: value inside log text.
_SECRET_TEXT = re.compile(r"""(?i)\b(psk|wpa_?passphrase|passphrase|passwd|password)(\s*[=:]\s*["']?)([^\s"'&,;]+)""")

LOG_LINES_MAX = 2000


def redact(obj):
    """Copy of a JSON-like value with every secret-looking field's value replaced."""
    if isinstance(obj, dict):
        return {
            k: REDACTED if isinstance(k, str) and _SECRET_KEY.match(k) and v not in ("", None) else redact(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def redact_text(text: str) -> str:
    """Replace secret values written as key=value / key: value in free text."""
    return _SECRET_TEXT.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)


def _base_url(config: FPPConfig) -> str:
    return f"http://{config.host}:{config.port}"


def _http_error(config: FPPConfig, e: httpx.HTTPStatusError) -> dict:
    code = e.response.status_code
    if code == 401:
        hint = (
            "no password configured for this FPP (add FPP_PASS_<NAME> to the FPP env file)"
            if config.auth() is None
            else "the password in the FPP env file was rejected"
        )
        return {"error": f"FPP {config.label} refused the login (HTTP 401): {hint}"}
    return {"error": f"FPP {config.label} answered HTTP {code} for {e.request.url.path}"}


def _send(config: FPPConfig, method: str, path: str, **kw) -> httpx.Response | dict:
    """The raw response, or an error dict."""
    url = f"{_base_url(config)}/api{path}"
    try:
        resp = httpx.request(method, url, auth=config.auth(), timeout=kw.pop("timeout", config.timeout), **kw)
        resp.raise_for_status()
        return resp
    except httpx.ConnectError:
        return {"error": f"Cannot connect to FPP at {config.label}. Is it on the network?"}
    except httpx.TimeoutException:
        return {"error": f"Timeout connecting to FPP at {config.label}"}
    except httpx.HTTPStatusError as e:
        return _http_error(config, e)
    except Exception as e:
        return {"error": f"FPP {config.label}: {type(e).__name__}"}


def _request(config: FPPConfig, method: str, path: str, **kw) -> dict:
    resp = _send(config, method, path, **kw)
    if isinstance(resp, dict):
        return resp
    if not resp.content:
        return {"success": True}
    try:
        return resp.json()
    except ValueError:
        return {"success": True, "text": resp.text[:500]}


def _get(config: FPPConfig, path: str, **kw) -> dict:
    """Make a GET request to the FPP API."""
    return _request(config, "GET", path, **kw)


def _post(config: FPPConfig, path: str, data: dict | None = None, **kw) -> dict:
    """Make a POST request to the FPP API."""
    return _request(config, "POST", path, json=data, **kw)


def get_fpp_status(config: FPPConfig) -> dict:
    """Get FPP system status including current playback state."""
    status = _get(config, "/system/status")
    if "error" in status:
        return status

    return {
        "connected": True,
        "fpp": config.name or None,
        "host": config.host,
        "status": redact(status),
    }


def _log_files(config: FPPConfig, folder: str) -> list[dict] | dict:
    result = _get(config, f"/files/{folder}")
    if "error" in result:
        return result
    files = result.get("files", []) if isinstance(result, dict) else result
    return [
        {"name": f.get("name"), "bytes": f.get("sizeBytes"), "modified": f.get("mtime")}
        for f in files
        if isinstance(f, dict)
    ]


def list_logs(config: FPPConfig) -> dict:
    """The FPP's log files and crash reports (names, sizes, times)."""
    logs = _log_files(config, "logs")
    if isinstance(logs, dict):
        return logs
    crashes = _log_files(config, "crashes")
    return {
        "fpp": config.name or None,
        "host": config.host,
        "logs": logs,
        # Crash reports are zips: download them from the FPP UI (File Manager -> Crash Reports).
        "crash_reports": crashes if isinstance(crashes, list) else [],
    }


def read_log(config: FPPConfig, name: str, lines: int = 200, grep: str | None = None) -> dict:
    """The last `lines` lines of one log file (only those matching `grep`, if given), secrets redacted."""
    # The list names system logs by absolute path ("/var/log/syslog"); FPP serves those as Logs/var/log/<name>.
    path = name[1:] if name.startswith("/var/log/") else name
    if not path or ".." in path or path.startswith(("/", "\\")) or ("/" in path and not path.startswith("var/log/")):
        return {"error": f"Bad log name: {name!r} (use a name from the log list)"}
    lines = max(1, min(int(lines), LOG_LINES_MAX))
    resp = _send(config, "GET", f"/file/Logs/{quote(path)}", timeout=max(config.timeout, 30))
    if isinstance(resp, dict):
        return resp
    if resp.text.startswith("File ") and resp.text.rstrip().endswith("does not exist."):
        return {"error": f"{name} does not exist on FPP {config.label}"}
    if resp.content[:2] in (b"PK", b"\x1f\x8b"):
        return {"error": f"{name} is compressed, not a text file"}
    # Runs of NUL bytes mean the FPP lost power before the log blocks were written: keep them visible.
    text = re.sub("\0+", lambda m: f"[{len(m.group())} NUL bytes: unclean power-off?]\n", resp.text)
    all_lines = redact_text(text).splitlines()
    picked = all_lines
    if grep:
        try:
            pattern = re.compile(grep, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(grep), re.IGNORECASE)
        picked = [ln for ln in all_lines if pattern.search(ln)]
    shown = picked[-lines:]
    return {
        "fpp": config.name or None,
        "host": config.host,
        "log": name,
        "total_lines": len(all_lines),
        "matching_lines": len(picked) if grep else None,
        "shown_lines": len(shown),
        "text": "\n".join(shown),
    }


def list_playlists(config: FPPConfig) -> dict:
    """List all playlists on FPP."""
    result = _get(config, "/playlists")
    if "error" in result:
        return result
    return {"playlists": result}


def start_playlist(config: FPPConfig, name: str, repeat: bool = False) -> dict:
    """Start a playlist on FPP."""
    repeat_val = "1" if repeat else "0"
    return _get(config, f"/playlist/{name}/start/{repeat_val}")


def stop_playback(config: FPPConfig) -> dict:
    """Stop current playback on FPP."""
    return _get(config, "/playlists/stop")


def list_sequences(config: FPPConfig) -> dict:
    """List sequences stored on FPP."""
    return _get(config, "/sequence")


def get_schedule(config: FPPConfig) -> dict:
    """Get the FPP schedule."""
    return _get(config, "/schedule")
