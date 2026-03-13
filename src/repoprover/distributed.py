# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Distributed execution support for RepoProver.

Pure infrastructure code - no BookCoordinator coupling.

Provides:
- SLURM environment detection and initialization
- ZMQ-based task/result queues for inter-process communication
- DistributedTask/DistributedResult dataclasses for serialization
- Standalone DistributedWorker for non-coordinator processes
- Mock worker spawning for local testing without SLURM

Import Cycles:
- distributed.py ↔ coordinator.py: SimplePR is imported lazily in simple_pr_from_dict()
  to avoid circular dependency
"""

from __future__ import annotations

import multiprocessing
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .coordinator import SimplePR  # noqa: F401

logger = getLogger(__name__)


# =============================================================================
# SLURM Utilities
# =============================================================================


def get_is_slurm_job() -> bool:
    """Check if running inside a SLURM job."""
    return "SLURM_JOB_ID" in os.environ


def get_global_rank() -> int:
    """Get the global rank of this process (0 = coordinator)."""
    # SLURM sets SLURM_PROCID for each task
    return int(os.environ.get("SLURM_PROCID", "0"))


def get_world_size() -> int:
    """Get the total number of processes (including coordinator)."""
    return int(os.environ.get("SLURM_NTASKS", "1"))


def get_master_addr() -> str:
    """Get the master address for distributed communication."""
    # SLURM provides the first node's hostname via SLURM_NODELIST
    # For single-node, this is the current host
    nodelist = os.environ.get("SLURM_NODELIST", "localhost")
    # Parse nodelist (handles both 'node1' and 'node[1-4]' formats)
    if "[" in nodelist:
        # Extract first node from range notation
        base = nodelist.split("[")[0]
        range_part = nodelist.split("[")[1].split("]")[0]
        first_num = range_part.split("-")[0].split(",")[0]
        return f"{base}{first_num}"
    return nodelist.split(",")[0]


def get_master_port() -> int:
    """Get the base port for distributed communication.

    Uses SLURM job ID to generate a unique port to avoid conflicts
    between concurrent jobs on the same node.

    For mock workers, uses MOCK_MASTER_PORT if set.
    """
    # Mock workers use explicit port
    if "MOCK_MASTER_PORT" in os.environ:
        return int(os.environ["MOCK_MASTER_PORT"])

    job_id = int(os.environ.get("SLURM_JOB_ID", "0"))
    # Base port range: 29500-32767 (avoids common ports)
    return 29500 + (job_id % 3000)


def init_distributed() -> tuple[int, int, bool]:
    """Initialize distributed environment.

    Returns:
        (rank, world_size, is_distributed)
    """
    rank = get_global_rank()
    world_size = get_world_size()
    is_distributed = world_size > 1

    if is_distributed:
        master_addr = get_master_addr()
        master_port = get_master_port()
        logger.info(f"Distributed init: rank={rank}/{world_size}, master={master_addr}:{master_port}")

    return rank, world_size, is_distributed


# =============================================================================
# ZMQ Queue Implementation
# =============================================================================


class ZmqQueueServer:
    """Server-side ZMQ queue for coordinator.

    Creates a PUSH socket for sending tasks and a PULL socket for receiving results.
    Thread-safe for multi-producer/multi-consumer patterns.
    """

    def __init__(self, addr: str, port: int):
        """Initialize server-side queue.

        Args:
            addr: Address to bind to (e.g., "*" for all interfaces)
            port: Base port number (uses port for push, port+1 for pull)
        """
        import zmq

        self.context = zmq.Context()
        self.port = port

        # PUSH socket for sending tasks to workers
        self.push_socket = self.context.socket(zmq.PUSH)
        self.push_socket.bind(f"tcp://{addr}:{port}")

        # PULL socket for receiving results from workers
        self.pull_socket = self.context.socket(zmq.PULL)
        self.pull_socket.bind(f"tcp://{addr}:{port + 1}")

        # For non-blocking receives
        self.poller = zmq.Poller()
        self.poller.register(self.pull_socket, zmq.POLLIN)

        self._lock = threading.Lock()
        self._tasks_sent = 0
        self._results_received = 0

        logger.info(f"[ZMQ-SERVER] Bound to ports {port} (tasks), {port + 1} (results)")
        print(f"[ZMQ-SERVER] Coordinator queue bound to *:{port} (tasks) and *:{port + 1} (results)", flush=True)

    def put(self, item: dict | None) -> None:
        """Send a task to workers (thread-safe)."""
        with self._lock:
            self.push_socket.send_json(item)
            self._tasks_sent += 1
            if item is None:
                logger.debug(f"[ZMQ-SERVER] Sent shutdown signal (total tasks: {self._tasks_sent})")
                print("[ZMQ-SERVER] Sent shutdown signal", flush=True)
            else:
                task_id = item.get("task_id", "unknown")
                agent_type = item.get("agent_type", "unknown")
                logger.debug(f"[ZMQ-SERVER] Sent task {task_id} ({agent_type}) - total: {self._tasks_sent}")
                print(f"[ZMQ-SERVER] Sent task {task_id} ({agent_type})", flush=True)

    def get(self, block: bool = True, timeout: float | None = None) -> dict | None:
        """Receive a result from workers.

        Args:
            block: If True, wait for a result. If False, return immediately.
            timeout: Timeout in seconds (only used if block=True)

        Returns:
            Result dict or None if no result available (non-blocking)

        Raises:
            queue.Empty: If block=False and no result available
        """
        timeout_ms = int(timeout * 1000) if timeout else None

        if block:
            socks = dict(self.poller.poll(timeout=timeout_ms))
            if self.pull_socket in socks:
                result = self.pull_socket.recv_json()
                self._results_received += 1
                task_id = result.get("task_id", "unknown")
                success = result.get("success", False)
                logger.debug(
                    f"[ZMQ-SERVER] Received result for {task_id} (success={success}) - total: {self._results_received}"
                )
                print(f"[ZMQ-SERVER] Received result for {task_id} (success={success})", flush=True)
                return result
            raise queue.Empty()
        else:
            socks = dict(self.poller.poll(timeout=0))
            if self.pull_socket in socks:
                result = self.pull_socket.recv_json()
                self._results_received += 1
                task_id = result.get("task_id", "unknown")
                success = result.get("success", False)
                logger.debug(
                    f"[ZMQ-SERVER] Received result for {task_id} (success={success}) - total: {self._results_received}"
                )
                return result
            raise queue.Empty()

    def close(self) -> None:
        """Close all sockets."""
        logger.info(f"[ZMQ-SERVER] Closing (sent {self._tasks_sent} tasks, received {self._results_received} results)")
        print(
            f"[ZMQ-SERVER] Closing (sent {self._tasks_sent} tasks, received {self._results_received} results)",
            flush=True,
        )
        self.push_socket.close()
        self.pull_socket.close()
        self.context.term()


class ZmqQueueClient:
    """Client-side ZMQ queue for workers.

    Connects to coordinator's PUSH/PULL sockets with reversed roles:
    - PULL from coordinator's PUSH (receive tasks)
    - PUSH to coordinator's PULL (send results)
    """

    def __init__(self, addr: str, port: int, rank: int = 0):
        """Initialize client-side queue.

        Args:
            addr: Coordinator address to connect to
            port: Base port number
            rank: Worker rank for logging
        """
        import zmq

        self.context = zmq.Context()
        self.rank = rank
        self._tasks_received = 0
        self._results_sent = 0

        # PULL socket to receive tasks from coordinator
        self.pull_socket = self.context.socket(zmq.PULL)
        self.pull_socket.connect(f"tcp://{addr}:{port}")

        # PUSH socket to send results to coordinator
        self.push_socket = self.context.socket(zmq.PUSH)
        self.push_socket.connect(f"tcp://{addr}:{port + 1}")

        logger.info(f"[WORKER-{rank}] Connected to {addr}:{port} (tasks) and {addr}:{port + 1} (results)")
        print(f"[WORKER-{rank}] Connected to {addr}:{port} (tasks) and {addr}:{port + 1} (results)", flush=True)

    def get_task(self) -> dict | None:
        """Receive a task from coordinator (blocking)."""
        task = self.pull_socket.recv_json()
        self._tasks_received += 1
        if task is None:
            logger.info(f"[WORKER-{self.rank}] Received shutdown signal")
            print(f"[WORKER-{self.rank}] Received shutdown signal", flush=True)
        else:
            task_id = task.get("task_id", "unknown")
            agent_type = task.get("agent_type", "unknown")
            logger.debug(f"[WORKER-{self.rank}] Received task {task_id} ({agent_type}) - total: {self._tasks_received}")
            print(f"[WORKER-{self.rank}] Received task {task_id} ({agent_type})", flush=True)
        return task

    def put_result(self, result: dict) -> None:
        """Send a result to coordinator."""
        self.push_socket.send_json(result)
        self._results_sent += 1
        task_id = result.get("task_id", "unknown")
        success = result.get("success", False)
        logger.debug(
            f"[WORKER-{self.rank}] Sent result for {task_id} (success={success}) - total: {self._results_sent}"
        )
        print(f"[WORKER-{self.rank}] Sent result for {task_id} (success={success})", flush=True)

    def close(self) -> None:
        """Close all sockets."""
        logger.info(
            f"[WORKER-{self.rank}] Closing (received {self._tasks_received} tasks, sent {self._results_sent} results)"
        )
        print(
            f"[WORKER-{self.rank}] Closing (received {self._tasks_received} tasks, sent {self._results_sent} results)",
            flush=True,
        )
        self.pull_socket.close()
        self.push_socket.close()
        self.context.term()


# =============================================================================
# Task/Result Dataclasses
# =============================================================================


@dataclass
class DistributedTask:
    """A task to be executed by a distributed worker."""

    task_id: str
    agent_type: str  # sketch, prove, triage, scan, progress, maintain
    task_data: dict  # Serialized ContributorTask
    agent_id: str
    chapter_id: str
    worktree_path: str  # Path to worktree (created by coordinator)
    branch_name: str  # Branch name for this agent
    feedback: str = ""
    revision_number: int = 0
    run_dir: str | None = None  # Path to run directory for agent recording

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent_type": self.agent_type,
            "task_data": self.task_data,
            "agent_id": self.agent_id,
            "chapter_id": self.chapter_id,
            "worktree_path": self.worktree_path,
            "branch_name": self.branch_name,
            "feedback": self.feedback,
            "revision_number": self.revision_number,
            "run_dir": self.run_dir,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DistributedTask:
        return cls(**d)


@dataclass
class DistributedResult:
    """Result from a distributed worker - mirrors ContributorResult + branch info."""

    task_id: str
    agent_id: str
    chapter_id: str

    # ContributorResult fields
    status: str  # "done", "fix", "issue", "blocked", "error"
    branch_name: str
    description: str = ""
    error: str | None = None
    fix_request: str | None = None
    issue_text: str | None = None
    theorem_name: str | None = None
    issue_id: str | None = None  # For maintain agents - the assigned issue
    iterations: int = 0  # Agent iteration count

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "chapter_id": self.chapter_id,
            "status": self.status,
            "branch_name": self.branch_name,
            "description": self.description,
            "error": self.error,
            "fix_request": self.fix_request,
            "issue_text": self.issue_text,
            "theorem_name": self.theorem_name,
            "issue_id": self.issue_id,
            "iterations": self.iterations,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DistributedResult:
        d = dict(d)  # Copy to avoid mutating original
        d.setdefault("issue_id", None)  # Backwards compat with in-flight results
        return cls(**d)


# =============================================================================
# ContributorTask Serialization Helpers
# =============================================================================


def contributor_task_to_dict(task: Any) -> dict:
    """Serialize a ContributorTask to dict for ZMQ transport.

    Args:
        task: A ContributorTask instance

    Returns:
        Dict representation for JSON serialization
    """
    return {
        "mode": task.mode.value,  # ContributorMode enum -> string
        "chapter_id": task.chapter_id,
        "theorem_name": task.theorem_name,
        "issue_id": task.issue_id,
        "lean_path": task.lean_path,
        "source_tex_path": task.source_tex_path,
    }


def contributor_task_from_dict(d: dict) -> Any:
    """Deserialize a ContributorTask from dict.

    Args:
        d: Dict representation from JSON

    Returns:
        ContributorTask instance
    """
    # Import here to avoid circular dependency
    from .agents import ContributorTask
    from .agents.contributor import ContributorMode

    return ContributorTask(
        mode=ContributorMode(d["mode"]),  # string -> ContributorMode enum
        chapter_id=d.get("chapter_id"),
        theorem_name=d.get("theorem_name"),
        issue_id=d.get("issue_id"),
        lean_path=d.get("lean_path", ""),
        source_tex_path=d.get("source_tex_path", ""),
    )


def simple_pr_to_dict(pr: Any) -> dict:
    """Serialize a SimplePR to dict for ZMQ transport."""
    return pr.to_dict()


def simple_pr_from_dict(d: dict) -> Any:
    """Deserialize a SimplePR from dict."""
    # Import here to avoid circular dependency
    from .coordinator import SimplePR

    return SimplePR.from_dict(d)


# =============================================================================
# Standalone Distributed Worker
# =============================================================================


class DistributedWorker:
    """Standalone worker process for distributed execution.

    No BookCoordinator coupling - just pulls tasks, executes ContributorAgent,
    and returns results. Uses a thread pool for concurrent task execution.

    NOTE: Workers do NOT manage worktrees. The coordinator creates worktrees
    before dispatching tasks and provides the path in DistributedTask.
    """

    def __init__(
        self,
        base_project: Path,
        agent_config: Any | None = None,
        max_concurrent: int = 512,
        lean_pool_size: int = 24,
    ):
        """Initialize worker.

        Args:
            base_project: Path to the Lean project
            agent_config: Optional AgentConfig for LLM settings
            max_concurrent: Maximum concurrent agent threads (I/O-bound LLM calls, cheap)
            lean_pool_size: Number of concurrent Lean REPL instances (memory-heavy)
        """
        from concurrent.futures import ThreadPoolExecutor

        self.rank = get_global_rank()
        self.base_project = base_project
        self.agent_config = agent_config
        self.max_concurrent = max_concurrent

        print(
            f"[WORKER-{self.rank}] Initializing (base={base_project}, max_concurrent={max_concurrent}, lean_pool_size={lean_pool_size})",
            flush=True,
        )

        # Connect to coordinator's ZMQ queues
        master_addr = get_master_addr()
        master_port = get_master_port()
        self.queue = ZmqQueueClient(master_addr, master_port, rank=self.rank)

        # Thread pool for concurrent task execution
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix=f"worker-{self.rank}")

        # Shared learnings
        from .agents.base import LearningsStore

        learnings_path = base_project / ".repoprover" / "learnings.json"
        self.learnings = LearningsStore(learnings_path)

        # Pre-configure global REPL pool with base project path
        # This MUST use base_project, not a worktree path, because the pool is global
        # and worktrees may be cleaned up while the pool is still running.
        from .agents.lean_tools import configure_global_pool

        configure_global_pool(
            workspace=base_project,
            pool_size=lean_pool_size,
        )

        logger.info(f"[WORKER-{self.rank}] Initialized successfully")
        print(f"[WORKER-{self.rank}] Ready and waiting for tasks (pool size: {max_concurrent})", flush=True)

    def run(self) -> None:
        """Main worker loop - pull tasks, execute concurrently, return results."""
        import zmq  # ZMQ only needed here for RCVTIMEO constant

        logger.info(f"[WORKER-{self.rank}] Starting main loop")
        print(f"[WORKER-{self.rank}] Entering main loop", flush=True)

        tasks_completed = 0
        tasks_failed = 0
        active_futures: dict[ThreadPoolExecutor, DistributedTask] = {}
        shutdown = False

        # Results queue for completed tasks (thread-safe)
        results_queue: queue.Queue[tuple[DistributedTask, DistributedResult]] = queue.Queue()

        def task_done_callback(future, task: DistributedTask) -> None:
            """Called when a task completes - puts result in queue."""
            try:
                result = future.result()
                results_queue.put((task, result))
            except Exception as e:
                logger.exception(f"Task {task.task_id} ({task.agent_type}) failed with exception")
                error_result = DistributedResult(
                    task_id=task.task_id,
                    agent_id=task.agent_id,
                    chapter_id=task.chapter_id,
                    status="error",
                    branch_name="",
                    error=str(e),
                )
                results_queue.put((task, error_result))

        while not shutdown or active_futures:
            # Send completed results back to coordinator
            while not results_queue.empty():
                try:
                    task, result = results_queue.get_nowait()
                    self.queue.put_result(result.to_dict())
                    if result.status in ("done", "fix"):
                        tasks_completed += 1
                        print(f"[WORKER-{self.rank}] Task {task.task_id} completed ({result.status})", flush=True)
                    else:
                        tasks_failed += 1
                        print(f"[WORKER-{self.rank}] Task {task.task_id} {result.status}: {result.error}", flush=True)
                except queue.Empty:
                    break

            # Clean up completed futures
            done_futures = [f for f in active_futures if f.done()]
            for f in done_futures:
                del active_futures[f]

            # If shutting down, just wait for remaining tasks
            if shutdown:
                if active_futures:
                    import time

                    time.sleep(0.1)
                continue

            # Accept new tasks if we have capacity
            if len(active_futures) < self.max_concurrent:
                # Use non-blocking receive with short timeout
                try:
                    # Set a short timeout so we can process results
                    self.queue.pull_socket.setsockopt(zmq.RCVTIMEO, 100)  # 100ms
                    task_dict = self.queue.pull_socket.recv_json()
                    self.queue._tasks_received += 1

                    if task_dict is None:
                        logger.info(f"[WORKER-{self.rank}] Received shutdown signal")
                        print(
                            f"[WORKER-{self.rank}] Received shutdown signal, finishing {len(active_futures)} active tasks",
                            flush=True,
                        )
                        shutdown = True
                        continue

                    task = DistributedTask.from_dict(task_dict)
                    task_id = task.task_id
                    agent_type = task.agent_type
                    logger.debug(f"[WORKER-{self.rank}] Received task {task_id} ({agent_type})")
                    print(f"[WORKER-{self.rank}] Received task {task_id} ({agent_type})", flush=True)

                    # Submit to thread pool
                    print(
                        f"[WORKER-{self.rank}] Executing task {task.task_id} ({task.agent_type}) [{len(active_futures) + 1}/{self.max_concurrent} active]",
                        flush=True,
                    )
                    future = self._executor.submit(self._execute, task)
                    future.add_done_callback(lambda f, t=task: task_done_callback(f, t))
                    active_futures[future] = task

                except zmq.Again:
                    # No task available, continue loop
                    pass
            else:
                # At capacity, wait a bit
                import time

                time.sleep(0.05)

        self._executor.shutdown(wait=True)
        self.queue.close()
        logger.info(f"[WORKER-{self.rank}] Stopped (completed={tasks_completed}, failed={tasks_failed})")
        print(f"[WORKER-{self.rank}] Stopped (completed={tasks_completed}, failed={tasks_failed})", flush=True)

    def _execute(self, task: DistributedTask) -> DistributedResult:
        """Execute a single task and return result (RPC for _run_contributor).

        The worktree is created by the coordinator before dispatching the task.
        Worker just uses the provided worktree_path directly.
        """
        from .agents import ContributorAgent
        from .agents.base import AgentConfig
        from .recording import AgentRecorder

        # Deserialize task
        contrib_task = contributor_task_from_dict(task.task_data)

        # Use worktree path provided by coordinator
        worktree_path = Path(task.worktree_path)

        # Verify worktree exists (coordinator should have created it)
        if not worktree_path.exists():
            error_msg = f"Worktree path does not exist: {worktree_path}"
            logger.error(f"[WORKER-{self.rank}] {error_msg}")
            print(f"[WORKER-{self.rank}] Task {task.task_id} error: {error_msg}", flush=True)
            return DistributedResult(
                task_id=task.task_id,
                agent_id=task.agent_id,
                chapter_id=task.chapter_id,
                status="error",
                branch_name=task.branch_name,
                error=error_msg,
            )

        try:
            # Set up recording if run_dir is provided
            recorder = None
            if task.run_dir:
                recorder = AgentRecorder(
                    run_dir=Path(task.run_dir),
                    agent_id=task.agent_id,
                    agent_type=task.agent_type,
                    config={"chapter_id": task.chapter_id, "revision_number": task.revision_number},
                )

            # Create and run agent (no worktree_manager needed - just use the path)
            agent = ContributorAgent(
                config=self.agent_config or AgentConfig(),
                repo_root=worktree_path,
                worktree_manager=None,  # Worker doesn't manage worktrees
                learnings=self.learnings,
                recorder=recorder,
                task=contrib_task,
            )

            # Pass feedback for revisions
            run_kwargs: dict[str, Any] = {}
            is_revision = task.revision_number > 0
            if is_revision and task.feedback:
                run_kwargs["feedback"] = task.feedback
                run_kwargs["is_initial"] = False
            elif task.agent_type == "sketch":
                run_kwargs["is_initial"] = not is_revision

            result = agent.run_task(**run_kwargs)

            # Finalize recording with result status
            if recorder:
                recorder.done(result.status, error=result.error or getattr(result, "issue_text", None))

            # Return ContributorResult fields + branch_name + iterations
            # Coordinator will do all post-processing (diff, PR creation, recording)
            iterations = recorder._iteration_count if recorder else 0
            return DistributedResult(
                task_id=task.task_id,
                agent_id=task.agent_id,
                chapter_id=task.chapter_id,
                status=result.status,
                branch_name=task.branch_name,
                description=result.description or "",
                error=result.error,
                fix_request=getattr(result, "fix_request", None),
                issue_text=getattr(result, "issue_text", None),
                theorem_name=contrib_task.theorem_name,
                issue_id=contrib_task.issue_id,
                iterations=iterations,
            )

        except Exception as e:
            import traceback

            error_msg = f"Agent execution failed: {e}"
            logger.error(f"[WORKER-{self.rank}] {error_msg}\n{traceback.format_exc()}")
            print(f"[WORKER-{self.rank}] Task {task.task_id} error: {error_msg}", flush=True)
            return DistributedResult(
                task_id=task.task_id,
                agent_id=task.agent_id,
                chapter_id=task.chapter_id,
                status="error",
                branch_name=task.branch_name,
                error=error_msg,
            )


# =============================================================================
# Mock Worker Support (for local testing)
# =============================================================================


def _mock_worker_process(
    base_project: Path,
    rank: int,
    master_port: int,
    lean_pool_size: int = 24,
) -> None:
    """Worker process entry point for mock workers."""
    # Set mock environment
    os.environ["SLURM_PROCID"] = str(rank)
    os.environ["SLURM_NTASKS"] = os.environ.get("SLURM_NTASKS", "1")
    os.environ["MOCK_MASTER_PORT"] = str(master_port)

    # Override get_master_port for mock workers
    worker = DistributedWorker(base_project, lean_pool_size=lean_pool_size)
    worker.run()


def spawn_mock_workers(
    n: int,
    base_project: Path,
    master_port: int,
    lean_pool_size: int = 24,
) -> list[multiprocessing.Process]:
    """Spawn N local worker processes for testing without SLURM.

    Args:
        n: Number of workers to spawn
        base_project: Path to the Lean project
        master_port: Base port for ZMQ communication

    Returns:
        List of Process objects (caller should join/terminate these)
    """
    import time

    # Set environment for coordinator BEFORE spawning workers
    # This ensures get_world_size() returns correct value when coordinator starts
    os.environ["SLURM_PROCID"] = "0"
    os.environ["SLURM_NTASKS"] = str(n + 1)  # n workers + 1 coordinator
    os.environ["MOCK_MASTER_PORT"] = str(master_port)  # Coordinator also needs this

    processes = []
    for i in range(n):
        rank = i + 1  # Workers are ranks 1..n, coordinator is 0
        p = multiprocessing.Process(
            target=_mock_worker_process,
            args=(base_project, rank, master_port, lean_pool_size),
        )
        p.start()
        processes.append(p)
        logger.info(f"Spawned mock worker {rank}")

    # Give workers a moment to start connecting
    # (They'll block on recv until coordinator starts sending)
    time.sleep(0.5)

    return processes


def cleanup_mock_workers(processes: list[multiprocessing.Process]) -> None:
    """Clean up mock worker processes."""
    for p in processes:
        if p.is_alive():
            p.terminate()
            p.join(timeout=5)
            if p.is_alive():
                p.kill()
    logger.info("All mock workers cleaned up")
