"""Falcon Pi Player (FPP) REST API client."""

from __future__ import annotations

import logging

import httpx

from xlights_mcp.config import FPPConfig

logger = logging.getLogger(__name__)


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


def _request(config: FPPConfig, method: str, path: str, **kw) -> dict:
    url = f"{_base_url(config)}/api{path}"
    try:
        resp = httpx.request(method, url, auth=config.auth(), timeout=kw.pop("timeout", config.timeout), **kw)
        resp.raise_for_status()
        if not resp.content:
            return {"success": True}
        try:
            return resp.json()
        except ValueError:
            return {"success": True, "text": resp.text[:500]}
    except httpx.ConnectError:
        return {"error": f"Cannot connect to FPP at {config.label}. Is it on the network?"}
    except httpx.TimeoutException:
        return {"error": f"Timeout connecting to FPP at {config.label}"}
    except httpx.HTTPStatusError as e:
        return _http_error(config, e)
    except Exception as e:
        return {"error": f"FPP {config.label}: {type(e).__name__}"}


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
        "status": status,
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
