# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Lean code snippet checking tool for agents.

Provides lean_check for testing code snippets using the REPL.
Uses a centralized REPL pool for efficient resource sharing across all agents.
"""

from __future__ import annotations

import atexit
import threading
from logging import getLogger
from pathlib import Path

from ..lean_checker import CheckResult, LeanChecker, LeanCheckerConfig

logger = getLogger(__name__)

LEAN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lean_check",
            "description": """Check a Lean 4 code snippet for compilation errors.

The code is checked using a centralized REPL pool with Mathlib preloaded,
making it much faster than running lake build each time.

Example:
  lean_check(code=\"\"\"
  import Mathlib

  example : 1 + 1 = 2 := by norm_num
  \"\"\")

Returns: "OK" if compiles, or error messages with goal states.""",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Lean 4 code to check",
                    },
                },
                "required": ["code"],
            },
        },
    },
]

LEAN_TOOL_NAMES = frozenset(tool["function"]["name"] for tool in LEAN_TOOLS)


# =============================================================================
# Global REPL Pool Service
# =============================================================================

# Global pool configuration
_pool_config: LeanCheckerConfig | None = None
_global_pool: LeanChecker | None = None
_pool_lock = threading.Lock()


def configure_global_pool(
    workspace: str | Path,
    pool_size: int = 10,
    timeout: float = 300.0,
    header_timeout: float = 180.0,
    instance_mem_limit_gb: int = 24,
) -> None:
    """Configure the global REPL pool. Must be called before any agent uses lean_check.

    The pool is started lazily on first use, but this configuration must be set first.

    Args:
        workspace: Path to Lean workspace (must have lake-manifest.json)
        pool_size: Number of concurrent REPL instances (default: 10)
        timeout: Request timeout in seconds
        header_timeout: Header compilation timeout in seconds
        instance_mem_limit_gb: Memory limit per REPL instance in GB
    """
    global _pool_config
    _pool_config = LeanCheckerConfig(
        workspace=str(workspace),
        pool_size=pool_size,
        timeout=timeout,
        header_timeout=header_timeout,
        instance_mem_limit_gb=instance_mem_limit_gb,
    )
    logger.info(f"Configured global REPL pool: workspace={workspace}, pool_size={pool_size}")


def get_global_pool() -> LeanChecker | None:
    """Get or create the global REPL pool.

    Returns None if the pool is not configured.
    Thread-safe: only one thread will create the pool.
    """
    global _global_pool

    if _pool_config is None:
        return None

    # Fast path: pool already exists
    if _global_pool is not None:
        return _global_pool

    # Slow path: create pool (thread-safe)
    with _pool_lock:
        # Double-check after acquiring lock
        if _global_pool is not None:
            return _global_pool

        logger.info("Starting global REPL pool...")
        _global_pool = LeanChecker(_pool_config)
        _global_pool.start()
        logger.info(f"Global REPL pool started with {_pool_config.pool_size} instances")
        return _global_pool


def shutdown_global_pool() -> None:
    """Shutdown the global REPL pool. Call this on application exit."""
    global _global_pool

    with _pool_lock:
        if _global_pool is not None:
            logger.info("Shutting down global REPL pool...")
            _global_pool.close()
            _global_pool = None
            logger.info("Global REPL pool shut down")


def is_global_pool_configured() -> bool:
    """Check if the global pool has been configured."""
    return _pool_config is not None


def is_global_pool_running() -> bool:
    """Check if the global pool is currently running."""
    return _global_pool is not None


# Register shutdown on interpreter exit
atexit.register(shutdown_global_pool)


# =============================================================================
# Agent Mixin
# =============================================================================


class LeanToolsMixin:
    """Mixin providing Lean snippet checking tool to agents.

    All agents share a centralized REPL pool, which is more efficient than
    each agent creating its own REPL instance.

    Requires:
        self.repo_root: Path - path to the Lean workspace (set by BaseAgent)
    """

    repo_root: Path | None

    def register_tools(self, defs: dict, handlers: dict) -> None:
        """Register Lean tools."""
        super().register_tools(defs, handlers)  # type: ignore[misc]
        self._register_tools_from_list(LEAN_TOOLS, defs, handlers)

    def _handle_lean_check(self, args: dict) -> str:
        """Handle lean_check tool call."""
        code = args.get("code", "")
        if not code:
            return "Error: code is required"

        # Use the global pool
        pool = get_global_pool()
        if pool is None:
            # Fallback: try to auto-configure from repo_root
            if self.repo_root is not None:
                logger.warning(
                    "Global pool not configured, auto-configuring from repo_root. "
                    "For better performance, call configure_global_pool() at startup."
                )
                configure_global_pool(self.repo_root, pool_size=10)
                pool = get_global_pool()

            if pool is None:
                return (
                    "Error: No Lean workspace configured. Call configure_global_pool() at startup or set repo_root."
                )

        result = pool.check_code(code)
        return result.format_for_agent()


# =============================================================================
# Convenience functions for external use
# =============================================================================


def check_code(code: str, timeout: float | None = None) -> CheckResult:
    """Check Lean code using the global pool.

    This is a convenience function for checking code without an agent.
    The global pool must be configured first with configure_global_pool().

    Args:
        code: Lean code to check
        timeout: Optional timeout override

    Returns:
        CheckResult with errors, warnings, and sorries

    Raises:
        RuntimeError: If the global pool is not configured
    """
    pool = get_global_pool()
    if pool is None:
        raise RuntimeError("Global REPL pool not configured. Call configure_global_pool() first.")
    return pool.check_code(code, timeout=timeout)
