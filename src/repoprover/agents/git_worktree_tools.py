# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Git worktree tool definitions and mixins for agents.

This module provides git tools for agents working in isolated worktrees:

- GitWorktreeToolsMixin - Git tools for agents in worktrees
  - git_status, git_add, git_commit, git_diff, git_log
  - git_unstage, git_restore, git_checkout_file
  - git_rebase, git_rebase_continue, git_rebase_abort, git_rebase_skip
  - git_conflicts, git_show, git_reset

Security: Commands are executed via subprocess.run() with explicit argument
lists (shell=False). No shell expansion or arbitrary command execution.
"""

from __future__ import annotations

import os
import subprocess
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..git_worktree import WorktreeManager

logger = getLogger(__name__)


# =============================================================================
# Feature Worker Tool Definitions
# =============================================================================


GIT_WORKTREE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": """Check the git status of your worktree.

Returns modified, staged, and untracked files. Use before committing to see what changed.

Example output:
  Changes staged for commit:
    M  MyBook/Chapter1.lean

  Changes not staged:
    M  MyBook/Chapter2.lean

  Untracked files:
    MyBook/Scratch.lean""",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_add",
            "description": """Stage files for commit.

Adds specified files to the staging area. Use '.' to stage all changes.
Only files within your worktree can be staged.

Example: git_add(paths=["MyBook/Chapter1.lean", "MyBook/Chapter2.lean"])""",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths to stage (relative to worktree root)",
                    },
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": """Commit staged changes with a message.

Creates a commit with all staged changes. You must stage files with git_add first.
Use git_status to verify what will be committed.

Example: git_commit(message="Prove theorem foo using induction")""",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Commit message describing the changes",
                    },
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": """Show uncommitted changes in your worktree.

Returns a unified diff of all unstaged changes, or changes for specific files.
Useful for reviewing your work before committing.

Example: git_diff() or git_diff(paths=["MyBook/Chapter1.lean"])""",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: specific files to diff",
                    },
                    "staged": {
                        "type": "boolean",
                        "description": "If true, show staged changes (--cached)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": """Show recent commits in your branch.

Returns the last N commits with hash, author, date, and message.
Useful for seeing what work has been done.

Example: git_log(n=5)""",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": "Number of commits to show (default: 10)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_unstage",
            "description": """Unstage files that were added with git_add.

Removes files from the staging area without discarding changes.
The files remain modified in your working directory.

Example: git_unstage(paths=["MyBook/Chapter1.lean"])""",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths to unstage (relative to worktree root)",
                    },
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_restore",
            "description": """Discard uncommitted changes to files.

Reverts files to their state at the last commit. WARNING: This discards
your changes permanently - use with caution.

Example: git_restore(paths=["MyBook/Chapter1.lean"])""",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths to restore (relative to worktree root)",
                    },
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_checkout_file",
            "description": """Checkout file(s) from a specific ref. Runs `git checkout <ref> -- <paths>`.

WARNING: This OVERWRITES your working copy with the version from <ref>.
Any uncommitted changes to these files will be LOST.

Use cases:
- Get a file from main: git_checkout_file(ref="main", paths=["file.lean"])
- Get a file from a commit: git_checkout_file(ref="abc123", paths=["file.lean"])
- Get a file from HEAD: git_checkout_file(ref="HEAD", paths=["file.lean"])

DO NOT use this to "resolve" merge conflicts - that deletes your work!
To resolve conflicts, edit the files manually to combine both versions.

Example: git_checkout_file(ref="main", paths=["MyBook/Chapter1.lean"])""",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Git ref (branch name, commit hash, HEAD, etc.)",
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths to checkout (relative to repo root)",
                    },
                },
                "required": ["ref", "paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_rebase",
            "description": """Rebase current branch onto another branch. Runs `git rebase <branch>`.

Replays your commits on top of the target branch (default: main). This is the
recommended way to sync your branch with main.

If conflicts occur, the rebase pauses. You must:
1. Edit conflicted files to resolve conflicts (remove <<<<<<<, =======, >>>>>>> markers)
2. Call git_add() to stage resolved files
3. Call git_rebase_continue() to continue

Or call git_rebase_abort() to cancel and return to the original state.

Example: git_rebase() or git_rebase(branch="main")""",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "description": "Branch to rebase onto (default: main)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_rebase_continue",
            "description": """Continue a paused rebase after resolving conflicts. Runs `git rebase --continue`.

Use after you have:
1. Edited all conflicted files to resolve conflicts
2. Staged the resolved files with git_add()

If more conflicts occur, you'll need to resolve them and continue again.

Example: git_rebase_continue()""",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_rebase_abort",
            "description": """Abort a rebase in progress and return to original state. Runs `git rebase --abort`.

Use when you want to cancel a rebase that has conflicts you cannot resolve.
Your branch will be restored to its state before the rebase started.

Example: git_rebase_abort()""",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_rebase_skip",
            "description": """Skip the current commit during a rebase. Runs `git rebase --skip`.

Use when the current commit's changes are no longer needed (e.g., they were
already applied in the target branch). The commit will be dropped.

Example: git_rebase_skip()""",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_conflicts",
            "description": """Show conflict markers and their line numbers. Runs `git diff --check`.

Use during a merge/rebase conflict to find exactly WHERE conflicts are in each file.
This is essential for large files - shows line numbers so you can read just the
conflicted sections.

Example output:
  AlgComb/PentagonalNumber.lean:45: leftover conflict marker
  AlgComb/PentagonalNumber.lean:47: leftover conflict marker
  AlgComb/PentagonalNumber.lean:49: leftover conflict marker
  AlgComb/DetSum.lean:120: leftover conflict marker

Then read just those sections:
  file_read("AlgComb/PentagonalNumber.lean", start_line=40, end_line=55)

Example: git_conflicts()""",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_show",
            "description": """Show information about a git object. Runs `git show <ref>`.

Displays commit information and diff for a commit, or file contents for a blob.

Examples:
- Show latest commit: git_show(ref="HEAD")
- Show specific commit: git_show(ref="abc1234")
- Show file at ref: git_show(ref="main:MyBook/Chapter1.lean")""",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Git ref - commit hash, branch, HEAD, or path like 'branch:path/to/file'",
                    },
                },
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_reset",
            "description": """Reset current HEAD to a specified state. Runs `git reset [--soft|--mixed|--hard] <ref>`.

Modes:
- soft: Move HEAD only, keep staging and working tree (undo commits, keep changes staged)
- mixed (default): Move HEAD and reset staging, keep working tree (undo commits, unstage changes)
- hard: Move HEAD, reset staging AND working tree (discard all changes - DANGEROUS)

Examples:
- Undo last commit, keep changes staged: git_reset(ref="HEAD~1", mode="soft")
- Undo last commit, unstage changes: git_reset(ref="HEAD~1", mode="mixed")
- Discard all changes since commit: git_reset(ref="abc123", mode="hard")""",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Git ref to reset to (default: HEAD)",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["soft", "mixed", "hard"],
                        "description": "Reset mode: soft, mixed (default), or hard",
                    },
                },
            },
        },
    },
]

GIT_WORKTREE_TOOL_NAMES = frozenset(tool["function"]["name"] for tool in GIT_WORKTREE_TOOLS)


# =============================================================================
# Main Agent Tool Definitions (Elevated Privileges)
# =============================================================================


GIT_MAIN_AGENT_TOOLS = [
    *GIT_WORKTREE_TOOLS,
    {
        "type": "function",
        "function": {
            "name": "git_merge",
            "description": """Merge a feature branch into the current branch.

Merges the specified branch (usually an agent's feature branch) into main.
Use --no-ff to preserve merge commit for history.

Example: git_merge(branch="agent-prover-ch1", no_ff=True)""",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "description": "Branch name to merge",
                    },
                    "no_ff": {
                        "type": "boolean",
                        "description": "Create merge commit even if fast-forward possible (default: True)",
                    },
                    "message": {
                        "type": "string",
                        "description": "Optional merge commit message",
                    },
                },
                "required": ["branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_checkout",
            "description": """Switch to a different branch.

Only allowed branches: 'main' or agent branches matching 'agent-*' pattern.

Example: git_checkout(branch="main")""",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "description": "Branch name to checkout",
                    },
                },
                "required": ["branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_branch_delete",
            "description": """Delete a feature branch after successful merge.

Only allows deleting agent branches (agent-*). Cannot delete main.

Example: git_branch_delete(branch="agent-prover-ch1")""",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "description": "Branch name to delete",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Force delete even if not fully merged (default: False)",
                    },
                },
                "required": ["branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_branch_list",
            "description": """List all branches, showing which are merged.

Returns all branches with indicators for current branch and merge status.

Example output:
  * main
    agent-prover-ch1 (merged)
    agent-prover-ch2
    agent-sketcher-ch3 (merged)""",
            "parameters": {
                "type": "object",
                "properties": {
                    "merged_only": {
                        "type": "boolean",
                        "description": "Only show branches merged into current branch",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_reset",
            "description": """Reset current branch to a specific state.

Use to undo a bad merge or reset to a known good state.
Only soft and mixed modes allowed (preserves working directory).

Example: git_reset(ref="HEAD~1", mode="soft")""",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Commit reference to reset to (e.g., HEAD~1, commit hash)",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["soft", "mixed"],
                        "description": "Reset mode: 'soft' keeps staged, 'mixed' unstages (default: mixed)",
                    },
                },
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_show",
            "description": """Show details of a specific commit.

Returns the commit message and diff for a given commit reference.

Example: git_show(ref="agent-prover-ch1") or git_show(ref="HEAD~2")""",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Commit reference (branch name, hash, HEAD~N)",
                    },
                    "stat_only": {
                        "type": "boolean",
                        "description": "Show only file change statistics, not full diff",
                    },
                },
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff_branches",
            "description": """Show diff between two branches.

Compares a feature branch to the current branch (usually main).
Useful for reviewing PR changes.

Example: git_diff_branches(branch="agent-prover-ch1") or
         git_diff_branches(branch="agent-prover-ch1", stat_only=True)""",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "description": "Branch to compare against current branch",
                    },
                    "stat_only": {
                        "type": "boolean",
                        "description": "Show only file change statistics, not full diff",
                    },
                },
                "required": ["branch"],
            },
        },
    },
]

GIT_MAIN_AGENT_TOOL_NAMES = frozenset(tool["function"]["name"] for tool in GIT_MAIN_AGENT_TOOLS)


# =============================================================================
# Feature Worker Tools Mixin
# =============================================================================


class GitWorktreeToolsMixin:
    """Mixin that adds git worktree tool handling to an agent.

    Agents using this mixin must have:
    - worktree_manager: WorktreeManager attribute

    Tools are registered automatically via register_tools.
    """

    worktree_manager: "WorktreeManager | None"
    repo_root: "Path | None"

    def _get_repo_root(self) -> Path:
        """Get the repo root path for git operations."""
        if self.repo_root is not None:
            return self.repo_root
        raise RuntimeError("GitWorktreeToolsMixin requires repo_root")

    def _validate_path(self, path: str) -> tuple[bool, str]:
        """Validate a path is safe for git operations."""
        root = self._get_repo_root()
        try:
            if Path(path).is_absolute():
                p = Path(path)
            else:
                p = root / path

            if p.exists() or p.is_symlink():
                resolved = p.parent.resolve() / p.name
            else:
                resolved = p.resolve()

            resolved.relative_to(root.resolve())
            return True, "ok"
        except ValueError:
            return False, f"Path escapes repository: {path}"

    def register_tools(self, defs: dict, handlers: dict) -> None:
        """Register git worktree tools."""
        super().register_tools(defs, handlers)  # type: ignore[misc]
        self._register_tools_from_list(GIT_WORKTREE_TOOLS, defs, handlers)

    def _handle_git_status(self, args: dict[str, Any]) -> str:
        logger.info("git_status()")
        return self._format_status(self._run_git_command(["status", "--porcelain=v1"]))

    def _handle_git_add(self, args: dict[str, Any]) -> str:
        paths = args.get("paths", [])
        if not paths:
            return "Error: paths parameter is required"

        for p in paths:
            ok, msg = self._validate_path(p)
            if not ok:
                return f"Error: {msg}"

        logger.info(f"git_add({paths})")
        return self._run_git_command(["add", "--"] + paths)

    def _handle_git_commit(self, args: dict[str, Any]) -> str:
        message = args.get("message", "")
        if not message:
            return "Error: commit message is required"

        log_message = message.replace("\n", "\\n")
        log_msg = f"git_commit('{log_message[:50]}...')" if len(log_message) > 50 else f"git_commit('{log_message}')"
        logger.info(log_msg)
        return self._run_git_command(["commit", "-m", message])

    def _handle_git_diff(self, args: dict[str, Any]) -> str:
        cmd = ["diff"]
        if args.get("staged"):
            cmd.append("--cached")

        paths = args.get("paths", [])
        for p in paths:
            ok, msg = self._validate_path(p)
            if not ok:
                return f"Error: {msg}"

        if paths:
            cmd.extend(["--"] + paths)

        logger.info(f"git_diff(staged={args.get('staged', False)}, paths={paths})")
        return self._run_git_command(cmd)

    def _handle_git_log(self, args: dict[str, Any]) -> str:
        n = args.get("n", 10)
        logger.info(f"git_log(n={n})")
        return self._run_git_command(["log", f"-{n}", "--oneline", "--decorate"])

    def _handle_git_unstage(self, args: dict[str, Any]) -> str:
        paths = args.get("paths", [])
        if not paths:
            return "Error: paths parameter is required"

        for p in paths:
            ok, msg = self._validate_path(p)
            if not ok:
                return f"Error: {msg}"

        logger.info(f"git_unstage({paths})")
        return self._run_git_command(["reset", "HEAD", "--"] + paths)

    def _handle_git_restore(self, args: dict[str, Any]) -> str:
        paths = args.get("paths", [])
        if not paths:
            return "Error: paths parameter is required"

        for p in paths:
            ok, msg = self._validate_path(p)
            if not ok:
                return f"Error: {msg}"

        logger.info(f"git_restore({paths})")
        return self._run_git_command(["checkout", "HEAD", "--"] + paths)

    def _handle_git_checkout_file(self, args: dict[str, Any]) -> str:
        ref = args.get("ref", "")
        paths = args.get("paths", [])

        if not ref:
            return "Error: ref parameter is required"
        if not paths:
            return "Error: paths parameter is required"

        for p in paths:
            ok, msg = self._validate_path(p)
            if not ok:
                return f"Error: {msg}"

        logger.info(f"git_checkout_file(ref={ref}, paths={paths})")
        return self._run_git_command(["checkout", ref, "--"] + paths)

    def _handle_git_rebase(self, args: dict[str, Any]) -> str:
        branch = args.get("branch")
        if not branch:
            branch = self._get_main_branch_name()
            if branch is None:
                return "Error: could not find main or master branch"

        logger.info(f"git_rebase({branch})")
        result = self._run_git_command(["rebase", branch])

        # Check for conflicts
        if "CONFLICT" in result or "conflict" in result.lower():
            # Get conflicted files
            status = self._run_git_command(["diff", "--name-only", "--diff-filter=U"])
            conflict_files = [f for f in status.strip().split("\n") if f and f != "(no output)"]

            return f"""Rebase paused due to conflicts.

Conflicted files:
{chr(10).join("  - " + f for f in conflict_files)}

To resolve:
1. Edit each file to resolve conflicts (look for <<<<<<<, =======, >>>>>>> markers)
2. Stage resolved files: git_add(paths=[...])
3. Continue rebase: git_rebase_continue()

Or abort: git_rebase_abort()"""

        # Return success message if git output is empty or unhelpful
        if not result or result == "(no output)":
            return f"Rebase onto '{branch}' completed successfully."
        return result

    def _handle_git_rebase_continue(self, args: dict[str, Any]) -> str:
        logger.info("git_rebase_continue()")

        # Set environment to avoid editor prompt
        result = self._run_git_command_with_env(["rebase", "--continue"], {"GIT_EDITOR": "true"})

        # Check for more conflicts
        if "CONFLICT" in result:
            status = self._run_git_command(["diff", "--name-only", "--diff-filter=U"])
            conflict_files = [f for f in status.strip().split("\n") if f and f != "(no output)"]
            return f"""More conflicts encountered.

Conflicted files:
{chr(10).join("  - " + f for f in conflict_files)}

Resolve these files, stage with git_add(), then git_rebase_continue() again."""

        # Return success message if git output is empty or unhelpful
        if not result or result == "(no output)":
            return "Rebase continued successfully."
        return result

    def _handle_git_rebase_abort(self, args: dict[str, Any]) -> str:
        logger.info("git_rebase_abort()")
        result = self._run_git_command(["rebase", "--abort"])
        if not result or result == "(no output)":
            return "Rebase aborted successfully."
        return result

    def _handle_git_rebase_skip(self, args: dict[str, Any]) -> str:
        logger.info("git_rebase_skip()")
        result = self._run_git_command(["rebase", "--skip"])
        if not result or result == "(no output)":
            return "Skipped commit and continued rebase."
        return result

    def _handle_git_conflicts(self, args: dict[str, Any]) -> str:
        """Show conflict markers with line numbers using git diff --check."""
        logger.info("git_conflicts()")

        # git diff --check returns non-zero when it finds issues,
        # so we need to handle that specially
        try:
            proc = subprocess.run(
                ["git", "diff", "--check"],
                cwd=self.worktree_manager.worktree_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            # git diff --check returns exit code 2 when conflicts found
            if proc.stdout.strip():
                return proc.stdout.strip()
            if proc.stderr.strip():
                return proc.stderr.strip()
            return "No conflict markers found."
        except subprocess.TimeoutExpired:
            return "Error: command timed out"
        except Exception as e:
            return f"Error: {e}"

    def _handle_git_show(self, args: dict[str, Any]) -> str:
        ref = args.get("ref", "HEAD")
        logger.info(f"git_show({ref})")
        return self._run_git_command(["show", ref])

    def _handle_git_reset(self, args: dict[str, Any]) -> str:
        ref = args.get("ref", "HEAD")
        mode = args.get("mode", "mixed")

        if mode not in ("soft", "mixed", "hard"):
            return f"Error: invalid mode '{mode}', must be soft, mixed, or hard"

        logger.info(f"git_reset(ref={ref}, mode={mode})")
        return self._run_git_command(["reset", f"--{mode}", ref])

    def _get_main_branch_name(self) -> str | None:
        """Determine if the repo uses 'main' or 'master' as the main branch."""
        result = self._run_git_command(["rev-parse", "--verify", "main"])
        if not result.startswith("Error:"):
            return "main"

        result = self._run_git_command(["rev-parse", "--verify", "master"])
        if not result.startswith("Error:"):
            return "master"

        return None

    def _run_git_command(self, args: list[str]) -> str:
        """Run a git command in the worktree directory."""
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=self._get_repo_root(),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return f"Error: {result.stderr.strip()}"
            return result.stdout.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: command timed out"
        except Exception as e:
            return f"Error: {e}"

    def _run_git_command_with_env(self, args: list[str], env: dict[str, str]) -> str:
        """Run a git command with additional environment variables."""
        try:
            full_env = os.environ.copy()
            full_env.update(env)
            result = subprocess.run(
                ["git"] + args,
                cwd=self._get_repo_root(),
                capture_output=True,
                text=True,
                timeout=30,
                env=full_env,
            )
            if result.returncode != 0:
                return f"Error: {result.stderr.strip()}"
            return result.stdout.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: command timed out"
        except Exception as e:
            return f"Error: {e}"

    def _format_status(self, porcelain_output: str) -> str:
        """Format porcelain status output into human-readable form."""
        if porcelain_output.startswith("Error:"):
            return porcelain_output

        if not porcelain_output or porcelain_output == "(no output)":
            return "Working tree clean - no changes"

        staged = []
        unstaged = []
        untracked = []
        conflicts = []

        for line in porcelain_output.split("\n"):
            if not line or len(line) < 4:
                continue
            x, y = line[0], line[1]
            filename = line[2:].lstrip(" ")

            # Check for conflicts (both sides modified)
            if x == "U" or y == "U" or (x == "A" and y == "A") or (x == "D" and y == "D"):
                conflicts.append(f"  {filename}")
            elif x == "?":
                untracked.append(filename)
            else:
                if x != " ":
                    staged.append(f"  {x}  {filename}")
                if y != " ":
                    unstaged.append(f"  {y}  {filename}")

        output = []

        # Show conflicts prominently at the top
        if conflicts:
            output.append("⚠️  CONFLICTS (must be resolved):")
            output.extend(conflicts)
            output.append("")

        if staged:
            output.append("Changes staged for commit:")
            output.extend(staged)
        if unstaged:
            if output:
                output.append("")
            output.append("Changes not staged:")
            output.extend(unstaged)
        if untracked:
            if output:
                output.append("")
            output.append("Untracked files:")
            output.extend(f"  {f}" for f in untracked)

        return "\n".join(output) if output else "Working tree clean - no changes"


# =============================================================================
# Main Agent Tools Mixin (Elevated Privileges)
# =============================================================================


class MainAgentGitToolsMixin:
    """Mixin for main agent with elevated git capabilities.

    Unlike feature workers, the main agent:
    - Operates in the base project directory (not a worktree)
    - Can merge feature branches
    - Can checkout different branches
    - Can delete branches after merge

    Agents using this mixin must have:
    - base_project: Path attribute pointing to the main repo directory

    Tools are registered automatically via register_tools (same pattern as other mixins).
    """

    base_project: Path

    def register_tools(self, defs: dict, handlers: dict) -> None:
        """Register main agent git tools."""
        super().register_tools(defs, handlers)  # type: ignore[misc]
        self._register_tools_from_list(GIT_MAIN_AGENT_TOOLS, defs, handlers)

    # -------------------------------------------------------------------------
    # Elevated privilege tools (main agent only)
    # -------------------------------------------------------------------------

    def _handle_git_merge(self, args: dict[str, Any]) -> str:
        branch = args.get("branch", "")
        if not self._validate_branch_name(branch):
            return f"Error: invalid branch name '{branch}'"

        cmd = ["merge"]
        if args.get("no_ff", True):
            cmd.append("--no-ff")
        if msg := args.get("message"):
            cmd.extend(["-m", msg])
        cmd.append(branch)

        logger.info(f"git_merge({branch})")
        return self._run_main_git_command(cmd)

    def _handle_git_checkout(self, args: dict[str, Any]) -> str:
        branch = args.get("branch", "")
        if not self._validate_branch_name(branch, allow_main=True):
            return f"Error: invalid branch name '{branch}'"

        logger.info(f"git_checkout({branch})")
        return self._run_main_git_command(["checkout", branch])

    def _handle_git_branch_delete(self, args: dict[str, Any]) -> str:
        branch = args.get("branch", "")
        if branch in ("main", "master"):
            return "Error: cannot delete main branch"
        if not branch.startswith("agent-"):
            return f"Error: can only delete agent branches, got '{branch}'"

        flag = "-D" if args.get("force") else "-d"
        logger.info(f"git_branch_delete({branch}, force={args.get('force', False)})")
        return self._run_main_git_command(["branch", flag, branch])

    def _handle_git_branch_list(self, args: dict[str, Any]) -> str:
        cmd = ["branch", "-vv"]
        if args.get("merged_only"):
            cmd.append("--merged")
        logger.info(f"git_branch_list(merged_only={args.get('merged_only', False)})")
        return self._run_main_git_command(cmd)

    def _handle_git_reset(self, args: dict[str, Any]) -> str:
        ref = args.get("ref", "")
        if not ref:
            return "Error: ref parameter is required"

        mode = args.get("mode", "mixed")
        if mode not in ("soft", "mixed"):
            return f"Error: only 'soft' and 'mixed' reset modes allowed, got '{mode}'"

        logger.info(f"git_reset({ref}, mode={mode})")
        return self._run_main_git_command(["reset", f"--{mode}", ref])

    def _handle_git_show(self, args: dict[str, Any]) -> str:
        ref = args.get("ref", "HEAD")
        cmd = ["show", ref]
        if args.get("stat_only"):
            cmd.append("--stat")
        logger.info(f"git_show({ref}, stat_only={args.get('stat_only', False)})")
        return self._run_main_git_command(cmd)

    def _handle_git_diff_branches(self, args: dict[str, Any]) -> str:
        branch = args.get("branch", "")
        if not branch:
            return "Error: branch parameter is required"
        cmd = ["diff", f"HEAD...{branch}"]
        if args.get("stat_only"):
            cmd.append("--stat")
        logger.info(f"git_diff_branches({branch})")
        return self._run_main_git_command(cmd)

    # -------------------------------------------------------------------------
    # Base worktree tools (re-implemented for base_project instead of worktree)
    # -------------------------------------------------------------------------

    def _handle_git_status(self, args: dict[str, Any]) -> str:
        logger.info("git_status()")
        return self._format_status(self._run_main_git_command(["status", "--porcelain=v1"]))

    def _handle_git_add(self, args: dict[str, Any]) -> str:
        paths = args.get("paths", [])
        if not paths:
            return "Error: paths parameter is required"
        logger.info(f"git_add({paths})")
        return self._run_main_git_command(["add", "--"] + paths)

    def _handle_git_commit(self, args: dict[str, Any]) -> str:
        message = args.get("message", "")
        if not message:
            return "Error: commit message is required"
        log_message = message.replace("\n", "\\n")
        log_msg = f"git_commit('{log_message[:50]}...')" if len(log_message) > 50 else f"git_commit('{log_message}')"
        logger.info(log_msg)
        return self._run_main_git_command(["commit", "-m", message])

    def _handle_git_diff(self, args: dict[str, Any]) -> str:
        cmd = ["diff"]
        if args.get("staged"):
            cmd.append("--cached")
        paths = args.get("paths", [])
        if paths:
            cmd.extend(["--"] + paths)
        logger.info(f"git_diff(staged={args.get('staged', False)}, paths={paths})")
        return self._run_main_git_command(cmd)

    def _handle_git_log(self, args: dict[str, Any]) -> str:
        n = args.get("n", 10)
        logger.info(f"git_log(n={n})")
        return self._run_main_git_command(["log", f"-{n}", "--oneline", "--decorate"])

    def _handle_git_unstage(self, args: dict[str, Any]) -> str:
        paths = args.get("paths", [])
        if not paths:
            return "Error: paths parameter is required"
        logger.info(f"git_unstage({paths})")
        return self._run_main_git_command(["reset", "HEAD", "--"] + paths)

    def _handle_git_restore(self, args: dict[str, Any]) -> str:
        paths = args.get("paths", [])
        if not paths:
            return "Error: paths parameter is required"
        logger.info(f"git_restore({paths})")
        return self._run_main_git_command(["restore", "--"] + paths)

    def _handle_git_checkout_file(self, args: dict[str, Any]) -> str:
        ref = args.get("ref", "")
        paths = args.get("paths", [])

        if not ref:
            return "Error: ref parameter is required"
        if not paths:
            return "Error: paths parameter is required"

        logger.info(f"git_checkout_file(ref={ref}, paths={paths})")
        return self._run_main_git_command(["checkout", ref, "--"] + paths)

    def _handle_git_rebase(self, args: dict[str, Any]) -> str:
        branch = args.get("branch")
        if not branch:
            branch = self._get_main_branch_name()
            if branch is None:
                return "Error: could not find main or master branch"

        logger.info(f"git_rebase({branch})")
        result = self._run_main_git_command(["rebase", branch])

        # Check for conflicts
        if "CONFLICT" in result or "conflict" in result.lower():
            # Get conflicted files
            status = self._run_main_git_command(["diff", "--name-only", "--diff-filter=U"])
            conflict_files = [f for f in status.strip().split("\n") if f and f != "(no output)"]

            return f"""Rebase paused due to conflicts.

Conflicted files:
{chr(10).join("  - " + f for f in conflict_files)}

To resolve:
1. Edit each file to resolve conflicts (look for <<<<<<<, =======, >>>>>>> markers)
2. Stage resolved files: git_add(paths=[...])
3. Continue rebase: git_rebase_continue()

Or abort: git_rebase_abort()"""

        # Return success message if git output is empty or unhelpful
        if not result or result == "(no output)":
            return f"Rebase onto '{branch}' completed successfully."
        return result

    def _handle_git_rebase_continue(self, args: dict[str, Any]) -> str:
        logger.info("git_rebase_continue()")

        # Set environment to avoid editor prompt
        result = self._run_main_git_command_with_env(["rebase", "--continue"], {"GIT_EDITOR": "true"})

        # Check for more conflicts
        if "CONFLICT" in result:
            status = self._run_main_git_command(["diff", "--name-only", "--diff-filter=U"])
            conflict_files = [f for f in status.strip().split("\n") if f and f != "(no output)"]
            return f"""More conflicts encountered.

Conflicted files:
{chr(10).join("  - " + f for f in conflict_files)}

Resolve these files, stage with git_add(), then git_rebase_continue() again."""

        # Return success message if git output is empty or unhelpful
        if not result or result == "(no output)":
            return "Rebase continued successfully."
        return result

    def _handle_git_rebase_abort(self, args: dict[str, Any]) -> str:
        logger.info("git_rebase_abort()")
        result = self._run_main_git_command(["rebase", "--abort"])
        if not result or result == "(no output)":
            return "Rebase aborted successfully."
        return result

    def _handle_git_rebase_skip(self, args: dict[str, Any]) -> str:
        logger.info("git_rebase_skip()")
        result = self._run_main_git_command(["rebase", "--skip"])
        if not result or result == "(no output)":
            return "Skipped commit and continued rebase."
        return result

    def _handle_git_conflicts(self, args: dict[str, Any]) -> str:
        """Show conflict markers with line numbers using git diff --check."""
        logger.info("git_conflicts()")

        # git diff --check returns non-zero when it finds issues,
        # so we need to handle that specially
        try:
            proc = subprocess.run(
                ["git", "diff", "--check"],
                cwd=self.base_project,
                capture_output=True,
                text=True,
                timeout=30,
            )
            # git diff --check returns exit code 2 when conflicts found
            if proc.stdout.strip():
                return proc.stdout.strip()
            if proc.stderr.strip():
                return proc.stderr.strip()
            return "No conflict markers found."
        except subprocess.TimeoutExpired:
            return "Error: command timed out"
        except Exception as e:
            return f"Error: {e}"

    # -------------------------------------------------------------------------
    # Helper methods
    # -------------------------------------------------------------------------

    def _validate_branch_name(self, name: str, allow_main: bool = False) -> bool:
        """Validate branch name against allowed patterns."""
        if not name:
            return False
        if allow_main and name in ("main", "master"):
            return True
        return name.startswith("agent-") and "/" not in name

    def _get_main_branch_name(self) -> str | None:
        """Determine if the repo uses 'main' or 'master' as the main branch."""
        result = self._run_main_git_command(["rev-parse", "--verify", "main"])
        if not result.startswith("Error:"):
            return "main"
        result = self._run_main_git_command(["rev-parse", "--verify", "master"])
        if not result.startswith("Error:"):
            return "master"
        return None

    def _run_main_git_command(self, args: list[str]) -> str:
        """Run a git command in the base project directory."""
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=self.base_project,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                return f"Error: {result.stderr.strip()}"
            return result.stdout.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: command timed out"
        except Exception as e:
            return f"Error: {e}"

    def _run_main_git_command_with_env(self, args: list[str], env: dict[str, str]) -> str:
        """Run a git command with additional environment variables."""
        try:
            full_env = os.environ.copy()
            full_env.update(env)
            result = subprocess.run(
                ["git"] + args,
                cwd=self.base_project,
                capture_output=True,
                text=True,
                timeout=60,
                env=full_env,
            )
            if result.returncode != 0:
                return f"Error: {result.stderr.strip()}"
            return result.stdout.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: command timed out"
        except Exception as e:
            return f"Error: {e}"

    def _format_status(self, porcelain_output: str) -> str:
        """Format porcelain status output into human-readable form."""
        if porcelain_output.startswith("Error:"):
            return porcelain_output

        if not porcelain_output or porcelain_output == "(no output)":
            return "Working tree clean - no changes"

        staged = []
        unstaged = []
        untracked = []
        conflicts = []

        for line in porcelain_output.split("\n"):
            if not line or len(line) < 4:
                continue
            x, y = line[0], line[1]
            filename = line[2:].lstrip(" ")

            # Check for conflicts (both sides modified)
            if x == "U" or y == "U" or (x == "A" and y == "A") or (x == "D" and y == "D"):
                conflicts.append(f"  {filename}")
            elif x == "?":
                untracked.append(filename)
            else:
                if x != " ":
                    staged.append(f"  {x}  {filename}")
                if y != " ":
                    unstaged.append(f"  {y}  {filename}")

        output = []

        # Show conflicts prominently at the top
        if conflicts:
            output.append("⚠️  CONFLICTS (must be resolved):")
            output.extend(conflicts)
            output.append("")

        if staged:
            output.append("Changes staged for commit:")
            output.extend(staged)
        if unstaged:
            if output:
                output.append("")
            output.append("Changes not staged:")
            output.extend(unstaged)
        if untracked:
            if output:
                output.append("")
            output.append("Untracked files:")
            output.extend(f"  {f}" for f in untracked)

        return "\n".join(output) if output else "Working tree clean - no changes"
