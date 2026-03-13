# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Recording system for agent runs.

This module provides JSONL-based recording of agent sessions:
- SessionRecorder: Records session-level events (coordinator only)
- AgentRecorder: Records per-agent dialog events (works in both local and distributed)

Directory structure:
    runs/<run_name>/
    ├── session.jsonl          # Session events (coordinator writes)
    └── agents/
        └── <agent_id>.jsonl   # Dialog events (local or distributed workers write)
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .lean_utils import parse_diff_stats

if TYPE_CHECKING:
    pass

logger = getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class RecorderConfig:
    """Configuration for the recording system."""

    run_dir: Path  # e.g., runs/my-run-2024-02-14

    def __post_init__(self) -> None:
        self.run_dir = Path(self.run_dir)


# =============================================================================
# Utilities
# =============================================================================


def _iso_now() -> str:
    """Return current UTC time in ISO8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _write_jsonl(path: Path, event: dict[str, Any]) -> None:
    """Append a JSON line to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def _get_current_branch(cwd: Path) -> str:
    """Get the current git branch name."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _get_head_commit(cwd: Path) -> str:
    """Get the current HEAD commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def read_agent_dialog(run_dir: Path, agent_id: str) -> list[dict[str, Any]]:
    """Read all dialog events from an agent's JSONL file.

    Args:
        run_dir: Directory for this run (e.g., runs/20240214-120000)
        agent_id: Unique agent identifier

    Returns:
        List of all events from the agent file, or empty list if not found
    """
    agent_file = Path(run_dir) / "agents" / f"{agent_id}.jsonl"
    if not agent_file.exists():
        return []

    events = []
    try:
        with open(agent_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass
    return events


# =============================================================================
# AgentRecorder (unified for local and distributed)
# =============================================================================


class AgentRecorder:
    """Records one agent's dialog. Append-only JSONL.

    Works in both local mode (with SessionRecorder) and distributed mode
    (standalone with just run_dir).

    Events:
    - start: Agent started with config
    - msg: User or assistant message
    - tool: Tool call result
    - done: Agent completed

    Example (local mode with session):
        recorder = session.register_agent("prove-123", "prove")
        recorder.record("user", "Prove this theorem...")
        recorder.done("done")

    Example (distributed mode without session):
        recorder = AgentRecorder(run_dir, "prove-123", "prove")
        recorder.record("user", "Prove this theorem...")
        recorder.done("done")
    """

    def __init__(
        self,
        run_dir: Path,
        agent_id: str,
        agent_type: str,
        config: dict[str, Any] | None = None,
    ):
        """Initialize agent recorder.

        Args:
            run_dir: Directory for this run (e.g., runs/20240214-120000)
            agent_id: Unique agent identifier
            agent_type: Type of agent (e.g., "prove", "sketch")
            config: Optional agent configuration
        """
        self.run_dir = Path(run_dir)
        self.agent_id = agent_id
        self.agent_type = agent_type
        self._iteration_count = 0
        self._done = False

        # Token tracking
        self._total_input_tokens = 0
        self._total_output_tokens = 0

        self.path = self.run_dir / "agents" / f"{agent_id}.jsonl"

        # Ensure agents directory exists
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Write start event
        self._write_event(
            {
                "event": "start",
                "ts": _iso_now(),
                "agent_type": agent_type,
                "config": config or {},
            }
        )

    def _write_event(self, event: dict[str, Any]) -> None:
        """Write an event to this agent's JSONL file."""
        _write_jsonl(self.path, event)

    def record(
        self,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """Record a message (user or assistant).

        Args:
            role: "user" or "assistant"
            content: Message content text
            tool_calls: Optional list of tool calls (for assistant messages)
            input_tokens: Optional token count for input to LLM on this turn
            output_tokens: Optional token count for LLM output on this turn
        """
        event: dict[str, Any] = {
            "event": "msg",
            "ts": _iso_now(),
            "role": role,
            "content": content,
        }
        if tool_calls:
            event["tool_calls"] = tool_calls
        if input_tokens is not None:
            event["input_tokens"] = input_tokens
            self._total_input_tokens += input_tokens
        if output_tokens is not None:
            event["output_tokens"] = output_tokens
            self._total_output_tokens += output_tokens
        self._write_event(event)

    def record_tool(
        self,
        name: str,
        args: dict[str, Any],
        result: str,
        duration_ms: float,
    ) -> None:
        """Record a tool call result.

        Args:
            name: Tool name
            args: Tool arguments
            result: Tool result
            duration_ms: Duration in milliseconds
        """
        self._write_event(
            {
                "event": "tool",
                "ts": _iso_now(),
                "name": name,
                "args": args,
                "result": result,
                "duration_ms": duration_ms,
            }
        )

    def flush(self) -> None:
        """Ensure buffered events are written to disk.

        Note: We use unbuffered writes (opening/closing per event),
        so this is a no-op but kept for API consistency.
        """
        pass

    def increment_iteration(self) -> int:
        """Increment and return the iteration count."""
        self._iteration_count += 1
        return self._iteration_count

    def record_compaction(
        self,
        compaction_number: int,
        context_tokens_before: int,
        context_tokens_after: int,
        input_tokens: int,
        output_tokens: int,
        summary: str = "",
    ) -> None:
        """Record a context compaction event.

        Args:
            compaction_number: Which compaction this is (1, 2, 3, ...)
            context_tokens_before: Estimated context size before compaction
            context_tokens_after: Estimated context size after compaction
            input_tokens: Tokens used for the compaction LLM call
            output_tokens: Tokens generated by the compaction LLM call
            summary: The summary text generated by the LLM
        """
        self._write_event(
            {
                "event": "compaction",
                "ts": _iso_now(),
                "compaction_number": compaction_number,
                "context_tokens_before": context_tokens_before,
                "context_tokens_after": context_tokens_after,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "summary": summary,
            }
        )

    def done(
        self,
        status: str,
        error: str | None = None,
    ) -> None:
        """Write done event. Idempotent.

        Args:
            status: Final status (e.g., "done", "error", "max_iterations")
            error: Error message if status is "error"
        """
        if self._done:
            return
        self._done = True

        event: dict[str, Any] = {
            "event": "done",
            "ts": _iso_now(),
            "status": status,
            "iterations": self._iteration_count,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
        }
        if error:
            event["error"] = error
        self._write_event(event)


# =============================================================================
# SessionRecorder
# =============================================================================


class SessionRecorder:
    """Records a single run. One folder = one run.

    Directory structure:
        <run_dir>/
        ├── session.jsonl          # Session events
        ├── agents/
        │   └── <agent_id>.jsonl   # Per-agent dialog
        └── diffs/
            └── *.patch            # Large diffs

    Example:
        recorder = SessionRecorder(run_dir)
        recorder.start(branch="fg/books", base_commit="abc123")
        agent = recorder.register_agent("prove-123", "prove")
        recorder.record_pr_submitted(...)
        recorder.finalize("completed")
    """

    def __init__(self, run_dir: Path):
        """Initialize the session recorder.

        Args:
            run_dir: Directory for this run (e.g., runs/my-run-2024-02-14)
        """
        self.run_dir = Path(run_dir)
        self._session_file = self.run_dir / "session.jsonl"
        self._agents: dict[str, AgentRecorder] = {}

        # Ensure directory exists
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def _write_event(self, event: dict[str, Any]) -> None:
        """Write an event to the session JSONL file."""
        _write_jsonl(self._session_file, event)

    def start(
        self,
        branch: str | None = None,
        base_commit: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        """Write session_start event.

        Args:
            branch: Git branch name (auto-detected if not provided)
            base_commit: Base commit hash (auto-detected if not provided)
            cwd: Working directory for git commands (for auto-detection)
        """
        if cwd and not branch:
            branch = _get_current_branch(cwd)
        if cwd and not base_commit:
            base_commit = _get_head_commit(cwd)

        self._write_event(
            {
                "event": "session_start",
                "ts": _iso_now(),
                "branch": branch or "",
                "base_commit": base_commit or "",
            }
        )

    def register_agent(
        self,
        agent_id: str,
        agent_type: str,
        config: dict[str, Any] | None = None,
    ) -> AgentRecorder:
        """Create AgentRecorder for an agent.

        Note: The agent_launched event should be recorded separately via
        record_agent_launched() which includes richer context (chapter_id,
        theorem_name, revision_number).

        Args:
            agent_id: Unique agent identifier
            agent_type: Type of agent (e.g., "prove", "sketch")
            config: Optional agent configuration

        Returns:
            AgentRecorder for recording this agent's dialog
        """
        recorder = AgentRecorder(self.run_dir, agent_id, agent_type, config)
        self._agents[agent_id] = recorder
        return recorder

    def record_agent_done(
        self,
        agent_id: str,
        status: str,
        chapter_id: str | None = None,
        theorem_name: str | None = None,
        iterations: int | None = None,
    ) -> None:
        """Record when an agent completes (called by coordinator on result receipt).

        Args:
            agent_id: The agent that completed
            status: Final status (e.g., "done", "fix", "issue", "blocked", "error")
            chapter_id: The chapter this agent worked on
            theorem_name: Theorem name (for prove mode agents)
            iterations: Number of iterations the agent ran
        """
        event: dict[str, Any] = {
            "event": "agent_done",
            "ts": _iso_now(),
            "agent_id": agent_id,
            "status": status,
        }
        if chapter_id:
            event["chapter_id"] = chapter_id
        if theorem_name:
            event["theorem_name"] = theorem_name
        if iterations is not None:
            event["iterations"] = iterations
        self._write_event(event)

    def record_agent_status_update(
        self,
        agent_id: str,
        status: str,
        reason: str | None = None,
    ) -> None:
        """Record when an agent's effective status changes after completion.

        This is used to update agent status when review outcomes change:
        - "pending_revision" when review requests changes
        - "approved" when review approves
        - "rejected" when review rejects

        Args:
            agent_id: The agent whose status is being updated
            status: New effective status
            reason: Optional reason for the status change
        """
        event: dict[str, Any] = {
            "event": "agent_status_update",
            "ts": _iso_now(),
            "agent_id": agent_id,
            "status": status,
        }
        if reason:
            event["reason"] = reason
        self._write_event(event)

    def record_pr_submitted(
        self,
        pr_id: str,
        agent_id: str,
        branch_name: str,
        agent_type: str,
        chapter_id: str,
        theorem_name: str | None = None,
        diff: str | None = None,
        revision_number: int = 0,
    ) -> None:
        """Record when a PR is submitted for review.

        Args:
            pr_id: Unique PR identifier
            agent_id: The agent that created the PR
            branch_name: Git branch name for the PR
            agent_type: Type of agent (e.g., "sketch", "prove", "maintain", "scan", "progress", "triage")
            chapter_id: The chapter this PR is for
            theorem_name: Theorem name (for prove mode PRs)
            diff: The git diff for this PR (optional)
            revision_number: Revision iteration (0 = initial, 1+ = after feedback)
        """
        event: dict[str, Any] = {
            "event": "pr_submitted",
            "ts": _iso_now(),
            "pr_id": pr_id,
            "agent_id": agent_id,
            "branch_name": branch_name,
            "agent_type": agent_type,
            "chapter_id": chapter_id,
            "revision_number": revision_number,
        }
        if theorem_name:
            event["theorem_name"] = theorem_name

        # Always store diff inline (diffs are typically <10k chars)
        if diff:
            # Calculate stats using shared utility
            additions, deletions = parse_diff_stats(diff)
            event["diff_stats"] = {"+": additions, "-": deletions}
            event["diff"] = diff

        self._write_event(event)

    def record_review(
        self,
        pr_id: str,
        agent_id: str,
        semantic_verdict: str | None = None,
        semantic_summary: str | None = None,
        engineering_verdict: str | None = None,
        engineering_summary: str | None = None,
        combined_verdict: str = "pending",
        build_passed: bool | None = None,
        build_error: str | None = None,
        build_output: str | None = None,
        revision_number: int = 0,
    ) -> None:
        """Record a review result for a PR.

        Args:
            pr_id: The PR being reviewed
            agent_id: The agent that authored the PR
            semantic_verdict: Verdict from math review
            semantic_summary: Summary from math review
            engineering_verdict: Verdict from engineering review
            engineering_summary: Summary from engineering review
            combined_verdict: Combined verdict (approve/reject/request_changes)
            build_passed: Whether the build passed
            build_error: Build error message if failed
            build_output: Full build stdout/stderr output (for failed builds)
            revision_number: Which revision of the PR this review is for
        """
        event: dict[str, Any] = {
            "event": "review_completed",
            "ts": _iso_now(),
            "pr_id": pr_id,
            "agent_id": agent_id,
            "combined_verdict": combined_verdict,
            "revision_number": revision_number,
        }
        if semantic_verdict:
            event["math"] = {
                "verdict": semantic_verdict,
                "summary": semantic_summary or "",
            }
        if engineering_verdict:
            event["engineering"] = {
                "verdict": engineering_verdict,
                "summary": engineering_summary or "",
            }
        if build_passed is not None:
            event["build_passed"] = build_passed
        if build_error:
            event["build_error"] = build_error
        if build_output:
            event["build_output"] = build_output
        self._write_event(event)

    def record_agent_launched(
        self,
        agent_id: str,
        agent_type: str,
        chapter_id: str,
        theorem_name: str | None = None,
        revision_number: int = 0,
        review_target: str | None = None,
        issue_id: str | None = None,
    ) -> None:
        """Record when an agent task is launched (sketch, prove, revision).

        Args:
            agent_id: Unique agent identifier
            agent_type: Type of agent (e.g., "sketch", "prove", "maintain", "scan", "progress", "triage", "math_reviewer", "engineering_reviewer")
            chapter_id: The chapter this agent is working on
            theorem_name: Theorem name (for prove mode agents)
            revision_number: Revision iteration (0 = initial)
            review_target: For reviewer agents, the agent_id of the PR being reviewed
            issue_id: For maintain agents, the issue ID being worked on
        """
        event: dict[str, Any] = {
            "event": "agent_launched",
            "ts": _iso_now(),
            "agent_id": agent_id,
            "agent_type": agent_type,
            "chapter_id": chapter_id,
            "revision_number": revision_number,
        }
        if theorem_name:
            event["theorem_name"] = theorem_name
        if review_target:
            event["review_target"] = review_target
        if issue_id:
            event["issue_id"] = issue_id
        self._write_event(event)

    def record_agent_resumed(
        self,
        agent_id: str,
        agent_type: str,
        chapter_id: str,
        pr_id: str,
        pr_status: str,
        theorem_name: str | None = None,
        revision_number: int = 0,
        diff_stats: dict[str, int] | None = None,
        diffs: dict[int, str] | None = None,
        dialog: list[dict[str, Any]] | None = None,
    ) -> None:
        """Record that an agent from a previous session is still active.

        Emitted at startup for all PRs in the queue, so the viewer can show
        agents whose PRs are still in progress (pending_review, in_review, etc.)

        Args:
            agent_id: Unique agent identifier
            agent_type: Type of agent (e.g., "sketch", "prove", "maintain", "scan", "progress", "triage")
            chapter_id: The chapter this agent is working on
            pr_id: The PR ID associated with this agent
            pr_status: Current status of the PR (pending_review, needs_revision, approved, etc.)
            theorem_name: Theorem name (for prove mode agents)
            revision_number: Current revision number
            diff_stats: Diff statistics {"+": additions, "-": deletions}
            diffs: Map of revision numbers to diff content (for viewer to display)
            dialog: Agent dialog events from previous session (for viewer to display)
        """
        event: dict[str, Any] = {
            "event": "agent_resumed",
            "ts": _iso_now(),
            "agent_id": agent_id,
            "agent_type": agent_type,
            "chapter_id": chapter_id,
            "pr_id": pr_id,
            "pr_status": pr_status,
            "revision_number": revision_number,
        }
        if theorem_name:
            event["theorem_name"] = theorem_name
        if diff_stats:
            event["diff_stats"] = diff_stats
        if diffs:
            # Convert int keys to strings for JSON serialization
            event["diffs"] = {str(k): v for k, v in diffs.items()}
        if dialog:
            event["dialog"] = dialog
        self._write_event(event)

    def record_review_launched(
        self,
        pr_id: str,
        agent_id: str,
    ) -> None:
        """Record when a review is launched for a PR.

        Args:
            pr_id: The PR being reviewed
            agent_id: The agent that authored the PR
        """
        self._write_event(
            {
                "event": "review_launched",
                "ts": _iso_now(),
                "pr_id": pr_id,
                "agent_id": agent_id,
            }
        )

    def record_build(
        self,
        context: str,
        pr_id: str,
        branch_name: str,
        passed: bool,
        error: str | None = None,
        duration_s: float | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> None:
        """Record a build operation result.

        Args:
            context: Where the build ran ("merge" or "review")
            pr_id: The PR this build is for
            branch_name: The branch that was built
            passed: Whether the build passed
            error: Build error message if failed
            duration_s: Build duration in seconds
            stdout: Build stdout output (captured for failed builds)
            stderr: Build stderr output (captured for failed builds)
        """
        event: dict[str, Any] = {
            "event": "build_completed",
            "ts": _iso_now(),
            "context": context,
            "pr_id": pr_id,
            "branch_name": branch_name,
            "passed": passed,
        }
        if error:
            event["error"] = error
        if duration_s is not None:
            event["duration_s"] = round(duration_s, 1)
        if stdout:
            event["stdout"] = stdout
        if stderr:
            event["stderr"] = stderr
        self._write_event(event)

    def record_revision_started(
        self,
        pr_id: str,
        agent_id: str,
        revision_number: int,
    ) -> None:
        """Record when a PR is sent back for revision.

        Args:
            pr_id: The PR being revised
            agent_id: The agent that will revise it
            revision_number: Which revision attempt this is
        """
        self._write_event(
            {
                "event": "revision_started",
                "ts": _iso_now(),
                "pr_id": pr_id,
                "agent_id": agent_id,
                "revision_number": revision_number,
            }
        )

    def record_pre_review_merge(
        self,
        pr_id: str,
        agent_id: str,
        success: bool,
        revision_number: int = 0,
        main_commit_hash: str | None = None,
        conflict_files: list[str] | None = None,
    ) -> None:
        """Record the pre-review merge-main step result.

        This is the step where main is merged into the PR branch before
        running the full review (build + LLM reviewers). Records both
        success and failure cases.

        Args:
            pr_id: The PR being reviewed
            agent_id: The agent that created the PR
            success: Whether the merge succeeded
            revision_number: Which revision of the PR this is
            main_commit_hash: The commit hash of main that was merged in
            conflict_files: List of files with merge conflicts (on failure)
        """
        event: dict[str, Any] = {
            "event": "pre_review_merge",
            "ts": _iso_now(),
            "pr_id": pr_id,
            "agent_id": agent_id,
            "success": success,
            "revision_number": revision_number,
        }
        if main_commit_hash:
            event["main_commit_hash"] = main_commit_hash
        if conflict_files:
            event["conflict_files"] = conflict_files
        self._write_event(event)

    def record_merge_conflict_pre_review(
        self,
        pr_id: str,
        agent_id: str,
        conflict_files: list[str],
        revision_number: int = 0,
        main_commit_hash: str | None = None,
    ) -> None:
        """Record when a merge conflict is detected during pre-review check.

        This is recorded before the full review runs, when we merge main
        into the PR branch and find conflicts. The review is skipped in
        this case to save build + LLM costs.

        Args:
            pr_id: The PR being reviewed
            agent_id: The agent that created the PR
            conflict_files: List of files with merge conflicts
            revision_number: Which revision of the PR this is
            main_commit_hash: The commit hash of main that conflict was tested against
        """
        event: dict[str, Any] = {
            "event": "merge_conflict_detected",
            "ts": _iso_now(),
            "pr_id": pr_id,
            "agent_id": agent_id,
            "conflict_files": conflict_files,
            "revision_number": revision_number,
        }
        if main_commit_hash:
            event["main_commit_hash"] = main_commit_hash
        self._write_event(event)

    def record_merge(
        self,
        pr_id: str,
        branch_name: str,
        success: bool,
        agent_id: str | None = None,
        diff_stats: dict[str, int] | None = None,
        commit_hash: str | None = None,
        error: str | None = None,
        conflict_files: list[str] | None = None,
        revision_number: int = 0,
        failure_reason: str | None = None,
        main_commit_hash: str | None = None,
        build_duration_s: float | None = None,
    ) -> None:
        """Record a merge operation result.

        Args:
            pr_id: The PR being merged
            branch_name: The branch being merged
            success: Whether the merge succeeded
            agent_id: The agent that created the PR
            diff_stats: Line stats {"+": additions, "-": deletions}
            commit_hash: The resulting commit hash if successful
            error: Error message if merge failed
            conflict_files: List of files with merge conflicts (on failure)
            revision_number: Which revision of the PR was merged
            failure_reason: Type of failure - "merge_conflict", "build_failed", "build_timeout", "unknown"
            main_commit_hash: The commit hash of main that merge was attempted against
            build_duration_s: Build duration in seconds (for build failures)
        """
        event: dict[str, Any] = {
            "event": "merge_completed",
            "ts": _iso_now(),
            "pr_id": pr_id,
            "branch_name": branch_name,
            "success": success,
            "revision_number": revision_number,
        }
        if agent_id:
            event["agent_id"] = agent_id
        if diff_stats:
            event["diff_stats"] = diff_stats
        if commit_hash:
            event["commit_hash"] = commit_hash
        if error:
            event["error"] = error
        if conflict_files:
            event["conflict_files"] = conflict_files
        if failure_reason:
            event["failure_reason"] = failure_reason
        if main_commit_hash:
            event["main_commit_hash"] = main_commit_hash
        if build_duration_s is not None:
            event["build_duration_s"] = round(build_duration_s, 1)
        self._write_event(event)

    def record_proof_stats(
        self,
        total_theorems: int,
        proven_theorems: int,
        remaining_sorries: int,
        open_issues: int = 0,
        closed_issues: int = 0,
        per_chapter: dict[str, dict[str, int]] | None = None,
        issues: list[dict[str, Any]] | None = None,
    ) -> None:
        """Record current proof statistics after a merge.

        Args:
            total_theorems: Total number of target theorems across all chapters
            proven_theorems: Number of theorems proven (no sorry)
            remaining_sorries: Number of theorems still with sorry
            open_issues: Number of open issues
            closed_issues: Number of closed issues
            per_chapter: Optional per-chapter breakdown {chapter_id: {"total": N, "proven": M, "sorries": K}}
            issues: Optional list of parsed issues [{id, chapter_id, description, origin, is_open}, ...]
        """
        event: dict[str, Any] = {
            "event": "proof_stats",
            "ts": _iso_now(),
            "total_theorems": total_theorems,
            "proven_theorems": proven_theorems,
            "remaining_sorries": remaining_sorries,
            "open_issues": open_issues,
            "closed_issues": closed_issues,
        }
        if per_chapter:
            event["per_chapter"] = per_chapter
        if issues is not None:
            event["issues"] = issues
        self._write_event(event)

    def record_event(
        self,
        event_type: str,
        **kwargs: Any,
    ) -> None:
        """Record a generic event with custom fields.

        This is a flexible method for recording events that don't fit
        existing specialized record_* methods.

        Args:
            event_type: The event type name (e.g., "custom_event")
            **kwargs: Additional fields to include in the event
        """
        event: dict[str, Any] = {
            "event": event_type,
            "ts": _iso_now(),
        }
        event.update(kwargs)
        self._write_event(event)

    def finalize(self, status: str = "completed", error: str | None = None) -> None:
        """Write session_end event.

        Args:
            status: Final session status (e.g., "completed", "crashed", "interrupted_by_user")
            error: Optional error traceback string for crash diagnostics
        """
        event = {
            "event": "session_end",
            "ts": _iso_now(),
            "status": status,
        }
        if error:
            event["error"] = error
        self._write_event(event)


# =============================================================================
# Factory Functions
# =============================================================================


def create_session_recorder(
    runs_dir: Path,
    run_name: str | None = None,
) -> SessionRecorder:
    """Create a new session recorder with a unique run directory.

    Args:
        runs_dir: Base directory for all runs
        run_name: Optional run name (auto-generated if not provided)

    Returns:
        SessionRecorder for the new run
    """
    if run_name is None:
        run_name = datetime.now().strftime("%Y%m%d-%H%M%S")

    run_dir = Path(runs_dir) / run_name
    return SessionRecorder(run_dir)
