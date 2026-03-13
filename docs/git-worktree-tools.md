# Git Worktree Tools for Multi-Agent Execution

This document describes the git worktree tool suite that enables multiple agents to work in parallel on isolated branches with safe, restricted git operations.

## Overview

When running multiple agents in parallel (e.g., proving different theorems simultaneously), each agent needs its own isolated workspace. Git worktrees provide this isolation while sharing expensive resources like Mathlib (~6.5GB).

The implementation follows the symlink strategy from [shared-mathlib-worktrees.md](./shared-mathlib-worktrees.md).

### Architecture

**Directory Structure:**

- **base-project/** — The fully-built Lean project
  - `.lake/`
    - `packages/` — 6.5G Mathlib (shared read-only)
    - `config/` — Lake config (shared read-only)
    - `build/` — Base project build artifacts
  - `FormalBook/`
    - `Chapter1.lean`

- **worktrees/** — Directory containing agent worktrees
  - **worktree-prover-ch1/** — Agent 1's isolated worktree
    - `.lake/`
      - `packages` → `base/.lake/packages` (symlink)
      - `config` → `base/.lake/config` (symlink)
      - `build/` — Agent 1's own build (~150M)
    - `FormalBook/`
      - `Chapter1.lean` — Agent 1's working copy
  - **worktree-prover-ch2/** — Agent 2's isolated worktree
    - `.lake/`
      - `packages` → `base/.lake/packages` (symlink)
      - `config` → `base/.lake/config` (symlink)
      - `build/` — Agent 2's own build (~150M)
    - `FormalBook/`
      - `Chapter2.lean` — Agent 2's working copy

### Two Agent Roles

| Role | Branch Access | Capabilities |
|------|---------------|--------------|
| **Feature Worker** | Own branch only (`agent-{id}`) | `git_status`, `git_add`, `git_commit`, `git_diff`, `git_log`, `git_unstage`, `git_restore`, `git_checkout_file`, `git_rebase`, `git_rebase_continue`, `git_rebase_abort`, `git_rebase_skip`, `git_conflicts` |
| **Main Agent** | Main + all agent branches | All above + `git_merge`, `git_checkout`, `git_branch_delete`, `git_branch_list`, `git_reset`, `git_show`, `git_diff_branches` |

---

## Worktree Lifecycle

### Ownership Model

**The Coordinator owns all worktree management.** Workers do not create or clean up worktrees.

```
COORDINATOR                                    WORKER
    |                                             |
    |-- setup(agent_id) ----------------------->  |
    |                                             |
    |-- dispatch task with worktree_path ------> |
    |                                             |-- use worktree_path
    |                                             |-- do work
    |                                             |-- return result
    |<-- result --------------------------------- |
    |                                             |
    |-- setup(agent_id) for review               |
    |   (idempotent - reuses existing)           |
    |                                             |
    |-- review                                    |
    |                                             |
    |-- if terminal: cleanup(agent_id)           |
```

### Who calls what?

| Action | Who | Method |
|--------|-----|--------|
| Create worktree before dispatch | Coordinator | `setup(agent_id)` |
| Create worktree for local run | Coordinator | `setup(agent_id)` |
| Get worktree for review | Coordinator | `setup(agent_id)` (idempotent) |
| Use worktree | Worker | Just uses `worktree_path` from task |
| Delete on terminal state | Coordinator | `cleanup(agent_id)` |

**Terminal states** (when `cleanup()` is called):
- Merged
- Failed (max revisions, blocked, error, rejected)

### API

```python
class WorktreePool:
    def setup(self, agent_id: str) -> WorktreeManager:
        """Set up a worktree for an agent (idempotent).

        Creates a new worktree if it doesn't exist, or returns the existing one.
        Safe to call multiple times - will reuse existing worktree.

        Returns:
            WorktreeManager for the agent's worktree
        """

    def cleanup(self, agent_id: str) -> None:
        """Clean up a worktree (delete directory, preserve branch).

        Call this ONLY on terminal states: merged, failed, rejected, blocked.
        Safe to call multiple times or if worktree doesn't exist.
        """
```

### Resumption Safety

`setup()` is **idempotent** and handles all resumption scenarios:

| Scenario | What happens |
|----------|--------------|
| Coordinator restarts, PR pending review | `setup()` reuses existing worktree ✓ |
| Coordinator restarts, agent was mid-work | `setup()` reuses existing worktree ✓ |
| Worktree was deleted manually | `setup()` recreates from branch ✓ |
| Partial worktree (no .lake symlinks) | `setup()` detects via `is_setup()`, cleans up and recreates ✓ |

---

## Quick Start

### Basic Usage (Coordinator)

```python
from repoprover.git_worktree import WorktreePool

# Create pool pointing to your Lean project
pool = WorktreePool(
    base_project=Path("/path/to/leanenv"),
    worktrees_root=Path("/path/to/worktrees"),
)

# Set up a worktree for an agent (idempotent)
manager = pool.setup("prover-chapter1")

# Agent can now work in manager.worktree_path
# with its own branch: prover-chapter1

# When agent reaches terminal state, clean up
pool.cleanup("prover-chapter1")
```

### Distributed Mode

In distributed mode, the coordinator creates the worktree before dispatching:

```python
# Coordinator (before dispatch)
worktree = pool.setup(agent_id)
task = DistributedTask(
    worktree_path=str(worktree.worktree_path),
    branch_name=worktree.branch_name,
    ...
)
zmq_server.put(task.to_dict())

# Worker (no WorktreePool needed)
def execute(task: DistributedTask):
    worktree_path = Path(task.worktree_path)
    # Just use the path directly - coordinator created it
    agent = ContributorAgent(repo_root=worktree_path, ...)
    return agent.run_task()
```

### With an Agent Class

```python
class MyProverAgent(GitWorktreeToolsMixin, BaseAgent):
    def __init__(self, worktree_manager: WorktreeManager, **kwargs):
        self.worktree_manager = worktree_manager
        super().__init__(**kwargs)

    def handle_tool_call(self, name: str, args: dict) -> str:
        # Try git tools first
        result = self.handle_git_worktree_tool(name, args)
        if result is not None:
            return result
        # Fall back to other tools
        return super().handle_tool_call(name, args)
```

---

## Core Components

### WorktreeConfig

Configuration for a single worktree:

```python
@dataclass
class WorktreeConfig:
    base_project: Path      # Fully-built Lean project with .lake/packages
    worktrees_root: Path    # Directory where worktrees are created
    agent_id: str           # Unique agent identifier (also used as branch name)
```

### WorktreeManager

Manages a single worktree's lifecycle:

```python
manager = WorktreeManager(config)

# Create worktree with proper symlinks
success, msg = manager.setup()

# Properties
manager.worktree_path  # Path: /worktrees/worktree-{agent_id}
manager.branch_name    # str: {agent_id}

# Validate paths (security - blocks escape attempts)
ok, msg = manager.validate_path("FormalBook/Chapter1.lean")  # True
ok, msg = manager.validate_path("../other/file.lean")        # False
ok, msg = manager.validate_path(".lake/packages/mathlib")    # False (shared)

# Check if properly configured
manager.is_setup()  # bool

# Clean up when done
success, msg = manager.cleanup()
```

### WorktreePool

Manages multiple worktrees. **Only the coordinator should use this.**

```python
pool = WorktreePool(base_project, worktrees_root)

# Set up worktree (idempotent - creates if needed, reuses if exists)
manager = pool.setup("agent-id")
manager = pool.setup("agent-id")  # Same worktree (idempotent)

# Clean up on terminal state only
pool.cleanup("agent-id")
```

### Thread Safety & Concurrency

The `WorktreePool` uses per-agent locks for thread safety within a process:

```python
# Thread A and B both call setup("maintain-123") simultaneously:
# - Thread A acquires the per-agent lock, creates the worktree
# - Thread B waits on the lock, then gets the already-created worktree
#
# Thread C calls setup("maintain-456") simultaneously:
# - Uses a different lock, can proceed in parallel with A
```

**Note:** Worktree state is managed via filesystem, not in-memory tracking.
This allows worktrees to be shared across processes (coordinator creates,
worker uses, coordinator reviews).

### Startup Cleanup

On pool initialization, `WorktreePool` performs aggressive cleanup to ensure a clean state:

```python
def _cleanup_locked_worktrees(self):
    # 1. Remove all .git/worktrees/*/locked files
    # 2. Delete entire worktrees_root directory
    # 3. Run git worktree prune
```

**Why this is safe**: Worktrees are throwaway working copies. All important work is committed to **branches**, which are preserved. On resume:
- Branches contain all committed work (safe)
- Worktrees are recreated on-demand via `setup()`
- No manual cleanup needed before restart

---

## Feature Worker Tools

These tools are available to feature worker agents via `GitWorktreeToolsMixin`:

### git_status

Check the status of the worktree.

```python
result = agent.handle_git_worktree_tool("git_status", {})
```

Output:
```
Changes staged for commit:
  M  FormalBook/Chapter1.lean

Changes not staged:
  M  FormalBook/Chapter2.lean

Untracked files:
  FormalBook/Scratch.lean
```

### git_add

Stage files for commit. Paths are validated to prevent escape.

```python
result = agent.handle_git_worktree_tool("git_add", {
    "paths": ["FormalBook/Chapter1.lean", "FormalBook/Chapter2.lean"]
})
```

### git_commit

Commit staged changes.

```python
result = agent.handle_git_worktree_tool("git_commit", {
    "message": "Prove theorem foo using induction"
})
```

### git_diff

Show uncommitted changes.

```python
# All unstaged changes
result = agent.handle_git_worktree_tool("git_diff", {})

# Staged changes only
result = agent.handle_git_worktree_tool("git_diff", {"staged": True})

# Specific files
result = agent.handle_git_worktree_tool("git_diff", {
    "paths": ["FormalBook/Chapter1.lean"]
})
```

### git_log

Show recent commits.

```python
result = agent.handle_git_worktree_tool("git_log", {"n": 5})
```

### git_unstage

Unstage files that were added with `git_add`. Removes files from the staging area without discarding changes.

```python
result = agent.handle_git_worktree_tool("git_unstage", {
    "paths": ["FormalBook/Chapter1.lean"]
})
```

### git_restore

Discard uncommitted changes to files. **Warning**: This permanently discards changes.

```python
result = agent.handle_git_worktree_tool("git_restore", {
    "paths": ["FormalBook/Chapter1.lean"]
})
```

### git_checkout_file

Checkout file(s) from a specific ref. Overwrites working copy with the version from that ref.

```python
# Get a file from main
result = agent.handle_git_worktree_tool("git_checkout_file", {
    "ref": "main",
    "paths": ["FormalBook/Chapter1.lean"]
})

# Get a file from a specific commit
result = agent.handle_git_worktree_tool("git_checkout_file", {
    "ref": "abc123",
    "paths": ["FormalBook/Chapter1.lean"]
})
```

### git_rebase

Rebase current branch onto another branch (default: main). This is the recommended way to sync with main.

```python
# Rebase onto main
result = agent.handle_git_worktree_tool("git_rebase", {})

# Rebase onto a specific branch
result = agent.handle_git_worktree_tool("git_rebase", {"branch": "main"})
```

If conflicts occur, the rebase pauses. See the conflict resolution workflow below.

### git_rebase_continue

Continue a paused rebase after resolving conflicts and staging resolved files.

```python
result = agent.handle_git_worktree_tool("git_rebase_continue", {})
```

### git_rebase_abort

Abort a rebase in progress and return to original state.

```python
result = agent.handle_git_worktree_tool("git_rebase_abort", {})
```

### git_rebase_skip

Skip the current commit during a rebase (when the commit's changes are no longer needed).

```python
result = agent.handle_git_worktree_tool("git_rebase_skip", {})
```

### git_conflicts

Show conflict markers and their line numbers during a rebase conflict.

```python
result = agent.handle_git_worktree_tool("git_conflicts", {})
```

### Conflict Resolution Workflow

When `git_rebase` results in conflicts:

1. **Find conflicts** - Use `git_conflicts()` to see which files and lines are conflicted
2. **Edit files** - Remove `<<<<<<<`, `=======`, `>>>>>>>` markers, keeping the desired content
3. **Stage resolved files** - `git_add(paths=[...])`
4. **Continue rebase** - `git_rebase_continue()`
5. **Or abort** - Use `git_rebase_abort()` to give up and restore to pre-rebase state

```python
# Rebase onto main
result = agent.handle_tool("git_rebase", {})
if "conflict" in result.lower():
    # Find where conflicts are
    conflicts = agent.handle_tool("git_conflicts", {})

    # Read and fix conflicted sections
    # ... edit conflicted files ...

    # Stage resolved files and continue
    agent.handle_tool("git_add", {"paths": ["conflicted_file.lean"]})
    agent.handle_tool("git_rebase_continue", {})

    # Or give up
    agent.handle_tool("git_rebase_abort", {})
```

---

## Main Agent Tools

The main agent (orchestrator) has elevated privileges via `MainAgentGitToolsMixin`. It operates on the base project, not a worktree.

### git_merge

Merge a feature branch into the current branch.

```python
result = main_agent.handle_main_agent_git_tool("git_merge", {
    "branch": "agent-prover-ch1",
    "no_ff": True,  # Create merge commit even if fast-forward possible
    "message": "Merge chapter 1 proofs"  # Optional
})
```

### git_checkout

Switch branches. Only `main`, `master`, or `agent-*` branches allowed.

```python
result = main_agent.handle_main_agent_git_tool("git_checkout", {
    "branch": "main"
})
```

### git_branch_delete

Delete a feature branch. Only `agent-*` branches can be deleted.

```python
result = main_agent.handle_main_agent_git_tool("git_branch_delete", {
    "branch": "agent-prover-ch1",
    "force": False  # Use True to delete unmerged branches
})
```

### git_branch_list

List all branches with merge status.

```python
result = main_agent.handle_main_agent_git_tool("git_branch_list", {
    "merged_only": True  # Only show branches merged into current
})
```

### git_reset

Reset to a previous state. Only `soft` and `mixed` modes allowed (no `--hard`).

```python
result = main_agent.handle_main_agent_git_tool("git_reset", {
    "ref": "HEAD~1",
    "mode": "soft"  # or "mixed"
})
```

### git_show

Show details of a specific commit.

```python
result = main_agent.handle_main_agent_git_tool("git_show", {
    "ref": "agent-prover-ch1",
    "stat_only": True  # Only file change statistics
})
```

---

## Typical Workflow

### Parallel Proving

```python
async def prove_theorems(theorems: list[Theorem]):
    pool = WorktreePool(base_project, worktrees_root)

    async def prove_one(theorem: Theorem):
        agent_id = f"prover-{theorem.name}"
        manager = pool.acquire(agent_id)

        agent = ProverAgent(worktree_manager=manager, ...)
        result = await agent.prove(theorem)

        # Keep worktree for main agent to review/merge
        pool.release(agent_id, cleanup=False)
        return result

    # Run all provers in parallel
    results = await asyncio.gather(*[prove_one(t) for t in theorems])

    # Main agent merges successful proofs
    main_agent = MainAgent(base_project=base_project)
    for agent_id, result in zip(agent_ids, results):
        if result.success:
            main_agent.handle_tool("git_merge", {"branch": f"agent-{agent_id}"})
            main_agent.handle_tool("git_branch_delete", {"branch": f"agent-{agent_id}"})

    # Final cleanup
    pool.cleanup_all()
```

### Iterative Development

```python
# Agent makes incremental commits as it works
agent.handle_tool("git_status", {})
agent.handle_tool("git_add", {"paths": ["FormalBook/Chapter1.lean"]})
agent.handle_tool("git_commit", {"message": "WIP: prove lemma A"})

# ... more work ...

agent.handle_tool("git_add", {"paths": ["FormalBook/Chapter1.lean"]})
agent.handle_tool("git_commit", {"message": "Complete proof of theorem B"})

# Review work before finishing
agent.handle_tool("git_log", {"n": 3})
agent.handle_tool("git_diff", {"staged": False})
```

---

## Security Model

### Command Execution

All git commands use `subprocess.run()` with explicit argument lists:

```python
# YES - what we do:
subprocess.run(["git", "add", "--", "file.lean"], cwd=worktree_path, shell=False)

# NO - what we avoid:
subprocess.run(f"git add {user_input}", shell=True)  # NEVER
```

This prevents:
- Shell injection
- Metacharacter interpretation (`; rm -rf /`)
- Command chaining

### Path Validation

All file paths are validated before operations:

1. **Containment**: Must resolve to within the worktree
2. **No escape**: `..` that escapes the worktree is rejected
3. **Shared dirs protected**: `.lake/packages` and `.lake/config` are read-only

```python
validate_path("FormalBook/Chapter1.lean")     # ✓ OK
validate_path("../other-worktree/file.lean")  # ✗ Escapes worktree
validate_path("/etc/passwd")                   # ✗ Absolute path outside
validate_path(".lake/packages/mathlib/...")    # ✗ Shared directory
validate_path(".lake/build/MyProject/...")     # ✓ OK (agent-specific)
```

### Forbidden Operations

These git operations are NOT exposed:

| Operation | Reason |
|-----------|--------|
| `git push` | Orchestrator handles external sync |
| `git pull/fetch` (remote) | Agents work locally; `git_rebase` syncs with local main |
| `git checkout` (feature worker) | Agents stay on their branch |
| `git reset --hard` | Destructive |
| `git clean` | Destructive |

### Branch Restrictions

Main agent can only operate on:
- `main` or `master`
- Branches matching `agent-*`

This prevents accidental modification of other branches.

---

## Disk Usage

| Component | Size | Shared? |
|-----------|------|---------|
| Mathlib packages | ~6.5 GB | Yes (symlinked) |
| Lake config | ~1 MB | Yes (symlinked) |
| Per-agent build | ~150 MB | No (isolated) |
| Source files | ~10 MB | No (git worktree) |

**Total for N agents**: ~6.5 GB + N × 160 MB

---

## Error Handling

### Setup Failures

```python
success, msg = manager.setup()
if not success:
    logger.error(f"Worktree setup failed: {msg}")
    # Common causes:
    # - Branch already exists (tries to reuse)
    # - Disk full
    # - Base project not a git repo
```

### Tool Errors

All tool handlers return error strings prefixed with "Error:":

```python
result = agent.handle_git_worktree_tool("git_add", {"paths": ["../escape"]})
# Returns: "Error: Path escapes worktree: ../escape"

result = agent.handle_git_worktree_tool("git_commit", {"message": ""})
# Returns: "Error: commit message is required"
```

### Cleanup Resilience

Cleanup is resilient to partial failures:

```python
# Even if git worktree remove fails, we force-remove the directory
# and clean up git metadata manually
manager.cleanup()  # Always succeeds
```

---

## Testing

Run the test suite:

```bash
pytest tests/test_git_worktree.py -v
```

Tests cover:
- Worktree creation with proper symlinks
- Path validation (escape blocking, shared dir protection)
- All git tool operations
- Pool lifecycle (acquire, release, cleanup_all)
- Main agent elevated operations
- Full integration workflow
