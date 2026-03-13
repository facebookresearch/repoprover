# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Agents for repoprover.

This module provides the agent infrastructure for the multi-file
autoformalization system:

- BaseAgent: Abstract base class with LLM integration
- ContributorAgent: Unified agent for all formalization work (sketch, prove, maintain, scan, progress, triage)
- ReviewerAgent: Reviews PRs for math and engineering quality
- FileToolsMixin: File manipulation tools
- GitWorktreeToolsMixin: Git tools for worktree-based workflow
- LeanToolsMixin: Lean code checking tools
- ShellToolsMixin: Shell command execution
"""

from .base import (
    AgentConfig,
    AgentResult,
    AgentRun,
    BaseAgent,
    LearningsStore,
    ToolCall,
    dialog_to_text,
)
from .contributor import (
    ContributorAgent,
    ContributorMode,
    ContributorResult,
    ContributorTask,
)
from .file_tools import FILE_TOOLS, FileToolsMixin
from .git_worktree_tools import GIT_WORKTREE_TOOLS, GitWorktreeToolsMixin
from .lean_tools import LEAN_TOOLS, LeanToolsMixin
from .shell_tools import SHELL_TOOLS, ShellToolsMixin

__all__ = [
    # Base
    "AgentConfig",
    "AgentResult",
    "AgentRun",
    "BaseAgent",
    "LearningsStore",
    "ToolCall",
    "dialog_to_text",
    # Tools
    "FILE_TOOLS",
    "FileToolsMixin",
    "GIT_WORKTREE_TOOLS",
    "GitWorktreeToolsMixin",
    "LEAN_TOOLS",
    "LeanToolsMixin",
    "SHELL_TOOLS",
    "ShellToolsMixin",
    # Unified Contributor Agent
    "ContributorAgent",
    "ContributorMode",
    "ContributorResult",
    "ContributorTask",
]
