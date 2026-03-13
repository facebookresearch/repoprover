# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Utility functions for RepoProver."""

import functools
import logging
import random
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable)


@contextmanager
def log_time(label: str, level: int = logging.INFO):
    """Context manager that logs the elapsed time for a block.

    Usage:
        with log_time("expensive operation"):
            do_something()
        # Logs: "expensive operation took 1.234s"
    """
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        logger.log(level, f"{label} took {elapsed:.3f}s")


def timed(label: str | None = None, level: int = logging.INFO) -> Callable[[F], F]:
    """Decorator that logs the elapsed time for a function call.

    Usage:
        @timed()
        def my_function():
            ...

        @timed("custom label", level=logging.INFO)
        def another_function():
            ...
    """

    def decorator(func: F) -> F:
        func_label = label or func.__qualname__

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - t0
                logger.log(level, f"{func_label} took {elapsed:.3f}s")

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return await func(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - t0
                logger.log(level, f"{func_label} took {elapsed:.3f}s")

        import asyncio

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return wrapper  # type: ignore

    return decorator


class TimedCompletedProcess(subprocess.CompletedProcess):
    """CompletedProcess with duration attribute."""

    duration: float


def timed_run(
    cmd: list[str],
    cwd: Path | str | None = None,
    check: bool = False,
    timeout: float | None = None,
    capture_output: bool = True,
    text: bool = True,
) -> TimedCompletedProcess:
    """Run a subprocess with timing logged.

    Usage:
        result = timed_run(["git", "checkout", "main"], cwd=repo_path)
        print(result.duration)  # access the duration

    Note: For `lake build`, use the centralized `build.lake_build()` function
    instead, which provides semaphore-controlled concurrency limiting.

    Logs: "git checkout main took 0.045s"
    """
    label = " ".join(cmd[:3]) if len(cmd) > 3 else " ".join(cmd)
    t0 = time.perf_counter()
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            check=check,
            timeout=timeout,
            capture_output=capture_output,
            text=text,
        )
        elapsed = time.perf_counter() - t0
        # Create a TimedCompletedProcess with duration
        timed_result = TimedCompletedProcess(result.args, result.returncode, result.stdout, result.stderr)
        timed_result.duration = elapsed
        logger.info(f"{label} took {elapsed:.3f}s")
        return timed_result
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - t0
        logger.info(f"{label} timed out after {elapsed:.3f}s")
        raise


def run_command_with_retry(
    command: list[str],
    cwd: Path | str,
    *,
    timeout: float = 60.0,
    retries: int = 2,
    retry_delay: float = 2.0,
    success_checker: Callable[[subprocess.CompletedProcess], bool] | None = None,
) -> tuple[bool, str | None, subprocess.CompletedProcess | None]:
    """Run a command with retries and exponential backoff.

    Args:
        command: Full command to run, e.g. ["git", "worktree", "add", ...]
        cwd: Working directory
        timeout: Timeout per attempt in seconds
        retries: Number of retries after initial attempt (total attempts = retries + 1)
        retry_delay: Base delay between retries. Uses exponential backoff with
                     jitter (0.5x-1.5x) to avoid thundering herd. Capped at 60s total.
        success_checker: Optional function to check if a non-zero exit is actually OK.
                        Receives CompletedProcess, returns True if should be treated as success.

    Returns:
        (success, error_message, result)
        - success: True if command succeeded
        - error_message: Error description if failed, None if success
        - result: CompletedProcess if command ran (even if failed), None if exception
    """
    max_attempts = retries + 1
    last_error: str | None = None
    last_result: subprocess.CompletedProcess | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            last_result = result
        except subprocess.TimeoutExpired as exc:
            last_error = f"timeout after {timeout}s ({exc})"
            logger.warning(
                "Command %s timed out on attempt %s/%s",
                command,
                attempt,
                max_attempts,
            )
        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                "Command %s raised %s on attempt %s/%s",
                command,
                exc,
                attempt,
                max_attempts,
            )
        else:
            if result.returncode == 0:
                if attempt > 1:
                    logger.info(
                        "Command %s succeeded after %s attempts",
                        command,
                        attempt,
                    )
                return True, None, result

            # Check if non-zero exit should be treated as success
            if success_checker and success_checker(result):
                logger.info("Command %s returned %s but success_checker passed", command, result.returncode)
                return True, None, result

            stderr = (result.stderr or "").strip()
            last_error = f"exit code {result.returncode}: {stderr}"
            logger.warning(
                "Command %s failed with exit code %s on attempt %s/%s: %s",
                command,
                result.returncode,
                attempt,
                max_attempts,
                stderr,
            )

        if attempt < max_attempts:
            # Exponential backoff with jitter to avoid thundering herd
            base_delay = min(retry_delay * (2 ** (attempt - 1)), 30.0)
            # Add jitter: random value between 0.5x and 1.5x the base delay
            jitter = base_delay * (0.5 + random.random())
            sleep_seconds = min(base_delay + jitter, 60.0)  # Cap total at 60s
            time.sleep(sleep_seconds)

    logger.error(
        "Command %s failed after %s attempts: %s",
        command,
        max_attempts,
        last_error,
    )
    return False, last_error, last_result


def run_git_with_retry(
    args: list[str],
    cwd: Path | str,
    *,
    timeout: float = 60.0,
    retries: int = 2,
    retry_delay: float = 2.0,
    allow_noop: bool = False,
) -> tuple[bool, str | None, subprocess.CompletedProcess | None]:
    """Run a git command with retries and exponential backoff.

    Convenience wrapper around run_command_with_retry for git commands.

    Args:
        args: Git arguments (without 'git' prefix), e.g. ["worktree", "add", ...]
        cwd: Working directory to run git in
        timeout: Timeout per attempt in seconds
        retries: Number of retries after initial attempt (total attempts = retries + 1)
        retry_delay: Base delay between retries. Uses exponential backoff with
                     jitter (0.5x-1.5x) to avoid thundering herd. Capped at 60s total.
        allow_noop: If True, treat "nothing to commit" as success

    Returns:
        (success, error_message, result)
    """

    def noop_checker(result: subprocess.CompletedProcess) -> bool:
        if allow_noop and "nothing to commit" in (result.stderr or "").lower():
            return True
        return False

    return run_command_with_retry(
        ["git", *args],
        cwd=cwd,
        timeout=timeout,
        retries=retries,
        retry_delay=retry_delay,
        success_checker=noop_checker if allow_noop else None,
    )
