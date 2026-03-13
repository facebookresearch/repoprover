# Recording System

The recording system provides JSONL-based logging for agent runs, enabling replay, debugging, and analysis of agent behavior.

## Overview

- **One folder per run**: Each run creates a timestamped directory under `runs/`
- **JSONL format**: All events are stored as newline-delimited JSON for easy parsing
- **Minimal overhead**: Append-only writes with no complex indexing
- **Separate large diffs**: Diffs exceeding a threshold are stored in separate `.patch` files

## Directory Structure

```
runs/<run_name>/
├── session.jsonl          # Session-level events
├── agents/
│   └── <agent_id>.jsonl   # Per-agent dialog events
└── diffs/
    └── *.patch            # Large diffs stored separately
```

**Quick listing**: `os.listdir(runs_dir)` → each folder is a run.

## Event Schemas

### Session Events

```jsonl
{"event": "session_start", "ts": "ISO8601", "branch": "fg/books", "base_commit": "abc123"}
{"event": "agent_launched", "ts": "ISO8601", "agent_id": "prove-123", "agent_type": "prove", "chapter_id": "ch1", "theorem_name": "theorem_foo"}
{"event": "pr_submitted", "ts": "ISO8601", "pr_id": "pr-001", "agent_id": "prove-123", "diff_stats": {"+": 50, "-": 10}}
{"event": "review_completed", "ts": "ISO8601", "pr_id": "pr-001", "agent_id": "prove-123", "combined_verdict": "approve"}
{"event": "merge_conflict_detected", "ts": "ISO8601", "pr_id": "pr-001", "agent_id": "prove-123", "conflict_files": ["Foo.lean"], "main_commit_hash": "abc123def"}
{"event": "merge_completed", "ts": "ISO8601", "pr_id": "pr-001", "agent_id": "prove-123", "success": true, "diff_stats": {"+": 50, "-": 10}, "commit_hash": "def456"}
{"event": "merge_completed", "ts": "ISO8601", "pr_id": "pr-002", "agent_id": "sketch-456", "success": false, "failure_reason": "merge_conflict", "conflict_files": ["Bar.lean"], "main_commit_hash": "abc123def"}
{"event": "merge_completed", "ts": "ISO8601", "pr_id": "pr-003", "agent_id": "prove-789", "success": false, "failure_reason": "build_failed", "error": "Build error...", "build_duration_s": 45.2, "main_commit_hash": "abc123def"}
{"event": "agent_done", "ts": "ISO8601", "agent_id": "prove-123", "status": "done", "iterations": 15}
{"event": "session_end", "ts": "ISO8601", "status": "completed"}
```

### Agent Events (`agents/<agent_id>.jsonl`)

```jsonl
{"event": "start", "ts": "ISO8601", "agent_type": "prove", "config": {"model": "claude-sonnet-4-20250514"}}
{"event": "msg", "ts": "ISO8601", "role": "user", "content": "..."}
{"event": "msg", "ts": "ISO8601", "role": "assistant", "content": "...", "tool_calls": [{"name": "read_file", "args": {...}}]}
{"event": "tool", "ts": "ISO8601", "name": "read_file", "args": {...}, "result": "...", "duration_ms": 50}
{"event": "done", "ts": "ISO8601", "status": "done", "iterations": 15, "error": null}
```

## Usage

### Automatic Recording (via BookCoordinator)

Recording is enabled by default when using `BookCoordinator`:

```python
from repoprover.coordinator import BookCoordinator, BookCoordinatorConfig

config = BookCoordinatorConfig(
    book_id="mybook",
    title="My Book",
    base_project=Path("/path/to/project"),
    worktrees_root=Path("/path/to/worktrees"),
    recording_enabled=True,  # Default: True
    runs_dir=Path("/path/to/runs"),  # Default: base_project/runs
)

coordinator = BookCoordinator(config)
coordinator.start()  # Starts recording session
# ... run agents ...
coordinator.stop()   # Finalizes recording
```

### Manual Recording

For direct control over recording:

```python
from pathlib import Path
from repoprover.recording import SessionRecorder, create_session_recorder

# Create a session recorder
recorder = create_session_recorder(
    runs_dir=Path("runs"),
    run_name="my-run-2024-02-14",  # Optional, auto-generated if not provided
)

# Start the session
recorder.start(branch="main", base_commit="abc123")
# Or auto-detect from git:
recorder.start(cwd=Path("/path/to/repo"))

# Register an agent and get its recorder
agent_recorder = recorder.register_agent(
    agent_id="prove-001",
    agent_type="prove",
    config={"model": "claude-sonnet-4-20250514"},
)

# Record agent dialog
agent_recorder.record("user", "Prove this theorem...")
agent_recorder.record("assistant", "I'll analyze...", tool_calls=[
    {"name": "read_file", "args": {"path": "Theorem.lean"}}
])
agent_recorder.record_tool("read_file", {"path": "Theorem.lean"}, "content...", 50.0)
agent_recorder.flush()

# Mark agent as done
agent_recorder.done("done")

# Record diffs (automatically handles large diffs)
recorder.record_diff(diff_content)

# Finalize session
recorder.finalize("completed")
```

### Integration with ContributorAgent

Agents automatically record when provided with a recorder:

```python
from repoprover.agents.contributor import ContributorAgent, ContributorTask
from repoprover.recording import SessionRecorder

session = SessionRecorder(Path("runs/my-run"))
session.start(cwd=project_path)

agent_recorder = session.register_agent("prove-001", "prove")

agent = ContributorAgent(
    config=agent_config,
    repo_root=project_path,
    recorder=agent_recorder,  # Pass the recorder
)

task = ContributorTask.prove(chapter=chapter_context, theorem_name="myTheorem")
result = agent.run(task=task)  # Automatically records everything
```

## Configuration

### RecorderConfig

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `run_dir` | `Path` | Required | Directory for this run |

### BookCoordinatorConfig Recording Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `recording_enabled` | `bool` | `True` | Enable/disable recording |
| `runs_dir` | `Path` | `base_project/runs` | Base directory for all runs |

## Event Reference

### Session Events

| Event | Fields | Description |
|-------|--------|-------------|
| `session_start` | `ts`, `branch`, `base_commit` | Session started |
| `agent_launched` | `ts`, `agent_id`, `agent_type`, `chapter_id`, `theorem_name?`, `revision_number` | Agent task launched |
| `pr_submitted` | `ts`, `pr_id`, `agent_id`, `diff_stats` | PR submitted for review |
| `review_completed` | `ts`, `pr_id`, `agent_id`, `combined_verdict` | PR reviewed |
| `merge_conflict_detected` | `ts`, `pr_id`, `agent_id`, `conflict_files`, `revision_number`, `main_commit_hash?` | Pre-review rebase conflict detected (skips build + LLM review) |
| `merge_completed` | `ts`, `pr_id`, `agent_id`, `success`, `diff_stats?`, `commit_hash?`, `error?`, `conflict_files?`, `failure_reason?`, `main_commit_hash?`, `build_duration_s?`, `revision_number` | PR merge attempt result |
| `agent_done` | `ts`, `agent_id`, `status`, `iterations` | Agent completed |
| `session_end` | `ts`, `status` | Session ended |

### Merge Event Details

The `merge_completed` event contains additional fields for failed merges:

| Field | Type | Description |
|-------|------|-------------|
| `failure_reason` | `"merge_conflict" \| "build_failed" \| "build_timeout" \| "unknown"` | Type of failure (only on failure) |
| `conflict_files` | `string[]` | Files with merge conflicts (only for merge_conflict) |
| `main_commit_hash` | `string` | Commit hash of main that merge was attempted against |
| `build_duration_s` | `float` | Build duration in seconds (only for build failures) |
| `error` | `string` | Error message (for build_failed, build_timeout, unknown) |

### Agent Events

| Event | Fields | Description |
|-------|--------|-------------|
| `start` | `ts`, `agent_type`, `config` | Agent started |
| `msg` | `ts`, `role`, `content`, `tool_calls?` | User or assistant message |
| `tool` | `ts`, `name`, `args`, `result`, `duration_ms` | Tool call completed |
| `done` | `ts`, `status`, `iterations`, `error?` | Agent finished |

## Reading Recordings

Recordings are plain JSONL files that can be read with standard tools:

```python
import json
from pathlib import Path

def load_session(run_dir: Path):
    """Load all events from a session."""
    events = []
    session_file = run_dir / "session.jsonl"

    with open(session_file) as f:
        for line in f:
            events.append(json.loads(line))

    return events

def load_agent_dialog(run_dir: Path, agent_id: str):
    """Load dialog for a specific agent."""
    events = []
    agent_file = run_dir / "agents" / f"{agent_id}.jsonl"

    with open(agent_file) as f:
        for line in f:
            events.append(json.loads(line))

    return events

# List all runs
runs = list(Path("runs").iterdir())

# Load a specific run
session_events = load_session(runs[0])
agent_events = load_agent_dialog(runs[0], "prove-001")
```

## Design Decisions

1. **JSONL over JSON**: Enables append-only writes and streaming reads
2. **Folder per run**: Simple discovery with `os.listdir()`, no index maintenance
3. **Separate large diffs**: Keeps JSONL files readable, diffs stored as `.patch`
4. **No viewer in core**: Viewer is a separate concern; this module focuses on recording
5. **Unbuffered writes**: Each event is immediately written to disk for crash safety
