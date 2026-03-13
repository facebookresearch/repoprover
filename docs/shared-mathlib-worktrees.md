# Shared Mathlib Setup for Multi-Agent Lean Worktrees

## Problem

Each Lean agent needs its own working directory (git worktree) to write and build `.lean` files independently. A naive setup duplicates Mathlib (~6.5G) per agent, which is wasteful.

## Solution

Use git worktrees with **symlinked `.lake/packages` and `.lake/config`**, but a **per-agent `.lake/build/`** directory.

**Directory Structure:**

- **base-project/** — one fully built Lean project
  - `.lake/`
    - `packages/` — 6.5G Mathlib + deps (shared, read-only)
    - `config/` — Lake workspace config (shared)
    - `build/` — base project's own build artifacts
  - `lakefile.lean`
  - `lake-manifest.json`
  - `lean-toolchain`

- **worktree-agent-1/** — git worktree
  - `.lake/`
    - `packages` → `base-project/.lake/packages` (symlink)
    - `config` → `base-project/.lake/config` (symlink)
    - `build/` — agent 1's own build artifacts (~150M)
  - `lakefile.lean` (same as base, via git)

- **worktree-agent-2/** — git worktree
  - `.lake/`
    - `packages` → `base-project/.lake/packages` (symlink)
    - `config` → `base-project/.lake/config` (symlink)
    - `build/` — agent 2's own build artifacts (~150M)
  - `lakefile.lean` (same as base, via git)

## Setup

### One-time: create the base project

```bash
cd /path/to/your/lean-project
lake update    # fetches all packages
lake build     # builds everything
```

### Per agent: create worktree and wire up .lake

```bash
# Create worktree
git worktree add /path/to/agent-N -b agent-N

# Wire up shared packages, isolated build
cd /path/to/agent-N
mkdir -p .lake
ln -s /path/to/base-project/.lake/packages .lake/packages
ln -s /path/to/base-project/.lake/config .lake/config
```

That's it. `lake build`, `lake exe repl`, and `lake env` all work from the worktree.

## Why this works

Lake resolves packages from `.lake/packages/` and workspace config from `.lake/config/`. By symlinking these, the worktree reuses the base project's fetched and built Mathlib without re-downloading or re-building.

Lake decides whether to rebuild a source file by checking trace/hash files in `.lake/build/`. Since each agent has its own `build/` directory, Lake correctly detects when a file needs rebuilding based on that agent's source.

## Why symlink the entire `.lake` does NOT work

If two agents symlink the entire `.lake/` (including `build/`), they share build artifacts. Lake's hash-based rebuild check sees an existing olean and skips rebuilding, even when the agent has different source content for that file. This causes silent stale-olean reuse — an agent gets build results from another agent's version of the file.

## Disk cost

- Shared: ~6.5G (Mathlib packages, one copy)
- Per agent: ~150M (own build artifacts)
- Total for N agents: ~6.5G + N × 150M

## Constraints

- All worktrees must use the same `lakefile.lean`, `lake-manifest.json`, and `lean-toolchain` (same package versions). This is naturally true for worktrees of the same git repo.
- The base project must be fully built (`lake build`) before agents start. Agents should not run `lake update` — that would try to fetch packages via the network.
- Concurrent `lake build` across agents is managed by the centralized build module (`build.py`), which uses a semaphore to limit simultaneous builds and prevent resource exhaustion. See [build.md](build.md) for configuration details.
