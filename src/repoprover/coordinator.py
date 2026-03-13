# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Book Coordinator - Simple run loop for book formalization.

Design principles:
- PR = branch name submitted to a queue (review or merge)
- Simple loop: agent → review → (fail → agent) → (success → merge)
- Resumable from partial runs
- Fully async for parallel agent execution
"""

from __future__ import annotations

import asyncio
import json
import queue
import random
import secrets
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any

import yaml

from .agents import (
    ContributorAgent,
    ContributorTask,
)
from .agents.base import AgentConfig, LearningsStore
from .agents.reviewers import ReviewResult, ReviewVerdict, review_pr
from .build import lake_build
from .distributed import (
    DistributedResult,
    DistributedTask,
    ZmqQueueServer,
    contributor_task_to_dict,
    get_master_port,
    get_world_size,
)
from .git_worktree import WorktreePool
from .lean_utils import DECL_HEADER_RE as _DECL_HEADER_RE
from .lean_utils import THEOREM_NAME_RE as _THEOREM_NAME_RE
from .lean_utils import parse_diff_stats, strip_comments
from .recording import SessionRecorder, create_session_recorder, read_agent_dialog
from .types import AgentType, ReviewContext
from .utils import timed, timed_run

logger = getLogger(__name__)


# =============================================================================
# Lean parsing helpers
# =============================================================================


# =============================================================================
# Config
# =============================================================================


@dataclass
class BookCoordinatorConfig:
    """Configuration for the book coordinator."""

    book_id: str
    title: str
    base_project: Path
    worktrees_root: Path
    state_file: Path | None = None
    max_revisions: int = 16  # Max revision attempts before giving up
    poll_interval: float = 1.0

    # Max concurrent maintain contributors (capped by open issue count)
    max_concurrent_contributors: int = 256

    # Max concurrent sketchers (avoids API throttling when many chapters launch at once)
    max_concurrent_sketchers: int = 20

    # Lean REPL pool size for parallel proof checking
    lean_pool_size: int = 24

    # Recording
    recording_enabled: bool = True
    runs_dir: Path | None = None  # Default: base_project/runs

    # Agent configuration (LLM provider, model, etc.)
    agent_config: AgentConfig | None = None

    # Startup behavior: convert existing PRs to issues instead of relaunching
    # When True, on resume, existing PRs with active statuses are converted
    # to issues (documenting unfinished work) and marked as closed/failed.
    prs_to_issues: bool = False

    # Multiple agents per target: allows launching multiple agents per theorem/issue.
    # Effective agents = min(agents_per_target, 32 // n_targets)
    # So with 2 sorries and agents_per_target=8 → 8 provers each (32//2=16 cap)
    # With 32 sorries and agents_per_target=8 → 1 prover each (32//32=1 cap)
    agents_per_target: int = 1


# =============================================================================
# Simple PR tracking (PR = branch name + status)
# =============================================================================


@dataclass
class SimplePR:
    """A PR is just a branch name + metadata."""

    pr_id: str
    branch_name: str
    chapter_id: str
    agent_type: str  # "sketch", "prove", "fix", "scan", "progress", or "triage"
    agent_id: str = ""  # The actual agent_id that created this PR
    theorem_name: str | None = None  # For prover PRs
    issue_id: str | None = None  # For maintain PRs: the assigned issue ID
    status: str = "pending_review"  # pending_review, needs_revision, approved, merged, failed
    revision_count: int = 0
    last_review_feedback: str = ""
    diff_stats: dict[str, int] | None = None  # {"+": additions, "-": deletions}
    description: str = ""  # PR description (shown to reviewers)
    # Tracks diff content per revision: {0: "diff content...", 1: "diff content...", ...}
    # This enables the viewer to show diffs for resumed agents
    diffs: dict[int, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pr_id": self.pr_id,
            "branch_name": self.branch_name,
            "chapter_id": self.chapter_id,
            "agent_type": self.agent_type,
            "agent_id": self.agent_id,
            "theorem_name": self.theorem_name,
            "issue_id": self.issue_id,
            "status": self.status,
            "revision_count": self.revision_count,
            "last_review_feedback": self.last_review_feedback,
            "diff_stats": self.diff_stats,
            "description": self.description,
            "diffs": self.diffs,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SimplePR":
        d = dict(d)  # Copy to avoid modifying original
        d.pop("closes_issues", None)  # Remove legacy field if present
        d.pop("diff_files", None)  # Remove deprecated field
        d.setdefault("description", "")  # Default for old state files
        d.setdefault("issue_id", None)  # Default for old state files
        # Convert diffs keys from strings (JSON) to ints
        raw_diffs = d.pop("diffs", {})
        d["diffs"] = {int(k): v for k, v in raw_diffs.items()} if raw_diffs else {}
        return cls(**d)


# =============================================================================
# Run State (for resumability)
# =============================================================================


@dataclass
class RunState:
    """Tracks the current run state for resumability."""

    book_id: str
    chapters: dict[str, dict] = field(default_factory=dict)  # chapter_id -> chapter info
    prs: dict[str, SimplePR] = field(default_factory=dict)  # pr_id -> SimplePR
    completed_theorems: dict[str, list[str]] = field(default_factory=dict)  # chapter_id -> [theorem_names]
    next_issue_id: int = 1  # For generating unique issue IDs

    # Scanner state tracking (simplified for unified scan mode)
    max_concurrent_scanners: int = 1  # How many scanners can run at once
    active_scanners: dict[str, str] = field(default_factory=dict)  # scanner_id -> status

    # Progress agent state tracking (similar to scanners)
    max_concurrent_progress: int = 1  # How many progress agents can run at once
    active_progress: dict[str, str] = field(default_factory=dict)  # progress_id -> status

    def save(self, path: Path) -> None:
        data = {
            "book_id": self.book_id,
            "chapters": self.chapters,
            "prs": {k: v.to_dict() for k, v in self.prs.items()},
            "completed_theorems": self.completed_theorems,
            "next_issue_id": self.next_issue_id,
            "max_concurrent_scanners": self.max_concurrent_scanners,
            "active_scanners": self.active_scanners,
            "max_concurrent_progress": self.max_concurrent_progress,
            "active_progress": self.active_progress,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path) -> "RunState":
        content = path.read_text().strip()
        if not content:
            raise ValueError(f"State file is empty: {path}")
        data = json.loads(content)
        state = cls(book_id=data["book_id"])
        state.chapters = data.get("chapters", {})
        state.prs = {k: SimplePR.from_dict(v) for k, v in data.get("prs", {}).items()}
        state.completed_theorems = data.get("completed_theorems", {})
        state.next_issue_id = data.get("next_issue_id", 1)
        state.max_concurrent_scanners = data.get("max_concurrent_scanners", 1)
        state.active_scanners = data.get("active_scanners", {})
        state.max_concurrent_progress = data.get("max_concurrent_progress", 1)
        state.active_progress = data.get("active_progress", {})
        return state


# =============================================================================
# Book Coordinator
# =============================================================================


class BookCoordinator:
    """Simple coordinator for book formalization.

    Run loop:
    1. For each chapter without a sketch: run sketcher → submit to review
    2. Process review queue: review → (fail → rerun agent) → (success → merge)
    3. Process merge queue: merge to main
    4. After merge: scan for sorries → run provers → submit to review
    5. Repeat until done
    """

    def __init__(self, config: BookCoordinatorConfig, skip_load: bool = False):
        self.config = config
        self.base_project = config.base_project

        # Validate that 'main' branch exists
        self._validate_main_branch()

        # Load or create state
        self.state_file = config.state_file or (config.base_project / ".repoprover" / "state.json")
        if skip_load:
            self.state = RunState(book_id=config.book_id)
        elif self.state_file.exists():
            self.state = RunState.load(self.state_file)
        else:
            self.state = RunState(book_id=config.book_id)

        # Worktree pool for agent isolation
        self.worktree_pool = WorktreePool(
            base_project=config.base_project,
            worktrees_root=config.worktrees_root,
        )

        # Shared learnings
        learnings_path = config.base_project / ".repoprover" / "learnings.json"
        self.learnings = LearningsStore(learnings_path)

        # Note: Global REPL pool is configured later in __init__ only for local mode.
        # In distributed mode, workers configure their own pools.

        # Recording
        self.session_recorder: SessionRecorder | None = None
        if config.recording_enabled:
            runs_dir = config.runs_dir or (config.base_project / "runs")
            self.session_recorder = create_session_recorder(runs_dir)

        # Running flag
        self._running = False
        # Draining flag: when True, stop launching new tasks but let existing ones complete
        self._draining = False

        # Background agent tasks (sketchers/provers running in parallel)
        # Maps task -> chapter_id so we can track what each task is working on
        self._agent_tasks: dict[asyncio.Task, tuple[str, str]] = {}  # task -> (chapter_id, agent_id)
        # Track (chapter_id, theorem_name) for active prover tasks to enforce one-agent-per-theorem
        self._active_prover_theorems: dict[asyncio.Task, tuple[str, str]] = {}

        # Background maintain contributor tasks (work on assigned issues)
        # Maps task -> (agent_id, issue_id) to track which issue each agent is working on
        self._maintain_tasks: dict[asyncio.Task, tuple[str, int]] = {}

        # Background scanner tasks (separate from agent tasks to avoid interference)
        # Maps task -> scanner_id
        self._scanner_tasks: dict[asyncio.Task, str] = {}

        # Background triage tasks (separate for cleaner lifecycle management)
        # Maps task -> agent_id (e.g., "triage-1")
        self._triage_tasks: dict[asyncio.Task, str] = {}
        self._triage_last_run: float = 0.0  # Last time we launched one
        self._triage_interval: float = 300.0  # Run every 5 minutes

        # Scanner timing (like triage, runs periodically)
        self._scanner_last_run: float = 0.0  # Last time we launched one
        self._scanner_interval: float = 300.0  # Run every 5 minutes

        # Progress agent tasks (similar to scanners)
        # Maps task -> progress_id
        self._progress_tasks: dict[asyncio.Task, str] = {}
        self._progress_last_run: float = 0.0  # Last time we launched one
        self._progress_interval: float = 300.0  # Run every 5 minutes

        # Background review tasks (reviews running in parallel)
        # Maps task -> (pr_id, revision_number) so we can track which PR/revision each review is for
        self._review_tasks: dict[asyncio.Task, tuple[str, int]] = {}

        # Distributed mode: auto-detect from SLURM world_size > 1
        self._is_distributed = get_world_size() > 1
        self._zmq_server: ZmqQueueServer | None = None
        # Maps task_id -> (chapter_id, agent_id, asyncio.Future) for pending distributed tasks
        self._pending_distributed: dict[str, tuple[str, str, str, int, asyncio.Future]] = {}

        if self._is_distributed:
            world_size = get_world_size()
            base_port = get_master_port()
            self._zmq_server = ZmqQueueServer("*", base_port)
            logger.info(f"[COORDINATOR] Distributed mode enabled (world_size={world_size}, port={base_port})")
            print(f"[COORDINATOR] Distributed mode: {world_size} processes", flush=True)
        else:
            # Local mode only: configure global REPL pool for running agents directly
            # In distributed mode, workers configure their own pools.
            from .agents.lean_tools import configure_global_pool

            configure_global_pool(
                workspace=config.base_project,
                pool_size=config.lean_pool_size,
            )
            logger.info("[COORDINATOR] Running in local mode (world_size=1)")
            print("[COORDINATOR] Local mode (single process)", flush=True)

    def clean(self) -> None:
        """Wipe previous run data and start truly from scratch.

        This performs a thorough clean:
        1. Removes worktrees
        2. Removes all Lean files (*.lean) - keeps tex/source files
        3. Reinitializes the git repo with a single clean initial commit
        4. Resets all state
        """
        import subprocess

        logger.info("Cleaning previous run data (full reset)...")

        # Clean worktrees
        if self.config.worktrees_root.exists():
            shutil.rmtree(self.config.worktrees_root)
            logger.info(f"Removed worktrees: {self.config.worktrees_root}")

        # Remove issues folder (will be regenerated fresh)
        issues_dir = self._get_issues_dir()
        if issues_dir.exists():
            shutil.rmtree(issues_dir)
            logger.info(f"Removed issues folder: {issues_dir}")

        # Remove all Lean files from the project
        lean_files_removed = 0
        for lean_file in self.base_project.rglob("*.lean"):
            # Skip any files in .lake or hidden directories
            if any(part.startswith(".") for part in lean_file.parts):
                continue
            # Skip lakefile.lean (project configuration)
            if lean_file.name == "lakefile.lean":
                continue
            lean_file.unlink()
            lean_files_removed += 1
            logger.debug(f"Removed: {lean_file}")
        logger.info(f"Removed {lean_files_removed} Lean files")

        # Also remove .lake/build directory (compiled artifacts) but keep packages
        lake_build = self.base_project / ".lake" / "build"
        if lake_build.exists():
            shutil.rmtree(lake_build)
            logger.info("Removed .lake/build directory")

        # Reinitialize git repo with a single initial commit
        logger.info("Reinitializing git repository...")

        # Remove existing .git directory
        git_dir = self.base_project / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir)
            logger.info("Removed existing .git directory")

        # Initialize new git repo with main branch (git init -b main)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.base_project,
            check=True,
            capture_output=True,
        )
        logger.info("Initialized new git repository with 'main' branch")

        # Ensure .repoprover and runs/ are in .gitignore
        gitignore_path = self.base_project / ".gitignore"
        required_ignores = [".repoprover/", "runs/"]
        if gitignore_path.exists():
            content = gitignore_path.read_text()
            missing = [e for e in required_ignores if e not in content]
            if missing:
                with open(gitignore_path, "a") as f:
                    if not content.endswith("\n"):
                        f.write("\n")
                    for entry in missing:
                        f.write(f"{entry}\n")
                logger.info("Added %s to .gitignore", ", ".join(missing))
        else:
            gitignore_path.write_text("\n".join(required_ignores) + "\n")
            logger.info("Created .gitignore with %s", ", ".join(required_ignores))

        # Generate initial issues from target theorems (before git add -A so it's included)
        self._generate_initial_issues()

        # Generate CONTENTS.md (before git add -A so it's included)
        self._generate_contents_md()

        # Add all files and create initial commit
        subprocess.run(
            ["git", "add", "-A"],
            cwd=self.base_project,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit (clean start)"],
            cwd=self.base_project,
            check=True,
            capture_output=True,
        )
        logger.info("Created initial commit")

        # Reset state (keep chapters, clear PRs)
        self.state.prs = {}
        self.state.completed_theorems = {}
        for chapter_id in self.state.chapters:
            self.state.chapters[chapter_id]["sketch_merged"] = False
        self.save_state()
        logger.info("Reset state (kept chapters, cleared PRs)")

        # Recreate worktrees dir
        self.config.worktrees_root.mkdir(parents=True, exist_ok=True)

    # =========================================================================
    # Issue Tracking
    # =========================================================================

    def _get_issues_dir(self) -> Path:
        """Get the path to issues/ folder in the repo root."""
        return self.base_project / "issues"

    def _parse_issues(self) -> list[dict[str, Any]]:
        """Parse all issues from issues/ folder.

        Returns:
            List of issue dicts: [{id, is_open, description, origin}, ...]
        """
        issues_dir = self._get_issues_dir()
        if not issues_dir.exists():
            return []

        issues = []
        for issue_file in sorted(issues_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(issue_file.read_text())
                if data:
                    issues.append(
                        {
                            "id": issue_file.stem,
                            "is_open": data.get("status") == "open",
                            "description": data.get("description", ""),
                            "origin": data.get("origin", ""),
                        }
                    )
            except (ValueError, yaml.YAMLError):
                pass
        return issues

    def _count_issues(self) -> tuple[int, int]:
        """Count issues in issues/ folder.

        Returns:
            (open_count, closed_count)
        """
        issues_dir = self._get_issues_dir()
        if not issues_dir.exists():
            return 0, 0

        open_count = closed_count = 0
        for issue_file in issues_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(issue_file.read_text())
                if data and data.get("status") == "open":
                    open_count += 1
                elif data and data.get("status") == "closed":
                    closed_count += 1
            except yaml.YAMLError:
                pass
        return open_count, closed_count

    def _convert_prs_to_issues(self, prs: list[SimplePR], batch_size: int = 4) -> list[str]:
        """Convert existing PRs to issues documenting unfinished work.

        Creates issue files in issues/ folder, batching multiple PRs per file.
        Includes the actual diff content from each PR branch so agents can
        directly copy useful code without needing to checkout branches.

        Args:
            prs: List of PRs to convert to issues
            batch_size: Number of PRs to group per issue file (default: 4)

        Returns:
            List of created issue IDs
        """
        if not prs:
            return []

        issues_dir = self._get_issues_dir()
        issues_dir.mkdir(exist_ok=True)

        created_issue_ids = []

        # Process PRs in batches
        for i in range(0, len(prs), batch_size):
            batch = prs[i : i + batch_size]
            issue_id = secrets.token_hex(4)

            # Build description for the batch
            entries = []
            for pr in batch:
                agent_type_desc = {
                    "sketch": "chapter sketch",
                    "prove": "theorem proof",
                    "fix": "fix contribution",
                    "scan": "codebase scan",
                    "maintain": "maintenance task",
                    "triage": "issue triage",
                    "progress": "progress update",
                }.get(pr.agent_type, pr.agent_type)

                target_desc = ""
                if pr.theorem_name:
                    target_desc = f" for `{pr.theorem_name}`"
                elif pr.chapter_id:
                    target_desc = f" for `{pr.chapter_id}`"

                feedback_line = ""
                if pr.last_review_feedback:
                    # Truncate long feedback
                    feedback = pr.last_review_feedback[:200]
                    if len(pr.last_review_feedback) > 200:
                        feedback += "..."
                    feedback_line = f"\n  Last feedback: {feedback}"

                # Get the diff for this PR's branch
                diff_section = ""
                try:
                    _, diff_content = self._get_branch_diff(pr.branch_name)
                    if diff_content and diff_content.strip():
                        # Truncate very large diffs
                        max_diff_len = 8000
                        if len(diff_content) > max_diff_len:
                            diff_content = (
                                diff_content[:max_diff_len] + f"\n\n... [truncated, {len(diff_content)} chars total]"
                            )
                        diff_section = f"\n  <diff>\n{diff_content}\n  </diff>"
                except Exception as e:
                    logger.warning(f"Failed to get diff for branch {pr.branch_name}: {e}")
                    diff_section = f"\n  (Could not retrieve diff: {e})"

                entry = (
                    f"- **{agent_type_desc}{target_desc}**\n"
                    f"  Branch: `{pr.branch_name}`\n"
                    f"  Revisions: {pr.revision_count}, PR: {pr.pr_id}"
                    f"{feedback_line}"
                    f"{diff_section}"
                )
                entries.append(entry)

            description = (
                f"Unfinished work from {len(batch)} PRs.\n\n"
                "⚠️ **CRITICAL: Evaluate this work critically!**\n"
                "Before salvaging any code, cross-reference with `CONTENTS.md` and existing issue files. "
                "Previous agents may have gone down rabbit holes or built unnecessary infrastructure. "
                "Only salvage work that directly advances the current goals - not speculative scaffolding "
                "or over-engineered solutions.\n\n" + "\n\n".join(entries) + "\n\n---\n\n"
                "**How to handle this issue:**\n"
                "1. **First**, review `CONTENTS.md` and open issues to understand current priorities\n"
                "2. Remove any entries that are clearly outdated, off-track, or building unnecessary infrastructure\n"
                "3. For salvageable work, save anything useful that is not redundant with "
                "the current state of the codebase:\n"
                "   - You can copy code directly from the `<diff>` sections above\n"
                '   - Or use `git_checkout_file(ref="<branch_name>", paths=["path/to/file.lean"])` '
                "to retrieve specific files\n"
                "   - **Important:** Make sure not to lose any useful work - check the diff carefully\n"
                "4. Keep any remaining entries in this issue for later agents to pick up\n"
                "5. Once all entries are handled, mark this issue as closed"
            )

            issue_data = {
                "status": "open",
                "origin": f"converted from {len(batch)} unfinished PRs",
                "description": description,
            }

            issue_path = issues_dir / f"{issue_id}.yaml"
            try:
                issue_path.write_text(yaml.safe_dump(issue_data, sort_keys=False, allow_unicode=True))
                logger.info(f"Created issue {issue_id} from {len(batch)} PRs")
                created_issue_ids.append(issue_id)
            except Exception as e:
                logger.error(f"Failed to create issue from PR batch: {e}")

        return created_issue_ids

    def _generate_initial_issues(self) -> None:
        """Generate initial issues from manifest.

        Creates issues/ folder with individual YAML files. Target theorem issues are added
        later when each chapter's sketch is merged (see _add_target_theorem_issues).
        """
        manifest_path = self.base_project / "manifest.json"
        if not manifest_path.exists():
            logger.debug("No manifest.json found, skipping initial issue generation")
            return

        with open(manifest_path) as f:
            manifest = json.load(f)

        issues_dir = self._get_issues_dir()
        issues_dir.mkdir(exist_ok=True)

        # Write README for humans
        (issues_dir / "README.md").write_text(
            "# Issues\n\nEach `.yaml` file is one issue.\n"
            "Close by changing `status: open` to `status: closed`.\n"
            "Filenames are random hex IDs to avoid conflicts.\n"
        )

        issue_count = 0
        for ch in manifest.get("chapters", []):
            source_path = ch.get("source_path", ch.get("source", ""))
            if source_path:
                issue_id = secrets.token_hex(4)
                issue_data = {
                    "status": "open",
                    "origin": "initial chapter sketch",
                    "description": f"formalize `{source_path}` (TeX → Lean)",
                }
                issue_path = issues_dir / f"{issue_id}.yaml"
                issue_path.write_text(yaml.safe_dump(issue_data, sort_keys=False, allow_unicode=True))
                issue_count += 1

        if issue_count == 0:
            logger.debug("No chapters found in manifest")
            return

        # Commit to main
        subprocess.run(
            ["git", "add", "issues/"],
            cwd=self.base_project,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "commit", "-m", f"Add {issue_count} initial issues (sketch chapters)"],
            cwd=self.base_project,
            capture_output=True,
            timeout=30,
        )

        self.save_state()
        logger.info(f"Generated {issue_count} initial issues (sketch chapters only)")

    def _generate_contents_md(self) -> None:
        """Generate initial CONTENTS.md from manifest.

        Creates a table of contents mapping tex sources to Lean files.
        Agents are instructed to keep this file updated when they modify the repo structure.
        """
        manifest_path = self.base_project / "manifest.json"
        if not manifest_path.exists():
            logger.debug("No manifest.json found, skipping CONTENTS.md generation")
            return

        with open(manifest_path) as f:
            manifest = json.load(f)

        chapters = manifest.get("chapters", [])
        if not chapters:
            logger.debug("No chapters found in manifest")
            return

        # Build the CONTENTS.md content
        lines = [
            "# Contents",
            "",
            "## TeX Sources",
            "",
            "| Chapter | Source | Lean Entry Point | Comments |",
            "|---------|--------|------------------|----------|",
        ]

        for ch in chapters:
            chapter_id = ch.get("id", "")
            title = ch.get("title", chapter_id)
            source_path = ch.get("source_path", ch.get("source", ""))
            lines.append(f"| {title} | `{source_path}` | | |")

        lines.extend(
            [
                "",
                "---",
                "",
                "## Lean Codebase Overview",
                "",
                "*(Document the structure of the Lean codebase here as it evolves:",
                "module hierarchy, shared utilities, naming conventions, etc.)*",
                "",
            ]
        )

        contents_path = self.base_project / "CONTENTS.md"
        contents_path.write_text("\n".join(lines))
        logger.info(f"Generated CONTENTS.md with {len(chapters)} chapters")

    def _run_git_with_retry(
        self,
        args: list[str],
        *,
        timeout: float = 60.0,
        retries: int = 2,
        retry_delay: float = 2.0,
        allow_noop: bool = False,
    ) -> bool:
        """Run a git command from base_project with retries and backoff."""
        from .utils import run_git_with_retry

        success, _error, _result = run_git_with_retry(
            args,
            cwd=self.base_project,
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
            allow_noop=allow_noop,
        )
        return success

    def _add_target_theorem_issues(self, chapter_id: str) -> None:
        """Add target theorem issues for a chapter after its sketch merges.

        Called from _process_merges when a sketch PR is successfully merged.
        Creates new issue YAML files in issues/ folder and commits to main.
        """
        chapter_info = self.state.chapters.get(chapter_id, {})
        target_theorems = chapter_info.get("target_theorems", [])

        if not target_theorems:
            return

        issues_dir = self._get_issues_dir()
        issues_dir.mkdir(exist_ok=True)

        created_issue_paths: list[Path] = []
        new_count = 0
        for theorem_name in target_theorems:
            issue_id = secrets.token_hex(4)
            issue_data = {
                "status": "open",
                "origin": "target theorem",
                "description": f"[{chapter_id}] Prove `{theorem_name}`",
            }
            issue_path = issues_dir / f"{issue_id}.yaml"
            issue_path.write_text(yaml.safe_dump(issue_data, sort_keys=False, allow_unicode=True))
            created_issue_paths.append(issue_path)
            new_count += 1

        if new_count == 0:
            return

        if not self._run_git_with_retry(["add", "issues/"], retries=3, timeout=30):
            logger.error("Failed to stage target theorem issues for %s", chapter_id)
            return

        commit_message = f"Add {new_count} target theorem issues for {chapter_id}"
        if not self._run_git_with_retry(["commit", "-m", commit_message], timeout=60, retries=3, allow_noop=True):
            logger.error("Failed to commit target theorem issues for %s", chapter_id)
            return

        self.save_state()
        logger.info(f"Added {new_count} target theorem issues for {chapter_id}")

    # =========================================================================
    # Distributed Agent Dispatch
    # =========================================================================

    def _dispatch_agent(
        self,
        agent_type: str,
        task: ContributorTask,
        agent_id: str,
        chapter_id: str,
        feedback: str = "",
        revision_number: int = 0,
    ) -> asyncio.Task:
        """Dispatch an agent - locally or to a distributed worker.

        This is the unified entry point for launching all contributor agents.
        In local mode, uses asyncio.to_thread for background execution.
        In distributed mode, sends task to ZMQ queue and returns a Future-wrapped Task.

        Args:
            agent_type: Type of agent (sketch, prove, triage, scan, progress, maintain)
            task: The ContributorTask to execute
            agent_id: Unique identifier for this agent (also used as branch name)
            chapter_id: Chapter being worked on (empty for cross-chapter agents)
            feedback: Review feedback for revisions
            revision_number: Current revision attempt number

          Returns:
            An asyncio.Task that will resolve to SimplePR | None
        """
        if self._is_distributed and self._zmq_server is not None:
            # Distributed mode: create worktree, push to ZMQ queue, return Future
            task_id = f"{agent_id}-{uuid.uuid4().hex[:8]}"

            # Coordinator creates worktree BEFORE dispatching to worker
            # This ensures worker can expect the worktree to exist
            try:
                worktree = self.worktree_pool.setup(agent_id)
                worktree_path = str(worktree.worktree_path)
                branch_name = worktree.branch_name
            except Exception as e:
                logger.error(f"Failed to create worktree for {agent_id}: {e}")

                # Record the failure for observability
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "agent_dispatch_failed",
                        agent_id=agent_id,
                        agent_type=agent_type,
                        chapter_id=chapter_id,
                        reason="worktree_setup_failed",
                        error=str(e),
                    )

                # Return a failed task
                async def failed_task() -> None:
                    return None

                return asyncio.create_task(failed_task())

            # Get run_dir for worker recording
            run_dir_str = None
            if self.session_recorder:
                run_dir_str = str(self.session_recorder.run_dir)

            dist_task = DistributedTask(
                task_id=task_id,
                agent_type=agent_type,
                task_data=contributor_task_to_dict(task),
                agent_id=agent_id,
                chapter_id=chapter_id,
                worktree_path=worktree_path,
                branch_name=branch_name,
                feedback=feedback,
                revision_number=revision_number,
                run_dir=run_dir_str,
            )

            # Record agent launch event (coordinator handles orchestration events)
            if self.session_recorder:
                self.session_recorder.record_agent_launched(
                    agent_id=agent_id,
                    agent_type=agent_type,
                    chapter_id=chapter_id,
                    theorem_name=task.theorem_name,
                    revision_number=revision_number,
                    issue_id=task.issue_id,
                )

            # Create a Future that will be resolved by _harvest_distributed_results
            loop = asyncio.get_event_loop()
            future: asyncio.Future[SimplePR | None] = loop.create_future()
            self._pending_distributed[task_id] = (chapter_id, agent_id, agent_type, revision_number, future)

            # Send task to workers
            self._zmq_server.put(dist_task.to_dict())
            logger.debug(f"[COORDINATOR] Dispatched task {task_id} to distributed workers")

            # Wrap future in a Task for compatibility with existing code
            async def await_future() -> SimplePR | None:
                return await future

            return asyncio.create_task(await_future())
        else:
            # Local mode - run _run_contributor in thread
            return asyncio.create_task(
                asyncio.to_thread(
                    self._run_contributor,
                    agent_type=agent_type,
                    task=task,
                    agent_id=agent_id,
                    chapter_id=chapter_id,
                    feedback=feedback,
                    revision_number=revision_number,
                )
            )

    async def _harvest_distributed_results(self) -> bool:
        """Poll ZMQ result queue and resolve pending futures.

        This is the coordinator-side RPC handler for _run_contributor results.
        Workers return ContributorResult fields + branch_name, and we do all
        post-processing here (diff capture, PR creation, recording).

        Returns:
            True if any results were harvested, False otherwise.
        """
        if not self._is_distributed or self._zmq_server is None:
            return False

        made_progress = False

        # Poll for results (non-blocking)
        while True:
            try:
                result_dict = self._zmq_server.get(block=False)
            except queue.Empty:
                break

            result = DistributedResult.from_dict(result_dict)
            task_id = result.task_id

            if task_id not in self._pending_distributed:
                logger.warning(f"[COORDINATOR] Received result for unknown task {task_id}")
                continue

            orig_chapter_id, orig_agent_id, agent_type, revision_number, future = self._pending_distributed.pop(task_id)
            agent_id = result.agent_id
            chapter_id = result.chapter_id

            # Use unified result processing (handles both new PRs and revisions)
            pr = self._process_contributor_result(
                status=result.status,
                branch_name=result.branch_name,
                agent_id=agent_id,
                agent_type=agent_type,
                chapter_id=chapter_id,
                description=result.description,
                theorem_name=result.theorem_name,
                issue_id=result.issue_id,
                revision_number=revision_number,
                error=result.error,
                fix_request=result.fix_request,
                issue_text=result.issue_text,
                iterations=result.iterations,
            )

            future.set_result(pr)
            logger.info(f"[COORDINATOR] Harvested result for {agent_id}: status={result.status}")

            made_progress = True

        return made_progress

    def _process_contributor_result(
        self,
        status: str,
        branch_name: str,
        agent_id: str,
        agent_type: str,
        chapter_id: str,
        description: str = "",
        theorem_name: str | None = None,
        issue_id: str | None = None,
        revision_number: int = 0,
        error: str | None = None,
        fix_request: str | None = None,
        issue_text: str | None = None,
        iterations: int | None = None,
    ) -> SimplePR | None:
        """Unified result processing for both local and distributed runs.

        Creates PRs, records events, handles revisions, and manages state.
        This is the single source of truth for processing agent results.

        For revisions (when an existing PR has status="revision_in_progress"):
        - Updates the existing PR instead of creating a new one
        - Updates diff stats and description from the new work

        Args:
            status: Result status (done, fix, issue, blocked, error)
            branch_name: Git branch name from the agent
            agent_id: The agent that produced this result
            agent_type: Type of agent (sketch, prove, triage, scan, progress, maintain)
            chapter_id: Chapter ID
            description: PR description
            theorem_name: Theorem name (for prove agents)
            issue_id: Issue ID (for maintain agents - the assigned issue)
            revision_number: Revision iteration (0 = initial)
            error: Error message if status is error
            fix_request: Fix request text if status is fix
            issue_text: Issue text if status is issue
            iterations: Number of agent iterations

        Returns:
            SimplePR if a PR was created/updated, None otherwise
        """
        pr: SimplePR | None = None

        # Check if this is a revision of an existing PR
        existing_pr = self._find_revision_in_progress(agent_id)

        if status == "done":
            diff_stats, diff_content = self._get_branch_diff(branch_name)

            if existing_pr:
                # Update existing PR for revision
                existing_pr.status = "pending_review"
                existing_pr.revision_count = revision_number
                existing_pr.diff_stats = diff_stats
                existing_pr.description = description
                existing_pr.branch_name = branch_name
                # Store diff content for this revision (for resume support)
                if diff_content:
                    existing_pr.diffs[revision_number] = diff_content
                pr = existing_pr
                logger.info(f"[{agent_id}] Updated PR {pr.pr_id} for revision {revision_number}")
            else:
                # Create new PR
                pr = SimplePR(
                    pr_id=f"pr-{uuid.uuid4().hex[:8]}",
                    branch_name=branch_name,
                    chapter_id=chapter_id,
                    agent_type=agent_type,
                    agent_id=agent_id,
                    theorem_name=theorem_name,
                    issue_id=issue_id,
                    status="pending_review",
                    revision_count=revision_number,
                    diff_stats=diff_stats,
                    description=description,
                    diffs={revision_number: diff_content} if diff_content else {},
                )
                # Add new PR to state
                self.state.prs[pr.pr_id] = pr
                logger.info(f"[{agent_id}] Created new PR {pr.pr_id}")

            if self.session_recorder:
                self.session_recorder.record_pr_submitted(
                    pr_id=pr.pr_id,
                    agent_id=agent_id,
                    branch_name=branch_name,
                    agent_type=agent_type,
                    chapter_id=chapter_id,
                    theorem_name=theorem_name,
                    diff=diff_content,
                    revision_number=revision_number,
                )

        elif status in ("fix", "issue"):
            # Agent committed partial progress (fix) or created an issue file (issue)
            # Both submit a PR for review
            diff_stats, diff_content = self._get_branch_diff(branch_name)
            pr_description = description or issue_text or ""

            if existing_pr:
                existing_pr.status = "pending_review"
                existing_pr.revision_count = revision_number
                existing_pr.diff_stats = diff_stats
                existing_pr.description = pr_description
                existing_pr.branch_name = branch_name
                if status == "fix":
                    existing_pr.agent_type = "fix"
                # Store diff content for this revision (for resume support)
                if diff_content:
                    existing_pr.diffs[revision_number] = diff_content
                pr = existing_pr
                logger.info(f"[{agent_id}] Updated {status} PR {pr.pr_id}")
            else:
                pr = SimplePR(
                    pr_id=f"pr-{uuid.uuid4().hex[:8]}",
                    branch_name=branch_name,
                    chapter_id=chapter_id,
                    agent_type="fix" if status == "fix" else agent_type,
                    agent_id=agent_id,
                    theorem_name=theorem_name,
                    issue_id=issue_id,
                    status="pending_review",
                    revision_count=revision_number,
                    diff_stats=diff_stats,
                    description=pr_description,
                    diffs={revision_number: diff_content} if diff_content else {},
                )
                self.state.prs[pr.pr_id] = pr
                logger.info(f"[{agent_id}] Created {status} PR {pr.pr_id}")

            if self.session_recorder:
                self.session_recorder.record_pr_submitted(
                    pr_id=pr.pr_id,
                    agent_id=agent_id,
                    branch_name=branch_name,
                    agent_type=pr.agent_type,
                    chapter_id=chapter_id,
                    theorem_name=theorem_name,
                    diff=diff_content,
                    revision_number=revision_number,
                )
                # Record status-specific events
                if status == "fix" and fix_request:
                    self.session_recorder.record_event(
                        "fix_request",
                        pr_id=pr.pr_id,
                        theorem_name=theorem_name,
                        request=fix_request,
                    )
                elif status == "issue":
                    self.session_recorder.record_event(
                        "issue_escalated",
                        pr_id=pr.pr_id,
                        theorem_name=theorem_name,
                        chapter_id=chapter_id,
                        issue_text=issue_text,
                    )

        elif status == "blocked":
            # Agent blocked/no changes needed (permanent failure)
            logger.info(f"[{agent_id}] Agent blocked/no changes needed")
            if existing_pr:
                existing_pr.status = "failed"
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "pr_status_changed",
                        pr_id=existing_pr.pr_id,
                        new_status="failed",
                        reason="agent_blocked",
                        agent_id=agent_id,
                    )
            # Clean up worktree since this is a terminal state
            self.worktree_pool.cleanup(agent_id)

        else:
            # Error or unknown status (permanent failure)
            # Build a meaningful error message
            error_msg = error or f"Unknown status: {status}"
            logger.warning(f"[{agent_id}] Agent failed: {error_msg}")
            if existing_pr:
                existing_pr.status = "failed"
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "pr_status_changed",
                        pr_id=existing_pr.pr_id,
                        new_status="failed",
                        reason="agent_error",
                        error=error_msg,
                        status=status,
                        agent_id=agent_id,
                    )
            # Clean up worktree since this is a terminal state
            self.worktree_pool.cleanup(agent_id)

        # Record agent completion (coordinator is source of truth)
        if self.session_recorder:
            self.session_recorder.record_agent_done(
                agent_id=agent_id,
                status=status,
                chapter_id=chapter_id,
                theorem_name=theorem_name,
                iterations=iterations,
            )

        return pr

    def _get_branch_diff(self, branch_name: str) -> tuple[dict[str, int], str]:
        """Get diff stats and content for a branch from the shared repo.

        Works via NFS - branch exists in shared git repo even after worktree release.

        Returns:
            (diff_stats, diff_content) where diff_stats is {"+": additions, "-": deletions}
        """
        # Get full diff content
        diff_content_result = subprocess.run(
            ["git", "diff", f"main...{branch_name}"],
            cwd=self.base_project,
            capture_output=True,
            text=True,
        )
        diff_content = diff_content_result.stdout

        # Parse stats from the unified diff
        additions, deletions = parse_diff_stats(diff_content)
        diff_stats = {"+": additions, "-": deletions}

        return diff_stats, diff_content

    async def _shutdown_distributed(self) -> None:
        """Shutdown distributed workers gracefully.

        Sends None (poison pill) to each worker to signal them to exit.
        Called from the coordinator's cleanup code.

        Note: Workers (including the rank-0 worker in CLI) will receive shutdown
        signals and exit gracefully. The coordinator just needs to send enough
        poison pills for all workers.
        """
        if not self._is_distributed or self._zmq_server is None:
            return

        world_size = get_world_size()
        # All ranks are workers in distributed mode (including rank 0)
        num_workers = world_size

        if num_workers <= 0:
            return

        logger.info(f"Sending shutdown signal to {num_workers} workers...")
        print(f"[COORDINATOR] Sending shutdown to {num_workers} workers...", flush=True)

        # Send poison pill to each worker
        for i in range(num_workers):
            self._zmq_server.put(None)

        # Give workers time to process the shutdown signal
        await asyncio.sleep(2)

        # Close ZMQ server
        self._zmq_server.close()
        self._zmq_server = None

        logger.info("Distributed shutdown complete")
        print("[COORDINATOR] Distributed shutdown complete", flush=True)

    # =========================================================================
    # Main Run Loop
    # =========================================================================

    def run(self) -> None:
        """Main run loop. Runs until all chapters complete or interrupted."""
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        """Async main run loop.

        Uses a priority-based step list. Each iteration runs steps from
        highest to lowest priority; the first step that makes progress
        causes a restart from the top so that high-priority work (landing
        results, merging) is never starved by lower-priority launches.

        Priority rationale (highest first):
        - Harvest reviews: land review results (enables merges)
        - Merge: gets approved code onto main
        - Launch reviews: non-blocking, just fires off tasks
        - Harvest agents: land agent results (new PRs for review)
        - Revisions: re-launches agents with feedback
        - Launch provers/sketchers: new work
        """
        self._running = True

        # Use a large thread pool — most threads are I/O-bound (waiting on
        # LLM API calls or subprocess builds), so they're cheap.  The default
        # pool (32 threads) gets starved when many agents + reviews run
        # concurrently, which blocks merges from getting a thread.
        import concurrent.futures

        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=512))

        logger.info(f"Starting run for {self.config.title}")

        # Log PR queue status at startup
        prs_by_status: dict[str, int] = {}
        for pr in self.state.prs.values():
            prs_by_status[pr.status] = prs_by_status.get(pr.status, 0) + 1
        total_prs = len(self.state.prs)
        if total_prs > 0:
            status_parts = [f"{count} {status}" for status, count in sorted(prs_by_status.items())]
            logger.info(f"Resuming with {total_prs} PRs in queue: {', '.join(status_parts)}")
        else:
            logger.info("Starting fresh (no PRs in queue)")

        # On resume: reset in-progress statuses since those agents are gone
        reset_count = 0
        for pr in self.state.prs.values():
            if pr.status == "revision_in_progress":
                pr.status = "needs_revision"
                reset_count += 1
        if reset_count > 0:
            logger.info(f"Reset {reset_count} in-progress PRs to needs_revision (agents from previous run)")

        # On resume: optionally convert existing PRs to issues instead of relaunching
        if self.config.prs_to_issues:
            active_statuses = {"pending_review", "in_review", "needs_revision", "approved"}
            prs_to_convert = [pr for pr in self.state.prs.values() if pr.status in active_statuses]
            if prs_to_convert:
                logger.info(f"Converting {len(prs_to_convert)} active PRs to issues (prs_to_issues=True)")
                issue_ids = self._convert_prs_to_issues(prs_to_convert)
                # Mark all converted PRs as failed and clean up
                for pr in prs_to_convert:
                    if pr.agent_id:
                        self.worktree_pool.cleanup(pr.agent_id)
                    pr.status = "failed"
                    if self.session_recorder:
                        self.session_recorder.record_event(
                            "pr_converted_to_issue",
                            pr_id=pr.pr_id,
                            agent_id=pr.agent_id,
                            agent_type=pr.agent_type,
                            chapter_id=pr.chapter_id,
                            theorem_name=pr.theorem_name,
                        )
                logger.info(f"Converted {len(prs_to_convert)} PRs to {len(issue_ids)} issues")
                # Commit the new issues to main
                if issue_ids:
                    subprocess.run(
                        ["git", "add", "issues/"],
                        cwd=self.base_project,
                        capture_output=True,
                        timeout=30,
                    )
                    subprocess.run(
                        [
                            "git",
                            "commit",
                            "-m",
                            f"Convert {len(prs_to_convert)} unfinished PRs to {len(issue_ids)} issues",
                        ],
                        cwd=self.base_project,
                        capture_output=True,
                        timeout=30,
                    )

        # On resume: clear phantom scanner entries (tasks are gone)
        self.state.active_scanners.clear()

        # On resume: clear phantom progress entries (tasks are gone)
        self.state.active_progress.clear()

        self.save_state()

        if self.session_recorder:
            self.session_recorder.start(cwd=self.base_project)
            # Record initial proof/issue stats so viewer shows counts immediately
            self._record_proof_stats()

            # Record resumed agents for all active PRs in the queue
            # This ensures the viewer shows agents whose PRs are still in progress
            active_statuses = {"pending_review", "in_review", "needs_revision", "revision_in_progress", "approved"}
            resumed_count = 0
            for pr in self.state.prs.values():
                if pr.status in active_statuses and pr.agent_id:
                    # Regenerate diff_stats and diffs if missing (e.g., old state file or crash before saving)
                    if (pr.diff_stats is None or not pr.diffs) and pr.branch_name:
                        try:
                            diff_stats, diff_content = self._get_branch_diff(pr.branch_name)
                            if pr.diff_stats is None:
                                pr.diff_stats = diff_stats
                                logger.info(
                                    f"Regenerated diff_stats for PR {pr.pr_id}: +{diff_stats.get('+', 0)}/-{diff_stats.get('-', 0)}"
                                )
                            # Regenerate diffs for backward compat with old state files
                            if not pr.diffs and diff_content:
                                pr.diffs[pr.revision_count] = diff_content
                                logger.info(
                                    f"Regenerated diff for PR {pr.pr_id} rev {pr.revision_count} (backward compat)"
                                )
                        except Exception as e:
                            logger.warning(f"Could not regenerate diff data for PR {pr.pr_id}: {e}")

                    # Read agent dialog from previous session if available
                    dialog = read_agent_dialog(self.session_recorder.run_dir, pr.agent_id)
                    if dialog:
                        logger.info(f"Read {len(dialog)} dialog events for resumed agent {pr.agent_id}")

                    self.session_recorder.record_agent_resumed(
                        agent_id=pr.agent_id,
                        agent_type=pr.agent_type,
                        chapter_id=pr.chapter_id,
                        pr_id=pr.pr_id,
                        pr_status=pr.status,
                        theorem_name=pr.theorem_name,
                        revision_number=pr.revision_count,
                        diff_stats=pr.diff_stats,
                        diffs=pr.diffs,
                        dialog=dialog if dialog else None,
                    )
                    resumed_count += 1
            if resumed_count > 0:
                logger.info(f"Recorded {resumed_count} resumed agents from active PRs")

        poll_interval = self.config.poll_interval
        loop_count = 0
        status_interval = 10  # Log status every N loops

        # Priority-ordered steps: later pipeline stages first
        # (merge/review before harvesting new agent results and launching)
        steps = [
            self._harvest_distributed_results,  # Resolve futures from remote workers (no-op if local)
            self._harvest_completed_reviews,
            self._process_merges,
            self._process_reviews,
            self._harvest_completed_agents,
            self._harvest_completed_scanners,
            self._harvest_completed_progress,
            self._harvest_completed_triage,
            self._harvest_completed_maintain_contributors,
            self._process_revisions,
            self._launch_pending_provers,
            self._launch_pending_sketchers,
            self._launch_maintain_contributors,
            self._launch_triage,
            self._launch_scanners,
            self._launch_progress_agents,
        ]

        try:
            while self._running:
                loop_count += 1
                progress = False
                for step in steps:
                    if await step():
                        progress = True

                # Log periodic status summary
                if loop_count % status_interval == 0:
                    self._log_status_summary()

                if not progress:
                    # Wait for something to complete before checking steps again
                    all_tasks = (
                        set(self._agent_tasks)
                        | set(self._maintain_tasks)
                        | set(self._scanner_tasks)
                        | set(self._progress_tasks)
                        | set(self._triage_tasks)
                        | set(self._review_tasks)
                    )
                    if all_tasks:
                        await asyncio.wait(
                            all_tasks,
                            timeout=poll_interval,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    else:
                        await asyncio.sleep(poll_interval)

                self.save_state()

                if (
                    self._is_complete()
                    and not self._agent_tasks
                    and not self._maintain_tasks
                    and not self._scanner_tasks
                    and not self._progress_tasks
                    and not self._triage_tasks
                    and not self._review_tasks
                ):
                    logger.info("All chapters complete!")
                    break

        except KeyboardInterrupt:
            logger.info("Interrupted by user, draining running tasks...")
            self._shutdown_reason = "interrupted_by_user"
            self._shutdown_error = None
            await self._drain_and_exit()
        except Exception:
            import traceback

            self._shutdown_error = traceback.format_exc()
            logger.exception("Coordinator crashed, draining running tasks...")
            self._shutdown_reason = "crashed"
            await self._drain_and_exit()
        finally:
            self._running = False
            self.save_state()
            # Shutdown distributed workers if in distributed mode
            await self._shutdown_distributed()
            if self.session_recorder:
                if self._is_complete():
                    status = "completed"
                else:
                    status = getattr(self, "_shutdown_reason", "interrupted")
                self.session_recorder.finalize(status, error=getattr(self, "_shutdown_error", None))

    def stop(self) -> None:
        """Stop the run loop."""
        self._running = False

    def _log_status_summary(self) -> None:
        """Log a summary of current coordinator status."""
        # Count tasks
        n_agents = len(self._agent_tasks)
        n_maintain = len(self._maintain_tasks)
        n_scanner = len(self._scanner_tasks)
        n_progress = len(self._progress_tasks)
        n_triage = len(self._triage_tasks)
        n_review = len(self._review_tasks)
        total_tasks = n_agents + n_maintain + n_scanner + n_progress + n_triage + n_review

        # Count PRs by status
        pr_counts: dict[str, int] = {}
        for pr in self.state.prs.values():
            pr_counts[pr.status] = pr_counts.get(pr.status, 0) + 1

        # Build status line
        task_parts = []
        if n_agents:
            task_parts.append(f"{n_agents} agents")
        if n_maintain:
            task_parts.append(f"{n_maintain} maintain")
        if n_scanner:
            task_parts.append(f"{n_scanner} scanners")
        if n_progress:
            task_parts.append(f"{n_progress} progress")
        if n_triage:
            task_parts.append(f"{n_triage} triage")
        if n_review:
            task_parts.append(f"{n_review} reviews")

        pr_parts = [f"{count} {status}" for status, count in sorted(pr_counts.items())]

        tasks_str = ", ".join(task_parts) if task_parts else "none"
        prs_str = ", ".join(pr_parts) if pr_parts else "none"

        logger.info(f"[STATUS] Tasks: {total_tasks} ({tasks_str}) | PRs: {len(self.state.prs)} ({prs_str})")

    def _cancel_all_tasks(self) -> None:
        """Cancel all running async tasks."""
        for task in (
            self._agent_tasks
            | self._maintain_tasks
            | self._scanner_tasks
            | self._progress_tasks
            | self._triage_tasks
            | self._review_tasks
        ):
            task.cancel()

    async def _drain_and_exit(self) -> None:
        """Drain all running tasks gracefully before exiting.

        Sets draining mode to stop launching new tasks, then waits for all
        existing tasks to complete. This allows clean shutdown where work
        in progress is finished rather than abruptly cancelled.
        """
        self._draining = True
        self._running = False  # Stop the main loop from iterating

        all_tasks = (
            set(self._agent_tasks)
            | set(self._maintain_tasks)
            | set(self._scanner_tasks)
            | set(self._progress_tasks)
            | set(self._triage_tasks)
            | set(self._review_tasks)
        )

        if not all_tasks:
            logger.info("No running tasks to drain")
            return

        logger.info(f"Draining {len(all_tasks)} running tasks...")
        print(f"[COORDINATOR] Draining {len(all_tasks)} running tasks...", flush=True)

        # Harvest steps - only the ones that collect results, not launch new work
        harvest_steps = [
            self._harvest_distributed_results,
            self._harvest_completed_reviews,
            self._harvest_completed_agents,
            self._harvest_completed_scanners,
            self._harvest_completed_progress,
            self._harvest_completed_triage,
            self._harvest_completed_maintain_contributors,
        ]

        # Wait for all tasks to complete, harvesting results as they finish
        while True:
            all_tasks = (
                set(self._agent_tasks)
                | set(self._maintain_tasks)
                | set(self._scanner_tasks)
                | set(self._progress_tasks)
                | set(self._triage_tasks)
                | set(self._review_tasks)
            )

            if not all_tasks:
                break

            # Harvest any completed tasks
            for step in harvest_steps:
                try:
                    await step()
                except Exception as e:
                    logger.exception(f"Error during drain harvest: {e}")

            # Wait for at least one task to complete
            remaining = (
                set(self._agent_tasks)
                | set(self._maintain_tasks)
                | set(self._scanner_tasks)
                | set(self._progress_tasks)
                | set(self._triage_tasks)
                | set(self._review_tasks)
            )
            if remaining:
                logger.info(f"  Waiting for {len(remaining)} tasks to complete...")
                await asyncio.wait(
                    remaining,
                    timeout=5.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )

        logger.info("All tasks drained successfully")
        print("[COORDINATOR] All tasks drained successfully", flush=True)

    # =========================================================================
    # Step 1: Launch Sketchers
    # =========================================================================

    async def _launch_pending_sketchers(self) -> bool:
        """Launch sketchers for ALL chapters that need initial sketch (non-blocking).

        Each chapter is launched only once. Retries happen later in _process_revisions
        only after review feedback. Tasks run in background and are collected by
        _harvest_completed_agents().
        """
        if self._draining:
            return False

        chapters_to_launch = []

        # Collect all chapters that need sketching
        for chapter_id, chapter_info in self.state.chapters.items():
            # Skip if already has a sketch PR (any status)
            if self._has_sketch_pr(chapter_id):
                continue

            # Skip if sketch already merged
            if chapter_info.get("sketch_merged"):
                continue

            # Skip if already has a running task
            if any(cid == chapter_id for cid, _ in self._agent_tasks.values()):
                continue

            chapters_to_launch.append(chapter_id)

        if not chapters_to_launch:
            return False

        # Cap concurrent sketchers to avoid API throttling
        active_sketchers = sum(1 for _, (_, aid) in self._agent_tasks.items() if aid.startswith("sketch-"))
        slots = max(0, self.config.max_concurrent_sketchers - active_sketchers)
        if slots == 0:
            return False
        chapters_to_launch = chapters_to_launch[:slots]

        logger.info(f"[LAUNCH] {len(chapters_to_launch)} sketcher(s) ({active_sketchers} already active)")

        for chapter_id in chapters_to_launch:
            agent_id = f"sketch-{chapter_id}-{uuid.uuid4().hex[:6]}"
            chapter = self.state.chapters.get(chapter_id, {})
            source_path = chapter.get("source_path", "")
            # For initial sketch, don't prescribe lean_path - let the sketcher decide
            # based on project structure (lakefile.toml, existing modules)
            contrib_task = ContributorTask.sketch(
                chapter_id=chapter_id,
                source_tex_path=source_path,
                lean_path="",  # Sketcher will explore and decide
            )
            task = self._dispatch_agent(
                agent_type="sketch",
                task=contrib_task,
                agent_id=agent_id,
                chapter_id=chapter_id,
            )
            self._agent_tasks[task] = (chapter_id, agent_id)
            logger.info(f"  → Started sketcher for {chapter_id}")

        return True  # We made progress by launching tasks

    async def _harvest_completed_agents(self) -> bool:
        """Check for completed agent tasks and process their results.

        This allows reviews to start as soon as ANY agent finishes, rather than
        waiting for all agents to complete.

        Note: Revision handling (updating existing PRs vs creating new ones) is
        handled inside _process_contributor_result(), which is called by the
        agent dispatch mechanisms. This function just harvests the already-processed
        results.
        """
        if not self._agent_tasks:
            return False

        # Check for completed tasks (non-blocking)
        done = set()
        for task in list(self._agent_tasks.keys()):
            if task.done():
                done.add(task)

        if not done:
            return False

        logger.info(f"[HARVEST] Harvesting {len(done)} completed agent task(s)")
        made_progress = False
        for task in done:
            chapter_id, agent_id = self._agent_tasks.pop(task)
            # Extract theorem_name before cleaning up (needed for logging)
            prover_info = self._active_prover_theorems.pop(task, None)
            theorem_name = prover_info[1] if prover_info else None
            try:
                result = task.result()
                if isinstance(result, SimplePR):
                    # PR was already processed and added to state by _process_contributor_result
                    logger.info(f"  ✓ Agent for {chapter_id} completed with PR {result.pr_id} (status={result.status})")
                    made_progress = True
                elif result is not None:
                    logger.warning(f"  ✗ Agent for {chapter_id} returned unexpected: {result}")
                    # Unexpected result - mark any in-progress PR as failed
                    self._mark_agent_pr_failed(
                        agent_id, reason="unexpected_result", error=f"Unexpected result: {type(result)}"
                    )
                    made_progress = True
                else:
                    # Agent returned None - it failed (already logged in _process_contributor_result)
                    # But if _process_contributor_result didn't find an existing PR, we need to mark it
                    logger.warning(f"  ✗ Agent for {chapter_id} returned None (failed)")
                    self._mark_agent_pr_failed(agent_id, reason="agent_returned_none")
                    made_progress = True
            except Exception as e:
                logger.exception(f"  ✗ Agent for {chapter_id} raised exception: {e}")
                # Update any in-progress PR to failed status (prevents stuck revision_in_progress)
                self._mark_agent_pr_failed(agent_id, reason="agent_exception", error=str(e))
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "agent_failed",
                        agent_id=agent_id,
                        chapter_id=chapter_id,
                        theorem_name=theorem_name,
                        error=str(e),
                    )
                made_progress = True

        return made_progress

    def _find_pr_for_chapter(
        self, chapter_id: str, agent_type: str, theorem_name: str | None = None
    ) -> SimplePR | None:
        """Find the active PR for a chapter, agent type, and optionally theorem."""
        for pr in self.state.prs.values():
            if pr.chapter_id == chapter_id and pr.agent_type == agent_type:
                if pr.status not in ("merged", "failed"):
                    if theorem_name is not None and pr.theorem_name != theorem_name:
                        continue
                    return pr
        return None

    def _find_revision_in_progress(self, agent_id: str) -> SimplePR | None:
        """Find a PR with revision_in_progress status owned by this agent.

        Always matches on agent_id to prevent cross-contamination — only the
        agent that originally created the PR should reclaim its revision.
        """
        for pr in self.state.prs.values():
            if pr.status != "revision_in_progress":
                continue
            if pr.agent_id == agent_id:
                return pr
        return None

    def _mark_agent_pr_failed(self, agent_id: str, reason: str, error: str | None = None) -> None:
        """Mark any in-progress PR for this agent as failed.

        This is called when an agent crashes with an exception or otherwise fails
        without going through normal result processing. It ensures the PR status
        is consistent with the agent status (both show failed/error).

        Without this, PRs can get stuck in 'revision_in_progress' when agents crash.
        """
        pr = self._find_revision_in_progress(agent_id)
        if pr:
            pr.status = "failed"
            logger.info(f"[{agent_id}] Marked PR {pr.pr_id} as failed (reason: {reason})")
            if self.session_recorder:
                self.session_recorder.record_event(
                    "pr_status_changed",
                    pr_id=pr.pr_id,
                    new_status="failed",
                    reason=reason,
                    error=error,
                    agent_id=agent_id,
                )
            # Clean up worktree since this is a terminal state
            self.worktree_pool.cleanup(agent_id)
            self.save_state()

    def _has_sketch_pr(self, chapter_id: str) -> bool:
        """Check if chapter has an active sketch PR."""
        for pr in self.state.prs.values():
            if pr.chapter_id == chapter_id and pr.agent_type == "sketch":
                if pr.status not in ("merged", "failed"):
                    return True
        return False

    def _run_contributor(
        self,
        agent_type: str,
        task: ContributorTask,
        agent_id: str | None = None,
        chapter_id: str = "",
        feedback: str = "",
        revision_number: int = 0,
    ) -> SimplePR | None:
        """Unified runner for all contributor agent types.

        This method handles the common pattern for running any contributor agent:
        1. Acquire worktree (agent_id is the branch name)
        2. Set up recording
        3. Run the agent with the task
        4. Create PR if successful

        Args:
            agent_type: Type of agent (sketch, prove, triage, scan, progress, maintain)
            task: The ContributorTask to execute
            agent_id: Optional agent ID (generated if not provided; also used as branch name)
            chapter_id: Chapter ID for chapter-based agents, empty for cross-chapter
            feedback: Review feedback for revision attempts
            revision_number: Current revision iteration (0 = initial)

        Returns:
            SimplePR if successful, None otherwise
        """
        # Generate agent_id if not provided
        if agent_id is None:
            agent_id = f"{agent_type}-{uuid.uuid4().hex[:6]}"

        is_revision = revision_number > 0
        logger.info(f"[{agent_id}] Starting {agent_type} contributor (revision={revision_number})")

        # Set up worktree (agent_id is also the branch name)
        worktree = self.worktree_pool.setup(agent_id)

        if worktree is None:
            logger.error(f"Failed to acquire worktree for {agent_type} {agent_id}")
            return None

        pr_submitted = False
        try:
            # Set up recording
            recorder = None
            if self.session_recorder:
                recorder = self.session_recorder.register_agent(
                    agent_id=agent_id,
                    agent_type=agent_type,
                    config={"chapter_id": chapter_id, "revision_number": revision_number},
                )
                self.session_recorder.record_agent_launched(
                    agent_id=agent_id,
                    agent_type=agent_type,
                    chapter_id=chapter_id,
                    theorem_name=task.theorem_name,
                    revision_number=revision_number,
                    issue_id=task.issue_id,
                )

            # Create and run agent
            agent = ContributorAgent(
                config=self.config.agent_config or AgentConfig(),
                repo_root=worktree.worktree_path,
                worktree_manager=worktree,
                learnings=self.learnings,
                recorder=recorder,
                task=task,
            )

            # Pass feedback for revisions
            run_kwargs: dict[str, Any] = {}
            if is_revision and feedback:
                run_kwargs["feedback"] = feedback
                run_kwargs["is_initial"] = False
            elif agent_type == "sketch":
                run_kwargs["is_initial"] = not is_revision

            result = agent.run_task(**run_kwargs)

            # Capture iterations before releasing recorder
            iterations = recorder._iteration_count if recorder else 0

            # Use unified result processing
            processed = self._process_contributor_result(
                status=result.status,
                branch_name=worktree.branch_name,
                agent_id=agent_id,
                agent_type=agent_type,
                chapter_id=chapter_id,
                description=result.description or "",
                theorem_name=task.theorem_name,
                issue_id=task.issue_id,
                revision_number=revision_number,
                error=result.error,
                fix_request=getattr(result, "fix_request", None),
                issue_text=getattr(result, "issue_text", None),
                iterations=iterations,
            )
            pr_submitted = processed is not None  # PR created → worktree stays for review
            return processed

        finally:
            # Keep worktree alive if a PR was submitted (reviewer will use it).
            # Clean up immediately for errors/blocked/issues since no review follows.
            if not pr_submitted:
                self.worktree_pool.cleanup(agent_id)

    # =========================================================================
    # Step 1b: Launch Triage
    # =========================================================================

    async def _launch_triage(self) -> bool:
        """Launch a triage agent to scan for stale issues.

        The triage agent reads the issues/ folder and identifies:
        - Issues that are already resolved (work done)
        - Issues that are obsolete/discarded (codebase took different direction)

        It marks them as closed but does NOT try to fix anything.
        Runs at most once per interval (5 minutes by default).
        """
        if self._draining:
            return False

        # Check if we already have a triage agent running
        if self._triage_tasks:
            return False

        # Check if enough time has passed since last run
        now = time.time()
        if now - self._triage_last_run < self._triage_interval:
            return False

        # Launch triage agent (it will decide what to tick)
        self._triage_last_run = now
        agent_id = f"triage-{uuid.uuid4().hex[:6]}"
        contrib_task = ContributorTask.triage()
        task = self._dispatch_agent(
            agent_type="triage",
            task=contrib_task,
            agent_id=agent_id,
            chapter_id="",  # Triage works across chapters
        )
        self._triage_tasks[task] = agent_id
        logger.info("[LAUNCH] Triage agent")

        self.save_state()
        return True

    # =========================================================================
    # Step 1c: Launch Refactor Scanners
    # =========================================================================

    async def _launch_scanners(self) -> bool:
        """Launch refactor scanners up to the concurrency limit.

        Dispatch model:
        - max_concurrent_scanners controls parallelism (default: 1)
        - Scanners run periodically (every 2 minutes by default)
        - Scanners cycle through types, each running once per cycle
        - When all types have run, the cycle resets

        NOTE: Using ContributorAgent scan mode for unified scanning.
        """
        if self._draining:
            return False

        # Check timing - only run periodically
        now = time.time()
        if now - self._scanner_last_run < self._scanner_interval:
            return False

        # Check capacity
        active_count = len(self.state.active_scanners)
        if active_count >= self.state.max_concurrent_scanners:
            return False

        # Get all Lean files
        lean_files = self._get_all_lean_files()
        if not lean_files:
            return False

        # Launch one scan agent
        scanner_id = f"scan-{uuid.uuid4().hex[:8]}"
        self.state.active_scanners[scanner_id] = "running"
        self._scanner_last_run = now  # Update timestamp after launching

        contrib_task = ContributorTask.scan()
        task = self._dispatch_agent(
            agent_type="scan",
            task=contrib_task,
            agent_id=scanner_id,
            chapter_id="",  # Scanners work across all chapters
        )
        self._scanner_tasks[task] = scanner_id
        logger.info(f"[LAUNCH] Scanner ({scanner_id})")

        self.save_state()
        return True

    # =========================================================================
    # Step 1d: Launch Progress Agents
    # =========================================================================

    async def _launch_progress_agents(self) -> bool:
        """Launch progress agents up to the concurrency limit.

        Dispatch model:
        - max_concurrent_progress controls parallelism (default: 1)
        - Progress agents run periodically (every 2 minutes by default)
        - They check target theorem status and update CONTENTS.md

        NOTE: Using ContributorAgent progress mode.
        """
        if self._draining:
            return False

        # Check timing - only run periodically
        now = time.time()
        if now - self._progress_last_run < self._progress_interval:
            return False

        # Check capacity
        active_count = len(self.state.active_progress)
        if active_count >= self.state.max_concurrent_progress:
            return False

        # Get all Lean files (need some files to analyze)
        lean_files = self._get_all_lean_files()
        if not lean_files:
            return False

        # Launch one progress agent
        progress_id = f"progress-{uuid.uuid4().hex[:8]}"
        self.state.active_progress[progress_id] = "running"
        self._progress_last_run = now  # Update timestamp after launching

        contrib_task = ContributorTask.progress()
        task = self._dispatch_agent(
            agent_type="progress",
            task=contrib_task,
            agent_id=progress_id,
            chapter_id="",  # Progress agents work across all chapters
        )
        self._progress_tasks[task] = progress_id
        logger.info(f"[LAUNCH] Progress agent ({progress_id})")

        self.save_state()
        return True

    # =========================================================================
    # Scanners: Issue Detection (deprecated methods removed)

    async def _harvest_completed_scanners(self) -> bool:
        """Collect results from completed scanners."""
        # Check for completed scanner tasks
        done_tasks = [t for t in self._scanner_tasks if t.done()]
        if not done_tasks:
            return False

        logger.info(f"[HARVEST] Harvesting {len(done_tasks)} completed scanner(s)")
        made_progress = False

        for task in done_tasks:
            scanner_id = self._scanner_tasks.get(task)
            if not scanner_id:
                continue

            # Get the scanner result
            try:
                result = task.result()
            except Exception as e:
                logger.exception(f"Scanner task {scanner_id} raised exception: {e}")
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "scanner_completed",
                        agent_id=scanner_id,
                        status="error",
                        error=str(e),
                    )
                result = None

            # _run_contributor returns SimplePR on success, None otherwise
            if result and isinstance(result, SimplePR):
                # Add PR to state for review
                self.state.prs[result.pr_id] = result
                logger.info(f"Scanner {scanner_id} completed with PR {result.pr_id}")
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "scanner_completed",
                        agent_id=scanner_id,
                        pr_id=result.pr_id,
                        status="success",
                    )
                made_progress = True
            else:
                logger.info(f"Scanner {scanner_id} completed with no changes")
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "scanner_completed",
                        agent_id=scanner_id,
                        status="no_changes",
                    )

            # Clean up
            self.state.active_scanners.pop(scanner_id, None)
            del self._scanner_tasks[task]

        if made_progress:
            self.save_state()
        return made_progress

    async def _harvest_completed_progress(self) -> bool:
        """Collect results from completed progress agents."""
        # Check for completed progress tasks
        done_tasks = [t for t in self._progress_tasks if t.done()]
        if not done_tasks:
            return False

        logger.info(f"[HARVEST] Harvesting {len(done_tasks)} completed progress agent(s)")
        made_progress = False

        for task in done_tasks:
            progress_id = self._progress_tasks.get(task)
            if not progress_id:
                continue

            # Get the progress agent result
            try:
                result = task.result()
            except Exception as e:
                logger.exception(f"Progress task {progress_id} raised exception: {e}")
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "progress_completed",
                        agent_id=progress_id,
                        status="error",
                        error=str(e),
                    )
                result = None

            # _run_contributor returns SimplePR on success, None otherwise
            if result and isinstance(result, SimplePR):
                # Add PR to state for review
                self.state.prs[result.pr_id] = result
                logger.info(f"Progress agent {progress_id} completed with PR {result.pr_id}")
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "progress_completed",
                        agent_id=progress_id,
                        pr_id=result.pr_id,
                        status="success",
                    )
                made_progress = True
            else:
                logger.info(f"Progress agent {progress_id} completed with no changes")
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "progress_completed",
                        agent_id=progress_id,
                        status="no_changes",
                    )

            # Clean up
            self.state.active_progress.pop(progress_id, None)
            del self._progress_tasks[task]

        if made_progress:
            self.save_state()
        return made_progress

    async def _harvest_completed_triage(self) -> bool:
        """Collect results from completed triage agents and handle PRs."""
        # Check for completed triage tasks
        done_tasks = [t for t in self._triage_tasks if t.done()]
        if not done_tasks:
            return False

        logger.info(f"[HARVEST] Harvesting {len(done_tasks)} completed triage task(s)")
        made_progress = False

        for task in done_tasks:
            agent_id = self._triage_tasks.get(task)
            if not agent_id:
                del self._triage_tasks[task]
                continue

            # Get the result
            try:
                result = task.result()
            except Exception as e:
                logger.exception(f"Triage task {agent_id} raised exception: {e}")
                del self._triage_tasks[task]
                continue

            # Triage returns SimplePR when it ticks off issues
            if result and isinstance(result, SimplePR):
                # Add PR to state (consistent with sketcher/prover handling)
                self.state.prs[result.pr_id] = result
                self.save_state()
                logger.info(f"Triage {agent_id} completed with PR {result.pr_id}")
                # NOTE: record_pr_submitted is called inside _run_contributor
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "triage_completed",
                        agent_id=agent_id,
                        pr_id=result.pr_id,
                        status="success",
                    )
                made_progress = True
            else:
                logger.info(f"Triage {agent_id} completed with no changes (nothing to tick)")
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "triage_completed",
                        agent_id=agent_id,
                        status="no_changes",
                    )

            # Clean up
            del self._triage_tasks[task]

        return made_progress

    # =========================================================================
    # Maintain Contributors (work on open issues)
    # =========================================================================

    async def _launch_maintain_contributors(self) -> bool:
        """Launch maintain contributors to work on assigned issues.

        Supports multiple agents per issue based on agents_per_target config.
        Effective agents = min(agents_per_target, 32 // n_issues)

        No explicit sketch-merged guard needed: target theorem issues are only
        added to the issues/ folder after a sketch merges (_add_target_theorem_issues),
        and sketch issues are excluded from maintainer assignment. So there are
        simply no assignable issues until a sketch merges.
        """
        if self._draining:
            return False

        made_progress = False

        # Check current active maintain tasks - cap is on RUNNING agents, not just newly launched
        active_count = len(self._maintain_tasks)
        max_contributors = self.config.max_concurrent_contributors

        if active_count >= max_contributors:
            return False

        # Get all open issues
        all_open_issues = self._get_open_issue_ids()
        n_targets = len(all_open_issues)

        if n_targets == 0:
            return False

        # Calculate effective agents per target: min(config, 32 // n_targets)
        effective_agents_per_target = min(self.config.agents_per_target, max(1, 32 // n_targets))

        # Count active agents per issue (tasks + active PRs)
        active_count_by_issue: dict[str, int] = {}
        for _, (_, issue_id) in self._maintain_tasks.items():
            active_count_by_issue[issue_id] = active_count_by_issue.get(issue_id, 0) + 1
        active_pr_statuses = {"pending_review", "in_review", "needs_revision", "revision_in_progress", "approved"}
        for pr in self.state.prs.values():
            if pr.issue_id and pr.status in active_pr_statuses:
                active_count_by_issue[pr.issue_id] = active_count_by_issue.get(pr.issue_id, 0) + 1

        # Build list of issue assignments to launch
        issues_to_assign = []
        issues_at_capacity = []

        for issue_id in all_open_issues:
            current_count = active_count_by_issue.get(issue_id, 0)
            slots_for_issue = effective_agents_per_target - current_count
            if slots_for_issue <= 0:
                issues_at_capacity.append(issue_id)
                continue
            for _ in range(slots_for_issue):
                issues_to_assign.append(issue_id)

        if not issues_to_assign:
            return False

        # Calculate how many we can launch (respecting running cap)
        slots_available = max_contributors - active_count
        to_launch = min(len(issues_to_assign), slots_available)

        if to_launch <= 0:
            return False

        issues_to_assign = issues_to_assign[:to_launch]

        if effective_agents_per_target > 1:
            logger.info(
                f"[LAUNCH] {len(issues_to_assign)} maintain contributor(s) "
                f"({n_targets} open issues, effective_agents={effective_agents_per_target})"
            )
        else:
            logger.info(
                f"[LAUNCH] {len(issues_to_assign)} maintain contributor(s) "
                f"({n_targets} open issues, {len(issues_at_capacity)} at capacity)"
            )

        for issue_id in issues_to_assign:
            agent_id = f"maintain-{uuid.uuid4().hex[:8]}"
            contrib_task = ContributorTask.maintain(issue_id=issue_id)  # Assign specific issue
            task = self._dispatch_agent(
                agent_type="maintain",
                task=contrib_task,
                agent_id=agent_id,
                chapter_id="",  # Maintain works across chapters
            )
            self._maintain_tasks[task] = (agent_id, issue_id)
            logger.info(f"  → Started maintain contributor {agent_id} assigned to issue #{issue_id}")
            made_progress = True

        return made_progress

    async def _harvest_completed_maintain_contributors(self) -> bool:
        """Collect results from completed maintain contributors."""
        done_tasks = [t for t in self._maintain_tasks if t.done()]
        if not done_tasks:
            return False

        logger.info(f"[HARVEST] Harvesting {len(done_tasks)} completed maintain contributor(s)")
        made_progress = False

        for task in done_tasks:
            task_info = self._maintain_tasks.get(task)
            if not task_info:
                del self._maintain_tasks[task]
                continue

            agent_id, issue_id = task_info

            try:
                result = task.result()
            except Exception as e:
                logger.exception(f"Maintain contributor {agent_id} (issue #{issue_id}) raised exception: {e}")
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "maintain_contributor_completed",
                        agent_id=agent_id,
                        issue_id=issue_id,
                        status="error",
                        error=str(e),
                    )
                result = None

            if result and isinstance(result, SimplePR):
                # Add PR to state for review
                self.state.prs[result.pr_id] = result
                logger.info(f"Maintain contributor {agent_id} (issue #{issue_id}) completed with PR {result.pr_id}")
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "maintain_contributor_completed",
                        agent_id=agent_id,
                        issue_id=issue_id,
                        pr_id=result.pr_id,
                        status="success",
                    )
                made_progress = True
            else:
                logger.info(f"Maintain contributor {agent_id} (issue #{issue_id}) completed with no changes")
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "maintain_contributor_completed",
                        agent_id=agent_id,
                        issue_id=issue_id,
                        status="no_changes",
                    )

            del self._maintain_tasks[task]

        return made_progress

    def _get_open_issue_ids(self) -> list[str]:
        """Get list of open issue IDs from issues/ folder.

        Returns list of UUID strings (filenames without .yaml extension).
        Skips issues with origin 'initial chapter sketch' as those are reserved for sketch agents.
        """
        issues_dir = self._get_issues_dir()
        if not issues_dir.exists():
            return []

        open_ids = []
        for issue_file in issues_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(issue_file.read_text())
                if data and data.get("status") == "open":
                    # Skip initial chapter sketch issues - reserved for sketch agents
                    if data.get("origin") == "initial chapter sketch":
                        continue
                    open_ids.append(issue_file.stem)
            except yaml.YAMLError:
                pass

        return open_ids

    def _get_all_lean_files(self) -> list[str]:
        """Get all Lean files in the project by globbing *.lean.

        Note: We explicitly skip hidden directories (.repoprover, .lake, .git)
        at the top level to avoid descending into worktrees that may be
        deleted mid-iteration (causing stale file handle errors on NFS).
        """
        lean_files = []
        for item in self.base_project.iterdir():
            if item.name.startswith("."):
                continue  # Skip .repoprover, .lake, .git entirely
            if item.is_dir():
                for lean_file in item.rglob("*.lean"):
                    if lean_file.name == "lakefile.lean":
                        continue
                    lean_files.append(str(lean_file.relative_to(self.base_project)))
            elif item.suffix == ".lean" and item.name != "lakefile.lean":
                lean_files.append(item.name)
        return lean_files

    # =========================================================================
    # Step 2: Process Revisions (rerun agents on failed reviews)
    # =========================================================================

    async def _process_revisions(self) -> bool:
        """Rerun agents on PRs that need revision (non-blocking).

        Launches revision tasks in background. Results are harvested by
        _harvest_completed_agents().
        """
        if self._draining:
            return False

        prs_to_revise = [pr for pr in self.state.prs.values() if pr.status == "needs_revision"]

        if not prs_to_revise:
            return False

        # Check revision limits first
        made_progress = False
        for pr in prs_to_revise:
            if pr.revision_count >= self.config.max_revisions:
                logger.warning(f"PR {pr.pr_id} exceeded max revisions, marking failed")
                pr.status = "failed"
                if pr.agent_id:
                    self.worktree_pool.cleanup(pr.agent_id)
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "pr_status_changed",
                        pr_id=pr.pr_id,
                        new_status="failed",
                        reason="max_revisions_exceeded",
                        revision_count=pr.revision_count,
                        agent_id=pr.agent_id,
                    )
                made_progress = True

        # Filter out failed ones and ones that already have running tasks
        # For chapter-scoped agents (sketch, prove): check chapter_id collision
        # For global agents (scan, triage, maintain with empty chapter_id): check agent_id collision
        active_chapters = set(cid for cid, _ in self._agent_tasks.values() if cid)  # Non-empty chapter IDs
        active_agent_ids = set(aid for _, aid in self._agent_tasks.values())
        prs_to_revise = [
            pr
            for pr in prs_to_revise
            if pr.status == "needs_revision"
            and (
                # Chapter-scoped: block if chapter already has running agent
                (pr.chapter_id and pr.chapter_id not in active_chapters)
                or
                # Global (empty chapter_id): block only if this exact agent_id is running
                (not pr.chapter_id and pr.agent_id not in active_agent_ids)
            )
        ]

        if not prs_to_revise:
            return made_progress

        logger.info(f"[LAUNCH] {len(prs_to_revise)} revision(s)")

        # Launch revisions as background tasks
        for pr in prs_to_revise:
            # Mark PR as in-progress so it doesn't get picked up again
            pr.status = "revision_in_progress"
            pr.revision_count += 1

            # Build task based on agent type
            if pr.agent_type == "sketch":
                chapter = self.state.chapters.get(pr.chapter_id, {})
                source_path = chapter.get("source_path", "")
                contrib_task = ContributorTask.sketch(
                    chapter_id=pr.chapter_id,
                    source_tex_path=source_path,
                    lean_path="",  # Agent finds its work via CONTENTS.md or git status
                )
            elif pr.agent_type == "prove":
                chapter = self.state.chapters.get(pr.chapter_id, {})
                contrib_task = ContributorTask.prove(
                    chapter_id=pr.chapter_id,
                    theorem_name=pr.theorem_name or "",
                    lean_path="",  # Agent finds file via CONTENTS.md
                )
            elif pr.agent_type == "triage":
                contrib_task = ContributorTask.triage()
            elif pr.agent_type == "scan":
                contrib_task = ContributorTask.scan()
            elif pr.agent_type == "maintain":
                contrib_task = ContributorTask.maintain(issue_id=pr.issue_id)
            elif pr.agent_type == "fix":
                contrib_task = ContributorTask.fix()
            else:
                logger.error(f"Unknown agent type '{pr.agent_type}' for PR {pr.pr_id}, marking as failed")
                pr.status = "failed"
                if pr.agent_id:
                    self.worktree_pool.cleanup(pr.agent_id)
                if self.session_recorder:
                    self.session_recorder.record_event(
                        "pr_status_changed",
                        pr_id=pr.pr_id,
                        new_status="failed",
                        reason="unknown_agent_type",
                        agent_type=pr.agent_type,
                        agent_id=pr.agent_id,
                    )
                made_progress = True
                continue

            task = self._dispatch_agent(
                agent_type=pr.agent_type,
                task=contrib_task,
                agent_id=pr.agent_id,
                chapter_id=pr.chapter_id,
                feedback=pr.last_review_feedback,
                revision_number=pr.revision_count,
            )
            self._agent_tasks[task] = (pr.chapter_id, pr.agent_id)
            # Track prover theorem for one-agent-per-theorem rule
            if pr.agent_type == "prove" and pr.theorem_name:
                self._active_prover_theorems[task] = (pr.chapter_id, pr.theorem_name)
            logger.info(f"  → Started revision for {pr.pr_id} (attempt {pr.revision_count})")
            # NOTE: record_agent_launched is called inside _run_contributor
            if self.session_recorder:
                self.session_recorder.record_revision_started(
                    pr_id=pr.pr_id,
                    agent_id=pr.agent_id,
                    revision_number=pr.revision_count,
                )

        return True

    # =========================================================================
    # Step 3: Process Reviews (fire-and-forget)
    # =========================================================================

    async def _process_reviews(self) -> bool:
        """Launch reviews for PRs pending review (non-blocking).

        Each review is launched as a background task. Results are harvested
        by _harvest_completed_reviews().

        To avoid thundering herd effects (especially on resume with many open PRs),
        reviews are launched with small random delays between them.
        """
        if self._draining:
            return False

        prs_to_review = [pr for pr in self.state.prs.values() if pr.status == "pending_review"]

        if not prs_to_review:
            return False

        # Don't launch duplicate reviews for PRs already being reviewed
        reviewing_pr_ids = {pr_id for pr_id, _ in self._review_tasks.values()}
        prs_to_review = [pr for pr in prs_to_review if pr.pr_id not in reviewing_pr_ids]

        if not prs_to_review:
            return False

        logger.info(f"[LAUNCH] {len(prs_to_review)} review(s)")

        # Shuffle to avoid any ordering bias and stagger launches
        random.shuffle(prs_to_review)

        for i, pr in enumerate(prs_to_review):
            # Stagger launches to avoid thundering herd on git operations
            # First review starts immediately, subsequent ones have random delays
            if i > 0:
                # Random delay between 2-10 seconds between launches
                stagger_delay = 2.0 + random.random() * 8.0
                await asyncio.sleep(stagger_delay)

            task = asyncio.create_task(asyncio.to_thread(self._run_review, pr))
            # Store both pr_id and revision_number at launch time
            self._review_tasks[task] = (pr.pr_id, pr.revision_count)
            logger.info(f"  → Review for PR {pr.pr_id} (rev {pr.revision_count})")
            # NOTE: record_review_launched moved to _run_review, after pre_review_merge

        return True

    def _run_review(self, pr: SimplePR) -> tuple[str, ReviewResult]:
        """Run a single PR review (blocking, intended for asyncio.to_thread).

        Returns (pr_id, ReviewResult).
        """
        # Acquire worktree — returns existing one left by contributor, or recreates from branch
        try:
            worktree = self.worktree_pool.setup(pr.agent_id)
            worktree_path = worktree.worktree_path
        except Exception as e:
            logger.exception(f"Could not acquire worktree for review of {pr.pr_id}: {e}")
            # Record the review launch even on failure so viewer shows it
            if self.session_recorder:
                self.session_recorder.record_review_launched(pr_id=pr.pr_id, agent_id=pr.agent_id)
            return pr.pr_id, ReviewResult(
                build_passed=False,
                build_error=f"Could not acquire worktree: {e}",
                build_output=None,
                math_review=None,
                engineering_review=None,
                combined_verdict=ReviewVerdict.REQUEST_CHANGES,
            )

        # Step 0: Merge main into the review worktree to catch conflicts
        # before wasting build + LLM review cycles. Each review has its own
        # worktree so no locking needed.
        try:
            merge_ok, conflict_files = self._merge_main_into_worktree(worktree_path)
            main_commit = self._get_main_commit_hash()

            # Record the pre-review merge result (success or failure)
            if self.session_recorder:
                self.session_recorder.record_pre_review_merge(
                    pr_id=pr.pr_id,
                    agent_id=pr.agent_id,
                    success=merge_ok,
                    revision_number=pr.revision_count,
                    main_commit_hash=main_commit,
                    conflict_files=conflict_files if not merge_ok else None,
                )

            # Record review_launched AFTER pre_review_merge (chronological order)
            if self.session_recorder:
                self.session_recorder.record_review_launched(
                    pr_id=pr.pr_id,
                    agent_id=pr.agent_id,
                )

            if not merge_ok:
                logger.info(f"PR {pr.pr_id} conflicts with main ({', '.join(conflict_files)}), skipping review")
                return pr.pr_id, ReviewResult(
                    build_passed=False,
                    build_error=f"Merge conflict with main in: {', '.join(conflict_files)}",
                    build_output=None,
                    math_review=None,
                    engineering_review=None,
                    combined_verdict=ReviewVerdict.REQUEST_CHANGES,
                )

            _, diff = self._get_branch_diff(pr.branch_name)
            files = self._get_branch_files(pr.branch_name)

            # Create review context from SimplePR
            # Map agent_type string to AgentType enum.
            # IMPORTANT: Preserve original type for empty diff handling in reviewers.py:
            #   - Open-ended types (triage, scan, maintain): empty diff = APPROVE (nothing to do)
            #   - Task-based types (sketch, prove, fix): empty diff = REQUEST_CHANGES (task not done)
            agent_type_map = {
                "sketch": AgentType.SKETCH,
                "fix": AgentType.FIX,
                "scan": AgentType.SCAN,
                "triage": AgentType.TRIAGE,
                "maintain": AgentType.MAINTAIN,
                "prove": AgentType.PROVE,
            }
            agent_type_enum = agent_type_map.get(pr.agent_type, AgentType.PROVE)

            review_context = ReviewContext(
                pr_id=pr.pr_id,
                branch_name=pr.branch_name,
                agent_type=agent_type_enum,
                agent_id=pr.agent_id,
                chapter_id=pr.chapter_id,
                title=f"{pr.agent_type} for {pr.chapter_id}",
                files_changed=list(files.keys()),
                source_content=self._read_source_for_chapter(pr.chapter_id),
                description=pr.description,
                revision_number=pr.revision_count,
                previous_review_feedback=pr.last_review_feedback,
            )

            result = review_pr(
                review_context,
                diff,
                files,
                worktree_path,
                self.config.agent_config,
                self.session_recorder,
            )
            return pr.pr_id, result
        finally:
            # Worktree stays alive — revision or merge may follow.
            # Cleanup only happens on terminal states (merged/failed/rejected).
            pass

    async def _harvest_completed_reviews(self) -> bool:
        """Check for completed review tasks and process their results.

        Non-blocking poll of _review_tasks. For each done task, update PR
        status and record to session.
        """
        if not self._review_tasks:
            return False

        done = set()
        for task in list(self._review_tasks.keys()):
            if task.done():
                done.add(task)

        if not done:
            return False

        logger.info(f"[HARVEST] Harvesting {len(done)} completed review(s)")
        made_progress = False
        for task in done:
            pr_id, revision_number = self._review_tasks.pop(task)
            pr = self.state.prs.get(pr_id)
            if not pr:
                logger.warning(f"Review completed for unknown PR {pr_id}")
                continue

            try:
                _, review_result = task.result()

                # Record review to session (use revision_number from launch time)
                if self.session_recorder:
                    self.session_recorder.record_review(
                        pr_id=pr.pr_id,
                        agent_id=pr.agent_id,
                        semantic_verdict=review_result.math_review.verdict if review_result.math_review else None,
                        semantic_summary=review_result.math_review.summary if review_result.math_review else None,
                        engineering_verdict=review_result.engineering_review.verdict
                        if review_result.engineering_review
                        else None,
                        engineering_summary=review_result.engineering_review.summary
                        if review_result.engineering_review
                        else None,
                        combined_verdict=review_result.combined_verdict,
                        build_passed=review_result.build_passed,
                        build_error=review_result.build_error,
                        build_output=review_result.build_output,
                        revision_number=revision_number,
                    )

                if review_result.combined_verdict == ReviewVerdict.APPROVE:
                    pr.status = "approved"
                    logger.info(f"PR {pr.pr_id} approved")
                    if self.session_recorder:
                        self.session_recorder.record_event(
                            "pr_status_changed",
                            pr_id=pr.pr_id,
                            new_status="approved",
                            agent_id=pr.agent_id,
                        )
                        # Update agent status to reflect approval
                        self.session_recorder.record_agent_status_update(
                            agent_id=pr.agent_id,
                            status="approved",
                            reason="review_approved",
                        )
                elif review_result.combined_verdict == ReviewVerdict.REJECT:
                    pr.status = "failed"
                    pr.last_review_feedback = self._extract_review_feedback(review_result)
                    if pr.agent_id:
                        self.worktree_pool.cleanup(pr.agent_id)
                    logger.info(f"PR {pr.pr_id} rejected")
                    if self.session_recorder:
                        self.session_recorder.record_event(
                            "pr_status_changed",
                            pr_id=pr.pr_id,
                            new_status="failed",
                            reason="rejected",
                            agent_id=pr.agent_id,
                        )
                        # Update agent status to reflect rejection
                        self.session_recorder.record_agent_status_update(
                            agent_id=pr.agent_id,
                            status="rejected",
                            reason="review_rejected",
                        )
                else:  # REQUEST_CHANGES or ABSTAIN
                    pr.status = "needs_revision"
                    pr.last_review_feedback = self._extract_review_feedback(review_result)
                    logger.info(f"PR {pr.pr_id} needs revision")
                    if self.session_recorder:
                        self.session_recorder.record_event(
                            "pr_status_changed",
                            pr_id=pr.pr_id,
                            new_status="needs_revision",
                            agent_id=pr.agent_id,
                        )
                        # Update agent status to reflect pending revision
                        self.session_recorder.record_agent_status_update(
                            agent_id=pr.agent_id,
                            status="pending_revision",
                            reason="review_requested_changes",
                        )

                made_progress = True

            except Exception as e:
                logger.exception(f"Review for PR {pr_id} raised exception: {e}")
                # Leave PR in pending_review so it can be retried
                made_progress = True

        return made_progress

    def _extract_review_feedback(self, result: ReviewResult) -> str:
        """Extract feedback from review result."""
        parts = []
        if result.build_error:
            parts.append(f"Build failed: {result.build_error}")
        if result.math_review:
            parts.append(f"Math: {result.math_review.summary}")
        if result.engineering_review:
            parts.append(f"Engineering: {result.engineering_review.summary}")
        return "\n\n".join(parts)

    # =========================================================================
    # Step 4: Process Merges
    # =========================================================================

    async def _process_merges(self) -> bool:
        """Merge approved PRs to main (sequential for safety, but greedy).

        Processes ALL approved PRs in one call to minimize merge conflicts.
        Saves state every 10 merges to balance durability with performance.
        """
        approved_prs = [pr for pr in self.state.prs.values() if pr.status == "approved"]
        if not approved_prs:
            return False

        merge_count = 0
        total_approved = len(approved_prs)
        logger.info(f"[MERGE] {total_approved} approved PR(s)")

        for pr in approved_prs:
            logger.info(f"Merging PR {pr.pr_id} (branch: {pr.branch_name}) [{merge_count + 1}/{total_approved}]")

            success, msg, conflict_files, details = await asyncio.to_thread(
                self._merge_branch, pr.branch_name, pr.pr_id
            )

            if success:
                pr.status = "merged"
                merge_count += 1

                # Clean up the worktree - after merge we never go back to revisions
                # This frees up ~170MB per worktree (disk space from .lake/build)
                if pr.agent_id:
                    self.worktree_pool.cleanup(pr.agent_id)
                    logger.info(f"Cleaned up worktree for merged agent {pr.agent_id}")

                # Update chapter state
                if pr.agent_type == "sketch":
                    self.state.chapters[pr.chapter_id]["sketch_merged"] = True
                    # Add target theorem issues now that the chapter has Lean code
                    self._add_target_theorem_issues(pr.chapter_id)
                elif pr.agent_type == "prove" and pr.theorem_name:
                    if pr.chapter_id not in self.state.completed_theorems:
                        self.state.completed_theorems[pr.chapter_id] = []
                    if pr.theorem_name not in self.state.completed_theorems[pr.chapter_id]:
                        self.state.completed_theorems[pr.chapter_id].append(pr.theorem_name)

                # Close issues referenced in commit messages (if any)
                # NOTE: Agents close issues by editing issue files in issues/ folder directly in their PRs.

                logger.info(f"PR {pr.pr_id} merged: {msg}")

                # Record successful merge
                if self.session_recorder:
                    self.session_recorder.record_merge(
                        pr_id=pr.pr_id,
                        branch_name=pr.branch_name,
                        success=True,
                        agent_id=pr.agent_id,
                        diff_stats=pr.diff_stats,
                        commit_hash=msg,
                        revision_number=pr.revision_count,
                        main_commit_hash=details.get("main_commit_hash"),
                    )

                # Save state every 10 merges for durability
                if merge_count % 10 == 0:
                    self.save_state()

            else:
                # Merge conflict or other issue - needs revision
                pr.status = "needs_revision"
                failure_reason = details.get("failure_reason", "unknown")
                if conflict_files:
                    pr.last_review_feedback = f"Merge conflict with main in: {', '.join(conflict_files)}"
                    logger.warning(
                        f"PR {pr.pr_id} merge failed: conflict in {len(conflict_files)} file(s): {', '.join(conflict_files)}"
                    )
                else:
                    pr.last_review_feedback = f"Merge failed: {msg}" if msg else f"Merge failed ({failure_reason})"
                    logger.warning(f"PR {pr.pr_id} merge failed ({failure_reason}): {msg or 'no details available'}")

                # Record failed merge with details
                if self.session_recorder:
                    self.session_recorder.record_merge(
                        pr_id=pr.pr_id,
                        branch_name=pr.branch_name,
                        success=False,
                        agent_id=pr.agent_id,
                        error=msg,
                        conflict_files=conflict_files or None,
                        revision_number=pr.revision_count,
                        failure_reason=details.get("failure_reason"),
                        main_commit_hash=details.get("main_commit_hash"),
                        build_duration_s=details.get("build_duration_s"),
                    )

        # Record proof stats once at end of batch (not after each merge)
        if merge_count > 0 and self.session_recorder:
            self._record_proof_stats()
            # Final save if we didn't just save (i.e., not divisible by 10)
            if merge_count % 10 != 0:
                self.save_state()

        return True

    def _merge_main_into_worktree(self, worktree_path: Path) -> tuple[bool, list[str]]:
        """Merge main into worktree branch to test for conflicts.

        Uses merge instead of rebase so that conflict resolutions stick
        across revisions — once an agent resolves a merge conflict, subsequent
        merges only bring in NEW main commits, not the ones already merged.

        Each worktree is independent so this is safe to call concurrently
        from multiple review threads.

        Returns:
            (success, conflicting_files) — True if clean merge, else list of conflicting paths.
        """
        import subprocess

        logger.info(f"Pre-review merge: merging main into {worktree_path.name}")

        try:
            result = subprocess.run(
                ["git", "merge", "main", "--no-edit"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode == 0:
                logger.info(f"Pre-review merge: {worktree_path.name} merged main cleanly")
                return True, []

            # Conflict — find which files
            logger.info(f"Pre-review merge: {worktree_path.name} has conflicts, checking files...")
            conflicts_result = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            conflict_files = [f for f in conflicts_result.stdout.strip().split("\n") if f]
            logger.info(f"Pre-review merge: {worktree_path.name} conflicts in: {conflict_files}")

            # Abort the failed merge to leave worktree clean
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=worktree_path,
                capture_output=True,
            )
            return False, conflict_files

        except Exception as e:
            logger.exception(f"Merge-main test failed in {worktree_path}: {e}")
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=worktree_path,
                capture_output=True,
            )
            # On error, don't block — let the review proceed
            return True, []

    def _get_main_commit_hash(self) -> str | None:
        """Get the current main branch commit hash."""
        import subprocess

        try:
            result = subprocess.run(
                ["git", "rev-parse", "main"],
                cwd=self.base_project,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()[:12]
        except Exception:
            pass
        return None

    @timed()
    def _merge_branch(self, branch_name: str, pr_id: str = "") -> tuple[bool, str, list[str], dict[str, Any]]:
        """Merge a branch to main.

        Returns:
            (success, message, conflict_files, details) where details contains:
                - main_commit_hash: str | None - commit hash of main before merge
                - failure_reason: str | None - "merge_conflict", "build_failed", "build_timeout"
                - build_duration_s: float | None - build duration in seconds
        """
        details: dict[str, Any] = {
            "main_commit_hash": None,
            "failure_reason": None,
            "build_duration_s": None,
        }

        try:
            # Checkout main
            timed_run(["git", "checkout", "main"], cwd=self.base_project, check=True)

            # Pull latest
            timed_run(["git", "pull", "--ff-only"], cwd=self.base_project)

            # Capture main commit hash before merge
            main_hash_result = timed_run(["git", "rev-parse", "HEAD"], cwd=self.base_project)
            details["main_commit_hash"] = (
                main_hash_result.stdout.strip()[:12] if main_hash_result.returncode == 0 else None
            )

            # Merge
            result = timed_run(
                ["git", "merge", "--no-ff", branch_name, "-m", f"Merge {branch_name}"],
                cwd=self.base_project,
            )

            if result.returncode != 0:
                # Detect which files conflict
                conflicts_result = timed_run(
                    ["git", "diff", "--name-only", "--diff-filter=U"],
                    cwd=self.base_project,
                )
                conflict_files = [f for f in conflicts_result.stdout.strip().split("\n") if f]

                # Abort failed merge
                timed_run(["git", "merge", "--abort"], cwd=self.base_project)
                details["failure_reason"] = "merge_conflict"
                # Git may write conflict info to stdout or stderr; capture both
                merge_error = result.stderr.strip() or result.stdout.strip() or "merge failed"
                return False, merge_error, conflict_files, details

            # Verify build using centralized build function with semaphore
            build_result = lake_build(self.base_project, label=f"merge:{branch_name[:20]}")
            build_dur = build_result.duration
            details["build_duration_s"] = build_dur

            build_passed = build_result.success
            build_error = build_result.error
            logger.info(
                f"Merge build {'passed' if build_passed else 'FAILED'} for {branch_name} ({build_dur:.1f}s)"
                if build_dur
                else f"Merge build FAILED for {branch_name} (timed out)"
            )

            if self.session_recorder and pr_id:
                self.session_recorder.record_build(
                    context="merge",
                    pr_id=pr_id,
                    branch_name=branch_name,
                    passed=build_passed,
                    error=build_error,
                    duration_s=build_dur,
                )

            if build_result.timed_out:
                # Reset merge on timeout
                timed_run(["git", "reset", "--hard", "HEAD~1"], cwd=self.base_project)
                details["failure_reason"] = "build_timeout"
                return False, "Build timed out", [], details

            if not build_passed:
                # Reset merge
                timed_run(["git", "reset", "--hard", "HEAD~1"], cwd=self.base_project)
                details["failure_reason"] = "build_failed"
                return False, f"Build failed: {build_error}", [], details

            # Get commit hash
            hash_result = timed_run(["git", "rev-parse", "HEAD"], cwd=self.base_project)

            return True, hash_result.stdout.strip()[:8], [], details

        except Exception as e:
            logger.exception("Build failed with unexpected error")
            details["failure_reason"] = "unknown"
            return False, str(e), [], details

    # =========================================================================
    # Step 5: Launch Provers
    # =========================================================================

    async def _launch_pending_provers(self) -> bool:
        """Launch provers for theorems with sorry in all Lean files (parallel).

        Supports multiple agents per theorem based on agents_per_target config.
        Effective agents = min(agents_per_target, 32 // n_sorries)
        """
        if self._draining:
            return False

        # Scan all lean files for sorries - this is the source of truth
        all_sorries: list[tuple[str, str]] = []  # (lean_path, theorem_name)
        for lean_path in self._get_all_lean_files():
            sorries = self._scan_sorries(lean_path)
            for theorem_name in sorries:
                all_sorries.append((lean_path, theorem_name))

        # Get unique theorems needing work (dedupe by theorem name)
        pending_theorems: dict[str, str] = {}  # theorem_name -> lean_path
        for lean_path, theorem_name in all_sorries:
            pending_theorems[theorem_name] = lean_path

        n_targets = len(pending_theorems)
        if n_targets == 0:
            return False

        # Calculate effective agents per target: min(config, 32 // n_targets)
        effective_agents_per_target = min(self.config.agents_per_target, max(1, 32 // n_targets))

        # Count active agents per theorem (tasks + PRs)
        active_count_by_theorem: dict[str, int] = {}
        for task, (_, thm_name) in self._active_prover_theorems.items():
            if not task.done():
                active_count_by_theorem[thm_name] = active_count_by_theorem.get(thm_name, 0) + 1
        for pr in self.state.prs.values():
            if pr.agent_type == "prove" and pr.theorem_name and pr.status not in ("merged", "failed"):
                active_count_by_theorem[pr.theorem_name] = active_count_by_theorem.get(pr.theorem_name, 0) + 1

        # Build list of (lean_path, theorem_name) to launch
        theorems_to_prove = []
        filtered_at_capacity = []

        for theorem_name, lean_path in pending_theorems.items():
            current_count = active_count_by_theorem.get(theorem_name, 0)
            slots_for_theorem = effective_agents_per_target - current_count
            if slots_for_theorem <= 0:
                filtered_at_capacity.append(theorem_name)
                continue
            for _ in range(slots_for_theorem):
                theorems_to_prove.append((lean_path, theorem_name))

        # Log scan results
        if all_sorries:
            logger.info(f"[SCAN] Found {len(all_sorries)} theorem(s) with sorry, {n_targets} pending")
            if effective_agents_per_target > 1:
                logger.info(
                    f"[SCAN]   - agents_per_target={self.config.agents_per_target}, effective={effective_agents_per_target} (32//{n_targets}={32 // n_targets})"
                )
            if filtered_at_capacity:
                logger.info(
                    f"[SCAN]   - {len(filtered_at_capacity)} at capacity ({effective_agents_per_target} agents)"
                )
            logger.info(f"[SCAN]   = {len(theorems_to_prove)} agent slot(s) ready to launch")

        if not theorems_to_prove:
            return False

        # Cap concurrent provers to avoid API throttling
        active_provers = sum(1 for _, (_, aid) in self._agent_tasks.items() if aid.startswith("prove-"))
        slots = max(0, self.config.max_concurrent_contributors - active_provers)
        if slots == 0:
            return False
        theorems_to_prove = theorems_to_prove[:slots]

        logger.info(f"[LAUNCH] {len(theorems_to_prove)} prover(s) ({active_provers} already active)")

        # Launch provers as background tasks - don't wait!
        for lean_path, theorem_name in theorems_to_prove:
            # Pre-generate agent_id to ensure session event matches agent file
            safe_name = theorem_name.replace(".", "_")[:20]
            agent_id = f"prove-{safe_name}-{uuid.uuid4().hex[:6]}"
            contrib_task = ContributorTask.prove(
                chapter_id="",  # No longer tracking by chapter
                theorem_name=theorem_name,
                lean_path=lean_path,  # Pass the file path directly
            )
            task = self._dispatch_agent(
                agent_type="prove",
                task=contrib_task,
                agent_id=agent_id,
                chapter_id="",
            )
            self._agent_tasks[task] = ("", agent_id)
            # Track theorem for counting active agents
            self._active_prover_theorems[task] = ("", theorem_name)
            logger.info(f"  → Started prover for {theorem_name} in {lean_path}")

        return True  # We made progress by launching tasks

    def _has_prover_pr_by_theorem(self, theorem_name: str) -> bool:
        """Check if theorem has an active prover PR (by theorem name only)."""
        for pr in self.state.prs.values():
            if pr.agent_type == "prove" and pr.theorem_name == theorem_name and pr.status not in ("merged", "failed"):
                return True
        return False

    def _has_active_prover_task_by_theorem(self, theorem_name: str) -> bool:
        """Check if theorem has a currently-running prover task (by theorem name only)."""
        for task, (_, thm_name) in self._active_prover_theorems.items():
            if thm_name == theorem_name and not task.done():
                return True
        return False

    def _scan_sorries(self, lean_path: str) -> list[str]:
        """Scan a Lean file for theorems with sorry.

        Uses comment stripping and declaration-boundary splitting
        to robustly find theorem/lemma declarations and detect sorry proofs.
        Skips theorems marked with `-- [cited]` or `-- [exercise]`.
        """
        full_path = self.base_project / lean_path
        if not full_path.exists():
            return []

        code = full_path.read_text()
        code_stripped = strip_comments(code, preserve_positions=True)

        matches = list(_DECL_HEADER_RE.finditer(code_stripped))
        if not matches:
            return []

        results = []
        for idx, m in enumerate(matches):
            kw = m.group("kw")
            if kw not in ("theorem", "lemma"):
                continue

            block_start = m.start()
            block_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(code)

            # Extract name from stripped code
            name_match = _THEOREM_NAME_RE.search(code_stripped[m.start() :])
            if not name_match:
                continue
            name = name_match.group(1)

            # Check for sorry in stripped code (avoids matching in comments)
            block_stripped = code_stripped[block_start:block_end]
            if "sorry" not in block_stripped:
                continue

            # Check for [cited] or [exercise] in original code (markers are in comments)
            block_original = code[block_start:block_end]
            if "[cited]" in block_original or "[exercise]" in block_original:
                continue

            results.append(name)

        return results

    def _scan_theorems(self, lean_path: str) -> tuple[int, int]:
        """Scan a Lean file and count total theorems and those with sorry.

        Returns:
            (total_theorems, sorry_count): Count of all theorems and those containing sorry
        """
        full_path = self.base_project / lean_path
        if not full_path.exists():
            return 0, 0

        code = full_path.read_text()
        code_stripped = strip_comments(code, preserve_positions=True)

        matches = list(_DECL_HEADER_RE.finditer(code_stripped))
        if not matches:
            return 0, 0

        total = 0
        sorry_count = 0

        for idx, m in enumerate(matches):
            kw = m.group("kw")
            if kw not in ("theorem", "lemma"):
                continue

            # Extract name from stripped code
            name_match = _THEOREM_NAME_RE.search(code_stripped[m.start() :])
            if not name_match:
                continue

            # Skip [cited] and [exercise] theorems (not our work)
            block_start = m.start()
            block_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(code)
            block_original = code[block_start:block_end]
            if "[cited]" in block_original or "[exercise]" in block_original:
                continue

            total += 1

            # Check for sorry in stripped code (avoids matching in comments)
            block_stripped = code_stripped[block_start:block_end]
            if "sorry" in block_stripped:
                sorry_count += 1

        return total, sorry_count

    @timed()
    def _record_proof_stats(self) -> None:
        """Calculate and record proof statistics after a merge.

        Scans all Lean files for theorem/sorry counts
        and emits a proof_stats event for the viewer.
        """
        if not self.session_recorder:
            return

        total_theorems = 0
        proven_theorems = 0
        remaining_sorries = 0
        per_file: dict[str, dict[str, int]] = {}

        for lean_path in self._get_all_lean_files():
            # Scan file to count actual theorems and sorries
            total, sorry_count = self._scan_theorems(lean_path)
            if total == 0:
                continue

            proven = total - sorry_count

            total_theorems += total
            proven_theorems += proven
            remaining_sorries += sorry_count

            per_file[lean_path] = {
                "total": total,
                "proven": proven,
                "sorries": sorry_count,
            }

        # Count issues using issues/ folder
        open_issues, closed_issues = self._count_issues()

        # Parse full issue details for the dump
        issues = self._parse_issues()

        self.session_recorder.record_proof_stats(
            total_theorems=total_theorems,
            proven_theorems=proven_theorems,
            remaining_sorries=remaining_sorries,
            open_issues=open_issues,
            closed_issues=closed_issues,
            per_chapter=per_file,  # Keep param name for compatibility
            issues=issues,
        )

    # =========================================================================
    # Git Helpers
    # =========================================================================

    def _validate_main_branch(self) -> None:
        """Validate that 'main' branch exists. Raises if not."""
        import subprocess

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", "main"],
                cwd=self.base_project,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Repository at {self.base_project} does not have a 'main' branch.\n"
                    "RepoProver requires the default branch to be named 'main'.\n"
                    "To rename 'master' to 'main', run:\n"
                    "    git branch -m master main\n"
                    "See the README for setup instructions."
                )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Timeout checking for 'main' branch in {self.base_project}")
        except FileNotFoundError:
            raise RuntimeError("git not found. Please ensure git is installed and in PATH.")

    def _read_source_for_chapter(self, chapter_id: str) -> str:
        """Read the LaTeX source file for a chapter (for reviewer cross-checking)."""
        chapter_info = self.state.chapters.get(chapter_id, {})
        source_path = chapter_info.get("source_path", "")
        if not source_path:
            return ""
        full_path = self.base_project / source_path
        try:
            return full_path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _get_branch_files(self, branch_name: str) -> dict[str, str]:
        """Get files changed by the branch since it diverged from main."""
        import subprocess

        files = {}
        try:
            # Get list of changed files (three-dot: only branch's own changes)
            result = subprocess.run(
                ["git", "diff", "--name-only", f"main...{branch_name}"],
                cwd=self.base_project,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                return {}

            for file_path in result.stdout.strip().split("\n"):
                if not file_path:
                    continue
                # Get file content at branch
                content_result = subprocess.run(
                    ["git", "show", f"{branch_name}:{file_path}"],
                    cwd=self.base_project,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if content_result.returncode == 0:
                    files[file_path] = content_result.stdout

            return files
        except Exception:
            return {}

    def _get_branch_commit_messages(self, branch_name: str) -> str:
        """Get all commit messages from a branch since it diverged from main."""
        import subprocess

        try:
            result = subprocess.run(
                ["git", "log", "main.." + branch_name, "--format=%B", "--"],
                cwd=self.base_project,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.stdout if result.returncode == 0 else ""
        except Exception:
            return ""

    # =========================================================================
    # State & Helpers
    # =========================================================================

    @timed()
    def save_state(self) -> None:
        """Save current state for resumability."""
        self.state.save(self.state_file)
        # Log state file size
        if self.state_file.exists():
            size_kb = self.state_file.stat().st_size / 1024
            logger.info(f"State file size: {size_kb:.1f} KB")

    @timed()
    def _is_complete(self) -> bool:
        """Check if all Lean files have no sorries and no open issues."""
        # Check if there are open issues
        if len(self._get_open_issue_ids()) > 0:
            return False

        # Check all sketches are merged
        for chapter_info in self.state.chapters.values():
            if not chapter_info.get("sketch_merged"):
                return False

        # Check no sorries remain in any Lean file
        for lean_path in self._get_all_lean_files():
            sorries = self._scan_sorries(lean_path)
            if sorries:
                return False

        return True

    def get_status(self) -> dict:
        """Get current status."""
        total_prs = len(self.state.prs)
        pending = sum(1 for p in self.state.prs.values() if p.status == "pending_review")
        revisions = sum(1 for p in self.state.prs.values() if p.status == "needs_revision")
        approved = sum(1 for p in self.state.prs.values() if p.status == "approved")
        merged = sum(1 for p in self.state.prs.values() if p.status == "merged")

        return {
            "book_id": self.state.book_id,
            "title": self.config.title,
            "total_chapters": len(self.state.chapters),
            "chapters_with_sketch": sum(1 for c in self.state.chapters.values() if c.get("sketch_merged")),
            "total_prs": total_prs,
            "pending_review": pending,
            "needs_revision": revisions,
            "approved": approved,
            "merged": merged,
            "completed_theorems": sum(len(t) for t in self.state.completed_theorems.values()),
        }

    # =========================================================================
    # Manifest Loading
    # =========================================================================

    def load_manifest(self, manifest_path: Path) -> int:
        """Load chapters from manifest.

        Manifest format:
        {
            "chapters": [
                {
                    "id": "ch1",
                    "title": "Introduction",
                    "source_path": "tex/ch1.tex",
                    "lean_path": "MyBook/Ch1.lean"
                }
            ]
        }
        """
        with open(manifest_path) as f:
            manifest = json.load(f)

        count = 0
        for ch in manifest.get("chapters", []):
            chapter_id = ch["id"]
            # Merge with existing state to preserve sketch_merged status on resume
            existing = self.state.chapters.get(chapter_id, {})
            self.state.chapters[chapter_id] = {
                "title": ch.get("title", chapter_id),
                "source_path": ch.get("source_path", ch.get("source", "")),
                "lean_path": ch.get("lean_path", ""),
                "target_theorems": ch.get("target_theorems", []),
                "sketch_merged": existing.get("sketch_merged", False),  # Preserve on resume!
            }
            count += 1

        self.save_state()
        logger.info(f"Loaded {count} chapters from manifest")
        return count
