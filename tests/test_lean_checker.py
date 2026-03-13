# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Tests for the standalone lean_checker module.

Tests response parsing, outcome classification, header splitting and
CheckResult formatting without requiring a Lean installation.
"""

import pytest

from repoprover.lean_checker import (
    CheckResult,
    CommandResponse,
    LeanCheckerConfig,
    LeanMessage,
    MessageSeverity,
    Pos,
    ReplOutcome,
    SorryInfo,
    TacticInfo,
    _parse_repl_response,
    _parse_repl_response_outcome,
    _split_imports_and_body,
)


# =============================================================================
# Header splitting
# =============================================================================


class TestSplitImportsAndBody:
    def test_simple_split(self):
        code = "import Mathlib\nimport Aesop\n\ntheorem foo : True := trivial"
        header, body = _split_imports_and_body(code)
        assert "import Aesop" in header
        assert "import Mathlib" in header
        assert "theorem foo" in body

    def test_no_imports(self):
        code = "theorem foo : True := trivial"
        header, body = _split_imports_and_body(code)
        assert header == ""
        assert "theorem foo" in body

    def test_comments_between_imports(self):
        code = "import Mathlib\n-- a comment\nimport Aesop\n\ndef x := 1"
        header, body = _split_imports_and_body(code)
        assert "import Mathlib" in header
        assert "import Aesop" in header
        assert "def x" in body

    def test_deduplicates_imports(self):
        code = "import Mathlib\nimport Mathlib\n\ndef x := 1"
        header, body = _split_imports_and_body(code)
        assert header.count("import Mathlib") == 1

    def test_empty_code(self):
        header, body = _split_imports_and_body("")
        assert header == ""
        assert body == ""


# =============================================================================
# Response parsing
# =============================================================================


class TestPos:
    def test_from_dict(self):
        p = Pos.from_dict({"line": 5, "column": 10})
        assert p.line == 5
        assert p.column == 10

    def test_from_none(self):
        p = Pos.from_dict(None)
        assert p.line == 0
        assert p.column == 0


class TestLeanMessage:
    def test_from_dict(self):
        msg = LeanMessage.from_dict({
            "severity": "error",
            "pos": {"line": 3, "column": 0},
            "endPos": {"line": 3, "column": 5},
            "data": "unknown identifier 'foo'",
        })
        assert msg.severity == MessageSeverity.ERROR
        assert msg.pos.line == 3
        assert msg.data == "unknown identifier 'foo'"

    def test_from_dict_no_endpos(self):
        msg = LeanMessage.from_dict({
            "severity": "warning",
            "pos": {"line": 1, "column": 0},
            "endPos": None,
            "data": "unused variable",
        })
        assert msg.endPos is None
        assert msg.severity == MessageSeverity.WARNING


class TestSorryInfo:
    def test_from_dict(self):
        sorry = SorryInfo.from_dict({
            "pos": {"line": 10, "column": 2},
            "endPos": {"line": 10, "column": 7},
            "goal": "⊢ True",
            "proofState": 0,
        })
        assert sorry.pos.line == 10
        assert sorry.goal == "⊢ True"


class TestTacticInfo:
    def test_from_dict(self):
        tactic = TacticInfo.from_dict({
            "pos": {"line": 5, "column": 2},
            "endPos": {"line": 5, "column": 5},
            "goals": "⊢ True",
            "tactic": "trivial",
        })
        assert tactic.tactic == "trivial"
        assert tactic.goals == "⊢ True"
        assert tactic.proofState is None

    def test_from_dict_with_proof_state(self):
        tactic = TacticInfo.from_dict({
            "pos": {"line": 5, "column": 2},
            "endPos": {"line": 5, "column": 5},
            "goals": "",
            "tactic": "rfl",
            "proofState": 1,
        })
        assert tactic.proofState == 1


class TestCommandResponse:
    def test_from_dict_success(self):
        resp = CommandResponse.from_dict({
            "env": 2,
            "messages": [],
            "sorries": [],
        })
        assert resp.env == 2
        assert resp.messages == []
        assert resp.sorries == []

    def test_from_dict_with_messages(self):
        resp = CommandResponse.from_dict({
            "env": 1,
            "messages": [
                {
                    "severity": "error",
                    "pos": {"line": 1, "column": 0},
                    "endPos": None,
                    "data": "error msg",
                }
            ],
            "sorries": [],
        })
        assert len(resp.messages) == 1
        assert resp.messages[0].severity == MessageSeverity.ERROR

    def test_from_dict_empty(self):
        resp = CommandResponse.from_dict({})
        assert resp.env is None
        assert resp.messages == []


# =============================================================================
# Outcome classification
# =============================================================================


class TestReplOutcome:
    def test_success(self):
        outcome = _parse_repl_response_outcome({"env": 1})
        assert outcome == ReplOutcome.SUCCESS

    def test_success_with_feedback(self):
        outcome = _parse_repl_response_outcome({
            "env": 1,
            "messages": [
                {
                    "severity": "info",
                    "pos": {"line": 1, "column": 0},
                    "endPos": None,
                    "data": "#check output",
                }
            ],
        })
        assert outcome == ReplOutcome.SUCCESS_WITH_FEEDBACK

    def test_error(self):
        outcome = _parse_repl_response_outcome({
            "env": 1,
            "messages": [
                {
                    "severity": "error",
                    "pos": {"line": 1, "column": 0},
                    "endPos": None,
                    "data": "type mismatch",
                }
            ],
        })
        assert outcome == ReplOutcome.ERROR

    def test_has_sorry(self):
        outcome = _parse_repl_response_outcome({
            "env": 1,
            "sorries": [
                {
                    "pos": {"line": 3, "column": 2},
                    "endPos": {"line": 3, "column": 7},
                    "goal": "⊢ False",
                    "proofState": 0,
                }
            ],
        })
        assert outcome == ReplOutcome.HAS_SORRY

    def test_repl_error(self):
        outcome = _parse_repl_response_outcome({
            "repl_error": "timeout"
        })
        assert outcome == ReplOutcome.REPL_ERROR


# =============================================================================
# CheckResult
# =============================================================================


class TestCheckResult:
    def test_no_errors(self):
        result = CheckResult(
            success=True,
            outcome=ReplOutcome.SUCCESS,
        )
        assert not result.has_errors
        assert not result.has_sorries
        assert "No errors" in result.format_errors()

    def test_with_errors(self):
        result = CheckResult(
            success=False,
            outcome=ReplOutcome.ERROR,
            errors=["Line 3:0: unknown identifier 'foo'"],
        )
        assert result.has_errors
        assert "foo" in result.format_errors()

    def test_with_sorries(self):
        result = CheckResult(
            success=True,
            outcome=ReplOutcome.HAS_SORRY,
            sorries=[{"line": 5, "goal": "⊢ True"}],
        )
        assert result.has_sorries
        assert "Line 5" in result.format_sorries()

    def test_format_for_agent_success(self):
        result = CheckResult(success=True, outcome=ReplOutcome.SUCCESS)
        text = result.format_for_agent()
        assert "Compiles successfully" in text

    def test_format_for_agent_errors(self):
        result = CheckResult(
            success=False,
            outcome=ReplOutcome.ERROR,
            errors=["Line 1:0: error"],
            warnings=["Line 2:0: warning"],
        )
        text = result.format_for_agent()
        assert "Compilation Errors" in text
        assert "Warnings" in text

    def test_format_for_agent_truncates_warnings(self):
        result = CheckResult(
            success=True,
            outcome=ReplOutcome.SUCCESS,
            warnings=[f"warning {i}" for i in range(10)],
        )
        text = result.format_for_agent()
        assert "5 more warnings" in text


# =============================================================================
# LeanCheckerConfig
# =============================================================================


class TestLeanCheckerConfig:
    def test_defaults(self):
        config = LeanCheckerConfig()
        assert config.timeout == 120.0
        assert config.header_timeout == 180.0
        assert config.pool_size == 0
        assert config.workspace  # should have a default

    def test_custom_workspace(self):
        config = LeanCheckerConfig(workspace="/my/project")
        assert config.workspace == "/my/project"
