# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""FastAPI server for the repoprover trajectory viewer.

Usage:
    python -m repoprover.viewer [--port 8385] [--dir runs/]

Serves the viewer HTML at / and exposes:
    GET  /list                       — list run directories with metadata
    GET  /session?path=...           — load session.jsonl events
    GET  /agents?path=...            — list agent IDs in a run
    GET  /agent?path=...&id=...      — load specific agent dialog (on-demand)
    GET  /diff?path=...&file=...     — load external diff patch file
    WS   /ws?path=...                — WebSocket for live session updates
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

VIEWER_HTML = Path(__file__).parent / "viewer.html"

BASE_DIR: Path = Path(".")

app = FastAPI(title="Repoprover Trajectory Viewer", docs_url=None, redoc_url=None)


def _read_jsonl(path: Path) -> list[dict]:
    """Read all events from a JSONL file."""
    if not path.exists():
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def _extract_session_stats(session_events: list[dict]) -> dict:
    """Extract summary statistics from session events."""
    stats = {
        "agents": 0,
        "agents_done": 0,
        "agents_error": 0,
        "reviews_launched": 0,
        "builds_passed": 0,
        "builds_failed": 0,
        "status": "unknown",
        "duration_seconds": None,
        "diff_stats": {"+": 0, "-": 0},
        "prs_merged": 0,
        "branch": "",
        "base_commit": "",
        # Proof stats
        "total_theorems": 0,
        "proven_theorems": 0,
        "remaining_sorries": 0,
        # Issue counts
        "open_issues": 0,
        "closed_issues": 0,
        # Active agents by type
        "active_sketchers": 0,
        "active_provers": 0,
    }

    start_ts = None
    end_ts = None

    # Track unique agents to avoid double-counting
    registered_agents: set[str] = set()
    done_agents: set[str] = set()
    error_agents: set[str] = set()
    # Track agent types for active count
    agent_types: dict[str, str] = {}  # agent_id -> agent_type

    for event in session_events:
        event_type = event.get("event")

        if event_type == "session_start":
            stats["branch"] = event.get("branch", "")
            stats["base_commit"] = event.get("base_commit", "")
            start_ts = event.get("ts")
            stats["status"] = "in_progress"

        elif event_type in ("agent_start", "agent_registered", "agent_launched", "agent_resumed"):
            agent_id = event.get("agent_id")
            agent_type = event.get("agent_type", "unknown")
            if agent_id:
                registered_agents.add(agent_id)
                agent_types[agent_id] = agent_type

        elif event_type == "agent_done":
            agent_id = event.get("agent_id")
            if agent_id:
                done_agents.add(agent_id)
                if event.get("status") == "error":
                    error_agents.add(agent_id)

        elif event_type == "review_launched":
            stats["reviews_launched"] += 1

        elif event_type == "build_completed":
            if event.get("passed"):
                stats["builds_passed"] += 1
            else:
                stats["builds_failed"] += 1

        elif event_type == "merge_completed":
            # Count stats directly from merge_completed events
            if event.get("success"):
                stats["prs_merged"] += 1
                diff_stats = event.get("diff_stats", {})
                stats["diff_stats"]["+"] += diff_stats.get("+", 0)
                stats["diff_stats"]["-"] += diff_stats.get("-", 0)

        elif event_type == "proof_stats":
            # Update with latest proof statistics
            stats["total_theorems"] = event.get("total_theorems", 0)
            stats["proven_theorems"] = event.get("proven_theorems", 0)
            stats["remaining_sorries"] = event.get("remaining_sorries", 0)
            stats["open_issues"] = event.get("open_issues", 0)
            stats["closed_issues"] = event.get("closed_issues", 0)

        elif event_type == "session_end":
            stats["status"] = event.get("status", "completed")
            end_ts = event.get("ts")

    # Exclude reviewers from top-level agent counts (they're internal to the review pipeline)
    reviewer_ids = {aid for aid, atype in agent_types.items() if "reviewer" in atype}
    stats["agents"] = len(registered_agents - reviewer_ids)
    stats["agents_done"] = len(done_agents - reviewer_ids)
    stats["agents_error"] = len(error_agents - reviewer_ids)

    # Calculate active agents by type
    active_agents = registered_agents - done_agents
    for agent_id in active_agents:
        agent_type = agent_types.get(agent_id, "unknown")
        if agent_type == "sketch":
            stats["active_sketchers"] += 1
        elif agent_type == "prove":
            stats["active_provers"] += 1

    if start_ts and end_ts:
        try:
            start = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
            stats["duration_seconds"] = (end - start).total_seconds()
        except (ValueError, TypeError):
            pass

    return stats


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the viewer HTML."""
    if not VIEWER_HTML.exists():
        return HTMLResponse("<h1>viewer.html not found</h1>", status_code=404)
    return HTMLResponse(VIEWER_HTML.read_text())


@app.get("/list")
async def list_runs(include_stats: bool = True):
    """List all run directories with basic metadata.

    Returns only path, timestamp, and optionally summary stats (no agent content).
    """
    runs = []

    for run_dir in sorted(BASE_DIR.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue

        session_file = run_dir / "session.jsonl"
        if not session_file.exists():
            continue

        entry = {
            "path": run_dir.name,
            "mtime": session_file.stat().st_mtime,
        }

        if include_stats:
            events = _read_jsonl(session_file)
            entry["stats"] = _extract_session_stats(events)

        runs.append(entry)

    return runs


@app.get("/session")
async def get_session(path: str = Query(..., description="Run directory name")):
    """Load session.jsonl events for a run."""
    run_dir = BASE_DIR / path
    session_file = run_dir / "session.jsonl"

    if not session_file.exists():
        raise HTTPException(404, f"Session file not found: {session_file}")

    events = _read_jsonl(session_file)
    stats = _extract_session_stats(events)

    return {
        "events": events,
        "stats": stats,
        "mtime": session_file.stat().st_mtime,
    }


@app.get("/agents")
async def list_agents(path: str = Query(..., description="Run directory name")):
    """List agent IDs in a run (without loading dialog content).

    Returns list of agent metadata extracted from session events.
    """
    run_dir = BASE_DIR / path
    session_file = run_dir / "session.jsonl"
    agents_dir = run_dir / "agents"

    if not session_file.exists():
        raise HTTPException(404, f"Session file not found: {session_file}")

    session_events = _read_jsonl(session_file)
    agents: dict[str, dict] = {}
    reviews: dict[str, dict] = {}  # agent_id -> latest review

    # Pre-compute existing agent files to avoid O(n) filesystem checks
    existing_agent_files: set[str] = set()
    if agents_dir.exists():
        existing_agent_files = {f.stem for f in agents_dir.glob("*.jsonl")}

    # Track PR submissions: pr_id -> agent_id (to link merge_completed events)
    pr_to_agent: dict[str, str] = {}
    # Track PR status per agent
    pr_statuses: dict[str, str] = {}  # agent_id -> pr_status

    for event in session_events:
        event_type = event.get("event")

        if event_type in ("agent_start", "agent_registered", "agent_launched", "agent_resumed"):
            agent_id = event.get("agent_id")
            if agent_id:
                agents[agent_id] = {
                    "id": agent_id,
                    "type": event.get("agent_type", "unknown"),
                    "start_ts": event.get("ts"),
                    "status": "in_progress",
                    "iterations": 0,
                    "has_file": agent_id in existing_agent_files,
                }
                # For resumed agents, capture the PR status and diff_stats from the event
                if event_type == "agent_resumed":
                    pr_id = event.get("pr_id")
                    pr_status = event.get("pr_status")
                    diff_stats = event.get("diff_stats")
                    if pr_id:
                        pr_to_agent[pr_id] = agent_id
                    if pr_status:
                        pr_statuses[agent_id] = pr_status
                    if diff_stats:
                        agents[agent_id]["diff_stats"] = diff_stats

        elif event_type == "agent_done":
            agent_id = event.get("agent_id")
            if agent_id and agent_id in agents:
                agents[agent_id]["status"] = event.get("status", "done")
                agents[agent_id]["iterations"] = event.get("iterations", 0)
                agents[agent_id]["done_ts"] = event.get("ts")

        elif event_type == "agent_status_update":
            # Update agent status based on review outcome (e.g., pending_revision, approved, rejected)
            agent_id = event.get("agent_id")
            if agent_id and agent_id in agents:
                agents[agent_id]["status"] = event.get("status")

        elif event_type == "pr_submitted":
            agent_id = event.get("agent_id")
            pr_id = event.get("pr_id")
            if agent_id and pr_id:
                pr_to_agent[pr_id] = agent_id
                pr_statuses[agent_id] = "pending_review"
                # Extract diff stats if available
                if agent_id in agents:
                    agents[agent_id]["diff_stats"] = event.get("diff_stats", {"+": 0, "-": 0})

        elif event_type == "review_launched":
            agent_id = event.get("agent_id")
            if agent_id:
                pr_statuses[agent_id] = "in_review"

        elif event_type == "revision_started":
            agent_id = event.get("agent_id")
            if agent_id:
                pr_statuses[agent_id] = "revision_in_progress"

        elif event_type == "review_completed":
            agent_id = event.get("agent_id")
            if agent_id:
                reviews[agent_id] = {
                    "combined_verdict": event.get("combined_verdict"),
                    "math": event.get("math") or event.get("semantic"),
                    "engineering": event.get("engineering"),
                    "build_passed": event.get("build_passed"),
                    "build_error": event.get("build_error"),
                    "build_output": event.get("build_output"),
                    "ts": event.get("ts"),
                    "pr_id": event.get("pr_id"),
                }
                # Update PR status based on verdict
                verdict = event.get("combined_verdict", "")
                if verdict == "approve":
                    pr_statuses[agent_id] = "approved"
                elif verdict in ("reject", "request_changes"):
                    pr_statuses[agent_id] = "needs_revision"

        elif event_type == "merge_completed":
            agent_id = event.get("agent_id")
            if agent_id and event.get("success"):
                pr_statuses[agent_id] = "merged"

    # Attach reviews and PR status to agents (exact match on agent_id)
    for agent_id, review in reviews.items():
        if agent_id in agents:
            agents[agent_id]["review"] = review

    for agent_id, pr_status in pr_statuses.items():
        if agent_id in agents:
            agents[agent_id]["pr_status"] = pr_status

    return list(agents.values())


@app.get("/agent")
async def get_agent(
    path: str = Query(..., description="Run directory name"),
    id: str = Query(..., description="Agent ID"),
):
    """Load specific agent's dialog (on-demand).

    Returns all events from the agent's JSONL file, or from agent_resumed event
    if the agent file doesn't exist (for resumed runs).
    """
    run_dir = BASE_DIR / path
    agent_file = run_dir / "agents" / f"{id}.jsonl"

    events = []
    agent_meta = {
        "id": id,
        "type": "unknown",
        "status": "unknown",
        "iterations": 0,
    }
    mtime = 0

    if agent_file.exists():
        events = _read_jsonl(agent_file)
        mtime = agent_file.stat().st_mtime
    else:
        # Try to get dialog from agent_resumed event in session.jsonl
        session_file = run_dir / "session.jsonl"
        if session_file.exists():
            session_events = _read_jsonl(session_file)
            for event in session_events:
                if event.get("event") == "agent_resumed" and event.get("agent_id") == id:
                    # Use dialog from resumed event
                    events = event.get("dialog", [])
                    agent_meta["type"] = event.get("agent_type", "unknown")
                    agent_meta["status"] = "resumed"
                    agent_meta["pr_status"] = event.get("pr_status")
                    agent_meta["chapter_id"] = event.get("chapter_id")
                    agent_meta["theorem_name"] = event.get("theorem_name")
                    mtime = session_file.stat().st_mtime
                    break

    if not events:
        return {
            "meta": {"id": id, "type": "unknown", "status": "initializing", "iterations": 0},
            "events": [],
            "mtime": 0,
        }

    # Extract metadata from events
    for event in events:
        if event.get("event") == "start":
            agent_meta["type"] = event.get("agent_type", "unknown")
            agent_meta["config"] = event.get("config", {})
            agent_meta["start_ts"] = event.get("ts")
        elif event.get("event") == "done":
            agent_meta["status"] = event.get("status", "done")
            agent_meta["iterations"] = event.get("iterations", 0)
            agent_meta["error"] = event.get("error")
            agent_meta["done_ts"] = event.get("ts")

    return {
        "meta": agent_meta,
        "events": events,
        "mtime": mtime,
    }


@app.get("/pr-timeline")
async def get_pr_timeline(
    path: str = Query(..., description="Run directory name"),
    agent_id: str = Query(..., description="Agent ID to get PR timeline for"),
):
    """Get all chronological events for a PR/agent.

    Returns all events related to the agent's PR lifecycle:
    - pr_submitted, review_launched, review_completed
    - build_completed, merge_completed, revision_started
    - pr_status_changed, etc.
    """
    run_dir = BASE_DIR / path
    session_file = run_dir / "session.jsonl"

    if not session_file.exists():
        raise HTTPException(404, f"Session file not found: {session_file}")

    events = _read_jsonl(session_file)

    # Find all PR IDs associated with this agent
    pr_ids = set()
    for event in events:
        if event.get("agent_id") == agent_id and event.get("pr_id"):
            pr_ids.add(event.get("pr_id"))

    # Collect all events related to this agent or its PR IDs
    pr_events = []
    for event in events:
        event_type = event.get("event", "")
        event_agent_id = event.get("agent_id")
        event_pr_id = event.get("pr_id")

        # Include events that match agent_id or any of its PR IDs
        is_relevant = event_agent_id == agent_id or event_pr_id in pr_ids

        # Only include PR-lifecycle events
        pr_lifecycle_events = {
            "pr_submitted",
            "review_launched",
            "review_completed",
            "build_completed",
            "merge_completed",
            "revision_started",
            "pr_status_changed",
            "merge_conflict_detected",
            "pre_review_rebase",
            "fix_request",
            "agent_dispatch_failed",
        }

        if is_relevant and event_type in pr_lifecycle_events:
            pr_events.append(event)

    # Sort by timestamp
    pr_events.sort(key=lambda e: e.get("ts", ""))

    return {
        "agent_id": agent_id,
        "pr_ids": list(pr_ids),
        "events": pr_events,
    }


@app.get("/diff", response_class=PlainTextResponse)
async def get_diff(
    path: str = Query(..., description="Run directory name"),
    file: str = Query(..., description="Diff file path relative to run dir"),
):
    """Load external diff patch file (on-demand)."""
    run_dir = BASE_DIR / path
    diff_file = run_dir / file

    if not diff_file.exists():
        raise HTTPException(404, f"Diff file not found: {diff_file}")

    if not diff_file.suffix == ".patch":
        raise HTTPException(400, "Only .patch files allowed")

    diff_path = diff_file.resolve()
    run_path = run_dir.resolve()
    if not str(diff_path).startswith(str(run_path)):
        raise HTTPException(400, "Invalid file path")

    return PlainTextResponse(diff_file.read_text())


@app.get("/issues")
async def get_issues(path: str = Query(None, description="Optional run directory to get issues from proof_stats")):
    """Load issues, either from proof_stats events (for historical runs) or issues/ folder (for live).

    If path is provided, attempts to get issues from the latest proof_stats event in that run's session.
    Falls back to parsing issues/ folder from the project root.
    """
    import yaml

    # If path provided, try to get issues from proof_stats events first
    if path:
        run_dir = BASE_DIR / path
        session_file = run_dir / "session.jsonl"
        if session_file.exists():
            events = _read_jsonl(session_file)
            # Find the latest proof_stats event with issues
            for event in reversed(events):
                if event.get("event") == "proof_stats" and "issues" in event:
                    issues = event["issues"]
                    open_issues = [i for i in issues if i.get("is_open", True)]
                    closed_issues = [i for i in issues if not i.get("is_open", True)]
                    return JSONResponse(
                        {
                            "raw": None,
                            "open": open_issues,
                            "closed": closed_issues,
                            "source": "proof_stats",
                        }
                    )

    # Fall back to parsing issues/ folder from the project root (parent of runs directory)
    issues_dir = BASE_DIR.parent / "issues"

    if not issues_dir.exists():
        raise HTTPException(404, "issues/ folder not found")

    open_issues = []
    closed_issues = []

    for issue_file in sorted(issues_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(issue_file.read_text())
            if data:
                is_open = data.get("status") == "open"
                issue = {
                    "id": issue_file.stem,
                    "description": data.get("description", ""),
                    "origin": data.get("origin", ""),
                    "is_open": is_open,
                }
                if is_open:
                    open_issues.append(issue)
                else:
                    closed_issues.append(issue)
        except yaml.YAMLError:
            pass

    return JSONResponse(
        {
            "raw": None,
            "open": open_issues,
            "closed": closed_issues,
            "source": "folder",
        }
    )


@app.websocket("/ws")
async def websocket_updates(ws: WebSocket, path: str = Query(...)):
    """WebSocket for live session updates.

    Polls session.jsonl for changes and pushes updates.
    """
    await ws.accept()

    run_dir = BASE_DIR / path
    session_file = run_dir / "session.jsonl"

    last_mtime = 0.0
    last_event_count = 0
    first_update = True

    try:
        while True:
            try:
                if session_file.exists():
                    mtime = session_file.stat().st_mtime
                    if mtime != last_mtime or first_update:
                        last_mtime = mtime
                        events = _read_jsonl(session_file)

                        if len(events) > last_event_count or first_update:
                            new_events = events[last_event_count:] if not first_update else events
                            last_event_count = len(events)
                            stats = _extract_session_stats(events)
                            first_update = False

                            await ws.send_json(
                                {
                                    "type": "update",
                                    "new_events": new_events,
                                    "stats": stats,
                                    "mtime": mtime,
                                }
                            )
                else:
                    await ws.send_json(
                        {
                            "type": "waiting",
                            "message": f"Waiting for {session_file.name}...",
                        }
                    )

            except WebSocketDisconnect:
                raise
            except Exception as e:
                await ws.send_json({"type": "error", "message": str(e)})

            await asyncio.sleep(2)

    except WebSocketDisconnect:
        pass


def main():
    global BASE_DIR

    parser = argparse.ArgumentParser(description="Repoprover trajectory viewer server")
    parser.add_argument("--port", type=int, default=8385, help="Port (default: 8385)")
    parser.add_argument(
        "--dir",
        type=str,
        default="runs",
        help="Base directory containing run folders",
    )
    args = parser.parse_args()

    BASE_DIR = Path(args.dir).resolve()

    if not BASE_DIR.exists():
        print(f"Warning: Base directory does not exist: {BASE_DIR}")
        print("Creating it...")
        BASE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Repoprover Trajectory Viewer at http://localhost:{args.port}/")
    print(f"Base directory: {BASE_DIR}")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
