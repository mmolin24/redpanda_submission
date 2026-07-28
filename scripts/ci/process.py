"""Bounded subprocess and signal handling for repository-owned CI commands."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import IO, Any

_MAX_CAPTURE_BYTES = 256 * 1024
_CAPTURE_CHUNK_BYTES = 64 * 1024


class ProcessFailure(RuntimeError):
    """A stable subprocess failure that contains no captured command content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProcessInterrupted(BaseException):
    """An external termination request handled after child cleanup."""

    def __init__(self, signal_number: int) -> None:
        super().__init__(signal_number)
        self.signal_number = signal_number


@contextmanager
def termination_signal_scope() -> Iterator[None]:
    """Convert SIGINT/SIGTERM into cleanup-safe exceptions."""
    previous_handlers: dict[int, Any] = {}

    def interrupt(signal_number: int, _frame: FrameType | None) -> None:
        raise ProcessInterrupted(signal_number)

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, interrupt)
    try:
        yield
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)


def run_capture(
    command: Sequence[str],
    root: Path,
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
    failure_code: str,
) -> str:
    """Run one command with bounded output, time, and process-group lifetime."""
    try:
        process = subprocess.Popen(
            tuple(command),
            cwd=root,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        raise ProcessFailure(failure_code) from None
    if process.stdout is None:
        _stop_process(process)
        raise ProcessFailure(failure_code)

    output = bytearray()
    deadline = time.monotonic() + timeout_seconds
    selector: selectors.BaseSelector | None = None
    try:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
    except OSError:
        _close_capture_resources(selector, process.stdout)
        _stop_process(process)
        raise ProcessFailure(failure_code) from None
    except BaseException:
        _close_capture_resources(selector, process.stdout)
        _stop_process(process)
        raise
    if selector is None:
        _stop_process(process)
        raise ProcessFailure(failure_code)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                raise ProcessFailure(failure_code)
            events = selector.select(timeout=min(remaining, 0.1))
            if not events:
                continue
            chunk = os.read(
                process.stdout.fileno(),
                min(
                    _CAPTURE_CHUNK_BYTES,
                    _MAX_CAPTURE_BYTES + 1 - len(output),
                ),
            )
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > _MAX_CAPTURE_BYTES:
                _stop_process(process)
                raise ProcessFailure("tool_output_too_large")
    except OSError:
        _stop_process(process)
        raise ProcessFailure(failure_code) from None
    except BaseException:
        _stop_process(process)
        raise
    finally:
        active_exception = sys.exc_info()[0] is not None
        close_failed = _close_capture_resources(selector, process.stdout)
        if close_failed:
            _stop_process(process)
            if not active_exception:
                raise ProcessFailure(failure_code) from None

    try:
        return_code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _stop_process(process)
        raise ProcessFailure(failure_code) from None
    descendants_detected = _process_group_exists(process.pid)
    if descendants_detected:
        _terminate_process_group(process, grace_seconds=0)
    if return_code != 0:
        raise ProcessFailure(failure_code)
    if descendants_detected:
        raise ProcessFailure("process_group_descendant_detected")
    try:
        return bytes(output).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ProcessFailure(failure_code) from None


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    grace_seconds: int = 10,
) -> None:
    """Allow cleanup traps to run before forcibly reaping one command group."""
    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
    if not _wait_for_process_group_exit(
        process,
        process_group_id,
        timeout_seconds=grace_seconds,
        stop_when_leader_exits=True,
    ):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        if not _wait_for_process_group_exit(
            process,
            process_group_id,
            timeout_seconds=5,
            stop_when_leader_exits=False,
        ):
            raise ProcessFailure("process_group_cleanup_failed")
    if process.poll() is None:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _wait_for_process_group_exit(
    process: subprocess.Popen[bytes],
    process_group_id: int,
    *,
    timeout_seconds: int,
    stop_when_leader_exits: bool,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        leader_exited = process.poll() is not None
        if not _process_group_exists(process_group_id):
            return True
        if stop_when_leader_exits and leader_exited:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return True
    return True


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    _terminate_process_group(process, grace_seconds=0)


def _close_capture_resources(
    selector: selectors.BaseSelector | None,
    stdout: IO[bytes],
) -> bool:
    close_failed = False
    if selector is not None:
        try:
            selector.close()
        except Exception:
            close_failed = True
    try:
        stdout.close()
    except Exception:
        close_failed = True
    return close_failed
