# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Tests for the recording system."""

from __future__ import annotations

import json
from pathlib import Path

from repoprover.recording import (
    AgentRecorder,
    SessionRecorder,
    create_session_recorder,
)


# =============================================================================
# SessionRecorder Tests
# =============================================================================


class TestSessionRecorder:
    """Tests for SessionRecorder."""

    def test_creates_run_directory(self, tmp_path: Path) -> None:
        """Test that SessionRecorder creates the run directory."""
        run_dir = tmp_path / "test-run"
        SessionRecorder(run_dir)

        assert run_dir.exists()
        assert run_dir.is_dir()

    def test_start_writes_session_start_event(self, tmp_path: Path) -> None:
        """Test that start() writes a session_start event."""
        run_dir = tmp_path / "test-run"
        recorder = SessionRecorder(run_dir)

        recorder.start(branch="main", base_commit="abc123")

        session_file = run_dir / "session.jsonl"
        assert session_file.exists()

        events = _read_jsonl(session_file)
        assert len(events) == 1
        assert events[0]["event"] == "session_start"
        assert events[0]["branch"] == "main"
        assert events[0]["base_commit"] == "abc123"
        assert "ts" in events[0]

    def test_register_agent_creates_agent_recorder(self, tmp_path: Path) -> None:
        """Test that register_agent() creates an AgentRecorder (no session event).

        Note: The agent_launched event should be recorded separately via
        record_agent_launched() which includes richer context.
        """
        run_dir = tmp_path / "test-run"
        recorder = SessionRecorder(run_dir)
        recorder.start(branch="main", base_commit="abc123")

        agent_recorder = recorder.register_agent("prove-001", "prove")

        # Session should only have session_start (no agent_start event)
        events = _read_jsonl(run_dir / "session.jsonl")
        assert len(events) == 1
        assert events[0]["event"] == "session_start"

        # Check agent recorder was returned
        assert isinstance(agent_recorder, AgentRecorder)
        assert agent_recorder.agent_id == "prove-001"

        # Agent file should have start event
        agent_events = _read_jsonl(run_dir / "agents" / "prove-001.jsonl")
        assert len(agent_events) == 1
        assert agent_events[0]["event"] == "start"
        assert agent_events[0]["agent_type"] == "prove"

    def test_finalize_writes_session_end_event(self, tmp_path: Path) -> None:
        """Test that finalize() writes a session_end event."""
        run_dir = tmp_path / "test-run"
        recorder = SessionRecorder(run_dir)
        recorder.start(branch="main", base_commit="abc123")

        recorder.finalize("completed")

        events = _read_jsonl(run_dir / "session.jsonl")
        assert events[-1]["event"] == "session_end"
        assert events[-1]["status"] == "completed"


# =============================================================================
# AgentRecorder Tests
# =============================================================================


class TestAgentRecorder:
    """Tests for AgentRecorder."""

    def test_creates_agent_file_with_start_event(self, tmp_path: Path) -> None:
        """Test that AgentRecorder creates file with start event."""
        run_dir = tmp_path / "test-run"
        session = SessionRecorder(run_dir)
        session.start(branch="main", base_commit="abc123")

        session.register_agent(
            "prove-001",
            "prove",
            config={"model": "claude-sonnet-4-20250514"},
        )

        agent_file = run_dir / "agents" / "prove-001.jsonl"
        assert agent_file.exists()

        events = _read_jsonl(agent_file)
        assert len(events) == 1
        assert events[0]["event"] == "start"
        assert events[0]["agent_type"] == "prove"
        assert events[0]["config"]["model"] == "claude-sonnet-4-20250514"

    def test_record_user_message(self, tmp_path: Path) -> None:
        """Test recording a user message."""
        run_dir = tmp_path / "test-run"
        session = SessionRecorder(run_dir)
        session.start(branch="main", base_commit="abc123")
        agent = session.register_agent("prove-001", "prove")

        agent.record("user", "Prove this theorem...")

        events = _read_jsonl(run_dir / "agents" / "prove-001.jsonl")
        msg_event = events[-1]
        assert msg_event["event"] == "msg"
        assert msg_event["role"] == "user"
        assert msg_event["content"] == "Prove this theorem..."

    def test_record_assistant_message_with_tool_calls(self, tmp_path: Path) -> None:
        """Test recording an assistant message with tool calls."""
        run_dir = tmp_path / "test-run"
        session = SessionRecorder(run_dir)
        session.start(branch="main", base_commit="abc123")
        agent = session.register_agent("prove-001", "prove")

        tool_calls = [
            {"name": "read_file", "args": {"path": "Theorem.lean"}},
            {"name": "bash", "args": {"command": "lake build"}},
        ]
        agent.record("assistant", "I'll read the file first...", tool_calls=tool_calls)

        events = _read_jsonl(run_dir / "agents" / "prove-001.jsonl")
        msg_event = events[-1]
        assert msg_event["event"] == "msg"
        assert msg_event["role"] == "assistant"
        assert msg_event["content"] == "I'll read the file first..."
        assert msg_event["tool_calls"] == tool_calls

    def test_record_tool_call(self, tmp_path: Path) -> None:
        """Test recording a tool call result."""
        run_dir = tmp_path / "test-run"
        session = SessionRecorder(run_dir)
        session.start(branch="main", base_commit="abc123")
        agent = session.register_agent("prove-001", "prove")

        agent.record_tool(
            "read_file",
            {"path": "Theorem.lean"},
            "theorem foo : 1 = 1 := rfl",
            duration_ms=42.5,
        )

        events = _read_jsonl(run_dir / "agents" / "prove-001.jsonl")
        tool_event = events[-1]
        assert tool_event["event"] == "tool"
        assert tool_event["name"] == "read_file"
        assert tool_event["args"] == {"path": "Theorem.lean"}
        assert tool_event["result"] == "theorem foo : 1 = 1 := rfl"
        assert tool_event["duration_ms"] == 42.5

    def test_increment_iteration(self, tmp_path: Path) -> None:
        """Test iteration counter."""
        run_dir = tmp_path / "test-run"
        session = SessionRecorder(run_dir)
        session.start(branch="main", base_commit="abc123")
        agent = session.register_agent("prove-001", "prove")

        assert agent.increment_iteration() == 1
        assert agent.increment_iteration() == 2
        assert agent.increment_iteration() == 3

    def test_done_writes_done_event_and_notifies_session(self, tmp_path: Path) -> None:
        """Test that done() writes event and notifies session."""
        run_dir = tmp_path / "test-run"
        session = SessionRecorder(run_dir)
        session.start(branch="main", base_commit="abc123")
        agent = session.register_agent("prove-001", "prove")

        agent.increment_iteration()
        agent.increment_iteration()
        agent.done("done")

        # Check agent file
        agent_events = _read_jsonl(run_dir / "agents" / "prove-001.jsonl")
        done_event = agent_events[-1]
        assert done_event["event"] == "done"
        assert done_event["status"] == "done"
        assert done_event["iterations"] == 2

        # Notify session (as the coordinator would)
        session.record_agent_done("prove-001", "done", iterations=2)

        # Check session file got the event
        session_events = _read_jsonl(run_dir / "session.jsonl")
        agent_done_event = session_events[-1]
        assert agent_done_event["event"] == "agent_done"
        assert agent_done_event["agent_id"] == "prove-001"
        assert agent_done_event["status"] == "done"
        assert agent_done_event["iterations"] == 2

    def test_done_with_error(self, tmp_path: Path) -> None:
        """Test done() with error status."""
        run_dir = tmp_path / "test-run"
        session = SessionRecorder(run_dir)
        session.start(branch="main", base_commit="abc123")
        agent = session.register_agent("prove-001", "prove")

        agent.done("error", error="Connection timeout")

        agent_events = _read_jsonl(run_dir / "agents" / "prove-001.jsonl")
        done_event = agent_events[-1]
        assert done_event["status"] == "error"
        assert done_event["error"] == "Connection timeout"


# =============================================================================
# Factory Function Tests
# =============================================================================


class TestCreateSessionRecorder:
    """Tests for create_session_recorder factory."""

    def test_creates_recorder_with_run_name(self, tmp_path: Path) -> None:
        """Test creating recorder with explicit run name."""
        recorder = create_session_recorder(tmp_path, run_name="my-test-run")

        assert recorder.run_dir == tmp_path / "my-test-run"
        assert recorder.run_dir.exists()

    def test_creates_recorder_with_auto_generated_name(self, tmp_path: Path) -> None:
        """Test creating recorder with auto-generated name."""
        recorder = create_session_recorder(tmp_path)

        # Name should be timestamp-based
        assert recorder.run_dir.parent == tmp_path
        assert recorder.run_dir.exists()
        # Name format: YYYYMMDD-HHMMSS
        name = recorder.run_dir.name
        assert len(name) == 15  # YYYYMMDD-HHMMSS


# =============================================================================
# Integration Tests
# =============================================================================


class TestRecordingIntegration:
    """Integration tests for complete recording workflows."""

    def test_full_session_workflow(self, tmp_path: Path) -> None:
        """Test a complete recording workflow."""
        run_dir = tmp_path / "integration-test"
        session = SessionRecorder(run_dir)

        # Start session
        session.start(branch="fg/test", base_commit="deadbeef")

        # Register agents
        prove1 = session.register_agent("prove-001", "prove", {"model": "claude"})
        prove2 = session.register_agent("prove-002", "prove", {"model": "claude"})

        # Simulate prove1 work
        prove1.record("user", "Prove theorem A")
        prove1.record("assistant", "Let me analyze...", tool_calls=[{"name": "file_read", "args": {"path": "A.lean"}}])
        prove1.record_tool("file_read", {"path": "A.lean"}, "theorem A...", 50)
        prove1.flush()
        prove1.increment_iteration()
        prove1.record("assistant", "Done!")
        prove1.done("done")
        session.record_agent_done("prove-001", "done")

        # Simulate prove2 work with error
        prove2.record("user", "Prove theorem B")
        prove2.increment_iteration()
        prove2.done("error", error="API timeout")
        session.record_agent_done("prove-002", "error")

        # Record a PR submission
        session.record_pr_submitted(
            pr_id="pr-001",
            agent_id="prove-001",
            branch_name="fg/test-pr",
            agent_type="prove",
            chapter_id="ch1",
            diff="+new line\n-old line",
        )

        # Finalize
        session.finalize("completed")

        # Verify structure
        assert (run_dir / "session.jsonl").exists()
        assert (run_dir / "agents" / "prove-001.jsonl").exists()
        assert (run_dir / "agents" / "prove-002.jsonl").exists()

        # Verify session events (no agent_start, only agent_done)
        session_events = _read_jsonl(run_dir / "session.jsonl")
        event_types = [e["event"] for e in session_events]
        assert event_types == [
            "session_start",
            "agent_done",
            "agent_done",
            "pr_submitted",
            "session_end",
        ]

        # Verify prove1 completed successfully
        prove1_events = _read_jsonl(run_dir / "agents" / "prove-001.jsonl")
        assert prove1_events[-1]["status"] == "done"
        assert prove1_events[-1]["iterations"] == 1

        # Verify prove2 failed
        prove2_events = _read_jsonl(run_dir / "agents" / "prove-002.jsonl")
        assert prove2_events[-1]["status"] == "error"
        assert prove2_events[-1]["error"] == "API timeout"


# =============================================================================
# Helpers
# =============================================================================


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file and return list of events."""
    events = []
    with open(path) as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line))
    return events
