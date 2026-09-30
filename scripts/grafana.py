"""Resolve the browser URL from Grafana's actual local Compose port mapping."""

from __future__ import annotations

import re


def published_grafana_url(published: str) -> str:
    """Require one loopback binding with a valid assigned TCP port."""
    match = re.fullmatch(r"(?:127\.0\.0\.1|localhost|\[::1\]):([0-9]{1,5})", published.strip())
    if match is None or not 1 <= int(match.group(1)) <= 65_535:
        raise ValueError("Grafana must publish one valid loopback port.")
    return f"http://127.0.0.1:{match.group(1)}"
