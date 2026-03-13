# Safe Shell: Secure Command Execution for Agents

This document describes the SafeShell module that provides sandboxed command execution with pipe support for AI agents working on Lean proof projects.

## Overview

When agents need to explore codebases, search for patterns, or run build commands, they need shell access. However, unrestricted shell access is dangerous. SafeShell provides a middle ground:

- **Pipes supported**: `grep pattern file | sort | uniq -c`
- **Allowlist enforced**: Only approved commands can run
- **Role-based git access**: Different agents get different git permissions
- **Path validation**: Commands can't escape the repository
- **No dangerous constructs**: `;`, `&&`, `||`, `$()`, redirects are blocked

### Security Model

When an agent submits a command, it goes through four validation stages:

1. **Forbidden Pattern Check**
   - Blocks: `;`, `&&`, `||`, `$()`, backticks, `>`, `>>`, `<`, `${VAR}`, `$VAR`
   - If any forbidden pattern is found, the command is rejected immediately

2. **Pipeline Splitting**
   - Splits command on `|` characters (pipes)
   - Correctly handles `|` inside quoted strings (e.g., `'theorem|lemma'` is not split)
   - Uses `shlex` tokenizer for proper quote parsing

3. **Per-Segment Validation**
   - For each pipeline segment:
     - Is the command in `ALLOWED_COMMANDS`?
     - If `git`: is the subcommand allowed for this role?
     - If `xargs`: is the target command allowed?
     - Do all path arguments resolve within the repo?

4. **Execution**
   - Sanitized environment (only allowed env vars)
   - Timeout enforced
   - Output size limited

---

## Quick Start

### Basic Usage

```python
from repoprover.safe_shell import SafeShell, SafeShellConfig, AgentRole

# Create a shell for a worker agent
config = SafeShellConfig(
    repo_root=Path("/path/to/lean-project"),
    role=AgentRole.WORKER,
    timeout_seconds=120,
)
shell = SafeShell(config)

# Run commands
result = shell.run("grep -r 'sorry' .")
if result.success:
    print(result.stdout)
else:
    print(f"Error: {result.error or result.stderr}")
```

### With an Agent Class

```python
from repoprover.safe_shell import SafeShell, SafeShellConfig, SafeShellToolsMixin

class MyProverAgent(SafeShellToolsMixin, BaseAgent):
    def __init__(self, repo_root: Path, **kwargs):
        config = SafeShellConfig(repo_root=repo_root, role=AgentRole.WORKER)
        self.safe_shell = SafeShell(config)
        super().__init__(**kwargs)

    def handle_tool_call(self, name: str, args: dict) -> str:
        # Try shell tool first
        result = self.handle_shell_tool(name, args)
        if result is not None:
            return result
        # Fall back to other tools
        return super().handle_tool_call(name, args)
```

---

## Configuration

### SafeShellConfig

```python
@dataclass
class SafeShellConfig:
    repo_root: Path              # Commands run here, paths validated against this
    role: AgentRole = WORKER     # Determines git command access
    timeout_seconds: int = 120   # Command timeout
    max_output_bytes: int = 2MB  # Output truncation limit
    allowed_env_vars: list[str]  # Environment variables to preserve
```

### Agent Roles

| Role | Description | Git Access |
|------|-------------|------------|
| `READER` | Read-only exploration | `status`, `log`, `show`, `diff`, `branch`, `ls-files`, `blame`, etc. |
| `WORKER` | Can modify files and commit | READER + `add`, `commit`, `checkout`, `reset`, `stash`, `rebase`, etc. |
| `MERGER` | Full git access (main agent) | WORKER + `merge` |

---

## Allowed Commands

### File Viewing
- `cat`, `head`, `tail`, `less`, `wc`, `ls`, `tree`, `file`

### Searching
- `grep`, `rg` (ripgrep), `find`

### Text Processing
- `sort`, `uniq`, `cut`, `awk`, `sed`, `tr`, `tee`, `xargs`

### Diffing
- `diff`, `comm`

### Build Tools
- `lake` (Lean build system)

### Utility
- `echo`, `printf`, `date`, `basename`, `dirname`, `realpath`, `true`, `false`, `sleep`

### Git
- Subcommands controlled by role (see above)

---

## Forbidden Commands

These commands are **always blocked**, regardless of role:

| Category | Commands |
|----------|----------|
| Destructive | `rm`, `rmdir`, `mv`, `cp` |
| Permissions | `chmod`, `chown`, `chgrp` |
| Privilege escalation | `sudo`, `su`, `doas` |
| Network | `curl`, `wget`, `nc`, `ssh`, `scp` |
| Process control | `kill`, `pkill`, `killall` |
| Shell execution | `eval`, `exec`, `source` |
| Interpreters | `python`, `python3`, `node`, `ruby`, `perl` |

---

## Forbidden Shell Constructs

These shell features are **blocked** to prevent injection:

| Construct | Example | Why Blocked |
|-----------|---------|-------------|
| Semicolon | `cmd1; cmd2` | Command chaining |
| AND | `cmd1 && cmd2` | Conditional execution |
| OR | `cmd1 \|\| cmd2` | Conditional execution |
| Background | `cmd &` | Process spawning |
| Command substitution | `$(cmd)` or `` `cmd` `` | Arbitrary execution |
| Redirects | `> file`, `>> file` | File writes (use `tee` instead) |
| Input redirect | `< file` | File reads (use `cat` instead) |
| Variable expansion | `$VAR`, `${VAR}` | Environment leakage |

### Pipes ARE Allowed

Pipes (`|`) are the one shell feature that IS supported:

```python
# All of these work:
shell.run("grep pattern file | wc -l")
shell.run("find . -name '*.lean' | xargs grep sorry")
shell.run("cat data | sort | uniq -c | sort -rn | head -10")
```

The pipe character inside quotes is handled correctly:

```python
# This works - the | in the regex is not treated as a pipe:
shell.run("grep -E '^(theorem|lemma)' file.lean | wc -l")
```

---

## Path Validation

All path arguments are validated to prevent escaping the repository:

```python
# ✓ Allowed
shell.run("cat src/Chapter1.lean")
shell.run("grep sorry subdir/../file.lean")  # Resolves within repo

# ✗ Blocked
shell.run("cat /etc/passwd")           # Absolute path outside repo
shell.run("cat ../../../etc/passwd")   # Relative escape
```

---

## Special Command Handling

### sed -i (In-place Edit)

In-place sed editing is blocked because agents should use file tools for writes:

```python
# ✗ Blocked
shell.run("sed -i 's/sorry/trivial/' file.lean")
# Error: sed -i (in-place edit) not allowed; use file_edit tool instead

# ✓ Use this instead
shell.run("sed 's/sorry/trivial/' file.lean")  # Outputs to stdout
```

### xargs

`xargs` is allowed but the command it runs is validated:

```python
# ✓ Allowed - grep is in allowlist
shell.run("find . -name '*.lean' | xargs grep sorry")

# ✗ Blocked - rm is forbidden
shell.run("find . -name '*.bak' | xargs rm")
# Error: xargs cannot run: rm
```

### Git Subcommands

Git access is role-based:

```python
# READER role:
shell.run("git status")      # ✓
shell.run("git log")         # ✓
shell.run("git add file")    # ✗ "requires higher permissions"

# WORKER role:
shell.run("git add file")    # ✓
shell.run("git commit -m x") # ✓
shell.run("git merge br")    # ✗ "requires higher permissions"

# MERGER role:
shell.run("git merge br")    # ✓

# Always blocked (all roles):
shell.run("git push")        # ✗ "not allowed"
shell.run("git pull")        # ✗ "not allowed"
shell.run("git clone")       # ✗ "not allowed"
```

---

## Output Handling

### ShellResult

```python
@dataclass
class ShellResult:
    success: bool       # True if return_code == 0
    stdout: str         # Command output
    stderr: str         # Error output
    return_code: int    # Exit code (-1 if validation failed)
    error: str          # Pre-execution error (validation failure)
```

### Formatting for Agents

```python
result = shell.run("grep pattern file")
agent_output = result.format_for_agent()

# Success: returns stdout
# Failure with stderr: returns "stdout\nstderr:\nstderr_content\n(exit code: N)"
# Validation error: returns "Error: <message>"
# No output: returns "(no output)"
```

### Output Truncation

Large outputs are automatically truncated:

```python
config = SafeShellConfig(
    repo_root=repo,
    max_output_bytes=10000,  # 10KB limit
)
shell = SafeShell(config)

result = shell.run("cat huge_file.txt")
# Output truncated to 10KB with "... (truncated)" appended
```

---

## Timeout Handling

Commands that exceed the timeout are killed:

```python
config = SafeShellConfig(
    repo_root=repo,
    timeout_seconds=30,
)
shell = SafeShell(config)

result = shell.run("lake build")  # Takes too long
# result.success = False
# result.error = "Command timed out after 30s"
```

---

## Tool Definition

The module provides a ready-to-use tool definition for agents:

```python
from repoprover.safe_shell import SHELL_TOOL

# SHELL_TOOL is a dict with:
# - name: "shell"
# - description: Detailed usage instructions
# - parameters: {"command": {"type": "string", ...}}

# Add to your agent's tools:
tools = [SHELL_TOOL, ...]
```

---

## Examples

### Find Sorry Statements

```python
result = shell.run("grep -rn 'sorry' --include='*.lean' .")
```

### Count Theorems

```python
result = shell.run("grep -E '^(theorem|lemma)' src/*.lean | wc -l")
```

### List Lean Files by Size

```python
result = shell.run("find . -name '*.lean' | xargs wc -l | sort -rn | head -10")
```

### Check Build Status

```python
result = shell.run("lake build 2>&1 | tail -20")
```

### Git Workflow

```python
shell.run("git status")
shell.run("git diff src/Chapter1.lean")
shell.run("git add src/Chapter1.lean")
shell.run("git commit -m 'Prove theorem foo'")
shell.run("git log --oneline -5")
```

---

## Error Messages

| Error | Cause |
|-------|-------|
| `Command not allowed: <cmd>` | Command not in allowlist |
| `Semicolons and background execution not allowed` | Found `;` or `&` |
| `Conditional execution (&&, \|\|) not allowed` | Found `&&` or `\|\|` |
| `Command substitution not allowed` | Found `$()` or backticks |
| `File redirects not allowed` | Found `>` or `>>` |
| `Variable expansion not allowed` | Found `$VAR` or `${VAR}` |
| `Path escapes repository: <path>` | Path resolves outside repo |
| `git <subcmd> is not allowed` | Git subcommand forbidden |
| `git <subcmd> requires higher permissions` | Role insufficient |
| `sed -i (in-place edit) not allowed` | Use file tools instead |
| `xargs cannot run: <cmd>` | xargs target not allowed |
| `Command timed out after Ns` | Exceeded timeout |

---

## Testing

Run the test suite:

```bash
micromamba run -n fort pytest repoprover/tests/test_safe_shell.py -v
```

Tests cover:
- Basic command execution
- Pipe support with complex patterns
- Forbidden command blocking
- Shell construct blocking
- Role-based git access
- Path escape prevention
- xargs validation
- Output truncation
- Timeout handling
- Pipeline splitting with quotes (regex alternation patterns)

---

## Integration with Git Worktree Tools

SafeShell complements the [Git Worktree Tools](./git-worktree-tools.md) for full agent isolation:

- **Git Worktree Tools**: High-level git operations with branch isolation
- **SafeShell**: Low-level shell access for exploration and builds

Typical setup:

```python
class ProverAgent(SafeShellToolsMixin, GitWorktreeToolsMixin, BaseAgent):
    def __init__(self, worktree_manager: WorktreeManager):
        # SafeShell for exploration
        config = SafeShellConfig(
            repo_root=worktree_manager.worktree_path,
            role=AgentRole.WORKER,
        )
        self.safe_shell = SafeShell(config)

        # Git tools for commits
        self.worktree_manager = worktree_manager
```
