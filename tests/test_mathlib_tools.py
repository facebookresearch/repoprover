# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Tests for the standalone mathlib_tools module.

Tests search functions, tool definitions and mixin formatting without
requiring Mathlib or ripgrep to be installed.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from repoprover.agents.mathlib_tools import (
    LEAN_KEYWORDS,
    MATHLIB_TOOL_NAMES,
    MATHLIB_TOOLS,
    MathlibToolsMixin,
    _find_mathlib_path,
    _run_grep_fallback,
    _run_ripgrep,
    mathlib_find_name,
    mathlib_grep,
    mathlib_read_file,
)


# =============================================================================
# _find_mathlib_path
# =============================================================================


class TestFindMathlibPath:
    def test_raises_when_not_found(self):
        with pytest.raises(RuntimeError, match="Could not find Mathlib"):
            _find_mathlib_path("/nonexistent/path")

    def test_finds_mathlib(self, tmp_path):
        """Create a fake mathlib directory structure and find it."""
        mathlib_dir = tmp_path / ".lake" / "packages" / "mathlib" / "Mathlib"
        mathlib_dir.mkdir(parents=True)
        result = _find_mathlib_path(str(tmp_path))
        assert result == tmp_path / ".lake" / "packages" / "mathlib"


# =============================================================================
# mathlib_grep
# =============================================================================


class TestMathlibGrep:
    def test_error_when_no_mathlib(self):
        result = mathlib_grep("foo", workspace="/nonexistent")
        assert "error" in result
        assert result["count"] == 0

    def test_kind_pattern_prepends_keyword(self):
        """Verify that kind filter prepends the Lean keyword regex."""
        assert "theorem" in LEAN_KEYWORDS
        assert LEAN_KEYWORDS["theorem"].startswith(r"\b")

    @patch("repoprover.agents.mathlib_tools._run_ripgrep")
    @patch("repoprover.agents.mathlib_tools._find_mathlib_path")
    def test_returns_matches(self, mock_find, mock_rg, tmp_path):
        mock_find.return_value = tmp_path
        mock_rg.return_value = [
            {"file": "Mathlib/Data/Nat.lean", "line": 42,
             "column": 0, "match": "theorem", "text": "theorem foo"},
            {"file": "Mathlib/Data/Nat.lean", "line": 50,
             "column": 0, "match": "theorem", "text": "theorem bar"},
        ]
        result = mathlib_grep("foo", workspace=str(tmp_path))
        assert result["count"] == 2
        assert not result["truncated"]

    @patch("repoprover.agents.mathlib_tools._run_ripgrep")
    @patch("repoprover.agents.mathlib_tools._find_mathlib_path")
    def test_truncation(self, mock_find, mock_rg, tmp_path):
        mock_find.return_value = tmp_path
        mock_rg.return_value = [
            {"file": f"f{i}.lean", "line": i, "column": 0,
             "match": "x", "text": f"line {i}"}
            for i in range(100)
        ]
        result = mathlib_grep("x", max_results=10, workspace=str(tmp_path))
        assert result["count"] == 10
        assert result["truncated"]


# =============================================================================
# mathlib_find_name
# =============================================================================


class TestMathlibFindName:
    def test_error_when_no_mathlib(self):
        result = mathlib_find_name("foo", workspace="/nonexistent")
        assert "error" in result

    @patch("repoprover.agents.mathlib_tools._run_ripgrep")
    @patch("repoprover.agents.mathlib_tools._find_mathlib_path")
    def test_exact_match(self, mock_find, mock_rg, tmp_path):
        mock_find.return_value = tmp_path
        mock_rg.return_value = [
            {"file": "Mathlib/Algebra.lean", "line": 10,
             "column": 0, "match": "theorem", "text": "theorem det_mul"},
        ]
        result = mathlib_find_name("det_mul", exact=True,
                                   workspace=str(tmp_path))
        assert result["count"] == 1
        assert result["exact"] is True


# =============================================================================
# mathlib_read_file
# =============================================================================


class TestMathlibReadFile:
    def test_error_when_no_mathlib(self):
        result = mathlib_read_file("Mathlib/foo.lean",
                                   workspace="/nonexistent")
        assert "error" in result

    def test_reads_file(self, tmp_path):
        """Create a fake mathlib file and read it."""
        mathlib_root = tmp_path / ".lake" / "packages" / "mathlib"
        mathlib_file = mathlib_root / "Mathlib" / "Test.lean"
        mathlib_file.parent.mkdir(parents=True)
        mathlib_file.write_text("line1\nline2\nline3\nline4\nline5\n")

        result = mathlib_read_file("Mathlib/Test.lean",
                                   workspace=str(tmp_path))
        assert result["total_lines"] == 5
        assert "line1" in result["content"]

    def test_reads_line_range(self, tmp_path):
        mathlib_root = tmp_path / ".lake" / "packages" / "mathlib"
        mathlib_file = mathlib_root / "Mathlib" / "Test.lean"
        mathlib_file.parent.mkdir(parents=True)
        mathlib_file.write_text("line1\nline2\nline3\nline4\nline5\n")

        result = mathlib_read_file("Mathlib/Test.lean",
                                   start_line=2, end_line=4,
                                   workspace=str(tmp_path))
        assert "line2" in result["content"]
        assert "line4" in result["content"]
        assert "line1" not in result["content"]
        assert "line5" not in result["content"]

    def test_rejects_non_lean(self, tmp_path):
        mathlib_root = tmp_path / ".lake" / "packages" / "mathlib"
        mathlib_file = mathlib_root / "Mathlib" / "Test.txt"
        mathlib_file.parent.mkdir(parents=True)
        mathlib_file.write_text("content")

        result = mathlib_read_file("Mathlib/Test.txt",
                                   workspace=str(tmp_path))
        assert "error" in result
        assert "Only .lean" in result["error"]

    def test_file_not_found(self, tmp_path):
        mathlib_root = tmp_path / ".lake" / "packages" / "mathlib"
        (mathlib_root / "Mathlib").mkdir(parents=True)

        result = mathlib_read_file("Mathlib/Missing.lean",
                                   workspace=str(tmp_path))
        assert "error" in result
        assert "not found" in result["error"]


# =============================================================================
# Tool definitions
# =============================================================================


class TestToolDefinitions:
    def test_tool_count(self):
        assert len(MATHLIB_TOOLS) == 3

    def test_tool_names(self):
        assert MATHLIB_TOOL_NAMES == {
            "mathlib_grep", "mathlib_find_name", "mathlib_read_file",
        }

    def test_tools_have_required_fields(self):
        for tool in MATHLIB_TOOLS:
            assert tool["type"] == "function"
            func = tool["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func
            assert "required" in func["parameters"]


# =============================================================================
# Grep fallback
# =============================================================================


class TestGrepFallback:
    def test_fallback_parses_grep_output(self, tmp_path):
        """Test grep fallback with a real file."""
        lean_file = tmp_path / "Test.lean"
        lean_file.write_text("theorem foo : True := trivial\nlemma bar : 1 = 1 := rfl\n")

        matches = _run_grep_fallback("theorem", tmp_path, case_insensitive=False)
        assert len(matches) >= 1
        assert any("theorem" in m["text"] for m in matches)
