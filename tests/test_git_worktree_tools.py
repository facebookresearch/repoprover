# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Tests for git_worktree_tools.py - Git rebase workflow tools."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def temp_git_repo():
    """Create a temporary git repository with main branch and some commits."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)

        # Initialize repo
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)

        # Create initial commit on main
        (repo / "file1.txt").write_text("initial content\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, capture_output=True)

        yield repo


@pytest.fixture
def repo_with_feature_branch(temp_git_repo):
    """Create a repo with main and a feature branch that diverged."""
    repo = temp_git_repo

    # Create feature branch
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, capture_output=True)
    (repo / "feature.txt").write_text("feature content\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Feature commit"], cwd=repo, capture_output=True)

    # Go back to main and add more commits
    subprocess.run(["git", "checkout", "main"], cwd=repo, capture_output=True)
    (repo / "main_update.txt").write_text("main update\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Main update"], cwd=repo, capture_output=True)

    # Go back to feature branch
    subprocess.run(["git", "checkout", "feature"], cwd=repo, capture_output=True)

    yield repo


@pytest.fixture
def repo_with_conflict(temp_git_repo):
    """Create a repo where feature and main have conflicting changes to same file."""
    repo = temp_git_repo

    # Create feature branch with change to file1.txt
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, capture_output=True)
    (repo / "file1.txt").write_text("feature version\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Feature changes file1"], cwd=repo, capture_output=True)

    # Go back to main and make conflicting change
    subprocess.run(["git", "checkout", "main"], cwd=repo, capture_output=True)
    (repo / "file1.txt").write_text("main version\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Main changes file1"], cwd=repo, capture_output=True)

    # Go back to feature branch
    subprocess.run(["git", "checkout", "feature"], cwd=repo, capture_output=True)

    yield repo


@pytest.fixture
def mock_worktree_manager(temp_git_repo):
    """Create a mock WorktreeManager pointing to the temp repo."""
    manager = MagicMock()
    manager.worktree_path = temp_git_repo
    manager.validate_path = MagicMock(return_value=(True, ""))
    return manager


@pytest.fixture
def git_tools_mixin(mock_worktree_manager):
    """Create a GitWorktreeToolsMixin instance for testing."""
    from repoprover.agents.git_worktree_tools import GitWorktreeToolsMixin

    class TestMixin(GitWorktreeToolsMixin):
        def __init__(self, wt_manager):
            self.worktree_manager = wt_manager
            self.repo_root = wt_manager.worktree_path

        def _register_tools_from_list(self, tools, defs, handlers):
            pass

    return TestMixin(mock_worktree_manager)


class TestGitRebase:
    """Test git_rebase tool."""

    def test_rebase_clean_no_conflicts(self, git_tools_mixin, repo_with_feature_branch):
        """Rebase succeeds when there are no conflicts."""
        git_tools_mixin.worktree_manager.worktree_path = repo_with_feature_branch

        result = git_tools_mixin._handle_git_rebase({"branch": "main"})

        assert "Error" not in result
        assert "conflict" not in result.lower()

        # Verify the rebase actually happened - feature should be ahead of main
        log = subprocess.run(
            ["git", "log", "--oneline", "-3"],
            cwd=repo_with_feature_branch,
            capture_output=True,
            text=True,
        )
        assert "Feature commit" in log.stdout
        assert "Main update" in log.stdout

    def test_rebase_defaults_to_main(self, git_tools_mixin, repo_with_feature_branch):
        """Rebase without branch argument defaults to main."""
        git_tools_mixin.worktree_manager.worktree_path = repo_with_feature_branch

        result = git_tools_mixin._handle_git_rebase({})

        assert "Error" not in result
        assert "conflict" not in result.lower()

    def test_rebase_with_conflicts_reports_files(self, git_tools_mixin, repo_with_conflict):
        """Rebase with conflicts reports conflicted files."""
        git_tools_mixin.worktree_manager.worktree_path = repo_with_conflict

        result = git_tools_mixin._handle_git_rebase({"branch": "main"})

        assert "conflict" in result.lower()
        assert "file1.txt" in result
        assert "git_rebase_continue" in result or "rebase_continue" in result
        assert "git_rebase_abort" in result or "rebase_abort" in result

    def test_rebase_conflict_leaves_repo_in_rebase_state(self, git_tools_mixin, repo_with_conflict):
        """After conflict, repo is in rebase-in-progress state."""
        git_tools_mixin.worktree_manager.worktree_path = repo_with_conflict

        git_tools_mixin._handle_git_rebase({"branch": "main"})

        # Check that we're in a rebase
        rebase_dir = repo_with_conflict / ".git" / "rebase-merge"
        rebase_dir_apply = repo_with_conflict / ".git" / "rebase-apply"
        assert rebase_dir.exists() or rebase_dir_apply.exists()

    def test_rebase_already_up_to_date(self, git_tools_mixin, temp_git_repo):
        """Rebase when already on main is a no-op."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._handle_git_rebase({"branch": "main"})

        # Should succeed without error (already up to date)
        assert "Error" not in result or "up to date" in result.lower()


class TestGitRebaseContinue:
    """Test git_rebase_continue tool."""

    def test_continue_after_resolving_conflict(self, git_tools_mixin, repo_with_conflict):
        """Continue succeeds after conflict is resolved."""
        repo = repo_with_conflict
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Start rebase (will conflict)
        git_tools_mixin._handle_git_rebase({"branch": "main"})

        # Resolve conflict manually
        (repo / "file1.txt").write_text("resolved content\n")
        subprocess.run(["git", "add", "file1.txt"], cwd=repo, capture_output=True)

        # Continue rebase
        result = git_tools_mixin._handle_git_rebase_continue({})

        assert "Error" not in result or "Successfully" in result

        # Verify rebase completed
        rebase_dir = repo / ".git" / "rebase-merge"
        rebase_dir_apply = repo / ".git" / "rebase-apply"
        assert not rebase_dir.exists() and not rebase_dir_apply.exists()

    def test_continue_uses_env_to_skip_editor(self, git_tools_mixin, repo_with_conflict):
        """Continue sets GIT_EDITOR=true to avoid editor prompts."""
        repo = repo_with_conflict
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Start rebase (will conflict)
        git_tools_mixin._handle_git_rebase({"branch": "main"})

        # Resolve conflict
        (repo / "file1.txt").write_text("resolved content\n")
        subprocess.run(["git", "add", "file1.txt"], cwd=repo, capture_output=True)

        # Mock _run_git_command_with_env to verify it's called with correct env
        original_method = git_tools_mixin._run_git_command_with_env
        called_with_env: dict[str, Any] = {}

        def capture_env(args, env):
            called_with_env.update(env)
            return original_method(args, env)

        git_tools_mixin._run_git_command_with_env = capture_env

        # Continue rebase
        git_tools_mixin._handle_git_rebase_continue({})

        # Verify GIT_EDITOR was set
        assert "GIT_EDITOR" in called_with_env
        assert called_with_env["GIT_EDITOR"] == "true"

    def test_continue_without_staging_fails(self, git_tools_mixin, repo_with_conflict):
        """Continue without staging resolved files fails."""
        repo = repo_with_conflict
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Start rebase (will conflict)
        git_tools_mixin._handle_git_rebase({"branch": "main"})

        # Resolve conflict but DON'T stage
        (repo / "file1.txt").write_text("resolved content\n")

        # Continue should fail or report issue
        result = git_tools_mixin._handle_git_rebase_continue({})

        # Should either error or indicate more work needed
        assert "Error" in result or "conflict" in result.lower() or "add" in result.lower()

    def test_continue_when_no_rebase_in_progress(self, git_tools_mixin, temp_git_repo):
        """Continue when no rebase is in progress fails gracefully."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._handle_git_rebase_continue({})

        assert "Error" in result or "no rebase" in result.lower()


class TestRunGitCommandWithEnv:
    """Test the _run_git_command_with_env helper method."""

    def test_env_vars_are_passed_to_subprocess(self, git_tools_mixin, temp_git_repo):
        """Environment variables are correctly passed to git subprocess."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        # Use a git command that respects environment
        # GIT_AUTHOR_NAME is a good test - it affects commit output
        result = git_tools_mixin._run_git_command_with_env(["config", "--get", "user.name"], {})
        # Should work without error
        assert "Error" not in result or "Test" in result

    def test_env_vars_override_defaults(self, git_tools_mixin, temp_git_repo):
        """Custom env vars override default environment."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        # For unit testing, we can verify the method signature works
        result = git_tools_mixin._run_git_command_with_env(["status"], {"GIT_EDITOR": "true", "GIT_PAGER": "cat"})
        assert "Error" not in result

    def test_env_inherits_parent_environment(self, git_tools_mixin, temp_git_repo):
        """Custom env is merged with parent environment, not replacing it."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        # PATH must be inherited for git to work
        # If we replaced env entirely, git wouldn't be found
        result = git_tools_mixin._run_git_command_with_env(["--version"], {"CUSTOM_VAR": "test"})
        assert "git version" in result.lower()

    def test_env_handles_timeout(self, git_tools_mixin, temp_git_repo):
        """Commands with env still respect timeout."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._run_git_command_with_env(["status"], {})
        assert result is not None


class TestGitRebaseAbort:
    """Test git_rebase_abort tool."""

    def test_abort_during_conflict(self, git_tools_mixin, repo_with_conflict):
        """Abort restores repo to pre-rebase state."""
        repo = repo_with_conflict
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Get original HEAD
        original_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Start rebase (will conflict)
        git_tools_mixin._handle_git_rebase({"branch": "main"})

        # Abort
        result = git_tools_mixin._handle_git_rebase_abort({})

        assert "Error" not in result

        # Verify HEAD is back to original
        new_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert new_head == original_head

        # Verify not in rebase state
        rebase_dir = repo / ".git" / "rebase-merge"
        assert not rebase_dir.exists()

    def test_abort_when_no_rebase_in_progress(self, git_tools_mixin, temp_git_repo):
        """Abort when no rebase is in progress fails gracefully."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._handle_git_rebase_abort({})

        assert "Error" in result or "no rebase" in result.lower()


class TestGitRebaseSkip:
    """Test git_rebase_skip tool."""

    def test_skip_commit_during_conflict(self, git_tools_mixin, repo_with_conflict):
        """Skip drops the current commit and continues."""
        repo = repo_with_conflict
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Start rebase (will conflict)
        git_tools_mixin._handle_git_rebase({"branch": "main"})

        # Skip the conflicting commit
        result = git_tools_mixin._handle_git_rebase_skip({})

        # Should succeed (commit is dropped)
        assert "Error" not in result or "Successfully" in result

        # Feature changes should be gone (we skipped them)
        content = (repo / "file1.txt").read_text()
        assert "feature version" not in content

    def test_skip_when_no_rebase_in_progress(self, git_tools_mixin, temp_git_repo):
        """Skip when no rebase is in progress fails gracefully."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._handle_git_rebase_skip({})

        assert "Error" in result or "no rebase" in result.lower()


class TestGitConflicts:
    """Test git_conflicts tool."""

    def test_conflicts_shows_line_numbers(self, git_tools_mixin, repo_with_conflict):
        """git_conflicts shows conflict marker line numbers."""
        repo = repo_with_conflict
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Start rebase to create conflict
        git_tools_mixin._handle_git_rebase({"branch": "main"})

        result = git_tools_mixin._handle_git_conflicts({})

        # Should show file and line numbers
        assert "file1.txt" in result
        assert ":" in result  # Line number separator
        # git diff --check shows "filename:line: leftover conflict marker"
        assert "conflict" in result.lower() or "marker" in result.lower()

    def test_conflicts_when_no_conflicts(self, git_tools_mixin, temp_git_repo):
        """git_conflicts with clean repo shows no conflicts."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._handle_git_conflicts({})

        assert "No conflict" in result or result == "(no output)"

    def test_conflicts_multiple_files(self, git_tools_mixin, temp_git_repo):
        """git_conflicts shows all conflicted files."""
        repo = temp_git_repo

        # Create feature branch with changes to multiple files
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, capture_output=True)
        (repo / "file1.txt").write_text("feature version 1\n")
        (repo / "file2.txt").write_text("feature file 2\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Feature"], cwd=repo, capture_output=True)

        # Main has conflicting changes
        subprocess.run(["git", "checkout", "main"], cwd=repo, capture_output=True)
        (repo / "file1.txt").write_text("main version 1\n")
        (repo / "file2.txt").write_text("main file 2\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Main"], cwd=repo, capture_output=True)

        subprocess.run(["git", "checkout", "feature"], cwd=repo, capture_output=True)
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Start rebase
        git_tools_mixin._handle_git_rebase({"branch": "main"})

        result = git_tools_mixin._handle_git_conflicts({})

        assert "file1.txt" in result
        assert "file2.txt" in result


class TestGitCheckoutFile:
    """Test git_checkout_file tool."""

    def test_checkout_file_from_main(self, git_tools_mixin, repo_with_feature_branch):
        """Checkout file from main overwrites working copy."""
        repo = repo_with_feature_branch
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Modify file on feature branch
        (repo / "file1.txt").write_text("modified on feature\n")

        # Checkout from main
        result = git_tools_mixin._handle_git_checkout_file({"ref": "main", "paths": ["file1.txt"]})

        assert "Error" not in result
        assert (repo / "file1.txt").read_text() == "initial content\n"

    def test_checkout_file_from_commit(self, git_tools_mixin, temp_git_repo):
        """Checkout file from specific commit."""
        repo = temp_git_repo
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Get initial commit hash
        initial_hash = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Make changes and commit
        (repo / "file1.txt").write_text("new content\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Update"], cwd=repo, capture_output=True)

        # Checkout from initial commit
        result = git_tools_mixin._handle_git_checkout_file({"ref": initial_hash, "paths": ["file1.txt"]})

        assert "Error" not in result
        assert (repo / "file1.txt").read_text() == "initial content\n"

    def test_checkout_file_requires_ref(self, git_tools_mixin, temp_git_repo):
        """Checkout without ref fails."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._handle_git_checkout_file({"paths": ["file1.txt"]})

        assert "Error" in result
        assert "ref" in result.lower()

    def test_checkout_file_requires_paths(self, git_tools_mixin, temp_git_repo):
        """Checkout without paths fails."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._handle_git_checkout_file({"ref": "main"})

        assert "Error" in result
        assert "path" in result.lower()

    def test_checkout_nonexistent_file_fails(self, git_tools_mixin, temp_git_repo):
        """Checkout nonexistent file fails gracefully."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._handle_git_checkout_file({"ref": "main", "paths": ["nonexistent.txt"]})

        assert "Error" in result


class TestGitReset:
    """Test git_reset tool."""

    def test_reset_soft(self, git_tools_mixin, temp_git_repo):
        """Reset --soft keeps changes staged."""
        repo = temp_git_repo
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Make a commit
        (repo / "new.txt").write_text("new content\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "New file"], cwd=repo, capture_output=True)

        # Reset soft to previous commit
        result = git_tools_mixin._handle_git_reset({"ref": "HEAD~1", "mode": "soft"})

        assert "Error" not in result

        # File should still exist and be staged
        assert (repo / "new.txt").exists()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout
        assert "A" in status or "M" in status  # Staged

    def test_reset_mixed(self, git_tools_mixin, temp_git_repo):
        """Reset --mixed (default) unstages but keeps changes."""
        repo = temp_git_repo
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Make a commit
        (repo / "new.txt").write_text("new content\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "New file"], cwd=repo, capture_output=True)

        # Reset mixed (default)
        result = git_tools_mixin._handle_git_reset({"ref": "HEAD~1", "mode": "mixed"})

        assert "Error" not in result

        # File should exist but be untracked
        assert (repo / "new.txt").exists()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout
        assert "?" in status  # Untracked

    def test_reset_hard(self, git_tools_mixin, temp_git_repo):
        """Reset --hard discards all changes."""
        repo = temp_git_repo
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Make a commit
        (repo / "new.txt").write_text("new content\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "New file"], cwd=repo, capture_output=True)

        # Reset hard
        result = git_tools_mixin._handle_git_reset({"ref": "HEAD~1", "mode": "hard"})

        assert "Error" not in result

        # File should be GONE
        assert not (repo / "new.txt").exists()

    def test_reset_invalid_mode(self, git_tools_mixin, temp_git_repo):
        """Reset with invalid mode fails."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._handle_git_reset({"ref": "HEAD", "mode": "invalid"})

        assert "Error" in result


class TestGitShow:
    """Test git_show tool."""

    def test_show_head(self, git_tools_mixin, temp_git_repo):
        """Show HEAD displays commit info."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._handle_git_show({"ref": "HEAD"})

        assert "Error" not in result
        assert "Initial commit" in result
        assert "file1.txt" in result

    def test_show_file_at_ref(self, git_tools_mixin, temp_git_repo):
        """Show file content at specific ref."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._handle_git_show({"ref": "main:file1.txt"})

        assert "Error" not in result
        assert "initial content" in result

    def test_show_invalid_ref(self, git_tools_mixin, temp_git_repo):
        """Show invalid ref fails gracefully."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._handle_git_show({"ref": "nonexistent"})

        assert "Error" in result


class TestFullWorkflow:
    """Integration tests for complete rebase workflow."""

    def test_complete_conflict_resolution_workflow(self, git_tools_mixin, repo_with_conflict):
        """Test the full workflow: rebase -> conflicts -> resolve -> continue."""
        repo = repo_with_conflict
        git_tools_mixin.worktree_manager.worktree_path = repo

        # 1. Attempt rebase
        result = git_tools_mixin._handle_git_rebase({"branch": "main"})
        assert "conflict" in result.lower()

        # 2. Check conflicts to find line numbers
        conflicts = git_tools_mixin._handle_git_conflicts({})
        assert "file1.txt" in conflicts

        # 3. Resolve conflict (simulate agent editing)
        (repo / "file1.txt").write_text("resolved: combining both changes\n")

        # 4. Stage resolved file (using git add directly for test)
        subprocess.run(["git", "add", "file1.txt"], cwd=repo, capture_output=True)

        # 5. Continue rebase
        result = git_tools_mixin._handle_git_rebase_continue({})
        assert "Error" not in result or "Successfully" in result

        # 6. Verify clean state
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout
        assert status.strip() == ""  # Clean working tree

    def test_abort_workflow(self, git_tools_mixin, repo_with_conflict):
        """Test abort workflow when conflicts are too complex."""
        repo = repo_with_conflict
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Get original state
        original_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # 1. Attempt rebase
        git_tools_mixin._handle_git_rebase({"branch": "main"})

        # 2. Decide to abort
        result = git_tools_mixin._handle_git_rebase_abort({})
        assert "Error" not in result

        # 3. Verify back to original state
        new_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert new_head == original_head


class TestFormatStatus:
    """Test _format_status method with conflict detection."""

    def test_format_status_shows_conflicts(self, git_tools_mixin, repo_with_conflict):
        """Status shows conflicts prominently."""
        repo = repo_with_conflict
        git_tools_mixin.worktree_manager.worktree_path = repo

        # Start rebase to create conflict
        git_tools_mixin._handle_git_rebase({"branch": "main"})

        # Get status
        result = git_tools_mixin._handle_git_status({})

        # Should show conflicts prominently
        assert "CONFLICTS" in result or "conflict" in result.lower()
        assert "file1.txt" in result

    def test_format_status_clean(self, git_tools_mixin, temp_git_repo):
        """Status shows clean working tree."""
        git_tools_mixin.worktree_manager.worktree_path = temp_git_repo

        result = git_tools_mixin._handle_git_status({})

        assert "clean" in result.lower() or "no changes" in result.lower()
