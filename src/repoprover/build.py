# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Centralized build management with concurrency control.

This module provides a semaphore-controlled `lake build` function to prevent
resource exhaustion when many agents try to build simultaneously.

Usage:
    from repoprover.build import lake_build

    # Synchronous (blocking) call
    result = lake_build(worktree_path, label="review")

    # Async call (preferred in async context)
    result = await lake_build_async(worktree_path, label="merge")

The semaphore limits concurrent builds to prevent:
- CPU contention from parallel compilation
- Memory exhaustion
- Disk I/O bottlenecks on .olean cache files
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

# Maximum concurrent lake build processes
# This prevents resource exhaustion when many reviews/merges run in parallel
MAX_CONCURRENT_BUILDS: int = 8

# Default build timeout in seconds (10 minutes)
DEFAULT_BUILD_TIMEOUT: float = 600.0

# =============================================================================
# Concurrency Primitives
# =============================================================================

# Thread-based semaphore for sync callers
_build_semaphore = threading.Semaphore(MAX_CONCURRENT_BUILDS)

# Async semaphore for async callers (lazily initialized per event loop)
_async_semaphores: dict[int, asyncio.Semaphore] = {}
_async_semaphore_lock = threading.Lock()


def _get_async_semaphore() -> asyncio.Semaphore:
    """Get or create an async semaphore for the current event loop."""
    try:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
    except RuntimeError:
        # No running event loop, create a placeholder
        loop_id = 0

    with _async_semaphore_lock:
        if loop_id not in _async_semaphores:
            _async_semaphores[loop_id] = asyncio.Semaphore(MAX_CONCURRENT_BUILDS)
        return _async_semaphores[loop_id]


# =============================================================================
# Build Result
# =============================================================================


@dataclass
class BuildResult:
    """Result of a lake build operation."""

    success: bool
    """Whether the build succeeded (returncode == 0)."""

    error: str | None
    """Error message if build failed, None otherwise."""

    duration: float | None
    """Build duration in seconds, None if timed out before completion."""

    returncode: int | None
    """Process return code, None if timed out."""

    stdout: str
    """Standard output from the build."""

    stderr: str
    """Standard error from the build."""

    timed_out: bool
    """Whether the build was killed due to timeout."""

    waited_for_semaphore: float
    """Time spent waiting for the build semaphore."""


# =============================================================================
# Build Functions
# =============================================================================


def lake_build(
    cwd: Path | str,
    *,
    target: str | None = None,
    timeout: float = DEFAULT_BUILD_TIMEOUT,
    label: str = "build",
) -> BuildResult:
    """Run `lake build` with concurrency control.

    This function acquires a semaphore before running the build to limit
    the number of concurrent builds across all agents/reviews.

    Args:
        cwd: Working directory (worktree path) to run the build in.
        target: Optional build target (e.g., "MyModule"). If None, builds all.
        timeout: Build timeout in seconds. Default is 10 minutes.
        label: Label for logging (e.g., "review", "merge", "agent").

    Returns:
        BuildResult with success status, error message, duration, and output.

    Example:
        result = lake_build(worktree_path, label="review")
        if not result.success:
            print(f"Build failed: {result.error}")
    """
    cwd = Path(cwd)

    # Build command
    cmd = ["lake", "build"]
    if target:
        cmd.append(target)
    cmd_str = " ".join(cmd)

    # Track semaphore wait time
    wait_start = time.monotonic()

    # Acquire semaphore (blocks if too many builds are running)
    logger.debug(f"[{label}] Waiting for build semaphore...")
    _build_semaphore.acquire()
    wait_duration = time.monotonic() - wait_start

    if wait_duration > 1.0:
        logger.info(f"[{label}] Waited {wait_duration:.1f}s for build semaphore")

    try:
        logger.info(f"[{label}] Build started: {cmd_str} in {cwd}")
        build_start = time.monotonic()

        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration = time.monotonic() - build_start

            success = result.returncode == 0
            error = None if success else (result.stderr.strip() or result.stdout.strip() or "Build failed")

            log_fn = logger.info if success else logger.warning
            log_fn(f"[{label}] Build {'passed' if success else 'FAILED'} ({duration:.1f}s)")

            return BuildResult(
                success=success,
                error=error,
                duration=duration,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                timed_out=False,
                waited_for_semaphore=wait_duration,
            )

        except subprocess.TimeoutExpired:
            duration = time.monotonic() - build_start
            logger.warning(f"[{label}] Build timed out after {timeout}s")
            return BuildResult(
                success=False,
                error=f"Build timed out after {int(timeout)} seconds",
                duration=None,
                returncode=None,
                stdout="",
                stderr="",
                timed_out=True,
                waited_for_semaphore=wait_duration,
            )

    finally:
        _build_semaphore.release()


async def lake_build_async(
    cwd: Path | str,
    *,
    target: str | None = None,
    timeout: float = DEFAULT_BUILD_TIMEOUT,
    label: str = "build",
) -> BuildResult:
    """Async version of lake_build.

    Uses asyncio.Semaphore for non-blocking concurrency control and
    runs the actual build in a thread pool to avoid blocking the event loop.

    Args:
        cwd: Working directory (worktree path) to run the build in.
        target: Optional build target (e.g., "MyModule"). If None, builds all.
        timeout: Build timeout in seconds. Default is 10 minutes.
        label: Label for logging (e.g., "review", "merge", "agent").

    Returns:
        BuildResult with success status, error message, duration, and output.
    """
    cwd = Path(cwd)

    # Build command
    cmd = ["lake", "build"]
    if target:
        cmd.append(target)
    cmd_str = " ".join(cmd)

    # Track semaphore wait time
    wait_start = time.monotonic()

    # Get async semaphore for current event loop
    semaphore = _get_async_semaphore()

    logger.debug(f"[{label}] Waiting for async build semaphore...")
    async with semaphore:
        wait_duration = time.monotonic() - wait_start

        if wait_duration > 1.0:
            logger.info(f"[{label}] Waited {wait_duration:.1f}s for build semaphore")

        logger.info(f"[{label}] Build started: {cmd_str} in {cwd}")
        build_start = time.monotonic()

        try:
            # Run subprocess in thread pool to avoid blocking event loop
            loop = asyncio.get_running_loop()
            proc = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        cmd,
                        cwd=cwd,
                        capture_output=True,
                        text=True,
                    ),
                ),
                timeout=timeout,
            )
            duration = time.monotonic() - build_start

            success = proc.returncode == 0
            error = None if success else (proc.stderr.strip() or proc.stdout.strip() or "Build failed")

            log_fn = logger.info if success else logger.warning
            log_fn(f"[{label}] Build {'passed' if success else 'FAILED'} ({duration:.1f}s)")

            return BuildResult(
                success=success,
                error=error,
                duration=duration,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                timed_out=False,
                waited_for_semaphore=wait_duration,
            )

        except asyncio.TimeoutError:
            duration = time.monotonic() - build_start
            logger.warning(f"[{label}] Build timed out after {timeout}s")
            return BuildResult(
                success=False,
                error=f"Build timed out after {int(timeout)} seconds",
                duration=None,
                returncode=None,
                stdout="",
                stderr="",
                timed_out=True,
                waited_for_semaphore=wait_duration,
            )


# =============================================================================
# Configuration Functions
# =============================================================================


def set_max_concurrent_builds(max_builds: int) -> None:
    """Update the maximum number of concurrent builds.

    Note: This only affects new semaphores. For full effect, call this
    before any builds have started.

    Args:
        max_builds: New maximum number of concurrent builds.
    """
    global MAX_CONCURRENT_BUILDS, _build_semaphore, _async_semaphores

    MAX_CONCURRENT_BUILDS = max_builds
    _build_semaphore = threading.Semaphore(max_builds)

    with _async_semaphore_lock:
        _async_semaphores.clear()

    logger.info(f"Set max concurrent builds to {max_builds}")


def get_build_stats() -> dict[str, int]:
    """Get current build concurrency stats.

    Returns:
        Dict with 'max_concurrent' and 'available_slots' keys.
    """
    # Note: _value is an internal attribute, might not be portable
    available = getattr(_build_semaphore, "_value", MAX_CONCURRENT_BUILDS)
    return {
        "max_concurrent": MAX_CONCURRENT_BUILDS,
        "available_slots": available,
        "active_builds": MAX_CONCURRENT_BUILDS - available,
    }
