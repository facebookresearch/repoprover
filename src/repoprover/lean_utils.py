# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Lean 4 code utilities.

Shared utilities for working with Lean 4 source code:
- Comment stripping (with position preservation or simple removal)
- Declaration parsing (theorem/lemma detection)
- Keyword detection (sorry, axiom)
- Diff statistics parsing
"""

from __future__ import annotations

import re

# =============================================================================
# Declaration Parsing Regexes
# =============================================================================

_DECL_KEYWORD_ALT = "|".join(
    re.escape(k)
    for k in [
        "theorem",
        "lemma",
        "def",
        "example",
        "structure",
        "inductive",
        "class",
        "instance",
        "abbrev",
    ]
)

_MODIFIER_ALT = "|".join(
    re.escape(m)
    for m in [
        "private",
        "protected",
        "noncomputable",
        "unsafe",
        "partial",
        "nonrec",
    ]
)

DECL_HEADER_RE = re.compile(
    rf"""
    ^[ \t]*                                      # indentation
    (?:set_option\s+\S+\s+\S+\s+in\s*)*         # set_option* prefix
    (?:@\[[^\]]*\]\s*)*                          # @[...] attributes
    (?:(?:{_MODIFIER_ALT})\b\s*)*                # modifiers
    (?P<kw>{_DECL_KEYWORD_ALT})\b                # declaration keyword
    """,
    re.MULTILINE | re.VERBOSE,
)

THEOREM_NAME_RE = re.compile(r"\b(?:theorem|lemma)\s+(\S+)")


# =============================================================================
# Comment Stripping
# =============================================================================


def strip_comments(code: str, preserve_positions: bool = False, strip_docstrings: bool = True) -> str:
    """Strip comments from Lean code.

    Args:
        code: Lean source code
        preserve_positions: If True, replace comments with spaces to preserve
            line/column positions. If False, remove comments entirely.
        strip_docstrings: If True (default), also strip docstrings (/-- ... -/).

    Returns:
        Code with comments removed/blanked.
    """
    if preserve_positions:
        return _strip_comments_preserve_positions(code, strip_docstrings=strip_docstrings)
    else:
        return _strip_comments_simple(code, strip_docstrings=strip_docstrings)


def _strip_comments_preserve_positions(code: str, strip_docstrings: bool = False) -> str:
    """Strip comments, replacing with spaces to preserve positions.

    Args:
        strip_docstrings: If True, also strip docstrings (/-- ... -/).
    """
    result = list(code)

    # Strip block comments, handling nesting
    # If strip_docstrings is True, also strip docstrings (/-- ... -/)
    i = 0
    while i < len(result) - 1:
        if result[i] == "/" and result[i + 1] == "-":
            is_docstring = i + 2 < len(result) and result[i + 2] == "-"

            if is_docstring and not strip_docstrings:
                # Docstring /-- ... -/, skip it (preserve)
                j = i + 3
                while j < len(result) - 1:
                    if result[j] == "-" and result[j + 1] == "/":
                        i = j + 2
                        break
                    j += 1
                else:
                    i = len(result)
                continue
            else:
                # Block comment /- ... -/ or docstring (if stripping), blank it out
                depth = 1
                start = i
                j = i + 2
                if is_docstring:
                    j = i + 3  # Skip past /--
                while j < len(result) - 1 and depth > 0:
                    if result[j] == "/" and result[j + 1] == "-":
                        depth += 1
                        j += 2
                    elif result[j] == "-" and result[j + 1] == "/":
                        depth -= 1
                        j += 2
                    else:
                        j += 1
                for k in range(start, j):
                    if result[k] != "\n":
                        result[k] = " "
                i = j
                continue
        i += 1

    code_without_blocks = "".join(result)

    # Strip line comments (-- to end of line)
    def replace_line_comment(m: re.Match[str]) -> str:
        return " " * len(m.group(0))

    return re.sub(r"--[^\n]*", replace_line_comment, code_without_blocks)


def _strip_comments_simple(code: str, strip_docstrings: bool = False) -> str:
    """Strip comments, removing them entirely (no position preservation).

    Args:
        strip_docstrings: If True, also strip docstrings (/-- ... -/).
    """
    result = []
    i = 0
    n = len(code)

    while i < n:
        if i + 1 < n and code[i : i + 2] == "/-":
            # Check if docstring
            is_docstring = i + 2 < n and code[i + 2] == "-"

            if is_docstring and not strip_docstrings:
                # Docstring /-- ... -/, preserve it
                j = i + 3
                while j < n - 1:
                    if code[j : j + 2] == "-/":
                        result.append(code[i : j + 2])
                        i = j + 2
                        break
                    j += 1
                else:
                    # Unterminated docstring, append rest
                    result.append(code[i:])
                    i = n
                continue
            else:
                # Block comment /- ... -/ or docstring (if stripping), skip it
                depth = 1
                i += 2
                if is_docstring:
                    i += 1  # Skip past /--
                while i < n and depth > 0:
                    if i + 1 < n and code[i : i + 2] == "/-":
                        depth += 1
                        i += 2
                    elif i + 1 < n and code[i : i + 2] == "-/":
                        depth -= 1
                        i += 2
                    else:
                        i += 1
        elif i + 1 < n and code[i : i + 2] == "--":
            # Line comment, skip to end of line
            while i < n and code[i] != "\n":
                i += 1
        else:
            result.append(code[i])
            i += 1

    return "".join(result)


# =============================================================================
# Keyword Detection
# =============================================================================


def has_sorry_or_axiom(code: str) -> tuple[bool, bool]:
    """Check if code contains 'sorry' or 'axiom' keywords (not in comments).

    Args:
        code: Lean source code

    Returns:
        (has_sorry, has_axiom) tuple
    """
    stripped = strip_comments(code, preserve_positions=False)
    has_sorry = bool(re.search(r"\bsorry\b", stripped, re.IGNORECASE))
    has_axiom = bool(re.search(r"\baxiom\b", stripped, re.IGNORECASE))
    return has_sorry, has_axiom


# =============================================================================
# Diff Statistics
# =============================================================================


def parse_diff_stats(diff: str) -> tuple[int, int]:
    """Parse diff statistics (additions and deletions).

    Args:
        diff: Git diff output

    Returns:
        (additions, deletions) tuple
    """
    lines = diff.split("\n")
    additions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return additions, deletions
