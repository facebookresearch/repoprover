# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

#!/usr/bin/env python3
"""Count input and output tokens for agents in a formalization repository.

Analyzes repoprover format: runs/<run_name>/agents/*.jsonl with 'input_tokens'/'output_tokens' in msg events

Usage:
    python count_tokens.py <formalization_repo_path>
    python count_tokens.py /path/to/leanenv/runs/20260216-073547

Output:
    - Per-agent token counts (input and output)
    - Total tokens across all agents
    - Breakdown by agent type (if available)
    - Breakdown by agent outcome (successful/unsuccessful/errored)
"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


def log_status(msg: str, end: str = "\n") -> None:
    """Print a status message to stderr."""
    print(msg, file=sys.stderr, end=end, flush=True)


class AgentOutcome(Enum):
    """Agent outcome categories."""

    # Successful outcomes
    MERGED = "merged"  # PR was successfully merged
    APPROVED = "approved"  # PR was approved but not yet merged

    # Unsuccessful outcomes (more granular)
    MAX_ITERATIONS = "max_iterations"  # Agent hit iteration limit
    NO_PR = "no_pr"  # Agent completed (done/fix/issue) but didn't submit a PR
    NO_PR_BLOCKED = "no_pr_blocked"  # Agent blocked - hit dead end
    MERGE_CONFLICT = "merge_conflict"  # PR couldn't merge due to conflicts
    BUILD_FAILED = "build_failed"  # PR's build failed
    REVIEW_REJECTED = "review_rejected"  # PR was rejected in review
    PENDING = "pending"  # PR still pending review (run didn't complete)

    # Error outcomes
    ERRORED = "errored"  # Agent errored during execution

    # Incomplete/Unknown outcomes
    INCOMPLETE = "incomplete"  # Agent never finished (no done event, still running)
    UNKNOWN = "unknown"  # Cannot determine outcome (unexpected status)

    @property
    def is_successful(self) -> bool:
        return self in (AgentOutcome.MERGED, AgentOutcome.APPROVED)

    @property
    def is_unsuccessful(self) -> bool:
        return self in (
            AgentOutcome.MAX_ITERATIONS,
            AgentOutcome.NO_PR,
            AgentOutcome.NO_PR_BLOCKED,
            AgentOutcome.MERGE_CONFLICT,
            AgentOutcome.BUILD_FAILED,
            AgentOutcome.REVIEW_REJECTED,
            AgentOutcome.PENDING,
        )

    @property
    def is_errored(self) -> bool:
        return self == AgentOutcome.ERRORED


@dataclass
class AgentTokenStats:
    """Token statistics for a single agent."""

    agent_id: str
    agent_type: str
    input_tokens: int = 0
    output_tokens: int = 0
    iterations: int = 0
    last_msg_tokens: int = 0  # Total tokens in the last message
    status: str = "unknown"  # done, error, max_iterations, etc.
    outcome: AgentOutcome = AgentOutcome.UNKNOWN

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class RunStats:
    """Statistics for an entire run."""

    run_path: Path
    agents: dict[str, AgentTokenStats] = field(default_factory=dict)

    @property
    def total_input_tokens(self) -> int:
        return sum(a.input_tokens for a in self.agents.values())

    @property
    def total_output_tokens(self) -> int:
        return sum(a.output_tokens for a in self.agents.values())

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens


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


def parse_session_events(run_dir: Path) -> dict[str, Any]:
    """Parse session.jsonl to extract agent outcomes.

    Returns a dict with:
        - merged_agents: set of agent_ids that were successfully merged
        - approved_agents: set of agent_ids with approved PRs (but not yet merged)
        - failed_agents: dict mapping agent_id to failure_reason
        - rejected_agents: set of agent_ids whose PRs were rejected
        - pending_agents: set of agent_ids with PRs still pending
        - agent_pr_map: dict mapping agent_id to list of pr_ids
        - agent_status: dict mapping agent_id to status from agent_done event
    """
    session_file = run_dir / "session.jsonl"
    result = {
        "merged_agents": set(),
        "approved_agents": set(),
        "failed_agents": {},  # agent_id -> failure_reason
        "rejected_agents": set(),
        "pending_agents": set(),
        "agent_pr_map": defaultdict(list),
        "pr_agent_map": {},  # pr_id -> agent_id
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
                # Initially mark as pending
                result["pending_agents"].add(agent_id)

        elif event_type == "merge_completed":
            agent_id = event.get("agent_id")
            pr_id = event.get("pr_id")
            success = event.get("success", False)
            failure_reason = event.get("failure_reason", "unknown")

            # Try to get agent_id from pr_id if not directly available
            if not agent_id and pr_id:
                agent_id = result["pr_agent_map"].get(pr_id)

            if agent_id:
                # Remove from pending/approved since we have a final result
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

    Categories:
    - MERGED: Agent's PR was successfully merged
    - APPROVED: Agent's PR was approved but not yet merged
    - MAX_ITERATIONS: Agent hit iteration limit
    - NO_PR: Agent completed (done/fix/issue) but didn't submit a PR
    - NO_PR_BLOCKED: Agent blocked - hit dead end
    - MERGE_CONFLICT: PR couldn't merge due to conflicts
    - BUILD_FAILED: PR's build failed
    - REVIEW_REJECTED: PR was rejected in review
    - PENDING: PR still pending review
    - ERRORED: Agent encountered an error during execution
    - INCOMPLETE: Agent never finished (no done event)
    - UNKNOWN: Cannot determine outcome (unexpected status)
    """
    # Prefer session status over agent JSONL status
    session_status = session_data.get("agent_status", {}).get(agent_id)
    if session_status:
        agent_status = session_status

    # Check for errors first
    if has_error or agent_status == "error":
        return AgentOutcome.ERRORED

    # Check if merged
    if agent_id in session_data["merged_agents"]:
        return AgentOutcome.MERGED

    # Check if approved (but not merged yet)
    if agent_id in session_data["approved_agents"]:
        return AgentOutcome.APPROVED

    # Check if PR was rejected
    if agent_id in session_data["rejected_agents"]:
        return AgentOutcome.REVIEW_REJECTED

    # Check if PR failed with specific reason
    if agent_id in session_data["failed_agents"]:
        failure_reason = session_data["failed_agents"][agent_id]
        if "conflict" in failure_reason.lower():
            return AgentOutcome.MERGE_CONFLICT
        elif "build" in failure_reason.lower():
            return AgentOutcome.BUILD_FAILED
        else:
            # Generic failure - treat as build failed
            return AgentOutcome.BUILD_FAILED

    # Check if PR is still pending
    if agent_id in session_data["pending_agents"]:
        return AgentOutcome.PENDING

    # Check agent status for specific failure modes
    if agent_status == "max_iterations":
        return AgentOutcome.MAX_ITERATIONS

    # Agent completed but no PR submitted
    if agent_status == "done":
        if agent_id in session_data["agent_pr_map"]:
            # PR submitted but no outcome recorded - still pending
            return AgentOutcome.PENDING
        else:
            # No PR submitted
            return AgentOutcome.NO_PR

    # Other statuses like fix, issue - still No PR
    if agent_status in ("fix", "issue"):
        return AgentOutcome.NO_PR

    # Blocked status - separate category
    if agent_status == "blocked":
        return AgentOutcome.NO_PR_BLOCKED

    # No done event found - agent never finished
    if not has_done_event:
        return AgentOutcome.INCOMPLETE

    # Truly unknown - unexpected status value
    return AgentOutcome.UNKNOWN


def analyze_run(run_dir: Path) -> RunStats:
    """Analyze a repoprover-style run directory with JSONL agent files."""
    stats = RunStats(run_path=run_dir)

    agents_dir = run_dir / "agents"
    if not agents_dir.exists():
        return stats

    # First, parse session events to get outcome data
    session_data = parse_session_events(run_dir)

    jsonl_files = list(agents_dir.glob("*.jsonl"))
    total_files = len(jsonl_files)

    for i, jsonl_file in enumerate(jsonl_files, 1):
        if i % 10 == 0 or i == total_files:
            log_status(f"  [{run_dir.name}] Processing agent {i}/{total_files}...", end="\r")

        agent_id = jsonl_file.stem
        events = parse_jsonl_file(jsonl_file)

        agent_type = "unknown"
        input_tokens = 0
        output_tokens = 0
        iterations = 0
        last_msg_tokens = 0
        agent_status = "unknown"
        has_error = False
        has_done_event = False

        for event in events:
            event_type = event.get("event", "")

            if event_type == "start":
                agent_type = event.get("agent_type", "unknown")
            elif event_type == "msg":
                msg_input = event.get("input_tokens", 0)
                msg_output = event.get("output_tokens", 0)
                if msg_input:
                    input_tokens += msg_input
                if msg_output:
                    output_tokens += msg_output
                # Track last message's total tokens
                last_msg_tokens = msg_input + msg_output

                role = event.get("role", "")
                if role == "assistant":
                    iterations += 1

            elif event_type == "done":
                has_done_event = True
                if "total_input_tokens" in event:
                    input_tokens = max(input_tokens, event["total_input_tokens"])
                if "total_output_tokens" in event:
                    output_tokens = max(output_tokens, event["total_output_tokens"])
                agent_status = event.get("status", "unknown")
                if event.get("error"):
                    has_error = True

        outcome = determine_agent_outcome(agent_id, agent_status, has_error, has_done_event, session_data)

        stats.agents[agent_id] = AgentTokenStats(
            agent_id=agent_id,
            agent_type=agent_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            iterations=iterations,
            last_msg_tokens=last_msg_tokens,
            status=agent_status,
            outcome=outcome,
        )

    if total_files > 0:
        log_status(f"  [{run_dir.name}] Processed {total_files} agents.        ")

    return stats


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


def format_m_0(n: int | float) -> str:
    """Format a token count in millions (M) with 0 decimals."""
    return f"{n / 1_000_000:.0f}"


def format_m_1(n: int | float) -> str:
    """Format a token count in millions (M) with 1 decimal."""
    return f"{n / 1_000_000:.1f}"


def format_k_0(n: int | float) -> str:
    """Format a token count in thousands (K) with 0 decimals."""
    return f"{n / 1_000:.0f}"


def format_k_1(n: int | float) -> str:
    """Format a token count in thousands (K) with 1 decimal."""
    return f"{n / 1_000:.1f}"


def capitalize_outcome(name: str) -> str:
    """Capitalize outcome name for display."""
    return name.replace("_", " ").title()


def format_agent_type(name: str) -> str:
    """Format agent type name for display."""
    # Special cases
    if name.lower() == "engineering reviewer":
        return "Eng. Reviewer"
    return name.title()


def print_outcome_breakdown_tex(agents: list[AgentTokenStats], title: str = "Token Stats by Outcome") -> None:
    """Print token stats broken down by agent outcome as LaTeX table.

    Uses simplified categories:
    - Merged: PR merged successfully
    - Approved: PR approved, run ended before merge
    - Max Revisions: Exhausted revisions (merge_conflict + build_failed + review_rejected)
    - Max Iterations: Agent hit iteration limit
    - No PR: Agent completed without submitting PR (done/fix/issue status)
    - No PR (blocked): Agent blocked - hit dead end
    - Pending: Run ended mid-flight (PR submitted)
    - Incomplete: Run ended mid-flight (no done event)
    - Aborted: Pending + Incomplete combined
    - Errored: Backend/API error
    """
    outcome_stats = compute_outcome_stats(agents)

    if not outcome_stats:
        return

    # Build simplified categories by aggregating detailed outcomes
    simplified: dict[str, dict[str, int | float]] = {}

    def add_to_cat(cat: str, s: dict[str, int | float]) -> None:
        if cat not in simplified:
            simplified[cat] = {"count": 0, "input": 0, "output": 0, "iterations": 0}
        simplified[cat]["count"] += s["count"]
        simplified[cat]["input"] += s["input_tokens"]
        simplified[cat]["output"] += s["output_tokens"]
        simplified[cat]["iterations"] += s["iterations"]

    # Map detailed outcomes to simplified categories
    for outcome_name, s in outcome_stats.items():
        if outcome_name == "merged":
            add_to_cat("Merged", s)
        elif outcome_name == "approved":
            add_to_cat("Approved", s)
        elif outcome_name in ("merge_conflict", "build_failed", "review_rejected"):
            add_to_cat("Max Revisions", s)
        elif outcome_name == "max_iterations":
            add_to_cat("Max Iterations", s)
        elif outcome_name == "no_pr":
            add_to_cat("No PR", s)
        elif outcome_name == "no_pr_blocked":
            add_to_cat("No PR (blocked)", s)
        elif outcome_name == "pending":
            add_to_cat("Pending", s)
        elif outcome_name == "incomplete":
            add_to_cat("Incomplete", s)
        elif outcome_name == "errored":
            add_to_cat("Errored", s)
        elif outcome_name == "unknown":
            add_to_cat("Unknown", s)

    # Compute Aborted = Pending + Incomplete + Unknown
    aborted = {"count": 0, "input": 0, "output": 0, "iterations": 0}
    for cat in ("Pending", "Incomplete", "Unknown"):
        if cat in simplified:
            aborted["count"] += simplified[cat]["count"]
            aborted["input"] += simplified[cat]["input"]
            aborted["output"] += simplified[cat]["output"]
            aborted["iterations"] += simplified[cat]["iterations"]

    print(f"% {title}")
    print(r"\begin{table}[tbp]")
    print(r"\centering")
    print(r"\begin{tabular}{lrrrrrrrr}")
    print(r"\toprule")
    print(
        r"\textbf{Outcome} & \textbf{Count} & \textbf{In (M)} & \textbf{Out (M)} & "
        r"\textbf{Total (M)} & \textbf{Avg In (K)} & \textbf{Avg Out (K)} & "
        r"\textbf{Turns} & \textbf{Avg Turns} \\"
    )
    print(r"\midrule")

    # Print in order (Unknown excluded - included in Aborted)
    cat_order = [
        "Merged",
        "Approved",
        "Max Revisions",
        "Max Iterations",
        "No PR",
        "No PR (blocked)",
        "Pending",
        "Incomplete",
        "Errored",
    ]

    total_count = 0
    total_input = 0
    total_output = 0
    total_iterations = 0
    errored_stats = simplified.get("Errored", {"count": 0, "input": 0, "output": 0, "iterations": 0})

    for cat in cat_order:
        if cat in simplified:
            s = simplified[cat]
            c = s["count"]
            total_count += c
            total_input += s["input"]
            total_output += s["output"]
            total_iterations += s["iterations"]
            print(
                f"{cat} & {c} & {format_m_0(s['input'])} & "
                f"{format_m_1(s['output'])} & {format_m_0(s['input'] + s['output'])} & "
                f"{format_k_0(s['input'] / c)} & {format_k_1(s['output'] / c)} & "
                f"{s['iterations']} & {s['iterations'] / c:.1f} \\\\"
            )

    # Print Aborted subtotal (Pending + Incomplete)
    if aborted["count"] > 0:
        c = aborted["count"]
        print(
            f"Aborted & {c} & {format_m_0(aborted['input'])} & "
            f"{format_m_1(aborted['output'])} & "
            f"{format_m_0(aborted['input'] + aborted['output'])} & "
            f"{format_k_0(aborted['input'] / c)} & "
            f"{format_k_1(aborted['output'] / c)} & "
            f"{aborted['iterations']} & {aborted['iterations'] / c:.1f} \\\\"
        )

    print(r"\midrule")
    if total_count > 0:
        print(
            f"\\textbf{{Total}} & {total_count} & {format_m_0(total_input)} & "
            f"{format_m_1(total_output)} & {format_m_0(total_input + total_output)} & "
            f"{format_k_0(total_input / total_count)} & {format_k_1(total_output / total_count)} & "
            f"{total_iterations} & {total_iterations / total_count:.1f} \\\\"
        )

    if errored_stats["count"] > 0:
        non_err_count = total_count - errored_stats["count"]
        non_err_input = total_input - errored_stats["input"]
        non_err_output = total_output - errored_stats["output"]
        non_err_iters = total_iterations - errored_stats["iterations"]
        if non_err_count > 0:
            print(
                f"\\textbf{{Total - Errored}} & {non_err_count} & {format_m_0(non_err_input)} & "
                f"{format_m_1(non_err_output)} & {format_m_0(non_err_input + non_err_output)} & "
                f"{format_k_0(non_err_input / non_err_count)} & "
                f"{format_k_1(non_err_output / non_err_count)} & "
                f"{non_err_iters} & {non_err_iters / non_err_count:.1f} \\\\"
            )

    print(r"\bottomrule")
    print(r"\end{tabular}")
    print(r"\caption{" + title + "}")
    print(r"\end{table}")
    print()


def print_agent_type_breakdown_tex(agents: list[AgentTokenStats], title: str = "Token Stats by Agent Type") -> None:
    """Print token stats by agent type as LaTeX table (excluding errored)."""
    non_errored = [a for a in agents if a.outcome != AgentOutcome.ERRORED]
    errored_count = len(agents) - len(non_errored)

    if not non_errored:
        return

    by_type: dict[str, list[AgentTokenStats]] = defaultdict(list)
    for agent in non_errored:
        by_type[agent.agent_type].append(agent)

    print(f"% {title} (excluding {errored_count} errored)")
    print(r"\begin{table}[tbp]")
    print(r"\centering")
    print(r"\begin{tabular}{lrrrrrrrr}")
    print(r"\toprule")
    print(
        r"\textbf{Agent Type} & \textbf{Count} & \textbf{In (M)} & \textbf{Out (M)} & "
        r"\textbf{Total (M)} & \textbf{Avg In (K)} & \textbf{Avg Out (K)} & "
        r"\textbf{Turns} & \textbf{Avg Turns} \\"
    )
    print(r"\midrule")

    for agent_type in sorted(by_type.keys()):
        type_agents = by_type[agent_type]
        count = len(type_agents)
        total_input = sum(a.input_tokens for a in type_agents)
        total_output = sum(a.output_tokens for a in type_agents)
        total = total_input + total_output
        total_iters = sum(a.iterations for a in type_agents)
        display_type = format_agent_type(agent_type)
        print(
            f"{display_type} & {count} & {format_m_0(total_input)} & "
            f"{format_m_1(total_output)} & {format_m_0(total)} & "
            f"{format_k_0(total_input / count if count else 0)} & "
            f"{format_k_1(total_output / count if count else 0)} & "
            f"{total_iters} & {total_iters / count if count else 0:.1f} \\\\"
        )

    print(r"\midrule")
    count = len(non_errored)
    total_input = sum(a.input_tokens for a in non_errored)
    total_output = sum(a.output_tokens for a in non_errored)
    total_iters = sum(a.iterations for a in non_errored)
    print(
        f"\\textbf{{Total}} & {count} & {format_m_0(total_input)} & "
        f"{format_m_1(total_output)} & {format_m_0(total_input + total_output)} & "
        f"{format_k_0(total_input / count if count else 0)} & "
        f"{format_k_1(total_output / count if count else 0)} & "
        f"{total_iters} & {total_iters / count if count else 0:.1f} \\\\"
    )

    print(r"\bottomrule")
    print(r"\end{tabular}")
    print(r"\caption{" + title + f" (excluding {errored_count} errored)" + "}")
    print(r"\end{table}")
    print()


def compute_outcome_stats(
    agents: list[AgentTokenStats],
) -> dict[str, dict[str, int | float]]:
    """Compute aggregate stats by outcome category."""
    stats: dict[str, dict[str, int | float]] = {}

    for outcome in AgentOutcome:
        outcome_agents = [a for a in agents if a.outcome == outcome]
        if outcome_agents:
            count = len(outcome_agents)
            input_tokens = sum(a.input_tokens for a in outcome_agents)
            output_tokens = sum(a.output_tokens for a in outcome_agents)
            total_tokens = sum(a.total_tokens for a in outcome_agents)
            iterations = sum(a.iterations for a in outcome_agents)
            last_msg_tokens = sum(a.last_msg_tokens for a in outcome_agents)

            stats[outcome.value] = {
                "count": count,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "iterations": iterations,
                "last_msg_tokens": last_msg_tokens,
                "avg_input": input_tokens / count if count > 0 else 0,
                "avg_output": output_tokens / count if count > 0 else 0,
                "avg_iterations": iterations / count if count > 0 else 0,
                "avg_last_msg": last_msg_tokens / count if count > 0 else 0,
            }

    return stats


def print_outcome_breakdown(agents: list[AgentTokenStats], title: str = "Breakdown by Outcome") -> None:
    """Print token stats broken down by agent outcome."""
    outcome_stats = compute_outcome_stats(agents)

    if not outcome_stats:
        return

    print(f"\n{title}")
    print("-" * 145)
    print(
        f"{'Outcome':<16} {'Count':>6} {'In (M)':>10} {'Out (M)':>11} {'Total (M)':>13} "
        f"{'Avg In (K)':>12} {'Avg Out (K)':>13} {'Turns':>7} {'Avg Turns':>9} {'Avg Len (K)':>12}"
    )
    print("-" * 145)

    # Define order for outcomes (grouped by category)
    outcome_order = [
        # Successful
        "merged",
        "approved",
        # Unsuccessful - granular
        "max_iterations",
        "no_pr",
        "merge_conflict",
        "build_failed",
        "review_rejected",
        "pending",
        # Errors
        "errored",
        # Incomplete/Unknown
        "incomplete",
        "unknown",
    ]

    total_count = 0
    total_input = 0
    total_output = 0
    total_iterations = 0
    total_last_msg = 0

    # Track subtotals for categories
    successful_stats = {"count": 0, "input": 0, "output": 0, "iterations": 0, "last_msg": 0}
    unsuccessful_stats = {"count": 0, "input": 0, "output": 0, "iterations": 0, "last_msg": 0}
    errored_stats = {"count": 0, "input": 0, "output": 0, "iterations": 0, "last_msg": 0}
    aborted_stats = {"count": 0, "input": 0, "output": 0, "iterations": 0, "last_msg": 0}

    for outcome_name in outcome_order:
        if outcome_name in outcome_stats:
            s = outcome_stats[outcome_name]
            print(
                f"{outcome_name:<16} {s['count']:>6} {format_m_0(s['input_tokens']):>10} "
                f"{format_m_1(s['output_tokens']):>11} {format_m_0(s['total_tokens']):>13} "
                f"{format_k_0(s['avg_input']):>12} {format_k_1(s['avg_output']):>13} "
                f"{s['iterations']:>7} {s['avg_iterations']:>9.1f} {format_k_1(s['avg_last_msg']):>12}"
            )
            total_count += s["count"]
            total_input += s["input_tokens"]
            total_output += s["output_tokens"]
            total_iterations += s["iterations"]
            total_last_msg += s["last_msg_tokens"]

            # Accumulate category subtotals
            outcome = AgentOutcome(outcome_name)
            if outcome.is_successful:
                successful_stats["count"] += s["count"]
                successful_stats["input"] += s["input_tokens"]
                successful_stats["output"] += s["output_tokens"]
                successful_stats["iterations"] += s["iterations"]
                successful_stats["last_msg"] += s["last_msg_tokens"]
            elif outcome.is_unsuccessful:
                unsuccessful_stats["count"] += s["count"]
                unsuccessful_stats["input"] += s["input_tokens"]
                unsuccessful_stats["output"] += s["output_tokens"]
                unsuccessful_stats["iterations"] += s["iterations"]
                unsuccessful_stats["last_msg"] += s["last_msg_tokens"]
            elif outcome.is_errored:
                errored_stats["count"] += s["count"]
                errored_stats["input"] += s["input_tokens"]
                errored_stats["output"] += s["output_tokens"]
                errored_stats["iterations"] += s["iterations"]
                errored_stats["last_msg"] += s["last_msg_tokens"]

            # Track aborted (pending + incomplete)
            if outcome_name in ("pending", "incomplete"):
                aborted_stats["count"] += s["count"]
                aborted_stats["input"] += s["input_tokens"]
                aborted_stats["output"] += s["output_tokens"]
                aborted_stats["iterations"] += s["iterations"]
                aborted_stats["last_msg"] += s["last_msg_tokens"]

    # Print Aborted subtotal (pending + incomplete)
    if aborted_stats["count"] > 0:
        c = aborted_stats["count"]
        print(
            f"{'> ABORTED':<16} {c:>6} {format_m_0(aborted_stats['input']):>10} "
            f"{format_m_1(aborted_stats['output']):>11} "
            f"{format_m_0(aborted_stats['input'] + aborted_stats['output']):>13} "
            f"{format_k_0(aborted_stats['input'] / c):>12} "
            f"{format_k_1(aborted_stats['output'] / c):>13} "
            f"{aborted_stats['iterations']:>7} {aborted_stats['iterations'] / c:>9.1f} "
            f"{format_k_1(aborted_stats['last_msg'] / c):>12}"
        )

    print("-" * 145)

    # Print category subtotals
    if successful_stats["count"] > 0:
        c = successful_stats["count"]
        print(
            f"{'> SUCCESSFUL':<16} {c:>6} {format_m_0(successful_stats['input']):>10} "
            f"{format_m_1(successful_stats['output']):>11} "
            f"{format_m_0(successful_stats['input'] + successful_stats['output']):>13} "
            f"{format_k_0(successful_stats['input'] / c):>12} "
            f"{format_k_1(successful_stats['output'] / c):>13} "
            f"{successful_stats['iterations']:>7} {successful_stats['iterations'] / c:>9.1f} "
            f"{format_k_1(successful_stats['last_msg'] / c):>12}"
        )

    if unsuccessful_stats["count"] > 0:
        c = unsuccessful_stats["count"]
        print(
            f"{'> UNSUCCESSFUL':<16} {c:>6} {format_m_0(unsuccessful_stats['input']):>10} "
            f"{format_m_1(unsuccessful_stats['output']):>11} "
            f"{format_m_0(unsuccessful_stats['input'] + unsuccessful_stats['output']):>13} "
            f"{format_k_0(unsuccessful_stats['input'] / c):>12} "
            f"{format_k_1(unsuccessful_stats['output'] / c):>13} "
            f"{unsuccessful_stats['iterations']:>7} {unsuccessful_stats['iterations'] / c:>9.1f} "
            f"{format_k_1(unsuccessful_stats['last_msg'] / c):>12}"
        )

    print("-" * 145)
    total = total_input + total_output
    avg_input = total_input / total_count if total_count > 0 else 0
    avg_output = total_output / total_count if total_count > 0 else 0
    avg_iterations = total_iterations / total_count if total_count > 0 else 0
    avg_last_msg = total_last_msg / total_count if total_count > 0 else 0
    print(
        f"{'TOTAL':<16} {total_count:>6} {format_m_0(total_input):>10} "
        f"{format_m_1(total_output):>11} {format_m_0(total):>13} "
        f"{format_k_0(avg_input):>12} {format_k_1(avg_output):>13} "
        f"{total_iterations:>7} {avg_iterations:>9.1f} {format_k_1(avg_last_msg):>12}"
    )

    # Print total - errored
    if errored_stats["count"] > 0:
        non_err_count = total_count - errored_stats["count"]
        non_err_input = total_input - errored_stats["input"]
        non_err_output = total_output - errored_stats["output"]
        non_err_iters = total_iterations - errored_stats["iterations"]
        non_err_last_msg = total_last_msg - errored_stats["last_msg"]
        if non_err_count > 0:
            print(
                f"{'TOTAL - ERRORED':<16} {non_err_count:>6} {format_m_0(non_err_input):>10} "
                f"{format_m_1(non_err_output):>11} {format_m_0(non_err_input + non_err_output):>13} "
                f"{format_k_0(non_err_input / non_err_count):>12} "
                f"{format_k_1(non_err_output / non_err_count):>13} "
                f"{non_err_iters:>7} {non_err_iters / non_err_count:>9.1f} "
                f"{format_k_1(non_err_last_msg / non_err_count):>12}"
            )


def print_run_stats(stats: RunStats, verbose: bool = False) -> None:
    """Print statistics for a single run."""
    print(f"\n{'=' * 130}")
    print(f"Run: {stats.run_path.name}")
    print(f"{'=' * 130}")

    if not stats.agents:
        print("No agents found.")
        return

    if stats.total_tokens == 0:
        print("(No token data recorded in this run)")
        return

    # Print outcome breakdown first
    print_outcome_breakdown(list(stats.agents.values()), "Token Stats by Agent Outcome")

    # Group agents by type (excluding errored agents)
    non_errored_agents = [a for a in stats.agents.values() if a.outcome != AgentOutcome.ERRORED]
    errored_count = len(stats.agents) - len(non_errored_agents)

    by_type: dict[str, list[AgentTokenStats]] = defaultdict(list)
    for agent in non_errored_agents:
        by_type[agent.agent_type].append(agent)

    print(f"\n{'Token Stats by Agent Type (excluding errored)'}")
    if errored_count > 0:
        print(f"(Excluded {errored_count} errored agent(s))")
    print("-" * 130)
    print(
        f"{'Agent Type':<20} {'Count':>6} {'In (M)':>10} {'Out (M)':>11} {'Total (M)':>13} "
        f"{'Avg In (K)':>12} {'Avg Out (K)':>13} {'Turns':>7} {'Avg Turns':>9}"
    )
    print("-" * 130)

    for agent_type in sorted(by_type.keys()):
        agents = by_type[agent_type]
        count = len(agents)
        total_input = sum(a.input_tokens for a in agents)
        total_output = sum(a.output_tokens for a in agents)
        total = total_input + total_output
        total_iters = sum(a.iterations for a in agents)
        print(
            f"{agent_type:<20} {count:>6} {format_m_0(total_input):>10} "
            f"{format_m_1(total_output):>11} {format_m_0(total):>13} "
            f"{format_k_0(total_input / count if count else 0):>12} "
            f"{format_k_1(total_output / count if count else 0):>13} "
            f"{total_iters:>7} {total_iters / count if count else 0:>9.1f}"
        )

    print("-" * 130)
    total_iters = sum(a.iterations for a in non_errored_agents)
    count = len(non_errored_agents)
    total_input = sum(a.input_tokens for a in non_errored_agents)
    total_output = sum(a.output_tokens for a in non_errored_agents)
    print(
        f"{'TOTAL':<20} {count:>6} {format_m_0(total_input):>10} "
        f"{format_m_1(total_output):>11} {format_m_0(total_input + total_output):>13} "
        f"{format_k_0(total_input / count if count else 0):>12} "
        f"{format_k_1(total_output / count if count else 0):>13} "
        f"{total_iters:>7} {total_iters / count if count else 0:>9.1f}"
    )

    if verbose:
        print(f"\n{'Individual Agents':^130}")
        print("-" * 130)
        print(f"{'Agent ID':<40} {'Type':<12} {'Outcome':<16} {'In (M)':>10} {'Out (M)':>11} {'Turns':>6}")
        print("-" * 130)

        for agent_id, agent in sorted(stats.agents.items()):
            print(
                f"{agent_id[:40]:<40} {agent.agent_type[:12]:<12} {agent.outcome.value[:16]:<16} "
                f"{format_m_0(agent.input_tokens):>10} {format_m_1(agent.output_tokens):>11} "
                f"{agent.iterations:>6}"
            )


def main():
    parser = argparse.ArgumentParser(
        description="Count input and output tokens for agents in a formalization repository."
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Path to the formalization repo, runs directory, or individual run",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show per-agent breakdown",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--tex",
        action="store_true",
        help="Output results as LaTeX tables",
    )

    args = parser.parse_args()

    if not args.path.exists():
        print(f"Error: Path {args.path} does not exist", file=sys.stderr)
        sys.exit(1)

    runs = find_all_runs(args.path)

    if not runs:
        print(f"No runs found in {args.path}", file=sys.stderr)
        sys.exit(1)

    all_stats = []

    for i, run_dir in enumerate(runs, 1):
        log_status(f"Analyzing run {i}/{len(runs)}: {run_dir.name}")
        stats = analyze_run(run_dir)
        all_stats.append(stats)

    log_status("Analysis complete. Generating report...")

    # Collect all agents across all runs for grand totals
    all_agents = [agent for stats in all_stats for agent in stats.agents.values()]

    if args.json:
        output = {
            "runs": [
                {
                    "path": str(stats.run_path),
                    "total_input_tokens": stats.total_input_tokens,
                    "total_output_tokens": stats.total_output_tokens,
                    "total_tokens": stats.total_tokens,
                    "by_outcome": compute_outcome_stats(list(stats.agents.values())),
                    "agents": [
                        {
                            "agent_id": a.agent_id,
                            "agent_type": a.agent_type,
                            "input_tokens": a.input_tokens,
                            "output_tokens": a.output_tokens,
                            "iterations": a.iterations,
                            "status": a.status,
                            "outcome": a.outcome.value,
                        }
                        for a in stats.agents.values()
                    ],
                }
                for stats in all_stats
            ],
            "grand_total": {
                "input_tokens": sum(s.total_input_tokens for s in all_stats),
                "output_tokens": sum(s.total_output_tokens for s in all_stats),
                "total_tokens": sum(s.total_tokens for s in all_stats),
                "by_outcome": compute_outcome_stats(all_agents),
            },
        }
        print(json.dumps(output, indent=2))
    elif args.tex:
        # LaTeX output
        print("% Auto-generated LaTeX tables from count_tokens.py")
        print()

        if len(all_stats) == 1:
            # Single run
            stats = all_stats[0]
            print_outcome_breakdown_tex(list(stats.agents.values()), f"Token Stats by Outcome ({stats.run_path.name})")
            print_agent_type_breakdown_tex(
                list(stats.agents.values()), f"Token Stats by Agent Type ({stats.run_path.name})"
            )
        else:
            # Multiple runs - show grand totals
            print_outcome_breakdown_tex(all_agents, "Token Stats by Outcome (All Runs)")
            print_agent_type_breakdown_tex(all_agents, "Token Stats by Agent Type (All Runs)")
    else:
        for stats in all_stats:
            print_run_stats(stats, verbose=args.verbose)

        if len(all_stats) > 1:
            print(f"\n{'=' * 130}")
            print("GRAND TOTAL ACROSS ALL RUNS")
            print(f"{'=' * 130}")

            grand_input = sum(s.total_input_tokens for s in all_stats)
            grand_output = sum(s.total_output_tokens for s in all_stats)
            grand_total = grand_input + grand_output
            total_agents = sum(len(s.agents) for s in all_stats)
            total_iterations = sum(a.iterations for a in all_agents)

            print(f"Runs: {len(all_stats)}")
            print(f"Agents: {total_agents}")
            print(f"Input Tokens: {format_m_0(grand_input)} M ({grand_input:,})")
            print(f"Output Tokens: {format_m_1(grand_output)} M ({grand_output:,})")
            print(f"Total Tokens: {format_m_0(grand_total)} M ({grand_total:,})")
            print(f"Total API Turns: {total_iterations:,}")
            print(f"Avg Turns/Agent: {total_iterations / total_agents:.1f}" if total_agents > 0 else "")

            # Print grand total outcome breakdown
            print_outcome_breakdown(all_agents, "\nGrand Total by Outcome")

            # Print grand total by agent type (excluding errored agents)
            non_errored_agents = [a for a in all_agents if a.outcome != AgentOutcome.ERRORED]
            errored_count = len(all_agents) - len(non_errored_agents)

            by_type: dict[str, list[AgentTokenStats]] = defaultdict(list)
            for agent in non_errored_agents:
                by_type[agent.agent_type].append(agent)

            print(f"\n{'Grand Total by Agent Type (excluding errored)'}")
            if errored_count > 0:
                print(f"(Excluded {errored_count} errored agent(s))")
            print("-" * 145)
            print(
                f"{'Agent Type':<20} {'Count':>6} {'In (M)':>10} {'Out (M)':>11} {'Total (M)':>13} "
                f"{'Avg In (K)':>12} {'Avg Out (K)':>13} {'Turns':>7} {'Avg Turns':>9} {'Avg Len (K)':>12}"
            )
            print("-" * 145)

            for agent_type in sorted(by_type.keys()):
                agents = by_type[agent_type]
                count = len(agents)
                total_input = sum(a.input_tokens for a in agents)
                total_output = sum(a.output_tokens for a in agents)
                total = total_input + total_output
                total_iters = sum(a.iterations for a in agents)
                total_last_msg = sum(a.last_msg_tokens for a in agents)
                print(
                    f"{agent_type:<20} {count:>6} {format_m_0(total_input):>10} "
                    f"{format_m_1(total_output):>11} {format_m_0(total):>13} "
                    f"{format_k_0(total_input / count if count else 0):>12} "
                    f"{format_k_1(total_output / count if count else 0):>13} "
                    f"{total_iters:>7} {total_iters / count if count else 0:>9.1f} "
                    f"{format_k_1(total_last_msg / count if count else 0):>12}"
                )

            print("-" * 145)
            total_iters = sum(a.iterations for a in non_errored_agents)
            count = len(non_errored_agents)
            total_input = sum(a.input_tokens for a in non_errored_agents)
            total_output = sum(a.output_tokens for a in non_errored_agents)
            total_last_msg = sum(a.last_msg_tokens for a in non_errored_agents)
            print(
                f"{'TOTAL':<20} {count:>6} {format_m_0(total_input):>10} "
                f"{format_m_1(total_output):>11} {format_m_0(total_input + total_output):>13} "
                f"{format_k_0(total_input / count if count else 0):>12} "
                f"{format_k_1(total_output / count if count else 0):>13} "
                f"{total_iters:>7} {total_iters / count if count else 0:>9.1f} "
                f"{format_k_1(total_last_msg / count if count else 0):>12}"
            )


if __name__ == "__main__":
    main()
