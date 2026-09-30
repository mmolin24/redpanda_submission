"""Start the local stack and configure API links from Grafana's published port."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

from .grafana import published_grafana_url


def _run(command: tuple[str, ...], environment: Mapping[str, str]) -> str:
    """Run Compose, capturing only its port lookup."""
    capture = command[-3:] == ("port", "grafana", "3000")
    result = subprocess.run(
        command,
        env=dict(environment),
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        timeout=900,
    )
    return result.stdout or ""


def start_stack(
    compose: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    sync_only: bool = False,
    runner: Callable[[tuple[str, ...], Mapping[str, str]], str] = _run,
) -> str:
    """Discover Grafana after startup, then recreate only the API with its URL."""
    if not sync_only:
        runner((*compose, "up", "--build", "--force-recreate", "--detach", "--wait"), environment)
    url = published_grafana_url(runner((*compose, "port", "grafana", "3000"), environment))
    api_environment = dict(environment)
    api_environment["GRAFANA_BASE_URL"] = url
    runner(
        (*compose, "up", "--detach", "--no-deps", "--force-recreate", "--wait", "api"),
        api_environment,
    )
    return url


def main(arguments: Sequence[str] | None = None) -> int:
    """Accept Compose global options after --, including project and file selectors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sync-only", action="store_true", help="Repair API links in a running stack"
    )
    parser.add_argument("compose_options", nargs=argparse.REMAINDER)
    args = parser.parse_args(arguments)
    options = args.compose_options
    if options[:1] == ["--"]:
        options = options[1:]
    try:
        url = start_stack(("docker", "compose", *options), os.environ, sync_only=args.sync_only)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"Stack startup failed: {error}", file=sys.stderr)
        return 1
    print(f"API trace links configured for Grafana at {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
