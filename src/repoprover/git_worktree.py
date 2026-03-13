# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Git worktree manager for isolated agent execution.

This module provides infrastructure for creating and managing git worktrees
that allow multiple agents to work in parallel on isolated branches. The
design follows the symlink strategy from docs/shared-mathlib-worktrees.md.

Key design decision: Commands are executed via subprocess.run() with explicit
argument lists (shell=False), not through a shell. This prevents shell injection.

Two agent roles:
- Feature Worker: Own branch only (agent-{id}), basic git operations
- Main Agent: Main branch + feature branches, merge/checkout/reset capabilities

Architecture:
```
base-project/                    # The fully-built Lean project
├── .lake/
│   ├── packages/                # 6.5G Mathlib (shared read-only)
│   ├── config/                  # Lake config (shared read-only)
│   └── build/                   # Base project build artifacts
└── FormalBook/
    └── Chapter1.lean
```
worktrees/
├── worktree-prove-ch1/         # Agent 1's isolated worktree
│   ├── .lake/
│   │   ├── packages -> base/.lake/packages  (symlink)
│   │   ├── config -> base/.lake/config      (symlink)
│   │   └── build/               # Agent 1's own build (~150M)
│   └── FormalBook/
│       └── Chapter1.lean        # Agent 1's working copy
```
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path

from .utils import run_git_with_retry

logger = getLogger(__name__)


def _parallel_rmtree(path: Path, max_workers: int = 8) -> None:
    """Remove a directory tree in parallel for faster deletion of many files.

    Strategy:
    1. Collect all top-level children of the directory
    2. Delete each child subtree in parallel using a thread pool
    3. Remove the now-empty root directory

    For directories with many files (e.g., .lake/build with thousands of .olean files),
    this can be significantly faster than sequential shutil.rmtree.

    Args:
        path: Directory to remove
        max_workers: Maximum number of parallel deletion threads
    """
    if not path.exists():
        return

    if not path.is_dir():
        path.unlink(missing_ok=True)
        return

    # Collect top-level children (don't follow symlinks)
    try:
        children = list(path.iterdir())
    except OSError:
        # Directory may have been removed by another process
        shutil.rmtree(path, ignore_errors=True)
        return

    if not children:
        # Empty directory, just remove it
        path.rmdir()
        return

    # Delete children in parallel
    def delete_child(child: Path) -> None:
        if child.is_symlink():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(delete_child, child) for child in children]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.debug(f"Error deleting child: {e}")

    # Remove the now-empty root
    try:
        path.rmdir()
    except OSError:
        # May still have files if some deletions failed, fall back to rmtree
        shutil.rmtree(path, ignore_errors=True)


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class WorktreeConfig:
    """Configuration for a git worktree.

    Attributes:
        base_project: The fully-built base Lean project (with .lake/packages etc.)
        worktrees_root: Directory where worktrees are created
        agent_id: Unique agent identifier (also used as branch name)
    """

    base_project: Path
    worktrees_root: Path
    agent_id: str

    def __post_init__(self) -> None:
        self.base_project = Path(self.base_project)
        self.worktrees_root = Path(self.worktrees_root)


# =============================================================================
# Worktree Manager
# =============================================================================


@dataclass
class WorktreeManager:
    """Manages a single git worktree for an agent.

    Handles worktree lifecycle:
    1. Creation with proper symlinks to shared .lake directories
    2. Path validation to prevent escape from sandbox
    3. Cleanup when agent is done
    """

    config: WorktreeConfig

    @property
    def worktree_path(self) -> Path:
        """Return the path to this agent's worktree."""
        return self.config.worktrees_root / f"worktree-{self.config.agent_id}"

    @property
    def branch_name(self) -> str:
        """Return the branch name for this agent."""
        return self.config.agent_id

    def setup(self) -> tuple[bool, str]:
        """Create worktree and wire up .lake symlinks.

        Steps:
        1. git worktree add {worktree_path} -b {branch_name}
        2. mkdir -p {worktree_path}/.lake
        3. ln -s {base_project}/.lake/packages {worktree_path}/.lake/packages
        4. ln -s {base_project}/.lake/config {worktree_path}/.lake/config
        5. mkdir -p {worktree_path}/.lake/build (agent-specific)

        Returns:
            (success, message) tuple
        """
        base = self.config.base_project
        wt_path = self.worktree_path
        branch = self.branch_name

        logger.info(f"Setting up worktree at {wt_path} on branch {branch}")

        # Ensure worktrees root exists
        self.config.worktrees_root.mkdir(parents=True, exist_ok=True)

        # Check if worktree already exists
        if wt_path.exists():
            if self.is_setup():
                logger.info("Worktree already exists and is properly configured")
                return True, "Worktree already exists"
            else:
                # Clean up incomplete setup
                logger.warning("Worktree exists but is incomplete, cleaning up")
                self._force_remove_worktree()

        # Create the worktree with a new branch
        success, error_msg = self._try_create_worktree(base, wt_path, branch)

        if not success:
            # If creation failed, try pruning stale worktrees and retry once
            # This handles the "failed to read .git/worktrees/.../commondir" error
            logger.warning(f"Worktree creation failed, pruning stale entries and retrying: {error_msg}")
            run_git_with_retry(
                ["worktree", "prune"],
                cwd=base,
                timeout=30,
                retries=1,
            )

            # Retry after prune
            success, error_msg = self._try_create_worktree(base, wt_path, branch)
            if not success:
                return False, error_msg

        # Set up .lake directory structure
        lake_dir = wt_path / ".lake"
        lake_dir.mkdir(parents=True, exist_ok=True)

        # Create symlinks to shared directories
        base_lake = base / ".lake"
        packages_link = lake_dir / "packages"
        config_link = lake_dir / "config"

        try:
            # Symlink packages (shared read-only)
            if (base_lake / "packages").exists():
                if not packages_link.exists():
                    packages_link.symlink_to(base_lake / "packages")
                    logger.debug(f"Created symlink: {packages_link} -> {base_lake / 'packages'}")

            # Symlink config (shared read-only)
            if (base_lake / "config").exists():
                if not config_link.exists():
                    config_link.symlink_to(base_lake / "config")
                    logger.debug(f"Created symlink: {config_link} -> {base_lake / 'config'}")

            # Create agent-specific build directory
            build_dir = lake_dir / "build"
            build_dir.mkdir(parents=True, exist_ok=True)

        except OSError as e:
            return False, f"Failed to set up .lake symlinks: {e}"

        logger.info(f"Worktree setup complete: {wt_path}")
        return True, "Worktree created successfully"

    def _try_create_worktree(self, base: Path, wt_path: Path, branch: str) -> tuple[bool, str]:
        """Attempt to create a git worktree with retry. Returns (success, error_message)."""
        # First check if branch already exists - no point retrying if it does
        branch_check = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=base,
            capture_output=True,
            text=True,
            timeout=30,
        )
        branch_exists = branch_check.returncode == 0

        # Build the base command - use -f (force) to handle "missing but registered" worktrees
        # This is safe because we've already checked/cleaned the directory above
        if branch_exists:
            # Branch exists, create worktree using existing branch
            logger.info(f"Branch {branch} already exists, creating worktree without -b")
            base_cmd = ["worktree", "add", "-f", str(wt_path), branch]
        else:
            # Branch doesn't exist, create with new branch
            base_cmd = ["worktree", "add", "-f", str(wt_path), "-b", branch]

        success, error, result = run_git_with_retry(
            base_cmd,
            cwd=base,
            timeout=120,
            retries=2,
        )

        if success:
            return True, ""

        # Check for "already registered" error - prune and retry with force
        stderr = (result.stderr or "") if result else ""
        if "already registered" in stderr:
            logger.info("Worktree path is stale-registered, pruning and retrying")
            run_git_with_retry(["worktree", "prune"], cwd=base, timeout=30, retries=1)
            # Retry after prune
            success, error, result = run_git_with_retry(
                base_cmd,
                cwd=base,
                timeout=120,
                retries=2,
            )
            if success:
                return True, ""

        # Fallback: Branch might have been created by another process in the meantime
        if result and "already exists" in (result.stderr or ""):
            logger.info(f"Branch {branch} was created concurrently, retrying without -b")
            success, error, result = run_git_with_retry(
                ["worktree", "add", "-f", str(wt_path), branch],
                cwd=base,
                timeout=120,
                retries=2,
            )
            if success:
                return True, ""
            return False, f"Failed to create worktree at {wt_path}: {error}"

        return False, f"Failed to create worktree at {wt_path}: {error}"

    def cleanup(self, delete_branch: bool = False) -> tuple[bool, str]:
        """Remove worktree, optionally the branch.

        NOTE: By default we preserve branches for replay/history purposes.

        Steps:
        1. git worktree remove {worktree_path}
        2. (if delete_branch) git branch -d {branch_name}

        Args:
            delete_branch: If True, also delete the branch (default: False)

        Returns:
            (success, message) tuple
        """
        base = self.config.base_project
        wt_path = self.worktree_path
        branch = self.branch_name

        logger.info(f"Cleaning up worktree at {wt_path}")

        if not wt_path.exists():
            logger.info("Worktree does not exist, nothing to clean up")
            return True, "Worktree does not exist"

        # Remove the worktree with retry
        success, error, _ = run_git_with_retry(
            ["worktree", "remove", str(wt_path), "--force"],
            cwd=base,
            timeout=60,
            retries=2,
        )
        if not success:
            logger.warning(f"git worktree remove failed: {error}, trying force removal")
            self._force_remove_worktree()

        # Prune worktree metadata
        run_git_with_retry(
            ["worktree", "prune"],
            cwd=base,
            timeout=30,
            retries=1,
        )

        # Optionally delete the branch (NOT by default - we preserve for history)
        if delete_branch:
            success, error, _ = run_git_with_retry(
                ["branch", "-d", branch],
                cwd=base,
                timeout=30,
                retries=1,
            )
            if not success:
                logger.debug(f"Could not delete branch {branch}: {error}")

        logger.info("Worktree cleanup complete")
        return True, "Worktree removed successfully"

    def _force_remove_worktree(self) -> None:
        """Force remove worktree directory and clean up git metadata."""
        wt_path = self.worktree_path
        base = self.config.base_project

        # Remove the directory using parallel deletion for speed
        if wt_path.exists():
            _parallel_rmtree(wt_path)

        # Clean up git worktree metadata - try both the directory name and worktree name
        # Git may name the metadata dir differently based on path normalization
        for name in [wt_path.name, f"worktree-{self.config.agent_id}"]:
            git_worktrees = base / ".git" / "worktrees" / name
            if git_worktrees.exists():
                shutil.rmtree(git_worktrees, ignore_errors=True)

        # CRITICAL: Always prune after force removal to clean up any stale entries
        # This fixes the "failed to read .git/worktrees/.../commondir: Success" error
        run_git_with_retry(
            ["worktree", "prune"],
            cwd=base,
            timeout=30,
            retries=1,
        )

    def validate_path(self, path: Path | str) -> tuple[bool, str]:
        """Validate that a path is safe for agent operations.

        Rules:
        1. Path must be within worktree_path (without following final symlinks)
        2. Cannot be in .lake/packages or .lake/config (read-only shared)
        3. Cannot contain .. that escapes worktree

        We allow symlinks that point outside the repo, as long as the symlink
        itself is placed within the worktree (e.g., .lake/packages/mathlib -> cache).

        Args:
            path: Relative path within the worktree

        Returns:
            (ok, message) tuple - ok is True if path is safe
        """
        wt_path = self.worktree_path

        # Handle absolute vs relative paths
        if Path(path).is_absolute():
            p = Path(path)
        else:
            p = wt_path / path

        # Resolve the path without following the final symlink
        # We resolve the parent directory and append the final component
        # This ensures we catch ".." traversals but allow symlinks
        if p.exists() or p.is_symlink():
            resolved = p.parent.resolve() / p.name
        else:
            resolved = p.resolve()

        # Check containment (path itself must be in worktree, not its target)
        try:
            resolved.relative_to(wt_path.resolve())
        except ValueError:
            return False, f"Path escapes worktree: {path}"

        # Check not in shared symlinked dirs (these are read-only)
        # For write operations, we need to block .lake/packages and .lake/config
        # Check if path starts with these symlinked directories
        try:
            resolved.relative_to((wt_path / ".lake" / "packages").parent.resolve() / "packages")
            return False, f"Cannot modify shared .lake/packages directory: {path}"
        except ValueError:
            pass  # Not in packages, good

        try:
            resolved.relative_to((wt_path / ".lake" / "config").parent.resolve() / "config")
            return False, f"Cannot modify shared .lake/config directory: {path}"
        except ValueError:
            pass  # Not in config, good

        return True, "ok"

    def is_setup(self) -> bool:
        """Check if worktree exists and is properly configured."""
        wt_path = self.worktree_path

        if not wt_path.exists():
            return False

        # Check it's a git worktree
        git_file = wt_path / ".git"
        if not git_file.exists():
            return False

        # Check .lake structure
        lake_dir = wt_path / ".lake"
        if not lake_dir.exists():
            return False

        # Check symlinks exist (if base has them)
        base_lake = self.config.base_project / ".lake"
        if (base_lake / "packages").exists():
            packages_link = lake_dir / "packages"
            if not packages_link.is_symlink():
                return False

        return True

    def checkout_branch(self, branch_name: str) -> tuple[bool, str]:
        """Checkout a specific branch in this worktree.

        Used by reviewers who need to review a specific PR branch.

        Args:
            branch_name: The branch to checkout

        Returns:
            (success, message) tuple
        """
        try:
            result = subprocess.run(
                ["git", "checkout", branch_name],
                cwd=self.worktree_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return False, f"Failed to checkout {branch_name}: {result.stderr}"
            return True, f"Checked out {branch_name}"
        except subprocess.TimeoutExpired:
            return False, "Timeout checking out branch"
        except Exception as e:
            return False, f"Error checking out branch: {e}"

    def get_current_branch(self) -> str:
        """Get the current branch name in this worktree."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.worktree_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""


# =============================================================================
# Worktree Pool
# =============================================================================


@dataclass
class WorktreePool:
    """Manages a pool of git worktrees for parallel agent execution.

    Creates worktrees on demand and cleans them up when agents reach terminal states.

    Uses per-agent locks to prevent race conditions when multiple threads
    try to acquire the same agent_id simultaneously within a process.

    NOTE: Worktree state is managed via filesystem, not in-memory tracking.
    This allows worktrees to be shared across processes (e.g., worker creates,
    coordinator reuses for review).

    Args:
        base_project: Path to the base Lean project (with .lake/packages etc.)
        worktrees_root: Directory where worktrees are created
        skip_cleanup: If True, skip startup cleanup (for workers - coordinator does cleanup)
    """

    base_project: Path
    worktrees_root: Path
    skip_cleanup: bool = False
    # Per-agent locks to prevent concurrent setup of the same worktree
    _setup_locks: dict[str, threading.Lock] = field(default_factory=dict)
    _setup_locks_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.base_project = Path(self.base_project)
        self.worktrees_root = Path(self.worktrees_root)
        if not self.skip_cleanup:
            self._cleanup_locked_worktrees()

    def _get_setup_lock(self, agent_id: str) -> threading.Lock:
        """Get or create a lock for setting up a specific agent's worktree."""
        with self._setup_locks_lock:
            if agent_id not in self._setup_locks:
                self._setup_locks[agent_id] = threading.Lock()
            return self._setup_locks[agent_id]

    def _cleanup_locked_worktrees(self) -> None:
        """Clean up all worktrees on pool startup for a fresh state.

        Only called by coordinator (rank 0). Workers skip this via skip_cleanup=True.

        Since worktrees are throwaway copies (all work is safe on branches),
        we can aggressively clean up on startup:
        1. Remove all lock files from .git/worktrees/*/locked
        2. Delete the entire worktrees directory
        3. Run git worktree prune to clean up git's metadata
        """
        logger.info("Performing worktree cleanup (coordinator only)")

        git_worktrees_dir = self.base_project / ".git" / "worktrees"

        # Step 1: Remove all lock files so git worktree prune can clean them
        if git_worktrees_dir.exists():
            unlocked_count = 0
            for wt_meta in git_worktrees_dir.iterdir():
                if not wt_meta.is_dir():
                    continue
                lock_file = wt_meta / "locked"
                if lock_file.exists():
                    try:
                        lock_file.unlink()
                        unlocked_count += 1
                    except Exception as e:
                        logger.debug(f"Could not remove lock file {lock_file}: {e}")

            if unlocked_count > 0:
                logger.info(f"Removed {unlocked_count} worktree lock files")

        # Step 2: Delete entire worktrees directory (it's all throwaway)
        # Use parallel deletion for speed - worktrees can have thousands of files
        if self.worktrees_root.exists():
            try:
                _parallel_rmtree(self.worktrees_root)
                logger.info(f"Removed worktrees directory: {self.worktrees_root}")
            except Exception as e:
                logger.warning(f"Failed to remove worktrees directory: {e}")

        # Step 3: Prune stale worktree metadata from git
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=self.base_project,
                capture_output=True,
                text=True,
                timeout=120,
            )
            logger.info("Pruned stale worktree metadata")
        except subprocess.TimeoutExpired:
            logger.warning("git worktree prune timed out, continuing anyway")
        except Exception as e:
            logger.warning(f"Failed to prune worktrees: {e}")

        logger.info("Worktree cleanup complete")

    def setup(self, agent_id: str) -> WorktreeManager:
        """Set up a worktree for an agent (idempotent).

        Creates a new worktree if it doesn't exist, or returns the existing one.
        The agent_id is used as the branch name. For new agents, a new branch
        is created. For revisions, the existing branch is reused (same agent_id).

        Thread-safe: uses per-agent locks to prevent concurrent setup of the
        same worktree by multiple threads within a process.

        Args:
            agent_id: Unique identifier for the agent (also used as branch name)

        Returns:
            WorktreeManager for the agent's worktree
        """
        # Per-agent lock to serialize setup attempts for the same agent_id
        # This prevents races when multiple threads try to set up the same worktree
        setup_lock = self._get_setup_lock(agent_id)

        with setup_lock:
            config = WorktreeConfig(
                base_project=self.base_project,
                worktrees_root=self.worktrees_root,
                agent_id=agent_id,
            )
            manager = WorktreeManager(config)
            success, msg = manager.setup()

            if not success:
                raise RuntimeError(f"Failed to set up worktree for {agent_id}: {msg}")

            return manager

    def cleanup(self, agent_id: str) -> None:
        """Clean up a worktree (delete directory, preserve branch).

        Call this ONLY on terminal states: merged, failed, rejected, blocked.
        Safe to call multiple times or if worktree doesn't exist.

        Args:
            agent_id: Agent identifier to clean up
        """
        config = WorktreeConfig(
            base_project=self.base_project,
            worktrees_root=self.worktrees_root,
            agent_id=agent_id,
        )
        manager = WorktreeManager(config)
        manager.cleanup(delete_branch=False)
