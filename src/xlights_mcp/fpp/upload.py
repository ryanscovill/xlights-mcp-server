"""Upload sequences and audio to Falcon Pi Player."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

from xlights_mcp.config import FPPConfig
from xlights_mcp.fpp.client import _get, _request

logger = logging.getLogger(__name__)


def upload_sequence(
    config: FPPConfig,
    fseq_path: Path,
    audio_path: Path | None = None,
    force: bool = False,
) -> dict:
    """Upload a .fseq sequence (and optional audio) to FPP, then read its header back.

    Args:
        config: FPP connection config (already resolved to one FPP)
        fseq_path: Path to the .fseq file
        audio_path: Optional path to audio file (.mp3, .ogg)
        force: Overwrite even if the FPP is playing this sequence right now
    """
    if not fseq_path.exists():
        return {"error": f"File not found: {fseq_path}"}
    name = fseq_path.name

    status = _get(config, "/fppd/status", timeout=6.0)
    if "error" in status:
        return status
    if (
        not force
        and status.get("status_name") == "playing"
        and status.get("current_sequence") == name
    ):
        return {"error": f"{config.label} is playing {name} right now; stop it first or pass force=True"}

    result = _request(
        config, "POST", f"/sequence/{quote(name)}",
        content=fseq_path.read_bytes(),
        headers={"Content-Type": "application/octet-stream"},
        timeout=120.0,
    )
    if "error" in result:
        return {"error": f"Failed to upload {name}: {result['error']}"}

    meta = _get(config, f"/sequence/{quote(name)}/meta")
    results: dict = {
        "fpp": config.name or None,
        "host": config.host,
        "fseq": {
            "uploaded": True,
            "name": name,
            "bytes": fseq_path.stat().st_size,
            "frames": meta.get("NumFrames"),
            "channels": meta.get("ChannelCount"),
            "step_ms": meta.get("StepTime"),
            "verified": "error" not in meta and int(meta.get("NumFrames") or 0) > 0,
        },
    }

    if audio_path and audio_path.exists():
        a = _request(
            config, "POST", f"/file/music/{quote(audio_path.name)}",
            content=audio_path.read_bytes(),
            headers={"Content-Type": "application/octet-stream"},
            timeout=120.0,
        )
        results["audio"] = {"error": a["error"]} if "error" in a else {"uploaded": True, "name": audio_path.name}

    return {"success": results["fseq"]["verified"], **results}
