# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""RepoProver: Multi-file git-based autoformalization."""

from .agents.lean_tools import (
    configure_global_pool,
    get_global_pool,
    shutdown_global_pool,
)
from .lean_checker import CheckResult, LeanChecker, LeanCheckerConfig

__version__ = "0.1.0"

__all__ = [
    "LeanChecker",
    "LeanCheckerConfig",
    "CheckResult",
    # Centralized pool management
    "configure_global_pool",
    "get_global_pool",
    "shutdown_global_pool",
]
