# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Mathlib search and read tools for agents (standalone).

Provides tools for searching and reading Mathlib source code:
- mathlib_grep: Search Mathlib using ripgrep patterns
- mathlib_find_name: Find declarations by name
- mathlib_read_file: Read Mathlib source files

These tools are READ-ONLY and available to all agents (reviewers and contributors).
"""

from __future__ import annotations

import json
import re
import subprocess
from logging import getLogger
from pathlib import Path
from typing import Any

logger = getLogger(__name__)


# =============================================================================
# Mathlib search implementation
# =============================================================================

LEAN_KEYWORDS = {
    "theorem": r"\btheorem\s+",
    "lemma": r"\blemma\s+",
    "def": r"\bdef\s+",
    "abbrev": r"\babbrev\s+",
    "structure": r"\bstructure\s+",
    "class": r"\bclass\s+",
    "instance": r"\binstance\s+",
    "inductive": r"\binductive\s+",
}


def _find_mathlib_path(workspace: str | None = None) -> Path:
    """Find the Mathlib installation under .lake/packages/mathlib."""
    candidates = []
    if workspace:
        candidates.append(
            Path(workspace) / ".lake" / "packages" / "mathlib"
        )
    for p in candidates:
        if p.exists() and (p / "Mathlib").exists():
            return p
    raise RuntimeError(
        f"Could not find Mathlib. Checked: {candidates}. "
        "Pass the workspace parameter or ensure Mathlib is installed."
    )


def _run_ripgrep(
    pattern: str,
    path: Path,
    *,
    case_insensitive: bool = True,
    context_lines: int = 0,
    fixed_strings: bool = False,
    subdir: str | None = None,
) -> list[dict[str, Any]]:
    """Run ripgrep and return structured results."""
    search_path = path / subdir if subdir else path
    if not search_path.exists():
        return []

    cmd = ["rg", "--json"]
    if case_insensitive:
        cmd.append("-i")
    if context_lines > 0:
        cmd.extend(["-C", str(context_lines)])
    cmd.extend(["-g", "*.lean"])
    if fixed_strings:
        cmd.append("-F")
    cmd.extend(["--", pattern, str(search_path)])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ripgrep timed out for pattern: %s", pattern)
        return []
    except FileNotFoundError:
        return _run_grep_fallback(
            pattern, search_path, case_insensitive
        )

    matches: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "match":
            match_data = data["data"]
            file_path = match_data["path"]["text"]
            try:
                rel_path = str(Path(file_path).relative_to(path))
            except ValueError:
                rel_path = file_path
            for submatch in match_data.get("submatches", []):
                matches.append({
                    "file": rel_path,
                    "line": match_data["line_number"],
                    "column": submatch.get("start", 0),
                    "match": submatch.get("match", {}).get("text", ""),
                    "text": match_data["lines"]["text"].rstrip("\n"),
                })
    return matches


def _run_grep_fallback(
    pattern: str,
    path: Path,
    case_insensitive: bool = True,
) -> list[dict[str, Any]]:
    """Fallback to grep if ripgrep is not available."""
    cmd = ["grep", "-rn", "--include", "*.lean"]
    if case_insensitive:
        cmd.append("-i")
    cmd.extend([pattern, str(path)])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return []

    matches: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) >= 3:
            try:
                rel_path = str(Path(parts[0]).relative_to(path))
            except ValueError:
                rel_path = parts[0]
            matches.append({
                "file": rel_path,
                "line": int(parts[1]),
                "column": 0,
                "match": pattern,
                "text": parts[2].rstrip("\n"),
            })
    return matches


def mathlib_grep(
    pattern: str,
    kind: str | None = None,
    subdir: str | None = None,
    max_results: int = 50,
    context_lines: int = 0,
    case_sensitive: bool = False,
    literal: bool = False,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Search Mathlib source code using ripgrep."""
    try:
        mathlib_path = _find_mathlib_path(workspace)
    except RuntimeError as e:
        return {"error": str(e), "matches": [], "count": 0}

    if kind and kind in LEAN_KEYWORDS:
        full_pattern = LEAN_KEYWORDS[kind] + pattern
        literal = False
    else:
        full_pattern = pattern

    search_subdir = f"Mathlib/{subdir}" if subdir else "Mathlib"
    matches = _run_ripgrep(
        full_pattern,
        mathlib_path,
        case_insensitive=not case_sensitive,
        context_lines=context_lines,
        fixed_strings=literal,
        subdir=search_subdir,
    )

    truncated = len(matches) > max_results
    if truncated:
        matches = matches[:max_results]

    return {
        "matches": matches,
        "count": len(matches),
        "truncated": truncated,
        "search_path": str(mathlib_path / search_subdir),
        "pattern": full_pattern,
    }


def mathlib_find_name(
    name: str,
    exact: bool = False,
    max_results: int = 30,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Find a theorem, lemma, or definition by name in Mathlib."""
    try:
        mathlib_path = _find_mathlib_path(workspace)
    except RuntimeError as e:
        return {"error": str(e), "matches": [], "count": 0}

    all_kinds = "|".join([
        "theorem", "lemma", "def", "abbrev", "structure",
        "class", "instance", "inductive", "axiom", "opaque",
    ])
    if exact:
        pattern = rf"\b({all_kinds})\s+{re.escape(name)}\b"
    else:
        pattern = rf"\b({all_kinds})\s+\S*{re.escape(name)}\S*"

    matches = _run_ripgrep(
        pattern,
        mathlib_path,
        case_insensitive=False,
        subdir="Mathlib",
    )

    truncated = len(matches) > max_results
    if truncated:
        matches = matches[:max_results]

    return {
        "matches": matches,
        "count": len(matches),
        "truncated": truncated,
        "name": name,
        "exact": exact,
    }


def mathlib_read_file(
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Read a file from Mathlib source."""
    try:
        mathlib_path = _find_mathlib_path(workspace)
    except RuntimeError as e:
        return {"error": str(e), "content": ""}

    full_path = mathlib_path / file_path
    if not full_path.exists():
        return {"error": f"File not found: {file_path}", "content": ""}
    if full_path.suffix != ".lean":
        return {"error": "Only .lean files can be read", "content": ""}

    try:
        content = full_path.read_text()
        lines = content.splitlines()
        total_lines = len(lines)

        if start_line is not None or end_line is not None:
            start_idx = (start_line - 1) if start_line else 0
            end_idx = end_line if end_line else total_lines
            start_idx = max(0, start_idx)
            end_idx = min(total_lines, end_idx)
            content = "\n".join(lines[start_idx:end_idx])

        return {
            "content": content,
            "total_lines": total_lines,
            "path": str(full_path),
            "start_line": start_line,
            "end_line": end_line,
        }
    except Exception as e:
        return {"error": str(e), "content": ""}


# =============================================================================
# Tool Definitions
# =============================================================================

MATHLIB_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "mathlib_grep",
            "description": """Search Mathlib source code using ripgrep.

Find theorems, lemmas, definitions in Mathlib by pattern.

Args:
    pattern: Search pattern (regex by default). Examples: "Finset.sum", "Matrix.*det"
    kind: Filter by declaration kind: "theorem", "lemma", "def", "abbrev", "structure", "class", "instance", "inductive"
    subdir: Subdirectory to search: "Algebra", "Analysis", "Topology", "Data", "LinearAlgebra", "NumberTheory"
    max_results: Max results (default: 50)
    context_lines: Lines of context (default: 0)
    literal: If true, treat pattern as literal string

Returns list of matches with file, line, and text.""",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Search pattern (regex)",
                    },
                    "kind": {
                        "type": "string",
                        "description": "Declaration kind filter",
                        "enum": [
                            "theorem", "lemma", "def", "abbrev",
                            "structure", "class", "instance", "inductive",
                        ],
                    },
                    "subdir": {
                        "type": "string",
                        "description": "Mathlib subdirectory to search",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return",
                        "default": 50,
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Lines of context around matches",
                        "default": 0,
                    },
                    "literal": {
                        "type": "boolean",
                        "description": "Treat pattern as literal string",
                        "default": False,
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mathlib_find_name",
            "description": """Find a theorem, lemma, or definition by name in Mathlib.

Search for declarations matching a name pattern.

Args:
    name: Name to search for (e.g., "sum_add_distrib", "det_mul")
    exact: Match exact name only (default: false)
    max_results: Max results (default: 30)

Returns list of matching declarations with file and line.""",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name to search for",
                    },
                    "exact": {
                        "type": "boolean",
                        "description": "Match exact name only",
                        "default": False,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results",
                        "default": 30,
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mathlib_read_file",
            "description": """Read a Mathlib source file.

Args:
    file_path: Path relative to Mathlib (e.g., "Mathlib/LinearAlgebra/Matrix/Determinant.lean")
    start_line: Starting line (1-indexed, optional)
    end_line: Ending line (inclusive, optional)

Returns file content.""",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path relative to Mathlib root",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Starting line number",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Ending line number",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
]

MATHLIB_TOOL_NAMES = frozenset(
    tool["function"]["name"] for tool in MATHLIB_TOOLS
)


# =============================================================================
# Mixin
# =============================================================================


class MathlibToolsMixin:
    """Mixin providing Mathlib search tools to agents.

    Requires:
        self.repo_root: Path | None - for workspace resolution

    Optional:
        self.config.mathlib_grep: bool - if False, tools are not registered
    """

    repo_root: Path | None

    def register_tools(self, defs: dict, handlers: dict) -> None:
        """Register Mathlib tools (if config.mathlib_grep is True or unset)."""
        super().register_tools(defs, handlers)  # type: ignore[misc]
        config = getattr(self, "config", None)
        if config is not None and not getattr(config, "mathlib_grep", True):
            return
        self._register_tools_from_list(MATHLIB_TOOLS, defs, handlers)

    def _handle_mathlib_grep(self, args: dict) -> str:
        pattern = args.get("pattern", "")
        workspace = str(self.repo_root) if self.repo_root else None
        log_prefix = getattr(self, "log_prefix", "")
        logger.info(f"{log_prefix} mathlib_grep('{pattern}')")

        result = mathlib_grep(
            pattern=pattern,
            kind=args.get("kind"),
            subdir=args.get("subdir"),
            max_results=args.get("max_results", 50),
            context_lines=args.get("context_lines", 0),
            literal=args.get("literal", False),
            workspace=workspace,
        )

        if "error" in result:
            return f"Error: {result['error']}"

        lines = [f"Found {result['count']} matches"]
        if result.get("truncated"):
            lines[0] += " (truncated)"
        lines.append("")
        for match in result["matches"]:
            lines.append(f"**{match['file']}:{match['line']}**")
            lines.append(f"  {match['text']}")
            lines.append("")

        logger.info(
            f"{log_prefix} mathlib_grep: {result['count']} matches"
        )
        return "\n".join(lines)

    def _handle_mathlib_find_name(self, args: dict) -> str:
        name = args.get("name", "")
        workspace = str(self.repo_root) if self.repo_root else None
        log_prefix = getattr(self, "log_prefix", "")
        logger.info(f"{log_prefix} mathlib_find_name('{name}')")

        result = mathlib_find_name(
            name=name,
            exact=args.get("exact", False),
            max_results=args.get("max_results", 30),
            workspace=workspace,
        )

        if "error" in result:
            return f"Error: {result['error']}"

        lines = [f"Found {result['count']} matches for '{name}'"]
        if result.get("truncated"):
            lines[0] += " (truncated)"
        lines.append("")
        for match in result["matches"]:
            lines.append(f"**{match['file']}:{match['line']}**")
            lines.append(f"  {match['text']}")
            lines.append("")

        logger.info(
            f"{log_prefix} mathlib_find_name: {result['count']} matches"
        )
        return "\n".join(lines)

    def _handle_mathlib_read_file(self, args: dict) -> str:
        file_path = args.get("file_path", "")
        start_line = args.get("start_line")
        end_line = args.get("end_line")
        workspace = str(self.repo_root) if self.repo_root else None
        log_prefix = getattr(self, "log_prefix", "")
        logger.info(f"{log_prefix} mathlib_read_file('{file_path}')")

        result = mathlib_read_file(
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            workspace=workspace,
        )

        if "error" in result:
            return f"Error: {result['error']}"

        lines = [f"**{file_path}** ({result['total_lines']} lines)"]
        if start_line or end_line:
            lines[0] += (
                f" [lines {start_line or 1}"
                f"-{end_line or result['total_lines']}]"
            )
        lines.append("")
        lines.append("```lean")
        lines.append(result["content"])
        lines.append("```")

        logger.info(
            f"{log_prefix} mathlib_read_file: "
            f"{result['total_lines']} lines"
        )
        return "\n".join(lines)
