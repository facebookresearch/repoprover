# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Safe shell command execution with pipe and conditional support.

Security model:
1. Tokenize command using shlex (properly handles quoting)
2. Split on operators: |, &&, || (distinguishing operators from quoted args)
3. Validate each segment against allowlist
4. Check for forbidden patterns: ;, &, $(), ``, redirects, variable expansion
5. Validate paths don't escape repo
6. Execute with restricted environment

Key insight: shlex tokenization naturally distinguishes:
- `cmd1 && cmd2` -> ['cmd1', '&', '&', 'cmd2'] (operator - two tokens)
- `cmd '&&' arg` -> ['cmd', '&&', 'arg'] (quoted string - single token)

This allows safe support for && and || without regex hacks.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class AgentRole(StrEnum):
    """Role determines which commands an agent can run."""

    READER = "reader"  # Read-only commands (grep, cat, etc.)
    WORKER = "worker"  # + file writes, git add/commit/branch
    MERGER = "merger"  # + git merge (only main agent has this)


# === Command Allowlist ===

ALLOWED_COMMANDS: set[str] = {
    # File viewing
    "cat",
    # Time/sleep (needed for timeout testing and scripting)
    "sleep",
    "head",
    "tail",
    "less",
    "wc",
    "ls",
    "tree",
    "file",
    # Searching
    "grep",
    "rg",
    "find",
    # Text processing (safe for pipes)
    "sort",
    "uniq",
    "cut",
    "awk",
    "sed",
    "tr",
    "tee",
    "xargs",  # Controlled - see validator
    # Diffing
    "diff",
    "comm",
    # Git (subcommands validated separately)
    "git",
    # Lean/Lake
    "lake",
    # Utility
    "echo",
    "printf",
    "date",
    "basename",
    "dirname",
    "realpath",
    "true",
    "false",
}

# Commands forbidden even in pipes
FORBIDDEN_COMMANDS: set[str] = {
    "rm",
    "rmdir",
    "mv",
    "cp",  # Destructive
    "chmod",
    "chown",
    "chgrp",  # Permissions
    "sudo",
    "su",
    "doas",  # Privilege escalation
    "curl",
    "wget",
    "nc",
    "ssh",
    "scp",  # Network
    "kill",
    "pkill",
    "killall",  # Process control
    "eval",
    "exec",
    "source",  # Shell execution
    "python",
    "python3",
    "node",
    "ruby",
    "perl",  # Interpreters
}

# Safe paths that are allowed even outside repo
ALLOWED_PATHS: set[str] = {
    "/dev/null",
    "/dev/stdin",
    "/dev/stdout",
    "/dev/stderr",
}
GIT_SUBCOMMANDS_BY_ROLE: dict[AgentRole, set[str]] = {
    AgentRole.READER: {
        "status",
        "log",
        "show",
        "diff",
        "branch",
        "rev-parse",
        "rev-list",
        "ls-tree",
        "ls-files",
        "cat-file",
        "blame",
        "describe",
        "shortlog",
        "reflog",
        "name-rev",
        "symbolic-ref",
    },
    AgentRole.WORKER: {
        # Additional commands for WORKER (on top of READER)
        "add",
        "commit",
        "checkout",
        "switch",
        "restore",
        "reset",
        "stash",
        "rebase",
        "cherry-pick",
        "tag",
    },
    AgentRole.MERGER: {
        # Additional commands for MERGER (on top of WORKER)
        "merge",
    },
}

GIT_FORBIDDEN_SUBCOMMANDS: set[str] = {
    "push",
    "pull",
    "fetch",
    "remote",
    "clone",
    "config",
    "gc",
    "prune",
    "fsck",
    "submodule",
    "filter-branch",
    "filter-repo",
    "worktree",  # Managed by WorktreePool, not agents
    "clean",  # Destructive
}


@dataclass
class SafeShellConfig:
    """Configuration for the safe shell."""

    repo_root: Path
    role: AgentRole = AgentRole.WORKER
    timeout_seconds: int = 120
    lake_timeout_seconds: int = 300
    max_output_bytes: int = 2_000_000  # 2MB
    allowed_env_vars: list[str] = field(
        default_factory=lambda: [
            "PATH",
            "HOME",
            "USER",
            "LANG",
            "LC_ALL",
            "TERM",
            "MATHLIB_CACHE_DIR",
            "ELAN_HOME",
            "LAKE_HOME",
        ]
    )


@dataclass
class ShellResult:
    """Result of a shell command execution."""

    success: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int = -1
    error: str = ""  # Pre-execution error (validation failure, etc.)

    def format_for_agent(self) -> str:
        """Format result for agent consumption."""
        if self.error:
            return f"Error: {self.error}"

        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(f"stderr:\n{self.stderr}")
        if self.return_code != 0:
            parts.append(f"(exit code: {self.return_code})")

        return "\n".join(parts) if parts else "(no output)"


class SafeShell:
    """Sandboxed command executor with pipe support.

    Unlike simple subprocess.run() with a list, this DOES use shell=True
    to support pipes, but validates the entire pipeline against an
    allowlist before execution.

    Security properties:
    - No arbitrary command execution (allowlist only)
    - No shell metacharacters except | for pipes
    - No path traversal outside repo
    - No network access (no curl, wget, git push/pull)
    - Resource bounded (timeout, output size)
    - Environment sanitized
    """

    def __init__(self, config: SafeShellConfig):
        self.config = config
        self._allowed_git_subcommands = self._build_allowed_git_subcommands()

    def _build_allowed_git_subcommands(self) -> set[str]:
        """Build set of allowed git subcommands based on role."""
        allowed = set()
        for role in AgentRole:
            allowed |= GIT_SUBCOMMANDS_BY_ROLE.get(role, set())
            if role == self.config.role:
                break
        return allowed

    def run(self, command: str) -> ShellResult:
        """Run a command string safely.

        Supports pipes: "grep pattern file | sort | uniq -c"
        Does NOT support: ;, &&, ||, $(), ``, redirects to files
        """
        # Validate entire command
        validation_error = self._validate_command(command)
        if validation_error:
            return ShellResult(success=False, error=validation_error)

        # Execute with restricted environment
        return self._execute(command)

    def _validate_command(self, command: str) -> str | None:
        """Validate a command string.

        Returns error message or None if valid.
        """
        # Tokenize and split on operators using shlex
        segments, error = self._tokenize_and_split(command)
        if error:
            return error

        if not segments:
            return "Empty command"

        # Validate each segment
        for segment_tokens in segments:
            err = self._validate_segment_tokens(segment_tokens)
            if err:
                return err

        return None

    def _mask_quoted_content(self, command: str) -> str:
        """Replace redirect chars inside quotes with safe placeholders.

        This allows redirect patterns to be detected via regex without
        false positives from quoted content like grep "<div>".
        """
        result = []
        i = 0
        in_single = False
        in_double = False
        while i < len(command):
            c = command[i]
            # Handle escapes in double quotes
            if c == "\\" and in_double and i + 1 < len(command):
                result.append(c + command[i + 1])
                i += 2
                continue
            if c == "'" and not in_double:
                in_single = not in_single
                result.append(c)
            elif c == '"' and not in_single:
                in_double = not in_double
                result.append(c)
            elif (in_single or in_double) and c in "<>":
                result.append("_")  # Replace redirect chars inside quotes
            else:
                result.append(c)
            i += 1
        return "".join(result)

    def _tokenize_and_split(self, command: str) -> tuple[list[list[str]], str | None]:
        """Tokenize command using shlex and split into segments on operators.

        Returns (segments, error) where:
        - segments is a list of token lists, one per pipeline/conditional segment
        - error is None on success, or an error message

        Handles |, &&, || as operators. Single & and ; are forbidden.

        Key insight: shlex properly handles quoting, so:
        - `cmd1 && cmd2` tokenizes to ['cmd1', '&', '&', 'cmd2'] (operator)
        - `cmd '&&' arg` tokenizes to ['cmd', '&&', 'arg'] (string argument)

        This lets us distinguish operators from quoted arguments naturally.
        """
        # Patterns that must be checked everywhere (including inside double quotes)
        # because shell expands them even in double quotes
        always_forbidden = [
            # Command substitution - dangerous even in double-quoted strings with shell=True
            (r"\$\(", "Command substitution $() not allowed"),
            (r"`", "Command substitution (backticks) not allowed"),
            # Variable expansion - would be expanded by shell in double quotes
            (r"\$\{", "Variable expansion ${} not allowed"),
            (r"\$[A-Za-z_]", "Variable expansion $VAR not allowed"),
        ]

        for pattern, msg in always_forbidden:
            if re.search(pattern, command):
                return [], msg

        # For redirect detection, mask quoted content so we don't match
        # redirects inside quoted strings like grep "<div>" or awk '$1 < 5'
        masked = self._mask_quoted_content(command)

        # Build pattern for allowed redirect targets
        allowed_redirect_targets = "|".join(re.escape(p) for p in ALLOWED_PATHS)

        redirect_patterns = [
            # Input redirects: single < (but not << or <()
            (r"(?<!<)<(?!<)(?!\s*\()", "Input redirects not allowed"),
            # Heredoc: << or <<< (but not inside longer sequences like <<<<<<)
            (r"(?<!<)<<(?!<)", "Heredoc/here-string not allowed"),
            # Output redirects to files (not fd redirects or allowed paths like /dev/null)
            (
                rf"(?<!\d)(?<!&)>(?!>)(?!\s*&)(?!\s*({allowed_redirect_targets}))\s*[^|>\s&]",
                "File redirects not allowed (use tee instead)",
            ),
            (
                rf"(?<!&)>>(?!\s*({allowed_redirect_targets}))\s*[^|>\s]",
                "File redirects not allowed (use tee instead)",
            ),
            (r"&>", "Dual redirect &> not allowed (use tee instead)"),
        ]

        for pattern, msg in redirect_patterns:
            if re.search(pattern, masked):
                return [], msg

        # Tokenize with shlex
        try:
            lexer = shlex.shlex(command, posix=True)
            lexer.wordchars += "-=_./:@~"  # Common shell word characters
            tokens = list(lexer)
        except ValueError as e:
            return [], f"Failed to parse command: {e}"

        # Split on operators, properly detecting && and || as two separate tokens
        # vs single-token '&&' (which is a quoted string argument)
        segments: list[list[str]] = []
        current: list[str] = []
        i = 0

        while i < len(tokens):
            token = tokens[i]

            # Check for double operators: && or ||
            # These appear as TWO consecutive single-char tokens when unquoted
            if token in ("&", "|") and i + 1 < len(tokens) and tokens[i + 1] == token:
                # This is && or || operator
                if current:
                    segments.append(current)
                    current = []
                i += 2
                continue

            # Check for single pipe (allowed as pipe operator)
            if token == "|":
                if current:
                    segments.append(current)
                    current = []
                i += 1
                continue

            # Check for single & - could be background or part of fd redirect like 2>&1
            if token == "&":
                # Check if this is part of a fd redirect pattern (e.g., 2>&1, >&2)
                # Previous token should end with > and next token should be a digit or -
                prev_token = current[-1] if current else ""
                next_token = tokens[i + 1] if i + 1 < len(tokens) else ""

                # Pattern: ...> & 1 or ...> & - (fd redirect with spaces)
                if prev_token.endswith(">") and (next_token.isdigit() or next_token == "-"):
                    # This is part of a fd redirect, combine with surrounding tokens
                    current[-1] = prev_token + "&" + next_token
                    i += 2
                    continue
                else:
                    return [], "Background execution (&) not allowed"

            # Check for semicolon (forbidden - command separator)
            if token == ";":
                return [], "Semicolons not allowed"

            # Regular token (including quoted '&&' which is a string argument)
            current.append(token)
            i += 1

        if current:
            segments.append(current)

        return segments, None

    def _validate_segment_tokens(self, tokens: list[str]) -> str | None:
        """Validate a single command segment (list of tokens)."""
        if not tokens:
            return "Empty command segment"

        cmd_name = tokens[0]
        cmd_args = tokens[1:]

        # Check if command is forbidden
        if cmd_name in FORBIDDEN_COMMANDS:
            return f"Command not allowed: {cmd_name}"

        # Check if command is allowed
        if cmd_name not in ALLOWED_COMMANDS:
            return f"Command not allowed: {cmd_name}"

        # Special handling for git
        if cmd_name == "git":
            return self._validate_git_segment(cmd_args)

        # Special handling for xargs (limit what it can run)
        if cmd_name == "xargs":
            return self._validate_xargs(cmd_args)

        # Special handling for sed -i (in-place edit not allowed)
        if cmd_name == "sed" and "-i" in cmd_args:
            return "sed -i (in-place edit) not allowed; use file_edit tool instead"

        # Validate paths don't escape repo
        for arg in cmd_args:
            if arg.startswith("-"):
                continue
            err = self._validate_path_arg(arg)
            if err:
                return err

        return None

    def _validate_git_segment(self, args: list[str]) -> str | None:
        """Validate a git command."""
        if not args:
            return "git requires a subcommand"

        subcommand = args[0]

        if subcommand in GIT_FORBIDDEN_SUBCOMMANDS:
            return f"git {subcommand} is not allowed"

        if subcommand not in self._allowed_git_subcommands:
            # Check if it exists at all in any role
            all_subcommands = set()
            for cmds in GIT_SUBCOMMANDS_BY_ROLE.values():
                all_subcommands |= cmds

            if subcommand in all_subcommands:
                return f"git {subcommand} requires higher permissions (current role: {self.config.role})"
            else:
                return f"git {subcommand} is not allowed"

        return None

    def _validate_xargs(self, args: list[str]) -> str | None:
        """Validate xargs usage - only allow safe commands."""
        # Find the command xargs will run
        xargs_cmd = None
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in ("-I", "-i", "-L", "-n", "-P", "-d"):
                skip_next = True
                continue
            if arg.startswith("-"):
                continue
            xargs_cmd = arg
            break

        if xargs_cmd:
            if xargs_cmd in FORBIDDEN_COMMANDS:
                return f"xargs cannot run: {xargs_cmd}"

            if xargs_cmd not in ALLOWED_COMMANDS:
                return f"xargs cannot run: {xargs_cmd}"

        return None

    def _validate_path_arg(self, arg: str) -> str | None:
        """Validate a potential path argument."""
        # Skip if it doesn't look like a path
        if "/" not in arg and arg not in (".", ".."):
            return None

        # Allow explicitly safe paths (even outside repo)
        if arg in ALLOWED_PATHS:
            return None

        # Also allow redirect suffixes to safe paths
        for safe_path in ALLOWED_PATHS:
            if arg.endswith(f">{safe_path}") or arg.endswith(f">>{safe_path}"):
                return None

        # Validate that the path itself (without following symlinks) stays within repo.
        # We allow symlinks that point outside the repo, as long as the symlink
        # is placed within the repo (e.g., .lake/packages/mathlib -> external cache).
        try:
            if Path(arg).is_absolute():
                # For absolute paths, resolve without following symlinks
                # by using strict=False and checking parent resolution
                path = Path(arg)
            else:
                path = self.config.repo_root / arg

            # Resolve the path without following the final symlink
            # We resolve the parent directory and append the final component
            # This ensures we catch ".." traversals but allow symlinks
            if path.exists() or path.is_symlink():
                # For existing paths, resolve parent to normalize ".." but keep symlinks
                resolved = path.parent.resolve() / path.name
            else:
                # For non-existing paths, resolve what we can
                resolved = path.resolve()

            repo_resolved = self.config.repo_root.resolve()

            # Check it's within repo
            try:
                resolved.relative_to(repo_resolved)
            except ValueError:
                return f"Path escapes repository: {arg}"

        except (ValueError, OSError):
            pass  # May not be a path, let the command handle it

        return None

    def _execute(self, command: str) -> ShellResult:
        """Execute a validated command."""
        # Use longer timeout for lake commands (build, env, etc.)
        first_cmd = command.split()[0] if command.split() else ""
        timeout = (
            self.config.lake_timeout_seconds
            if first_cmd == "lake"
            else self.config.timeout_seconds
        )

        # Build restricted environment
        env = {k: v for k, v in os.environ.items() if k in self.config.allowed_env_vars}
        env["HOME"] = os.environ.get("HOME", "/tmp")
        env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")

        try:
            result = subprocess.run(
                command,
                shell=True,  # Required for pipes
                cwd=self.config.repo_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            stdout = result.stdout
            stderr = result.stderr

            # Truncate if too large
            if len(stdout) > self.config.max_output_bytes:
                stdout = stdout[: self.config.max_output_bytes] + "\n... (truncated)"
            if len(stderr) > self.config.max_output_bytes:
                stderr = stderr[: self.config.max_output_bytes] + "\n... (truncated)"

            return ShellResult(
                success=(result.returncode == 0),
                stdout=stdout,
                stderr=stderr,
                return_code=result.returncode,
            )

        except subprocess.TimeoutExpired:
            return ShellResult(
                success=False,
                error=f"Command timed out after {timeout}s",
            )
        except OSError as e:
            return ShellResult(
                success=False,
                error=f"Failed to execute: {e}",
            )


# === Tool Definition for Agents ===

SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": """Execute a shell command safely.

Supports:
- Pipes: "grep pattern file | sort | uniq -c"
- Conditionals: "lake build && git status" or "cmd1 || cmd2"

Allowed commands:
- File viewing: cat, head, tail, less, wc, ls, tree, file
- Searching: grep, rg, find
- Text processing: sort, uniq, cut, awk, sed, tr, tee, xargs
- Diffing: diff, comm
- Git (read): status, log, show, diff, branch, rev-parse, ls-tree, ls-files, blame
- Git (write): add, commit, checkout, switch, restore, reset, stash, rebase
- Lean: lake build, lake check

NOT allowed:
- Git push/pull/fetch/remote/clone
- File modification commands (rm, mv, cp) - use file_* tools instead
- sed -i (use file_edit instead)
- Shell features: ;, background (&), $(), ``, redirects, variable expansion
- Network commands: curl, wget, ssh

Examples:
  shell(command="git status")
  shell(command="grep -r 'sorry' MyBook/ | wc -l")
  shell(command="git log --oneline -10")
  shell(command="lake build && git status")
  shell(command="lake build MyBook.Chapter1 || echo 'Build failed'")
  shell(command="find . -name '*.lean' | xargs grep 'theorem'")
""",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to execute (pipes allowed)",
                },
            },
            "required": ["command"],
        },
    },
}


class SafeShellToolsMixin:
    """Mixin providing shell tool to agents.

    Requires self.safe_shell: SafeShell to be set.
    """

    safe_shell: SafeShell

    def get_shell_tools(self) -> list[dict]:
        """Return shell tool definitions."""
        return [SHELL_TOOL]

    def handle_shell_tool(self, name: str, args: dict) -> str | None:
        """Handle a shell tool call. Returns None if not a shell tool."""
        if name != "shell":
            return None

        command = args.get("command", "")
        if not command:
            return "Error: command is required"

        result = self.safe_shell.run(command)
        return result.format_for_agent()
