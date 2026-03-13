# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""CLI for RepoProver - Multi-file git-based autoformalization."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .agents.lean_tools import (
    configure_global_pool,
    shutdown_global_pool,
)
from .coordinator import BookCoordinator, BookCoordinatorConfig
from .distributed import (
    DistributedWorker,
    cleanup_mock_workers,
    get_global_rank,
    get_master_port,
    get_world_size,
    spawn_mock_workers,
)

DEFAULT_MANIFEST_NAME = "manifest.json"


def setup_logging(log_dir: Path | None = None, verbose: bool = False) -> None:
    """Set up logging configuration with file handlers.

    When log_dir is provided, logs go to files (with console summary):
      - debug.log: all messages (DEBUG+)
      - info.log:  INFO+ messages
      - error.log: WARNING+ messages
    When log_dir is None, logs go to console only.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # capture everything; handlers filter

    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    # File handlers (if log_dir provided)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        for filename, level in [
            ("debug.log", logging.DEBUG),
            ("info.log", logging.INFO),
            ("error.log", logging.WARNING),
        ]:
            fh = logging.FileHandler(log_dir / filename)
            fh.setLevel(level)
            fh.setFormatter(fmt)
            root.addHandler(fh)

    # Console handler (always, but less verbose when logging to files)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    # Silence noisy third-party loggers
    for noisy_logger in [
        "asyncio",
        "httpcore",
        "httpx",
        "openai",
        "openai._base_client",
        "urllib3.connectionpool",
    ]:
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)


def find_manifest(base_project: Path) -> Path | None:
    """Find manifest file in project directory."""
    candidates = [
        base_project / DEFAULT_MANIFEST_NAME,
        base_project / "repoprover.json",
        base_project / ".repoprover" / "manifest.json",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def cmd_status(args: argparse.Namespace) -> int:
    """Show project status."""
    base_project = Path(args.path).resolve()
    state_file = base_project / ".repoprover" / "state.json"

    if not state_file.exists():
        print(f"Error: No RepoProver project found at {base_project}")
        print("Run 'repoprover run' first")
        return 1

    config = BookCoordinatorConfig(
        book_id="",
        title="",
        base_project=base_project,
        worktrees_root=base_project / ".repoprover" / "worktrees",
        state_file=state_file,
    )

    coordinator = BookCoordinator(config)
    status = coordinator.get_status()

    # Print status
    print(f"\n📚 {status['title'] or status['book_id']}")
    print("=" * 50)
    print(f"Chapters:          {status['chapters_with_sketch']}/{status['total_chapters']} sketched")
    print(f"PRs:               {status['total_prs']} total")
    print(f"  Pending review:  {status['pending_review']}")
    print(f"  Needs revision:  {status['needs_revision']}")
    print(f"  Approved:        {status['approved']}")
    print(f"  Merged:          {status['merged']}")
    print(f"Theorems proved:   {status['completed_theorems']}")

    if args.chapters:
        print("\n📖 Chapters:")
        print("-" * 50)
        for chapter_id, info in coordinator.state.chapters.items():
            sketch_status = "✅" if info.get("sketch_merged") else "⬜"
            print(f"  {sketch_status} {chapter_id}: {info.get('title', '')}")
            print(f"     Source: {info.get('source_path', 'N/A')}")
            print(f"     Lean:   {info.get('lean_path', 'N/A')}")

    if args.prs:
        print("\n📋 PRs:")
        print("-" * 50)
        for pr in coordinator.state.prs.values():
            status_icon = {
                "pending_review": "🔵",
                "needs_revision": "🟠",
                "approved": "🟢",
                "merged": "✅",
                "failed": "❌",
            }.get(pr.status, "❓")
            print(f"  {status_icon} {pr.pr_id}: {pr.agent_type} for {pr.chapter_id}")
            if pr.theorem_name:
                print(f"     Theorem: {pr.theorem_name}")
            print(f"     Branch: {pr.branch_name}")
            print(f"     Status: {pr.status} (rev {pr.revision_count})")

    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run the coordinator and/or worker based on SLURM rank.

    In distributed mode (SLURM):
    - Rank 0: Launches coordinator in background thread, then runs as worker
    - Rank 1+: Runs as distributed workers (pull tasks from coordinator)

    In local mode (no SLURM or world_size=1):
    - Runs single-node coordinator with local asyncio.to_thread workers (no ZMQ)

    With --mock-workers N:
    - Spawns N local worker processes for testing without SLURM
    - Rank 0 also runs as worker alongside the mock workers
    """
    import threading

    base_project = Path(args.path).resolve()

    # Distributed mode detection
    rank = get_global_rank()
    world_size = get_world_size()
    mock_workers = getattr(args, "mock_workers", 0)

    # Determine if we're in distributed mode
    # Distributed = SLURM with multiple ranks OR mock workers
    is_distributed = world_size > 1 or mock_workers > 0

    # Local mode: just run coordinator directly (no workers, uses asyncio.to_thread)
    if not is_distributed:
        return _run_coordinator(base_project, args)

    # === Distributed mode ===

    # Optionally spawn mock workers for local testing (rank 0 only)
    mock_processes = []
    if rank == 0 and mock_workers > 0:
        print(f"[MOCK] Spawning {mock_workers} mock workers for local testing...")
        mock_processes = spawn_mock_workers(
            n=mock_workers,
            base_project=base_project,
            master_port=get_master_port(),
            lean_pool_size=args.pool_size,
        )

    # Rank 0: start coordinator in background thread
    coordinator_thread = None
    coordinator_result = [0]  # Use list to capture result from thread

    if rank == 0:

        def coordinator_thread_fn():
            try:
                coordinator_result[0] = _run_coordinator(base_project, args)
            except Exception as e:
                print(f"[COORDINATOR] Error: {e}")
                coordinator_result[0] = 1

        coordinator_thread = threading.Thread(
            target=coordinator_thread_fn,
            name="coordinator",
            daemon=False,  # Not daemon - we want to wait for it
        )
        coordinator_thread.start()
        print("[RANK-0] Started coordinator in background thread")

        # Give coordinator a moment to bind ZMQ sockets
        import time

        time.sleep(1.0)

    # ALL ranks run as workers (including rank 0)
    try:
        worker_result = _run_worker(base_project, args)
    finally:
        # Clean up mock workers if we spawned any
        if mock_processes:
            print("[MOCK] Cleaning up mock workers...")
            cleanup_mock_workers(mock_processes)

        # Wait for coordinator thread to finish (rank 0 only)
        if coordinator_thread is not None:
            print("[RANK-0] Waiting for coordinator thread to finish...")
            coordinator_thread.join(timeout=30)  # Don't wait forever
            if coordinator_thread.is_alive():
                print("[RANK-0] Coordinator thread still running, proceeding anyway")

    # Return coordinator result if we're rank 0, else worker result
    return coordinator_result[0] if rank == 0 else worker_result


def _run_worker(base_project: Path, args: argparse.Namespace) -> int:
    """Run as a distributed worker (non-coordinator process)."""
    rank = get_global_rank()
    world_size = get_world_size()
    print(f"[WORKER] Starting distributed worker (rank={rank}/{world_size})")

    # Set up minimal logging for workers
    setup_logging(log_dir=None, verbose=args.verbose)

    # Pool size for Lean REPL instances (memory-heavy)
    lean_pool_size = args.pool_size

    try:
        worker = DistributedWorker(
            base_project=base_project,
            lean_pool_size=lean_pool_size,
        )
        worker.run()
        return 0
    except KeyboardInterrupt:
        print(f"Worker {rank} interrupted")
        return 0
    except Exception as e:
        print(f"Worker {rank} error: {e}")
        return 1
    finally:
        shutdown_global_pool()


def _run_coordinator(base_project: Path, args: argparse.Namespace) -> int:
    """Run as the coordinator (main control process)."""
    base_project = Path(args.path).resolve()
    repoprover_dir = base_project / ".repoprover"
    state_file = repoprover_dir / "state.json"
    worktrees_root = repoprover_dir / "worktrees"
    runs_dir = base_project / "runs"

    # Configure centralized REPL pool for lean_check
    pool_size = args.pool_size
    configure_global_pool(base_project, pool_size=pool_size)
    print(f"[INIT] lean_check REPL pool configured (size={pool_size})")

    # Find manifest - required
    manifest_path = find_manifest(base_project)
    if manifest_path is None:
        print(f"Error: No manifest found in {base_project}")
        print(f"  Looked for: {DEFAULT_MANIFEST_NAME}, repoprover.json, .repoprover/manifest.json")
        return 1

    # Auto-initialize if needed
    needs_init = not state_file.exists()
    needs_reinit = False

    if needs_init:
        print(f"[INIT] Initializing RepoProver project at {base_project}...")
        worktrees_root.mkdir(parents=True, exist_ok=True)
    else:
        # Check for empty or corrupt state file - this must happen BEFORE loading state
        # regardless of --clean flag, since the state file may be empty/corrupt from
        # a previous interrupted run
        try:
            content = state_file.read_text().strip()
            if not content:
                needs_reinit = True
                print("[WARN] State file was empty, reinitializing...")
        except Exception:
            needs_reinit = True
            print("[WARN] State file was corrupt, reinitializing...")

    # Clean previous run data if requested
    should_clean = args.clean and not needs_init
    if should_clean:
        print("[CLEAN] Cleaning previous run data...")

    # Use directory name as book_id
    book_id = base_project.name

    config = BookCoordinatorConfig(
        book_id=book_id,
        title=book_id,
        base_project=base_project,
        worktrees_root=worktrees_root,
        state_file=state_file,
        runs_dir=runs_dir,
        prs_to_issues=args.prs_to_issues,
        agents_per_target=args.agents_per_target,
    )

    # Skip loading state if: initializing, reinitializing, or cleaning
    skip_load = needs_init or needs_reinit or should_clean
    coordinator = BookCoordinator(config, skip_load=skip_load)

    # Set up logging to the run directory (now that we have it)
    log_dir = None
    if coordinator.session_recorder:
        log_dir = coordinator.session_recorder.run_dir / "logs"
    setup_logging(log_dir=log_dir, verbose=args.verbose)

    # Load manifest (always reload to pick up changes)
    print(f"[INIT] Loading manifest from {manifest_path}...")
    count = coordinator.load_manifest(manifest_path)
    print(f"   Loaded {count} chapters")

    # Clean coordinator state if requested (after manifest load)
    if args.clean and not needs_init:
        coordinator.clean()

    print(f"[START] Starting RepoProver for {coordinator.state.book_id}")
    print(f"   Chapters: {len(coordinator.state.chapters)}")
    print(f"   State file: {state_file}")
    if coordinator.session_recorder:
        print(f"   Recording to: {coordinator.session_recorder.run_dir}")
        print(f"   Logs: {log_dir}")
    print("\nPress Ctrl+C to stop\n")

    try:
        # Run the main loop
        coordinator.run()
    finally:
        # Finalize recording
        if coordinator.session_recorder:
            coordinator.session_recorder.finalize("completed")
        # Shutdown the global REPL pool
        shutdown_global_pool()

    # Print final status
    status = coordinator.get_status()
    print("\n" + "=" * 50)
    print("Final status:")
    print(f"  Chapters sketched: {status['chapters_with_sketch']}/{status['total_chapters']}")
    print(f"  PRs merged: {status['merged']}")
    print(f"  Theorems proved: {status['completed_theorems']}")

    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Export project state as JSON."""
    base_project = Path(args.path).resolve()
    state_file = base_project / ".repoprover" / "state.json"

    if not state_file.exists():
        print("Error: No RepoProver project found", file=sys.stderr)
        return 1

    with open(state_file) as f:
        state = json.load(f)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(state, f, indent=2)
        print(f"Exported to {args.output}")
    else:
        print(json.dumps(state, indent=2))

    return 0


def add_path_argument(parser: argparse.ArgumentParser) -> None:
    """Add the common path argument to a subparser."""
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Project directory (default: current directory)",
    )


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="repoprover",
        description="Multi-file git-based autoformalization",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # status
    p_status = subparsers.add_parser("status", help="Show project status")
    add_path_argument(p_status)
    p_status.add_argument("-c", "--chapters", action="store_true", help="Show chapter details")
    p_status.add_argument("--prs", action="store_true", help="Show PR details")

    # run
    p_run = subparsers.add_parser("run", help="Run the coordinator (main loop)")
    add_path_argument(p_run)
    p_run.add_argument(
        "--clean", action="store_true", help="Start from scratch: remove all Lean files, reinit git repo, wipe state"
    )
    p_run.add_argument(
        "--pool-size",
        type=int,
        default=10,
        dest="pool_size",
        help="Number of REPL instances in the centralized pool (default: 10)",
    )
    p_run.add_argument(
        "--mock-workers",
        type=int,
        default=0,
        dest="mock_workers",
        help="Spawn N local worker processes for testing without SLURM (default: 0 = disabled)",
    )
    p_run.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output (DEBUG level logging)",
    )
    p_run.add_argument(
        "--prs-to-issues",
        action="store_true",
        dest="prs_to_issues",
        help="Convert existing PRs to issues instead of relaunching them on resume",
    )
    p_run.add_argument(
        "--agents-per-target",
        type=int,
        default=1,
        dest="agents_per_target",
        help="Max agents per theorem/issue. Effective = min(this, 32 // n_targets) (default: 1)",
    )

    # export
    p_export = subparsers.add_parser("export", help="Export state as JSON")
    add_path_argument(p_export)
    p_export.add_argument("-o", "--output", help="Output file (default: stdout)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    commands = {
        "status": cmd_status,
        "run": cmd_run,
        "export": cmd_export,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        return cmd_func(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
