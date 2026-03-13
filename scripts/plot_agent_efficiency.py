# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

#!/usr/bin/env python3
"""Plot agent efficiency over time from repoprover runs.

Analyzes agent outcomes from runs/<run_name>/agents/*.jsonl files.
Produces publication-quality plots showing:
- Fail rate over time (anything not Merged)
- Vertical grey lines indicating orchestrator restarts (new run directories)

Follows the same plotting style as ~/alg-comb-exps/scripts/gen_figures.py

Usage:
    python plot_agent_efficiency.py <formalization_repo_path> [--out OUTPUT_DIR]
    python plot_agent_efficiency.py /path/to/leanenv --out ./assets
"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


# ============================================================
# Configuration (following gen_figures.py style)
# ============================================================

COL_WIDTH = 3.4  # inches (1/2 column width)
FIG_HEIGHT = 2.4  # inches

plt.rcParams.update(
    {
        "font.size": 8,
        "axes.titlesize": 9,
        "axes.labelsize": 8,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "legend.fontsize": 7,
    }
)


# ============================================================
# Data structures
# ============================================================


class AgentOutcome(Enum):
    MERGED = "merged"
    APPROVED = "approved"
    MAX_ITERATIONS = "max_iterations"
    NO_PR = "no_pr"  # status="done"/"fix"/"issue" but no PR submitted
    NO_PR_BLOCKED = "no_pr_blocked"  # status="blocked" - hit dead end
    MERGE_CONFLICT = "merge_conflict"
    BUILD_FAILED = "build_failed"
    REVIEW_REJECTED = "review_rejected"
    PENDING = "pending"
    ERRORED = "errored"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"

    @property
    def is_successful(self) -> bool:
        return self in (AgentOutcome.MERGED, AgentOutcome.APPROVED)


@dataclass
class AgentStats:
    """Stats for an agent."""

    agent_id: str
    agent_type: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    final_outcome: AgentOutcome = AgentOutcome.UNKNOWN
    run_name: str = ""  # Which run directory this came from
    total_tokens: int = 0  # Total input + output tokens

    @property
    def duration(self) -> timedelta | None:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return None


@dataclass
class RunInfo:
    """Info about an orchestrator run (a runs/ subdirectory)."""

    run_dir: Path
    run_name: str
    start_time: datetime | None = None  # Parsed from directory name or first event
    end_time: datetime | None = None  # Last agent end time

    @staticmethod
    def parse_run_start_time(run_name: str) -> datetime | None:
        """Parse run start time from directory name like '20260216-073521'."""
        try:
            return datetime.strptime(run_name, "%Y%m%d-%H%M%S")
        except ValueError:
            return None


# ============================================================
# Parsing functions
# ============================================================


def log_status(msg: str, end: str = "\n") -> None:
    print(msg, file=sys.stderr, end=end, flush=True)


def parse_jsonl_file(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file and return a list of events."""
    events = []
    try:
        with open(path, encoding="utf-8") as f:
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


def parse_timestamp(ts_str: str) -> datetime | None:
    """Parse an ISO format timestamp, returning naive UTC datetime."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        # Convert to naive UTC for consistent comparisons
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def parse_session_events(run_dir: Path) -> dict[str, Any]:
    """Parse session.jsonl to extract agent outcomes."""
    session_file = run_dir / "session.jsonl"
    result = {
        "merged_agents": set(),
        "approved_agents": set(),
        "failed_agents": {},
        "rejected_agents": set(),
        "pending_agents": set(),
        "agent_pr_map": defaultdict(list),
        "pr_agent_map": {},
        "agent_status": {},  # agent_id -> status from agent_done event
    }

    if not session_file.exists():
        return result

    events = parse_jsonl_file(session_file)

    for event in events:
        event_type = event.get("event", "")

        if event_type == "agent_done":
            # Capture the actual status from session.jsonl (fix, issue, blocked, done, etc.)
            agent_id = event.get("agent_id")
            status = event.get("status", "unknown")
            if agent_id:
                result["agent_status"][agent_id] = status

        elif event_type == "pr_submitted":
            agent_id = event.get("agent_id")
            pr_id = event.get("pr_id")
            if agent_id and pr_id:
                result["agent_pr_map"][agent_id].append(pr_id)
                result["pr_agent_map"][pr_id] = agent_id
                result["pending_agents"].add(agent_id)

        elif event_type == "merge_completed":
            agent_id = event.get("agent_id")
            pr_id = event.get("pr_id")
            success = event.get("success", False)
            failure_reason = event.get("failure_reason", "unknown")

            if not agent_id and pr_id:
                agent_id = result["pr_agent_map"].get(pr_id)

            if agent_id:
                result["pending_agents"].discard(agent_id)
                result["approved_agents"].discard(agent_id)

                if success:
                    result["merged_agents"].add(agent_id)
                else:
                    result["failed_agents"][agent_id] = failure_reason

        elif event_type == "review_completed":
            agent_id = event.get("agent_id")
            verdict = event.get("combined_verdict", "")
            if agent_id:
                if verdict == "approve":
                    result["approved_agents"].add(agent_id)
                    result["pending_agents"].discard(agent_id)
                elif verdict == "reject":
                    result["rejected_agents"].add(agent_id)
                    result["pending_agents"].discard(agent_id)

    return result


def determine_agent_outcome(
    agent_id: str,
    agent_status: str,
    has_error: bool,
    has_done_event: bool,
    session_data: dict[str, Any],
) -> AgentOutcome:
    """Determine the outcome category for an agent.

    Uses the status from session.jsonl agent_done event (if available)
    which has the correct ContributorResult status (fix, issue, blocked, etc.)
    rather than the agent JSONL status which is often just "done".
    """
    # Prefer session status over agent JSONL status
    session_status = session_data.get("agent_status", {}).get(agent_id)
    if session_status:
        agent_status = session_status

    if has_error or agent_status == "error":
        return AgentOutcome.ERRORED

    if agent_id in session_data["merged_agents"]:
        return AgentOutcome.MERGED

    if agent_id in session_data["approved_agents"]:
        return AgentOutcome.APPROVED

    if agent_id in session_data["rejected_agents"]:
        return AgentOutcome.REVIEW_REJECTED

    if agent_id in session_data["failed_agents"]:
        failure_reason = session_data["failed_agents"][agent_id]
        if "conflict" in failure_reason.lower():
            return AgentOutcome.MERGE_CONFLICT
        elif "build" in failure_reason.lower():
            return AgentOutcome.BUILD_FAILED
        else:
            return AgentOutcome.BUILD_FAILED

    if agent_id in session_data["pending_agents"]:
        return AgentOutcome.PENDING

    if agent_status == "max_iterations":
        return AgentOutcome.MAX_ITERATIONS

    if agent_status == "done":
        if agent_id in session_data["agent_pr_map"]:
            return AgentOutcome.PENDING
        else:
            return AgentOutcome.NO_PR

    if agent_status in ("fix", "issue"):
        return AgentOutcome.NO_PR

    if agent_status == "blocked":
        return AgentOutcome.NO_PR_BLOCKED

    if not has_done_event:
        return AgentOutcome.INCOMPLETE

    return AgentOutcome.UNKNOWN


def analyze_agent_file(jsonl_file: Path, session_data: dict[str, Any], run_name: str = "") -> AgentStats | None:
    """Analyze a single agent JSONL file."""
    agent_id = jsonl_file.stem
    events = parse_jsonl_file(jsonl_file)

    if not events:
        return None

    stats = AgentStats(agent_id=agent_id, agent_type="unknown", run_name=run_name)

    # Overall tracking for final outcome determination
    final_status = "unknown"
    has_error = False
    has_done_event = False

    for event in events:
        event_type = event.get("event", "")
        ts = parse_timestamp(event.get("ts", ""))

        if event_type == "start":
            agent_type = event.get("agent_type", "unknown")
            stats.agent_type = agent_type
            if stats.start_time is None:
                stats.start_time = ts

        elif event_type == "done":
            has_done_event = True
            final_status = event.get("status", "unknown")
            if event.get("error"):
                has_error = True
            stats.end_time = ts
            # Capture token counts
            input_tokens = event.get("total_input_tokens", 0) or 0
            output_tokens = event.get("total_output_tokens", 0) or 0
            stats.total_tokens = input_tokens + output_tokens

        elif event_type == "msg" and ts:
            # Update end time to latest message
            if stats.end_time is None or ts > stats.end_time:
                stats.end_time = ts

    # Determine final outcome
    stats.final_outcome = determine_agent_outcome(agent_id, final_status, has_error, has_done_event, session_data)

    return stats


def analyze_run(run_dir: Path) -> tuple[RunInfo, list[AgentStats]]:
    """Analyze a repoprover-style run directory."""
    run_name = run_dir.name
    run_info = RunInfo(
        run_dir=run_dir,
        run_name=run_name,
        start_time=RunInfo.parse_run_start_time(run_name),
    )

    agents_dir = run_dir / "agents"
    if not agents_dir.exists():
        return run_info, []

    session_data = parse_session_events(run_dir)
    jsonl_files = list(agents_dir.glob("*.jsonl"))

    agents = []
    for jsonl_file in jsonl_files:
        stats = analyze_agent_file(jsonl_file, session_data, run_name=run_name)
        if stats is None:
            continue
        agents.append(stats)

    # If we couldn't parse run start from dir name, use first agent start
    if run_info.start_time is None and agents:
        first_start = min((a.start_time for a in agents if a.start_time), default=None)
        run_info.start_time = first_start

    # Compute run end time from last agent end
    if agents:
        end_times = [a.end_time for a in agents if a.end_time]
        if end_times:
            run_info.end_time = max(end_times)

    return run_info, agents


def compute_run_duration_hours(agents: list[AgentStats]) -> float | None:
    """Compute total run duration from first agent start to last agent end."""
    start_times = [a.start_time for a in agents if a.start_time]
    end_times = [a.end_time for a in agents if a.end_time]

    if not start_times or not end_times:
        return None

    first_start = min(start_times)
    last_end = max(end_times)
    duration = last_end - first_start
    return duration.total_seconds() / 3600


def is_valid_run(run_dir: Path) -> bool:
    """Check if the directory is a valid repoprover run directory."""
    agents_dir = run_dir / "agents"
    return agents_dir.exists() and any(agents_dir.glob("*.jsonl"))


def find_all_runs(base_path: Path) -> list[Path]:
    """Find all run directories under a base path."""
    base_path = Path(base_path)
    runs = []

    log_status(f"Scanning {base_path}...")

    if is_valid_run(base_path):
        runs.append(base_path)
        return runs

    runs_dir = base_path / "runs"
    if runs_dir.exists():
        log_status("Found runs directory, scanning...")
        for run_dir in sorted(runs_dir.iterdir()):
            if run_dir.is_dir() and is_valid_run(run_dir):
                runs.append(run_dir)

    log_status(f"Found {len(runs)} run(s)")
    return runs


# ============================================================
# Runtime conversion (skip inactive periods)
# ============================================================


def compute_runtime_mapping(
    run_infos: list[RunInfo],
) -> tuple[list[tuple[datetime, datetime, float]], float]:
    """
    Compute mapping from wall-clock time to cumulative runtime hours.

    Returns:
        - List of (run_start, run_end, runtime_offset_hours) for each run
        - Total runtime hours

    This allows converting any wall-clock timestamp to runtime hours
    by finding which run it belongs to and adding the offset.
    """
    # Sort runs by start time
    sorted_runs = [r for r in run_infos if r.start_time and r.end_time]
    sorted_runs.sort(key=lambda r: r.start_time)

    mapping = []
    cumulative_hours = 0.0

    for run_info in sorted_runs:
        run_start = run_info.start_time
        run_end = run_info.end_time
        run_duration_hours = (run_end - run_start).total_seconds() / 3600

        mapping.append((run_start, run_end, cumulative_hours))
        cumulative_hours += run_duration_hours

    return mapping, cumulative_hours


def wallclock_to_runtime(
    ts: datetime,
    runtime_mapping: list[tuple[datetime, datetime, float]],
) -> float | None:
    """Convert wall-clock timestamp to runtime hours (skipping inactive periods)."""
    for run_start, run_end, offset_hours in runtime_mapping:
        if run_start <= ts <= run_end:
            elapsed = (ts - run_start).total_seconds() / 3600
            return offset_hours + elapsed
        # Handle timestamps slightly before run_end (could be exactly at boundary)
        if ts < run_start:
            return None  # Before first run
    return None  # After all runs or in gap


def get_restart_runtime_hours(
    run_infos: list[RunInfo],
    runtime_mapping: list[tuple[datetime, datetime, float]],
) -> list[float]:
    """Get restart points in runtime hours (where each new run starts)."""
    sorted_runs = [r for r in run_infos if r.start_time and r.end_time]
    sorted_runs.sort(key=lambda r: r.start_time)

    restart_hours = []
    for i, (run_start, run_end, offset_hours) in enumerate(runtime_mapping):
        if i > 0:  # Skip first run
            restart_hours.append(offset_hours)

    return restart_hours


# ============================================================
# Plotting functions (following gen_figures.py style)
# ============================================================


def savefig_both(fig: plt.Figure, filename_base: str) -> None:
    """Save figure as both PNG and PDF."""
    fig.savefig(f"{filename_base}.png", dpi=300, facecolor="white")
    fig.savefig(f"{filename_base}.pdf", facecolor="white")
    print(f"Saved {Path(filename_base).name}.png/.pdf")


def add_restart_lines(ax: plt.Axes, restart_hours: list[float]) -> None:
    """Add vertical lines at orchestrator restart times (in runtime hours)."""
    for rh in restart_hours:
        ax.axvline(x=rh, color="#000000", linewidth=0.5, linestyle=":", alpha=0.4)


def plot_fail_rate_over_time(
    agents: list[AgentStats],
    filename_base: str,
    restart_hours: list[float],
    runtime_mapping: list[tuple[datetime, datetime, float]],
    window_size: int = 50,
) -> None:
    """Plot fail rate over time using a rolling window.

    Excludes errored agents to match the paper's methodology.
    Fail = anything not MERGED (among non-errored agents).
    X-axis is runtime hours (skipping inactive periods).
    """
    # Sort agents by start time, exclude errored
    sorted_agents = [a for a in agents if a.start_time is not None and a.final_outcome != AgentOutcome.ERRORED]
    sorted_agents.sort(key=lambda a: a.start_time)

    if len(sorted_agents) < window_size:
        log_status(f"Not enough agents ({len(sorted_agents)}) for window size {window_size}")
        return

    # Compute rolling fail rate
    times = []
    fail_rates = []

    for i in range(window_size, len(sorted_agents) + 1):
        window = sorted_agents[i - window_size : i]
        # Fail = anything not MERGED
        fails = sum(1 for a in window if a.final_outcome != AgentOutcome.MERGED)
        fail_rate = fails / window_size
        runtime_h = wallclock_to_runtime(window[-1].start_time, runtime_mapping)
        if runtime_h is not None:
            times.append(runtime_h)
            fail_rates.append(fail_rate * 100)

    fig, ax = plt.subplots(figsize=(COL_WIDTH, FIG_HEIGHT))
    ax.fill_between(times, fail_rates, alpha=0.7, color="#F44336", edgecolor="none", linewidth=0)
    add_restart_lines(ax, restart_hours)

    ax.set_xlabel("Runtime (h)")
    ax.set_ylabel("Fail rate (%)")
    ax.set_ylim(0, 100)
    ax.set_xlim(0, max(times) if times else 1)
    ax.grid(axis="y", alpha=0.3, linewidth=0.5)
    fig.tight_layout()
    savefig_both(fig, filename_base)
    plt.close()


def plot_outcome_distribution_over_time(
    agents: list[AgentStats],
    filename_base: str,
    restart_hours: list[float],
    runtime_mapping: list[tuple[datetime, datetime, float]],
    window_size: int = 100,
    weight_by_tokens: bool = False,
) -> None:
    """Plot stacked area chart of outcome distribution over time.

    X-axis is runtime hours (skipping inactive periods).

    Args:
        weight_by_tokens: If True, weight each agent by its total_tokens instead of counting equally.
    """
    sorted_agents = [a for a in agents if a.start_time is not None]
    sorted_agents.sort(key=lambda a: a.start_time)

    if len(sorted_agents) < window_size:
        log_status(f"Not enough agents ({len(sorted_agents)}) for window size {window_size}")
        return

    # Categories matching the paper's Table (excluding Errored from display)
    # No PR split into regular vs blocked
    categories = [
        ("Merged", [AgentOutcome.MERGED]),
        ("Approved", [AgentOutcome.APPROVED]),
        (
            "Max Revisions",
            [
                AgentOutcome.MERGE_CONFLICT,
                AgentOutcome.BUILD_FAILED,
                AgentOutcome.REVIEW_REJECTED,
            ],
        ),
        ("Max Iterations", [AgentOutcome.MAX_ITERATIONS]),
        ("No PR", [AgentOutcome.NO_PR]),
        ("No PR (blocked)", [AgentOutcome.NO_PR_BLOCKED]),
        ("Aborted", [AgentOutcome.PENDING, AgentOutcome.INCOMPLETE, AgentOutcome.UNKNOWN]),
        # Errored excluded from stacked chart to match paper's total
    ]

    colors = {
        "Merged": "#4CAF50",
        "Approved": "#8BC34A",
        "Max Revisions": "#FF9800",
        "Max Iterations": "#FFC107",
        "No PR": "#9E9E9E",
        "No PR (blocked)": "#616161",  # Darker grey
        "Aborted": "#E57373",  # Light red
    }

    times = []
    data = {cat: [] for cat, _ in categories}

    for i in range(window_size, len(sorted_agents) + 1):
        window = sorted_agents[i - window_size : i]
        # Exclude errored from the denominator to match paper
        non_errored = [a for a in window if a.final_outcome != AgentOutcome.ERRORED]

        if weight_by_tokens:
            # Weight by tokens
            total_weight = sum(a.total_tokens for a in non_errored) or 1
        else:
            # Equal weight per agent
            total_weight = len(non_errored) if non_errored else 1

        runtime_h = wallclock_to_runtime(window[-1].start_time, runtime_mapping)
        if runtime_h is None:
            continue

        times.append(runtime_h)

        for cat_name, outcomes in categories:
            if weight_by_tokens:
                weight = sum(a.total_tokens for a in non_errored if a.final_outcome in outcomes)
            else:
                weight = sum(1 for a in non_errored if a.final_outcome in outcomes)
            data[cat_name].append((weight / total_weight) * 100)

    fig, ax = plt.subplots(figsize=(COL_WIDTH, FIG_HEIGHT))

    # Stack from bottom up
    bottom = np.zeros(len(times))
    for cat_name, _ in categories:
        values = np.array(data[cat_name])
        ax.fill_between(
            times, bottom, bottom + values, alpha=0.7, color=colors[cat_name], edgecolor="none", linewidth=0
        )
        bottom += values

    add_restart_lines(ax, restart_hours)

    ax.set_xlabel("Runtime (h)")
    ylabel = "Share by tokens (%)" if weight_by_tokens else "Share (%)"
    ax.set_ylabel(ylabel)
    ax.set_ylim(0, 100)
    ax.set_xlim(0, max(times) if times else 1)

    # Legend (reverse order so it matches visual stacking)
    handles = [plt.Rectangle((0, 0), 1, 1, fc=colors[cat], alpha=0.7) for cat, _ in reversed(categories)]
    labels = [cat for cat, _ in reversed(categories)]
    ax.legend(
        handles,
        labels,
        loc="center left",
        bbox_to_anchor=(1.02, 0.5),
        fontsize=5,
        framealpha=0.8,
    )

    fig.tight_layout()
    savefig_both(fig, filename_base)
    plt.close()


def plot_token_usage_over_time(
    agents: list[AgentStats],
    filename_base: str,
    restart_hours: list[float],
    runtime_mapping: list[tuple[datetime, datetime, float]],
    window_size: int = 100,
) -> None:
    """Plot stacked area chart of absolute token usage over time.

    X-axis is runtime hours (skipping inactive periods).
    Y-axis is total tokens (in millions) used in the rolling window.
    """
    sorted_agents = [a for a in agents if a.start_time is not None]
    sorted_agents.sort(key=lambda a: a.start_time)

    if len(sorted_agents) < window_size:
        log_status(f"Not enough agents ({len(sorted_agents)}) for window size {window_size}")
        return

    # Categories matching the paper's Table (excluding Errored from display)
    categories = [
        ("Merged", [AgentOutcome.MERGED]),
        ("Approved", [AgentOutcome.APPROVED]),
        (
            "Max Revisions",
            [
                AgentOutcome.MERGE_CONFLICT,
                AgentOutcome.BUILD_FAILED,
                AgentOutcome.REVIEW_REJECTED,
            ],
        ),
        ("Max Iterations", [AgentOutcome.MAX_ITERATIONS]),
        ("No PR", [AgentOutcome.NO_PR]),
        ("No PR (blocked)", [AgentOutcome.NO_PR_BLOCKED]),
        ("Aborted", [AgentOutcome.PENDING, AgentOutcome.INCOMPLETE, AgentOutcome.UNKNOWN]),
    ]

    colors = {
        "Merged": "#4CAF50",
        "Approved": "#8BC34A",
        "Max Revisions": "#FF9800",
        "Max Iterations": "#FFC107",
        "No PR": "#9E9E9E",
        "No PR (blocked)": "#616161",
        "Aborted": "#E57373",
    }

    times = []
    data = {cat: [] for cat, _ in categories}

    for i in range(window_size, len(sorted_agents) + 1):
        window = sorted_agents[i - window_size : i]
        # Exclude errored from display
        non_errored = [a for a in window if a.final_outcome != AgentOutcome.ERRORED]

        runtime_h = wallclock_to_runtime(window[-1].start_time, runtime_mapping)
        if runtime_h is None:
            continue

        times.append(runtime_h)

        for cat_name, outcomes in categories:
            # Sum tokens in millions
            tokens = sum(a.total_tokens for a in non_errored if a.final_outcome in outcomes)
            data[cat_name].append(tokens / 1e6)

    fig, ax = plt.subplots(figsize=(COL_WIDTH, FIG_HEIGHT))

    # Stack from bottom up
    bottom = np.zeros(len(times))
    for cat_name, _ in categories:
        values = np.array(data[cat_name])
        ax.fill_between(
            times, bottom, bottom + values, alpha=0.7, color=colors[cat_name], edgecolor="none", linewidth=0
        )
        bottom += values

    add_restart_lines(ax, restart_hours)

    ax.set_xlabel("Runtime (h)")
    ax.set_ylabel("Tokens (M)")
    ax.set_ylim(bottom=0)
    ax.set_xlim(0, max(times) if times else 1)

    # Legend (reverse order so it matches visual stacking)
    handles = [plt.Rectangle((0, 0), 1, 1, fc=colors[cat], alpha=0.7) for cat, _ in reversed(categories)]
    labels = [cat for cat, _ in reversed(categories)]
    ax.legend(
        handles,
        labels,
        loc="center left",
        bbox_to_anchor=(1.02, 0.5),
        fontsize=5,
        framealpha=0.8,
    )

    fig.tight_layout()
    savefig_both(fig, filename_base)
    plt.close()


def plot_token_breakdown_combined(
    agents: list[AgentStats],
    filename_base: str,
    restart_hours: list[float],
    runtime_mapping: list[tuple[datetime, datetime, float]],
    window_size: int = 100,
) -> None:
    """Plot combined figure: left = absolute tokens, right = token share.

    Both plots share a single legend.
    """
    sorted_agents = [a for a in agents if a.start_time is not None]
    sorted_agents.sort(key=lambda a: a.start_time)

    if len(sorted_agents) < window_size:
        log_status(f"Not enough agents ({len(sorted_agents)}) for window size {window_size}")
        return

    categories = [
        ("Merged", [AgentOutcome.MERGED]),
        ("Approved", [AgentOutcome.APPROVED]),
        (
            "Max Revisions",
            [
                AgentOutcome.MERGE_CONFLICT,
                AgentOutcome.BUILD_FAILED,
                AgentOutcome.REVIEW_REJECTED,
            ],
        ),
        ("Max Iterations", [AgentOutcome.MAX_ITERATIONS]),
        ("No PR", [AgentOutcome.NO_PR]),
        ("No PR (blocked)", [AgentOutcome.NO_PR_BLOCKED]),
        ("Aborted", [AgentOutcome.PENDING, AgentOutcome.INCOMPLETE, AgentOutcome.UNKNOWN]),
    ]

    colors = {
        "Merged": "#4CAF50",
        "Approved": "#8BC34A",
        "Max Revisions": "#FF9800",
        "Max Iterations": "#FFC107",
        "No PR": "#9E9E9E",
        "No PR (blocked)": "#616161",
        "Aborted": "#E57373",
    }

    # Compute data for both plots
    times = []
    data_absolute = {cat: [] for cat, _ in categories}
    data_share = {cat: [] for cat, _ in categories}

    for i in range(window_size, len(sorted_agents) + 1):
        window = sorted_agents[i - window_size : i]
        non_errored = [a for a in window if a.final_outcome != AgentOutcome.ERRORED]

        runtime_h = wallclock_to_runtime(window[-1].start_time, runtime_mapping)
        if runtime_h is None:
            continue

        times.append(runtime_h)
        total_tokens = sum(a.total_tokens for a in non_errored) or 1

        for cat_name, outcomes in categories:
            tokens = sum(a.total_tokens for a in non_errored if a.final_outcome in outcomes)
            data_absolute[cat_name].append(tokens / 1e6)
            data_share[cat_name].append((tokens / total_tokens) * 100)

    # Create figure with two subplots
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(COL_WIDTH * 2 + 0.5, FIG_HEIGHT))

    # Left plot: absolute tokens
    bottom = np.zeros(len(times))
    for cat_name, _ in categories:
        values = np.array(data_absolute[cat_name])
        ax1.fill_between(
            times, bottom, bottom + values, alpha=0.7, color=colors[cat_name], edgecolor="none", linewidth=0
        )
        bottom += values

    add_restart_lines(ax1, restart_hours)
    ax1.set_xlabel("Runtime (h)")
    ax1.set_ylabel("Tokens (M)")
    ax1.set_ylim(bottom=0)
    ax1.set_xlim(0, max(times) if times else 1)

    # Right plot: token share
    bottom = np.zeros(len(times))
    for cat_name, _ in categories:
        values = np.array(data_share[cat_name])
        ax2.fill_between(
            times, bottom, bottom + values, alpha=0.7, color=colors[cat_name], edgecolor="none", linewidth=0
        )
        bottom += values

    add_restart_lines(ax2, restart_hours)
    ax2.set_xlabel("Runtime (h)")
    ax2.set_ylabel("Share (%)")
    ax2.set_ylim(0, 100)
    ax2.set_xlim(0, max(times) if times else 1)

    # Shared legend (outside, to the right)
    handles = [plt.Rectangle((0, 0), 1, 1, fc=colors[cat], alpha=0.7) for cat, _ in reversed(categories)]
    labels = [cat for cat, _ in reversed(categories)]
    fig.legend(
        handles,
        labels,
        loc="center right",
        fontsize=6,
        framealpha=0.8,
    )

    fig.tight_layout()
    fig.subplots_adjust(right=0.82, wspace=0.45)  # Make room for legend, adjust gap between plots
    savefig_both(fig, filename_base)
    plt.close()


def plot_cumulative_success(
    agents: list[AgentStats],
    filename_base: str,
    restart_hours: list[float],
    runtime_mapping: list[tuple[datetime, datetime, float]],
) -> None:
    """Plot cumulative success (merged) count over time.

    X-axis is runtime hours (skipping inactive periods).
    """
    sorted_agents = [a for a in agents if a.start_time is not None]
    sorted_agents.sort(key=lambda a: a.start_time)

    if not sorted_agents:
        return

    times = []
    cum_merged = []
    cum_total = []

    merged_count = 0
    total_count = 0

    for agent in sorted_agents:
        total_count += 1
        if agent.final_outcome == AgentOutcome.MERGED:
            merged_count += 1
        runtime_h = wallclock_to_runtime(agent.start_time, runtime_mapping)
        if runtime_h is not None:
            times.append(runtime_h)
            cum_merged.append(merged_count)
            cum_total.append(total_count)

    fig, ax = plt.subplots(figsize=(COL_WIDTH, FIG_HEIGHT))
    ax.fill_between(times, cum_merged, alpha=0.7, color="#4CAF50", label="Merged", edgecolor="none", linewidth=0)
    ax.fill_between(times, cum_total, alpha=0.3, color="#9E9E9E", label="Total", edgecolor="none", linewidth=0)
    add_restart_lines(ax, restart_hours)

    ax.set_xlabel("Runtime (h)")
    ax.set_ylabel("Count")
    ax.set_ylim(bottom=0)
    ax.set_xlim(0, max(times) if times else 1)
    ax.grid(axis="y", alpha=0.3, linewidth=0.5)
    ax.legend(loc="upper left", fontsize=6, framealpha=0.8)
    fig.tight_layout()
    savefig_both(fig, filename_base)
    plt.close()


# ============================================================
# Main
# ============================================================


def main():
    parser = argparse.ArgumentParser(description="Plot agent efficiency metrics from repoprover runs")
    parser.add_argument("path", help="Path to formalization repo or runs directory")
    parser.add_argument(
        "--out",
        "-o",
        default=".",
        help="Output directory for plots (default: current directory)",
    )
    parser.add_argument(
        "--window",
        "-w",
        type=int,
        default=50,
        help="Rolling window size for time series (default: 50)",
    )
    parser.add_argument(
        "--min-hours",
        type=float,
        default=1.0,
        help="Minimum run duration in hours to include (default: 1.0)",
    )
    args = parser.parse_args()

    base_path = Path(args.path)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    log_status(f"Analyzing {base_path}...")
    log_status(f"Output directory: {out_dir}")
    log_status(f"Minimum run duration: {args.min_hours} hours")
    log_status(f"Rolling window: {args.window}")

    # Find and analyze all runs
    run_dirs = find_all_runs(base_path)
    all_agents: list[AgentStats] = []
    run_infos: list[RunInfo] = []
    skipped_runs = 0

    for run_dir in run_dirs:
        log_status(f"Processing {run_dir.name}...", end="")
        run_info, agents = analyze_run(run_dir)

        # Filter out runs shorter than min_hours
        duration_hours = compute_run_duration_hours(agents)
        if duration_hours is None or duration_hours < args.min_hours:
            log_status(
                f" SKIPPED (duration: {duration_hours:.1f}h < {args.min_hours}h)"
                if duration_hours
                else " SKIPPED (no duration)"
            )
            skipped_runs += 1
            continue

        run_infos.append(run_info)
        all_agents.extend(agents)
        log_status(f" {len(agents)} agents, {duration_hours:.1f}h")

    log_status(f"\nTotal agents: {len(all_agents)}")
    log_status(f"Included runs: {len(run_infos)} (skipped {skipped_runs} short runs)")

    if not all_agents:
        log_status("No agents found!")
        return

    # Compute runtime mapping (wall-clock to runtime hours)
    runtime_mapping, total_runtime_hours = compute_runtime_mapping(run_infos)
    log_status(f"Total runtime: {total_runtime_hours:.1f} hours")

    # Compute restart positions in runtime hours
    restart_hours = get_restart_runtime_hours(run_infos, runtime_mapping)
    log_status(f"Restarts (new runs): {len(restart_hours)}")

    # Compute summary statistics
    merged = sum(1 for a in all_agents if a.final_outcome == AgentOutcome.MERGED)
    log_status(f"Merged: {merged} ({100 * merged / len(all_agents):.1f}%)")

    # Generate plots
    log_status("\nGenerating plots...")

    out = str(out_dir)

    log_status("  - Fail rate over time...")
    plot_fail_rate_over_time(all_agents, f"{out}/agent_fail_rate", restart_hours, runtime_mapping, args.window)

    log_status("  - Outcome distribution (by agent count)...")
    plot_outcome_distribution_over_time(
        all_agents,
        f"{out}/agent_outcomes",
        restart_hours,
        runtime_mapping,
        max(100, args.window),
        weight_by_tokens=False,
    )

    log_status("  - Outcome distribution (by tokens)...")
    plot_outcome_distribution_over_time(
        all_agents,
        f"{out}/agent_outcomes_by_tokens",
        restart_hours,
        runtime_mapping,
        max(100, args.window),
        weight_by_tokens=True,
    )

    log_status("  - Token usage (absolute)...")
    plot_token_usage_over_time(
        all_agents,
        f"{out}/agent_token_usage",
        restart_hours,
        runtime_mapping,
        max(100, args.window),
    )

    log_status("  - Token breakdown (combined)...")
    plot_token_breakdown_combined(
        all_agents,
        f"{out}/agent_token_breakdown",
        restart_hours,
        runtime_mapping,
        max(100, args.window),
    )

    log_status("  - Cumulative success...")
    plot_cumulative_success(all_agents, f"{out}/agent_cumulative", restart_hours, runtime_mapping)

    log_status("\nDone!")


if __name__ == "__main__":
    main()
