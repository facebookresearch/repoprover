# Build Management

This document describes the centralized build system for RepoProver.

## Overview

RepoProver uses a centralized `build` module to manage all `lake build` operations. This provides:

1. **Concurrency Control** — Limits simultaneous builds to prevent resource exhaustion
2. **Unified Interface** — Single function for all build operations
3. **Monitoring** — Tracks build duration and semaphore wait times
4. **Async Support** — Works with both sync and async code

## The Problem

When running many agents in parallel (e.g., 200+ provers), each agent's review triggers a `lake build` to verify the code compiles. Without concurrency control:

- Dozens of `lake build` processes compete for CPU, memory, and disk I/O
- Builds that normally take 30 seconds start timing out at 10 minutes
- The entire system grinds to a halt

## The Solution

The `build` module provides a semaphore-controlled build function that limits concurrent builds:

```python
from repoprover.build import lake_build

# Synchronous call (blocks until semaphore available + build complete)
result = lake_build(worktree_path, label="review")

if result.success:
    print(f"Build passed in {result.duration:.1f}s")
else:
    print(f"Build failed: {result.error}")
```

## API Reference

### `lake_build(cwd, *, target=None, timeout=600, label="build") -> BuildResult`

Run `lake build` with concurrency control.

**Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `cwd` | `Path \| str` | required | Working directory (worktree path) |
| `target` | `str \| None` | `None` | Optional build target (e.g., `"MyModule"`) |
| `timeout` | `float` | `600` | Build timeout in seconds (10 minutes) |
| `label` | `str` | `"build"` | Label for logging (e.g., `"review"`, `"merge"`) |

**Returns:** `BuildResult`

### `lake_build_async(...)`

Async version with identical parameters. Uses `asyncio.Semaphore` for non-blocking concurrency control.

```python
result = await lake_build_async(worktree_path, label="review")
```

### `BuildResult`

Dataclass returned by build functions:

| Field | Type | Description |
|-------|------|-------------|
| `success` | `bool` | Whether the build succeeded |
| `error` | `str \| None` | Error message if failed |
| `duration` | `float \| None` | Build duration in seconds (`None` if timed out) |
| `returncode` | `int \| None` | Process return code (`None` if timed out) |
| `stdout` | `str` | Standard output |
| `stderr` | `str` | Standard error |
| `timed_out` | `bool` | Whether the build was killed due to timeout |
| `waited_for_semaphore` | `float` | Time spent waiting for semaphore |

### Configuration Functions

#### `set_max_concurrent_builds(max_builds: int)`

Update the maximum number of concurrent builds at runtime.

```python
from repoprover.build import set_max_concurrent_builds

# Allow more builds on a beefy machine
set_max_concurrent_builds(16)
```

**Note:** Call this before any builds start for full effect.

#### `get_build_stats() -> dict`

Get current build concurrency statistics.

```python
from repoprover.build import get_build_stats

stats = get_build_stats()
# {'max_concurrent': 8, 'available_slots': 5, 'active_builds': 3}
```

## Configuration

### Default Settings

| Setting | Value | Description |
|---------|-------|-------------|
| `MAX_CONCURRENT_BUILDS` | 8 | Maximum simultaneous `lake build` processes |
| `DEFAULT_BUILD_TIMEOUT` | 600s | 10 minute timeout per build |

### Tuning Guidelines

**For the semaphore limit (`MAX_CONCURRENT_BUILDS`):**

| Machine Type | Recommended Value |
|--------------|-------------------|
| 8-core laptop | 4 |
| 32-core workstation | 8-12 |
| 64+ core server | 16-24 |

The optimal value depends on:
- CPU cores (Lean compilation is CPU-bound)
- Available memory (~2-4GB per build)
- Disk I/O speed (builds read/write many `.olean` files)

**Signs you need to lower the limit:**
- Builds timing out that previously succeeded
- System becoming unresponsive during builds
- High `waited_for_semaphore` times (>30s)

**Signs you can increase the limit:**
- Low CPU utilization during builds
- Builds completing quickly with low wait times

## Usage in RepoProver

### Review Builds

Reviews use `lake_build` to verify code compiles before LLM review:

```python
# In agents/reviewers.py
result = lake_build(worktree_path, label=f"review:{branch_name[:20]}")
if not result.success:
    return ReviewResult(build_passed=False, build_error=result.error, ...)
```

### Merge Builds

The coordinator uses `lake_build` to verify merges:

```python
# In coordinator.py
result = lake_build(self.base_project, label=f"merge:{branch_name[:20]}")
if result.timed_out:
    # Reset merge and report timeout
    ...
```

## Migration from Direct subprocess Calls

If you have code using direct `subprocess.run` for builds:

**Before:**
```python
import subprocess
result = subprocess.run(
    ["lake", "build"],
    cwd=worktree_path,
    capture_output=True,
    text=True,
    timeout=600,
)
if result.returncode != 0:
    error = result.stderr
```

**After:**
```python
from repoprover.build import lake_build

result = lake_build(worktree_path, label="my-build")
if not result.success:
    error = result.error
```

## Monitoring

Build operations are logged with timing information:

```
INFO [review:sketch-ch1] Waited 12.3s for build semaphore
INFO [review:sketch-ch1] Build started: lake build in /path/to/worktree
INFO [review:sketch-ch1] Build passed (45.2s)
```

High semaphore wait times indicate the system is build-constrained. Consider:
- Increasing `MAX_CONCURRENT_BUILDS` if resources allow
- Reducing agent parallelism (`max_concurrent_contributors`)
- Using faster storage for `.lake/build/` directories
