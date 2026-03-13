# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Shell tool for agents to execute commands.

Provides a bash tool that uses SafeShell for command validation and execution.
Agents can run commands like `lake build` through this interface.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..safe_shell import AgentRole, SafeShell

SHELL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": """Execute a shell command in the repository.

Use for running build commands, git operations, or other shell tasks.
Commands are validated for safety before execution.

Examples:
  bash(command="lake build MyBook.Chapter1")
  bash(command="lake build")
  bash(command="git status")
  bash(command="grep -r 'sorry' MyBook/")

Returns: Command output (stdout + stderr) or error message.""",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                },
                "required": ["command"],
            },
        },
    },
]

SHELL_TOOL_NAMES = frozenset(tool["function"]["name"] for tool in SHELL_TOOLS)


class ShellToolsMixin:
    """Mixin providing shell command execution to agents.

    Requires either:
    - self.safe_shell: SafeShell instance (preferred)
    - self.repo_root: Path (will create SafeShell on demand)

    Set self.agent_role to control permissions (default: WORKER).
    """

    safe_shell: "SafeShell | None"
    repo_root: Path | None
    agent_role: "AgentRole | None"

    def register_tools(self, defs: dict, handlers: dict) -> None:
        """Register shell tools."""
        super().register_tools(defs, handlers)  # type: ignore[misc]
        self._register_tools_from_list(SHELL_TOOLS, defs, handlers)

    def _get_safe_shell(self) -> "SafeShell":
        """Get or create the SafeShell instance."""
        if hasattr(self, "safe_shell") and self.safe_shell is not None:
            return self.safe_shell

        # Create on demand
        from ..safe_shell import SafeShell, SafeShellConfig, AgentRole

        role = getattr(self, "agent_role", None) or AgentRole.WORKER
        root = self._get_shell_root()

        shell = SafeShell(
            SafeShellConfig(
                repo_root=root,
                role=role,
            )
        )

        # Cache it
        if hasattr(self, "safe_shell"):
            self.safe_shell = shell

        return shell

    def _get_shell_root(self) -> Path:
        """Get the root path for shell operations."""
        if hasattr(self, "worktree_manager") and self.worktree_manager is not None:
            return self.worktree_manager.worktree_path
        if hasattr(self, "repo_root") and self.repo_root is not None:
            return self.repo_root
        raise RuntimeError("ShellToolsMixin requires worktree_manager or repo_root")

    def _handle_bash(self, args: dict[str, Any]) -> str:
        """Handle bash tool call."""
        command = args.get("command", "")
        if not command:
            return "Error: command is required"

        shell = self._get_safe_shell()
        result = shell.run(command)

        if result.error:
            return f"Error: {result.error}"

        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"

        # Truncate very long outputs
        max_chars = 10000
        if len(output) > max_chars:
            output = output[:max_chars] + f"\n\n... [truncated, showing first {max_chars} chars]"

        return output if output else "(no output)"
