"""Narrow cleanup helper for disposable SIP/WebRTC acceptance runs."""

import subprocess
from pathlib import Path
from typing import Callable


def remove_smoke_recordings(
    root: Path,
    names: set[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> bool:
    """Remove exact WAV basenames from this Compose project's SIP volume."""
    safe_names = sorted(
        name for name in names
        if Path(name).name == name and name.endswith(".wav")
    )
    if not safe_names:
        return True
    run = runner or subprocess.run
    result = run(
        [
            "docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.sip.yml",
            "exec", "-T", "sip", "rm", "-f", "--",
            *(f"/recordings/{name}" for name in safe_names),
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    verification = run(
        [
            "docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.sip.yml",
            "exec", "-T", "sip", "find", "/recordings", "-maxdepth", "1",
            "-type", "f", "-name", "*.wav",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if verification.returncode != 0:
        return False
    remaining = {Path(line).name for line in verification.stdout.splitlines()}
    return not remaining.intersection(safe_names)
