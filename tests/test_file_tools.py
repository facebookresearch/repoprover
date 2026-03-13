# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Tests for file_cut_paste and file_copy_paste tools.

These tests verify:
1. file_copy_paste - copy lines between files and within a file
2. file_cut_paste - cut lines between files and within a file
3. Edge cases: boundaries, empty ranges, same file operations
"""

import pytest
from pathlib import Path
import tempfile
import shutil

from repoprover.agents.file_tools import FileToolsMixin


class MockFileToolsAgent(FileToolsMixin):
    """Mock agent with file tools for testing."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.worktree_manager = None
        self.allow_source_writes = False

    def register_tools(self, defs: dict, handlers: dict) -> None:
        """Register tools - base case for mixin chain."""
        pass

    def _register_tools_from_list(self, tools: list, defs: dict, handlers: dict) -> None:
        """Register tools from list."""
        for tool_def in tools:
            name = tool_def["function"]["name"]
            defs[name] = tool_def
            handler = getattr(self, f"_handle_{name}", None)
            if handler:
                handlers[name] = handler


@pytest.fixture
def temp_repo():
    """Create a temporary repository with test files."""
    temp_dir = Path(tempfile.mkdtemp())
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def agent(temp_repo):
    """Create a mock agent with the temp repo as root."""
    return MockFileToolsAgent(temp_repo)


def create_file(repo: Path, name: str, content: str) -> Path:
    """Helper to create a file with given content."""
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class TestFileCopyPaste:
    """Tests for file_copy_paste tool."""

    def test_copy_between_different_files(self, agent, temp_repo):
        """Copy lines from one file to another."""
        # Create source file with numbered lines
        create_file(temp_repo, "src.lean", "line1\nline2\nline3\nline4\nline5")
        create_file(temp_repo, "dest.lean", "destA\ndestB\ndestC")

        result = agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 2,
                "src_end_line": 4,
                "dest_path": "dest.lean",
                "dest_line": 2,
            }
        )

        assert "Copied 3 lines" in result
        assert "Error" not in result

        # Verify source unchanged
        src_content = (temp_repo / "src.lean").read_text()
        assert src_content == "line1\nline2\nline3\nline4\nline5"

        # Verify destination has inserted lines
        dest_content = (temp_repo / "dest.lean").read_text()
        expected = "destA\nline2\nline3\nline4\ndestB\ndestC"
        assert dest_content == expected

    def test_copy_to_beginning(self, agent, temp_repo):
        """Copy lines and insert at beginning of file (dest_line=1)."""
        create_file(temp_repo, "src.lean", "line1\nline2\nline3")
        create_file(temp_repo, "dest.lean", "destA\ndestB")

        result = agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 2,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Copied 2 lines" in result

        dest_content = (temp_repo / "dest.lean").read_text()
        expected = "line1\nline2\ndestA\ndestB"
        assert dest_content == expected

    def test_copy_to_end(self, agent, temp_repo):
        """Copy lines and append at end of file."""
        create_file(temp_repo, "src.lean", "line1\nline2\nline3")
        create_file(temp_repo, "dest.lean", "destA\ndestB")

        result = agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 2,
                "src_end_line": 3,
                "dest_path": "dest.lean",
                "dest_line": 999,  # Beyond file length
            }
        )

        assert "Copied 2 lines" in result

        dest_content = (temp_repo / "dest.lean").read_text()
        expected = "destA\ndestB\nline2\nline3"
        assert dest_content == expected

    def test_copy_single_line(self, agent, temp_repo):
        """Copy a single line (start == end)."""
        create_file(temp_repo, "src.lean", "line1\nline2\nline3")
        create_file(temp_repo, "dest.lean", "destA\ndestB")

        result = agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 2,
                "src_end_line": 2,
                "dest_path": "dest.lean",
                "dest_line": 2,
            }
        )

        assert "Copied 1 lines" in result

        dest_content = (temp_repo / "dest.lean").read_text()
        expected = "destA\nline2\ndestB"
        assert dest_content == expected

    def test_copy_within_same_file(self, agent, temp_repo):
        """Copy (duplicate) lines within the same file."""
        create_file(temp_repo, "file.lean", "line1\nline2\nline3\nline4")

        result = agent._handle_file_copy_paste(
            {
                "src_path": "file.lean",
                "src_start_line": 2,
                "src_end_line": 3,
                "dest_path": "file.lean",
                "dest_line": 5,  # Append at end
            }
        )

        assert "Duplicated 2 lines" in result

        content = (temp_repo / "file.lean").read_text()
        expected = "line1\nline2\nline3\nline4\nline2\nline3"
        assert content == expected

    def test_copy_error_invalid_range(self, agent, temp_repo):
        """Error when start_line > end_line."""
        create_file(temp_repo, "src.lean", "line1\nline2\nline3")
        create_file(temp_repo, "dest.lean", "destA")

        result = agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 3,
                "src_end_line": 1,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Error" in result
        assert "src_start_line (3) > src_end_line (1)" in result

    def test_copy_error_line_exceeds_file(self, agent, temp_repo):
        """Error when end_line exceeds file length."""
        create_file(temp_repo, "src.lean", "line1\nline2\nline3")
        create_file(temp_repo, "dest.lean", "destA")

        result = agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 10,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Error" in result
        assert "exceeds source file length" in result

    def test_copy_error_source_not_found(self, agent, temp_repo):
        """Error when source file doesn't exist."""
        create_file(temp_repo, "dest.lean", "destA")

        result = agent._handle_file_copy_paste(
            {
                "src_path": "nonexistent.lean",
                "src_start_line": 1,
                "src_end_line": 1,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Error" in result
        assert "Source file not found" in result

    def test_copy_error_dest_not_found(self, agent, temp_repo):
        """Error when destination file doesn't exist."""
        create_file(temp_repo, "src.lean", "line1")

        result = agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 1,
                "dest_path": "nonexistent.lean",
                "dest_line": 1,
            }
        )

        assert "Error" in result
        assert "Destination file not found" in result


class TestFileCutPaste:
    """Tests for file_cut_paste tool."""

    def test_cut_between_different_files(self, agent, temp_repo):
        """Cut lines from one file to another."""
        create_file(temp_repo, "src.lean", "line1\nline2\nline3\nline4\nline5")
        create_file(temp_repo, "dest.lean", "destA\ndestB\ndestC")

        result = agent._handle_file_cut_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 2,
                "src_end_line": 4,
                "dest_path": "dest.lean",
                "dest_line": 2,
            }
        )

        assert "Cut 3 lines" in result
        assert "Error" not in result

        # Verify source has lines removed
        src_content = (temp_repo / "src.lean").read_text()
        expected_src = "line1\nline5"
        assert src_content == expected_src

        # Verify destination has inserted lines
        dest_content = (temp_repo / "dest.lean").read_text()
        expected_dest = "destA\nline2\nline3\nline4\ndestB\ndestC"
        assert dest_content == expected_dest

    def test_cut_to_beginning(self, agent, temp_repo):
        """Cut lines and insert at beginning of file (dest_line=1)."""
        create_file(temp_repo, "src.lean", "line1\nline2\nline3")
        create_file(temp_repo, "dest.lean", "destA\ndestB")

        result = agent._handle_file_cut_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 2,
                "src_end_line": 3,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Cut 2 lines" in result

        src_content = (temp_repo / "src.lean").read_text()
        assert src_content == "line1"

        dest_content = (temp_repo / "dest.lean").read_text()
        expected = "line2\nline3\ndestA\ndestB"
        assert dest_content == expected

    def test_cut_to_end(self, agent, temp_repo):
        """Cut lines and append at end of file."""
        create_file(temp_repo, "src.lean", "line1\nline2\nline3")
        create_file(temp_repo, "dest.lean", "destA\ndestB")

        result = agent._handle_file_cut_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 2,
                "dest_path": "dest.lean",
                "dest_line": 999,  # Beyond file length
            }
        )

        assert "Cut 2 lines" in result

        src_content = (temp_repo / "src.lean").read_text()
        assert src_content == "line3"

        dest_content = (temp_repo / "dest.lean").read_text()
        expected = "destA\ndestB\nline1\nline2"
        assert dest_content == expected

    def test_cut_within_same_file_move_down(self, agent, temp_repo):
        """Move lines down within the same file."""
        create_file(temp_repo, "file.lean", "line1\nline2\nline3\nline4\nline5")

        # Move lines 2-3 to after line 5
        result = agent._handle_file_cut_paste(
            {
                "src_path": "file.lean",
                "src_start_line": 2,
                "src_end_line": 3,
                "dest_path": "file.lean",
                "dest_line": 6,  # After original line 5
            }
        )

        assert "Moved lines" in result

        content = (temp_repo / "file.lean").read_text()
        expected = "line1\nline4\nline5\nline2\nline3"
        assert content == expected

    def test_cut_within_same_file_move_up(self, agent, temp_repo):
        """Move lines up within the same file."""
        create_file(temp_repo, "file.lean", "line1\nline2\nline3\nline4\nline5")

        # Move lines 4-5 to before line 2
        result = agent._handle_file_cut_paste(
            {
                "src_path": "file.lean",
                "src_start_line": 4,
                "src_end_line": 5,
                "dest_path": "file.lean",
                "dest_line": 2,
            }
        )

        assert "Moved lines" in result

        content = (temp_repo / "file.lean").read_text()
        expected = "line1\nline4\nline5\nline2\nline3"
        assert content == expected

    def test_cut_within_same_file_move_to_beginning(self, agent, temp_repo):
        """Move lines to the very beginning of the same file."""
        create_file(temp_repo, "file.lean", "line1\nline2\nline3\nline4")

        # Move lines 3-4 to beginning
        result = agent._handle_file_cut_paste(
            {
                "src_path": "file.lean",
                "src_start_line": 3,
                "src_end_line": 4,
                "dest_path": "file.lean",
                "dest_line": 1,
            }
        )

        assert "Moved lines" in result

        content = (temp_repo / "file.lean").read_text()
        expected = "line3\nline4\nline1\nline2"
        assert content == expected

    def test_cut_single_line(self, agent, temp_repo):
        """Cut a single line (start == end)."""
        create_file(temp_repo, "src.lean", "line1\nline2\nline3")
        create_file(temp_repo, "dest.lean", "destA\ndestB")

        result = agent._handle_file_cut_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 2,
                "src_end_line": 2,
                "dest_path": "dest.lean",
                "dest_line": 2,
            }
        )

        assert "Cut 1 lines" in result

        src_content = (temp_repo / "src.lean").read_text()
        assert src_content == "line1\nline3"

        dest_content = (temp_repo / "dest.lean").read_text()
        expected = "destA\nline2\ndestB"
        assert dest_content == expected

    def test_cut_all_lines(self, agent, temp_repo):
        """Cut all lines from a file."""
        create_file(temp_repo, "src.lean", "line1\nline2\nline3")
        create_file(temp_repo, "dest.lean", "destA")

        result = agent._handle_file_cut_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 3,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Cut 3 lines" in result

        src_content = (temp_repo / "src.lean").read_text()
        assert src_content == ""

        dest_content = (temp_repo / "dest.lean").read_text()
        expected = "line1\nline2\nline3\ndestA"
        assert dest_content == expected

    def test_cut_error_invalid_range(self, agent, temp_repo):
        """Error when start_line > end_line."""
        create_file(temp_repo, "src.lean", "line1\nline2\nline3")
        create_file(temp_repo, "dest.lean", "destA")

        result = agent._handle_file_cut_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 3,
                "src_end_line": 1,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Error" in result
        assert "src_start_line (3) > src_end_line (1)" in result

    def test_cut_error_line_exceeds_file(self, agent, temp_repo):
        """Error when end_line exceeds file length."""
        create_file(temp_repo, "src.lean", "line1\nline2\nline3")
        create_file(temp_repo, "dest.lean", "destA")

        result = agent._handle_file_cut_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 10,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Error" in result
        assert "exceeds source file length" in result

    def test_cut_error_source_not_found(self, agent, temp_repo):
        """Error when source file doesn't exist."""
        create_file(temp_repo, "dest.lean", "destA")

        result = agent._handle_file_cut_paste(
            {
                "src_path": "nonexistent.lean",
                "src_start_line": 1,
                "src_end_line": 1,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Error" in result
        assert "Source file not found" in result

    def test_cut_error_dest_not_found(self, agent, temp_repo):
        """Error when destination file doesn't exist."""
        create_file(temp_repo, "src.lean", "line1")

        result = agent._handle_file_cut_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 1,
                "dest_path": "nonexistent.lean",
                "dest_line": 1,
            }
        )

        assert "Error" in result
        assert "Destination file not found" in result


class TestInclusiveRanges:
    """Tests specifically for inclusive range behavior."""

    def test_copy_inclusive_range_verification(self, agent, temp_repo):
        """Verify that line ranges are inclusive on both ends."""
        # File with 10 lines
        create_file(temp_repo, "src.lean", "\n".join(f"line{i}" for i in range(1, 11)))
        create_file(temp_repo, "dest.lean", "")

        # Copy lines 3-7 (should be 5 lines: 3, 4, 5, 6, 7)
        result = agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 3,
                "src_end_line": 7,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Copied 5 lines" in result

        dest_content = (temp_repo / "dest.lean").read_text()
        assert dest_content == "line3\nline4\nline5\nline6\nline7"

    def test_cut_inclusive_range_verification(self, agent, temp_repo):
        """Verify that line ranges are inclusive on both ends for cut."""
        # File with 10 lines
        create_file(temp_repo, "src.lean", "\n".join(f"line{i}" for i in range(1, 11)))
        create_file(temp_repo, "dest.lean", "target")

        # Cut lines 3-7 (should be 5 lines: 3, 4, 5, 6, 7)
        result = agent._handle_file_cut_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 3,
                "src_end_line": 7,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Cut 5 lines" in result

        # Source should have lines 1, 2, 8, 9, 10
        src_content = (temp_repo / "src.lean").read_text()
        assert src_content == "line1\nline2\nline8\nline9\nline10"


class TestInsertionBehavior:
    """Tests specifically for insertion line behavior."""

    def test_dest_line_1_inserts_at_beginning(self, agent, temp_repo):
        """dest_line=1 should insert BEFORE line 1 (at very beginning)."""
        create_file(temp_repo, "src.lean", "inserted")
        create_file(temp_repo, "dest.lean", "first\nsecond\nthird")

        agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 1,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        dest_content = (temp_repo / "dest.lean").read_text()
        assert dest_content == "inserted\nfirst\nsecond\nthird"

    def test_dest_line_2_inserts_before_line_2(self, agent, temp_repo):
        """dest_line=2 should insert BEFORE line 2 (after line 1)."""
        create_file(temp_repo, "src.lean", "inserted")
        create_file(temp_repo, "dest.lean", "first\nsecond\nthird")

        agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 1,
                "dest_path": "dest.lean",
                "dest_line": 2,
            }
        )

        dest_content = (temp_repo / "dest.lean").read_text()
        assert dest_content == "first\ninserted\nsecond\nthird"

    def test_dest_line_beyond_file_appends(self, agent, temp_repo):
        """dest_line > file_length should append at end."""
        create_file(temp_repo, "src.lean", "inserted")
        create_file(temp_repo, "dest.lean", "first\nsecond\nthird")

        agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 1,
                "dest_path": "dest.lean",
                "dest_line": 100,  # Way beyond 3 lines
            }
        )

        dest_content = (temp_repo / "dest.lean").read_text()
        assert dest_content == "first\nsecond\nthird\ninserted"

    def test_dest_line_equals_last_line_plus_one(self, agent, temp_repo):
        """dest_line = N+1 (where N is last line) should append at end."""
        create_file(temp_repo, "src.lean", "inserted")
        create_file(temp_repo, "dest.lean", "first\nsecond\nthird")  # 3 lines

        agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 1,
                "dest_path": "dest.lean",
                "dest_line": 4,  # N+1 = 4
            }
        )

        dest_content = (temp_repo / "dest.lean").read_text()
        assert dest_content == "first\nsecond\nthird\ninserted"


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_destination_file(self, agent, temp_repo):
        """Copy into an empty file."""
        create_file(temp_repo, "src.lean", "line1\nline2")
        create_file(temp_repo, "dest.lean", "")

        result = agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 2,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Error" not in result
        dest_content = (temp_repo / "dest.lean").read_text()
        assert dest_content == "line1\nline2"

    def test_single_line_file(self, agent, temp_repo):
        """Operations on single-line files."""
        create_file(temp_repo, "src.lean", "only-line")
        create_file(temp_repo, "dest.lean", "dest-line")

        result = agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 1,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Error" not in result
        dest_content = (temp_repo / "dest.lean").read_text()
        assert dest_content == "only-line\ndest-line"

    def test_start_line_zero_error(self, agent, temp_repo):
        """Error when start_line is 0 (lines are 1-indexed)."""
        create_file(temp_repo, "src.lean", "line1\nline2")
        create_file(temp_repo, "dest.lean", "dest")

        result = agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 0,
                "src_end_line": 1,
                "dest_path": "dest.lean",
                "dest_line": 1,
            }
        )

        assert "Error" in result
        assert "must be >= 1" in result

    def test_files_in_subdirectories(self, agent, temp_repo):
        """Operations with files in subdirectories."""
        create_file(temp_repo, "subdir1/src.lean", "line1\nline2")
        create_file(temp_repo, "subdir2/dest.lean", "destA")

        result = agent._handle_file_copy_paste(
            {
                "src_path": "subdir1/src.lean",
                "src_start_line": 1,
                "src_end_line": 2,
                "dest_path": "subdir2/dest.lean",
                "dest_line": 1,
            }
        )

        assert "Error" not in result
        dest_content = (temp_repo / "subdir2/dest.lean").read_text()
        assert dest_content == "line1\nline2\ndestA"

    def test_preserves_trailing_newline(self, agent, temp_repo):
        """Test that we handle files with/without trailing newlines correctly."""
        create_file(temp_repo, "src.lean", "line1\nline2")  # No trailing newline
        create_file(temp_repo, "dest.lean", "destA\ndestB")

        agent._handle_file_copy_paste(
            {
                "src_path": "src.lean",
                "src_start_line": 1,
                "src_end_line": 1,
                "dest_path": "dest.lean",
                "dest_line": 2,
            }
        )

        dest_content = (temp_repo / "dest.lean").read_text()
        expected = "destA\nline1\ndestB"
        assert dest_content == expected
