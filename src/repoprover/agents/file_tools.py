# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""File manipulation tools for agents working on disk.

Tool sets:
- FILE_READ_TOOLS: Read-only tools (file_read, file_list, file_grep)
- FILE_WRITE_TOOLS: Write tools (file_write, file_edit, file_edit_lines)
- FILE_TOOLS: All file tools (FILE_READ_TOOLS + FILE_WRITE_TOOLS)

Reviewers only get FILE_READ_TOOLS. Contributors get all FILE_TOOLS.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..git_worktree import WorktreeManager


# === Read-Only Tool Definitions ===

FILE_READ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": """Read a file from the worktree.

Returns file content with line numbers. Use start_line/limit for large files.
Default returns first 200 lines.

Examples:
  file_read(path="MyBook/Chapter1.lean")  # lines 1-200
  file_read(path="MyBook/Chapter1.lean", start_line=100, limit=50)  # lines 100-149
""",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path within the repository",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Start line (1-indexed, default: 1)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to return (default: 200)",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_inspect_lines",
            "description": """Inspect exact whitespace/indentation for specific lines.

Returns detailed info about each line including:
- Exact indentation (spaces/tabs count)
- Line content with visible whitespace markers
- Raw repr() of the line for debugging

Use BEFORE file_edit to verify exact whitespace when matches fail.

Example:
  file_inspect_lines(path="MyBook/Chapter1.lean", start_line=100, end_line=105)
""",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path within the repository",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Start line (1-indexed)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "End line (1-indexed, inclusive)",
                    },
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_list",
            "description": """List files in a directory.

Example:
  file_list(path="MyBook")
  file_list(path=".")  # Repository root
""",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path (default: repository root)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_grep",
            "description": """Search for a pattern in a file.

Returns matching lines with context. Uses regex matching.

Example:
  file_grep(path="MyBook/Chapter1.lean", pattern="sorry", context_lines=2)
""",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to search",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Lines of context before/after matches (default: 0)",
                    },
                },
                "required": ["path", "pattern"],
            },
        },
    },
]


# === Write Tool Definitions ===

FILE_WRITE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": """Write entire content to a file.

Creates parent directories if needed. Overwrites existing content.
Use file_edit for targeted changes to existing files.

Example:
  file_write(path="MyBook/Basics.lean", content="import Mathlib\\n\\nnamespace MyBook...")
""",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path within the repository",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete file content",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_delete",
            "description": """Delete a file from the repository.

Use when restructuring the codebase (e.g., splitting a file, removing obsolete code).

Example:
  file_delete(path="MyBook/OldChapter.lean")
""",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the file to delete",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_edit",
            "description": """Replace exact text in a file.

The old_string must appear exactly once. Use for surgical edits.

Example:
  file_edit(
    path="MyBook/Chapter1.lean",
    old_string="theorem foo : True := by sorry",
    new_string="theorem foo : True := trivial"
  )
""",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_edit_lines",
            "description": """Replace a range of lines in a file.

Replaces lines start_line through end_line (inclusive, 1-indexed).

Example:
  file_edit_lines(
    path="MyBook/Chapter1.lean",
    start_line=10,
    end_line=15,
    new_content="-- New content replacing lines 10-15"
  )
""",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "new_content": {"type": "string"},
                },
                "required": ["path", "start_line", "end_line", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_cut_paste",
            "description": """Cut lines from source file and insert them into destination file.

Removes lines from src_path and inserts them into dest_path.
This is like Ctrl-X on source, then Ctrl-V on destination.

Line ranges are INCLUSIVE and 1-indexed:
  - src_start_line=10, src_end_line=15 cuts lines 10, 11, 12, 13, 14, 15

Insertion behavior:
  - dest_line specifies the line number BEFORE which to insert
  - dest_line=1 inserts at the very beginning
  - dest_line=N where N > total lines appends at the end

Can operate on the same file (move lines within a file).

Example:
  file_cut_paste(
    src_path="MyBook/Chapter1.lean",
    src_start_line=10,
    src_end_line=15,
    dest_path="MyBook/Chapter2.lean",
    dest_line=5
  )
  # Removes lines 10-15 from Chapter1.lean and inserts them before line 5 in Chapter2.lean
""",
            "parameters": {
                "type": "object",
                "properties": {
                    "src_path": {
                        "type": "string",
                        "description": "Source file path (file to cut from)",
                    },
                    "src_start_line": {
                        "type": "integer",
                        "description": "First line to cut (1-indexed, inclusive)",
                    },
                    "src_end_line": {
                        "type": "integer",
                        "description": "Last line to cut (1-indexed, inclusive)",
                    },
                    "dest_path": {
                        "type": "string",
                        "description": "Destination file path (file to paste into)",
                    },
                    "dest_line": {
                        "type": "integer",
                        "description": "Line number BEFORE which to insert (1-indexed)",
                    },
                },
                "required": ["src_path", "src_start_line", "src_end_line", "dest_path", "dest_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_copy_paste",
            "description": """Copy lines from source file and insert them into destination file.

Copies lines from src_path (without removing them) and inserts them into dest_path.
This is like Ctrl-C on source, then Ctrl-V on destination.

Line ranges are INCLUSIVE and 1-indexed:
  - src_start_line=10, src_end_line=15 copies lines 10, 11, 12, 13, 14, 15

Insertion behavior:
  - dest_line specifies the line number BEFORE which to insert
  - dest_line=1 inserts at the very beginning
  - dest_line=N where N > total lines appends at the end

Can operate on the same file (duplicate lines within a file).

Example:
  file_copy_paste(
    src_path="MyBook/Chapter1.lean",
    src_start_line=10,
    src_end_line=15,
    dest_path="MyBook/Chapter2.lean",
    dest_line=5
  )
  # Copies lines 10-15 from Chapter1.lean and inserts them before line 5 in Chapter2.lean
""",
            "parameters": {
                "type": "object",
                "properties": {
                    "src_path": {
                        "type": "string",
                        "description": "Source file path (file to copy from)",
                    },
                    "src_start_line": {
                        "type": "integer",
                        "description": "First line to copy (1-indexed, inclusive)",
                    },
                    "src_end_line": {
                        "type": "integer",
                        "description": "Last line to copy (1-indexed, inclusive)",
                    },
                    "dest_path": {
                        "type": "string",
                        "description": "Destination file path (file to paste into)",
                    },
                    "dest_line": {
                        "type": "integer",
                        "description": "Line number BEFORE which to insert (1-indexed)",
                    },
                },
                "required": ["src_path", "src_start_line", "src_end_line", "dest_path", "dest_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_issues",
            "description": """List issues from the issues/ folder with summaries.

By default shows only OPEN issues with truncated descriptions (first 100 chars).
Use to quickly see what issues exist before deciding which to read in full.

Args:
  status: Filter by status ("open", "closed", "all"). Default: "open"
  max_desc_len: Max chars of description to show. Default: 100. Use 0 for full.

Returns:
  issue_id | status | description (truncated)

Example:
  list_issues()  # open issues, 100 char descriptions
  list_issues(status="all", max_desc_len=200)  # all issues, 200 chars
  list_issues(status="closed")  # closed issues only

To read a full issue: file_read(path="issues/<issue_id>.yaml")
""",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["open", "closed", "all"],
                        "description": "Filter by status (default: open)",
                    },
                    "max_desc_len": {
                        "type": "integer",
                        "description": "Max description chars (default: 100, 0=full)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_issue",
            "description": """Create a new issue with a random UUID filename.

Use when you discover a problem that can't be fixed in this PR.
Returns the issue ID (8-char hex).

Example:
  create_issue(description="Missing API for foo", origin="scan")
""",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What the issue is about"},
                    "origin": {"type": "string", "description": "Where this came from: scan, blocked, refactor, etc."},
                },
                "required": ["description"],
            },
        },
    },
]


# === Combined Tool Set (for backward compatibility) ===

FILE_TOOLS = FILE_READ_TOOLS + FILE_WRITE_TOOLS

FILE_READ_TOOL_NAMES = frozenset(tool["function"]["name"] for tool in FILE_READ_TOOLS)
FILE_WRITE_TOOL_NAMES = frozenset(tool["function"]["name"] for tool in FILE_WRITE_TOOLS)
FILE_TOOL_NAMES = FILE_READ_TOOL_NAMES | FILE_WRITE_TOOL_NAMES


# Extensions that are read-only (source files)
READ_ONLY_EXTENSIONS = frozenset({".tex", ".pdf", ".txt"})


# =============================================================================
# Read-Only Mixin (for reviewers)
# =============================================================================


class FileReadToolsMixin:
    """Mixin providing READ-ONLY file tools to agents (for reviewers).

    Requires self.worktree_manager: WorktreeManager or self.repo_root: Path.

    Only provides: file_read, file_list, file_grep
    """

    worktree_manager: "WorktreeManager | None"
    repo_root: Path | None

    def register_tools(self, defs: dict, handlers: dict) -> None:
        """Register file read tools."""
        super().register_tools(defs, handlers)  # type: ignore[misc]
        self._register_tools_from_list(FILE_READ_TOOLS, defs, handlers)

    def _get_root_path(self) -> Path:
        """Get the root path for file operations."""
        if hasattr(self, "worktree_manager") and self.worktree_manager is not None:
            return self.worktree_manager.worktree_path
        if hasattr(self, "repo_root") and self.repo_root is not None:
            return self.repo_root
        raise RuntimeError("FileReadToolsMixin requires worktree_manager or repo_root")

    def _validate_path(self, path: str) -> tuple[bool, str]:
        """Validate a path is safe for operations."""
        if hasattr(self, "worktree_manager") and self.worktree_manager is not None:
            return self.worktree_manager.validate_path(path)

        root = self._get_root_path()
        try:
            if Path(path).is_absolute():
                p = Path(path)
            else:
                p = root / path

            if p.exists() or p.is_symlink():
                resolved = p.parent.resolve() / p.name
            else:
                resolved = p.resolve()

            resolved.relative_to(root.resolve())
            return True, "ok"
        except ValueError:
            return False, f"Path escapes repository: {path}"

    DEFAULT_READ_LINES = 200

    def _handle_file_read(self, args: dict) -> str:
        path = args.get("path", "")
        start_line = args.get("start_line", 1)
        limit = args.get("limit", self.DEFAULT_READ_LINES)

        if not path:
            return "Error: path is required"

        ok, msg = self._validate_path(path)
        if not ok:
            return f"Error: {msg}"

        root = self._get_root_path()
        full_path = root / path

        if not full_path.exists():
            return f"Error: File not found: {path}"
        if not full_path.is_file():
            return f"Error: Not a file: {path}"

        try:
            content = full_path.read_text()
        except Exception as e:
            return f"Error reading file: {e}"

        lines = content.split("\n")
        total_lines = len(lines)

        start = max(0, start_line - 1)
        end = start + limit
        selected = lines[start:end]

        result_lines = []
        for i, line in enumerate(selected, start=start + 1):
            result_lines.append(f"{i:6}  {line}")

        header = f"# {path} ({total_lines} lines total)"
        if end < total_lines:
            header += f" [showing lines {start + 1}-{min(end, total_lines)}]"

        return header + "\n" + "\n".join(result_lines)

    def _handle_file_inspect_lines(self, args: dict) -> str:
        """Inspect exact whitespace/indentation for specific lines."""
        path = args.get("path", "")
        start_line = args.get("start_line", 1)
        end_line = args.get("end_line", 1)

        if not path:
            return "Error: path is required"

        ok, msg = self._validate_path(path)
        if not ok:
            return f"Error: {msg}"

        root = self._get_root_path()
        full_path = root / path

        if not full_path.exists():
            return f"Error: File not found: {path}"
        if not full_path.is_file():
            return f"Error: Not a file: {path}"

        try:
            content = full_path.read_text()
        except Exception as e:
            return f"Error reading file: {e}"

        lines = content.split("\n")
        total_lines = len(lines)

        if start_line < 1:
            return f"Error: start_line must be >= 1, got {start_line}"
        if end_line > total_lines:
            return f"Error: end_line ({end_line}) exceeds file length ({total_lines} lines)"
        if start_line > end_line:
            return f"Error: start_line ({start_line}) > end_line ({end_line})"

        result_lines = [f"# {path} - Lines {start_line}-{end_line} whitespace inspection\n"]

        for i in range(start_line - 1, end_line):
            line = lines[i]
            line_num = i + 1

            # Count leading whitespace
            stripped = line.lstrip()
            leading = line[: len(line) - len(stripped)]
            spaces = leading.count(" ")
            tabs = leading.count("\t")

            # Create visible whitespace representation
            visible = line.replace("\t", "→   ").replace(" ", "·")

            result_lines.append(f"Line {line_num}:")
            result_lines.append(f"  indent: {spaces} spaces, {tabs} tabs")
            result_lines.append(f"  visible: {visible}")
            result_lines.append(f"  repr: {repr(line)}")
            result_lines.append("")

        return "\n".join(result_lines)

    def _handle_file_list(self, args: dict) -> str:
        path = args.get("path", ".")

        ok, msg = self._validate_path(path)
        if not ok:
            return f"Error: {msg}"

        root = self._get_root_path()
        full_path = root / path

        if not full_path.exists():
            return f"Error: Directory not found: {path}"
        if not full_path.is_dir():
            return f"Error: Not a directory: {path}"

        entries = []
        try:
            for entry in sorted(full_path.iterdir()):
                if entry.name.startswith("."):
                    continue
                name = entry.name
                if entry.is_dir():
                    name += "/"
                entries.append(name)
        except Exception as e:
            return f"Error listing directory: {e}"

        return "\n".join(entries) if entries else "(empty directory)"

    def _handle_file_grep(self, args: dict) -> str:
        import re

        path = args.get("path", "")
        pattern = args.get("pattern", "")
        context_lines = args.get("context_lines", 0)

        if not path:
            return "Error: path is required"
        if not pattern:
            return "Error: pattern is required"

        ok, msg = self._validate_path(path)
        if not ok:
            return f"Error: {msg}"

        root = self._get_root_path()
        full_path = root / path

        if not full_path.exists():
            return f"Error: File not found: {path}"
        if not full_path.is_file():
            return f"Error: Not a file: {path}"

        try:
            content = full_path.read_text()
        except Exception as e:
            return f"Error reading file: {e}"

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"Error: Invalid regex: {e}"

        lines = content.split("\n")
        matches = []
        shown_lines: set[int] = set()

        for i, line in enumerate(lines, 1):
            if regex.search(line):
                start = max(1, i - context_lines)
                end = min(len(lines), i + context_lines)

                if matches and start > max(shown_lines) + 1:
                    matches.append("---")

                for j in range(start, end + 1):
                    if j not in shown_lines:
                        prefix = ">" if j == i else " "
                        matches.append(f"{j:6}{prefix} {lines[j - 1]}")
                        shown_lines.add(j)

        if not matches:
            return f"No matches for pattern: {pattern}"

        return "\n".join(matches)


# =============================================================================
# Full File Tools Mixin (for contributors)
# =============================================================================


class FileToolsMixin(FileReadToolsMixin):
    """Mixin providing ALL file manipulation tools to agents (for contributors).

    Inherits from FileReadToolsMixin and adds write tools.
    Requires self.worktree_manager: WorktreeManager or self.repo_root: Path.

    Provides: file_read, file_list, file_grep (from parent) + file_write, file_edit, file_edit_lines
    Source files (.tex, .md, etc.) are always read-only.
    """

    def register_tools(self, defs: dict, handlers: dict) -> None:
        """Register all file tools (read + write)."""
        super().register_tools(defs, handlers)  # Registers read tools from parent
        self._register_tools_from_list(FILE_WRITE_TOOLS, defs, handlers)

    def _handle_file_write(self, args: dict) -> str:
        path = args.get("path", "")
        content = args.get("content", "")

        if not path:
            return "Error: path is required"

        ok, msg = self._validate_path(path)
        if not ok:
            return f"Error: {msg}"

        # Check for read-only source files
        allow_source = getattr(self, "allow_source_writes", False)
        if not allow_source:
            ext = Path(path).suffix.lower()
            if ext in READ_ONLY_EXTENSIONS:
                return f"Error: Cannot write to source file ({ext}). Source files are read-only."

        root = self._get_root_path()
        full_path = root / path

        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
            line_count = content.count("\n") + (1 if content else 0)
            return f"Written: {path} ({len(content)} chars, {line_count} lines)"
        except Exception as e:
            return f"Error writing file: {e}"

    def _handle_file_delete(self, args: dict) -> str:
        path = args.get("path", "")

        if not path:
            return "Error: path is required"

        ok, msg = self._validate_path(path)
        if not ok:
            return f"Error: {msg}"

        # Check for read-only source files
        allow_source = getattr(self, "allow_source_writes", False)
        if not allow_source:
            ext = Path(path).suffix.lower()
            if ext in READ_ONLY_EXTENSIONS:
                return f"Error: Cannot delete source file ({ext}). Source files are read-only."

        root = self._get_root_path()
        full_path = root / path

        if not full_path.exists():
            return f"Error: File not found: {path}"

        if full_path.is_dir():
            return f"Error: Cannot delete directory: {path}. Use file_delete only for files."

        try:
            full_path.unlink()
            return f"Deleted: {path}"
        except Exception as e:
            return f"Error deleting file: {e}"

    def _handle_file_edit(self, args: dict) -> str:
        path = args.get("path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")

        if not path:
            return "Error: path is required"
        if not old_string:
            return "Error: old_string is required"

        ok, msg = self._validate_path(path)
        if not ok:
            return f"Error: {msg}"

        root = self._get_root_path()
        full_path = root / path

        if not full_path.exists():
            return f"Error: File not found: {path}"

        try:
            content = full_path.read_text()
        except Exception as e:
            return f"Error reading file: {e}"

        count = content.count(old_string)
        if count == 0:
            preview = old_string[:100] + "..." if len(old_string) > 100 else old_string
            return f"Error: old_string not found in file. Searched for:\n{preview}\n\nTip: use file_inspect_lines to check indentation"
        if count > 1:
            return f"Error: old_string appears {count} times (must be unique)"

        new_content = content.replace(old_string, new_string, 1)

        try:
            full_path.write_text(new_content)
        except Exception as e:
            return f"Error writing file: {e}"

        return "Edit applied successfully"

    def _handle_file_edit_lines(self, args: dict) -> str:
        path = args.get("path", "")
        start_line = args.get("start_line", 1)
        end_line = args.get("end_line", 1)
        new_content = args.get("new_content", "")

        if not path:
            return "Error: path is required"

        ok, msg = self._validate_path(path)
        if not ok:
            return f"Error: {msg}"

        root = self._get_root_path()
        full_path = root / path

        if not full_path.exists():
            return f"Error: File not found: {path}"

        try:
            content = full_path.read_text()
        except Exception as e:
            return f"Error reading file: {e}"

        lines = content.split("\n")
        total_lines = len(lines)

        if start_line < 1:
            return f"Error: start_line must be >= 1, got {start_line}"
        if end_line > total_lines:
            return f"Error: end_line ({end_line}) exceeds file length ({total_lines} lines)"
        if start_line > end_line:
            return f"Error: start_line ({start_line}) > end_line ({end_line})"

        new_lines = lines[: start_line - 1] + new_content.split("\n") + lines[end_line:]

        try:
            full_path.write_text("\n".join(new_lines))
        except Exception as e:
            return f"Error writing file: {e}"

        return f"Replaced lines {start_line}-{end_line}"

    def _handle_file_cut_paste(self, args: dict) -> str:
        """Cut lines from source file and insert into destination file."""
        src_path = args.get("src_path", "")
        src_start_line = args.get("src_start_line", 1)
        src_end_line = args.get("src_end_line", 1)
        dest_path = args.get("dest_path", "")
        dest_line = args.get("dest_line", 1)

        if not src_path:
            return "Error: src_path is required"
        if not dest_path:
            return "Error: dest_path is required"

        # Validate source path
        ok, msg = self._validate_path(src_path)
        if not ok:
            return f"Error: {msg}"

        # Validate destination path
        ok, msg = self._validate_path(dest_path)
        if not ok:
            return f"Error: {msg}"

        # Check for read-only source files
        allow_source = getattr(self, "allow_source_writes", False)
        if not allow_source:
            src_ext = Path(src_path).suffix.lower()
            if src_ext in READ_ONLY_EXTENSIONS:
                return f"Error: Cannot cut from source file ({src_ext}). Source files are read-only."
            dest_ext = Path(dest_path).suffix.lower()
            if dest_ext in READ_ONLY_EXTENSIONS:
                return f"Error: Cannot paste to source file ({dest_ext}). Source files are read-only."

        root = self._get_root_path()
        src_full_path = root / src_path
        dest_full_path = root / dest_path

        if not src_full_path.exists():
            return f"Error: Source file not found: {src_path}"
        if not dest_full_path.exists():
            return f"Error: Destination file not found: {dest_path}"

        # Read source file
        try:
            src_content = src_full_path.read_text()
        except Exception as e:
            return f"Error reading source file: {e}"

        src_lines = src_content.split("\n")
        src_total_lines = len(src_lines)

        # Validate source line range
        if src_start_line < 1:
            return f"Error: src_start_line must be >= 1, got {src_start_line}"
        if src_end_line > src_total_lines:
            return f"Error: src_end_line ({src_end_line}) exceeds source file length ({src_total_lines} lines)"
        if src_start_line > src_end_line:
            return f"Error: src_start_line ({src_start_line}) > src_end_line ({src_end_line})"

        # Extract lines to cut (inclusive range, 1-indexed)
        cut_lines = src_lines[src_start_line - 1 : src_end_line]

        # Handle same-file case
        same_file = src_full_path.resolve() == dest_full_path.resolve()

        if same_file:
            # For same file, we need to handle the indexing carefully
            # First remove the cut lines, then insert at the adjusted position
            new_lines = src_lines[: src_start_line - 1] + src_lines[src_end_line:]

            # Adjust dest_line if it was after the cut region
            adjusted_dest_line = dest_line
            if dest_line > src_end_line:
                # Destination was after cut region, adjust for removed lines
                adjusted_dest_line = dest_line - (src_end_line - src_start_line + 1)
            elif dest_line > src_start_line:
                # Destination was within cut region, clamp to start
                adjusted_dest_line = src_start_line

            # Insert at adjusted position (dest_line=1 means insert before line 1)
            insert_idx = max(0, min(adjusted_dest_line - 1, len(new_lines)))
            final_lines = new_lines[:insert_idx] + cut_lines + new_lines[insert_idx:]

            try:
                src_full_path.write_text("\n".join(final_lines))
            except Exception as e:
                return f"Error writing file: {e}"

            return f"Moved lines {src_start_line}-{src_end_line} to line {dest_line} within {src_path}"
        else:
            # Different files: read dest, modify both
            try:
                dest_content = dest_full_path.read_text()
            except Exception as e:
                return f"Error reading destination file: {e}"

            # Handle empty file: split("") returns [""], but we want []
            dest_lines = dest_content.split("\n") if dest_content else []

            # Remove lines from source
            new_src_lines = src_lines[: src_start_line - 1] + src_lines[src_end_line:]

            # Insert into destination (dest_line=1 means insert before line 1)
            insert_idx = max(0, min(dest_line - 1, len(dest_lines)))
            new_dest_lines = dest_lines[:insert_idx] + cut_lines + dest_lines[insert_idx:]

            try:
                src_full_path.write_text("\n".join(new_src_lines))
                dest_full_path.write_text("\n".join(new_dest_lines))
            except Exception as e:
                return f"Error writing files: {e}"

            lines_moved = src_end_line - src_start_line + 1
            return f"Cut {lines_moved} lines ({src_start_line}-{src_end_line}) from {src_path} and inserted before line {dest_line} in {dest_path}"

    def _handle_file_copy_paste(self, args: dict) -> str:
        """Copy lines from source file and insert into destination file."""
        src_path = args.get("src_path", "")
        src_start_line = args.get("src_start_line", 1)
        src_end_line = args.get("src_end_line", 1)
        dest_path = args.get("dest_path", "")
        dest_line = args.get("dest_line", 1)

        if not src_path:
            return "Error: src_path is required"
        if not dest_path:
            return "Error: dest_path is required"

        # Validate source path
        ok, msg = self._validate_path(src_path)
        if not ok:
            return f"Error: {msg}"

        # Validate destination path
        ok, msg = self._validate_path(dest_path)
        if not ok:
            return f"Error: {msg}"

        # Check for read-only destination files (source can be read-only for copy)
        allow_source = getattr(self, "allow_source_writes", False)
        if not allow_source:
            dest_ext = Path(dest_path).suffix.lower()
            if dest_ext in READ_ONLY_EXTENSIONS:
                return f"Error: Cannot paste to source file ({dest_ext}). Source files are read-only."

        root = self._get_root_path()
        src_full_path = root / src_path
        dest_full_path = root / dest_path

        if not src_full_path.exists():
            return f"Error: Source file not found: {src_path}"
        if not dest_full_path.exists():
            return f"Error: Destination file not found: {dest_path}"

        # Read source file
        try:
            src_content = src_full_path.read_text()
        except Exception as e:
            return f"Error reading source file: {e}"

        src_lines = src_content.split("\n")
        src_total_lines = len(src_lines)

        # Validate source line range
        if src_start_line < 1:
            return f"Error: src_start_line must be >= 1, got {src_start_line}"
        if src_end_line > src_total_lines:
            return f"Error: src_end_line ({src_end_line}) exceeds source file length ({src_total_lines} lines)"
        if src_start_line > src_end_line:
            return f"Error: src_start_line ({src_start_line}) > src_end_line ({src_end_line})"

        # Extract lines to copy (inclusive range, 1-indexed)
        copy_lines = src_lines[src_start_line - 1 : src_end_line]

        # Read destination file
        try:
            dest_content = dest_full_path.read_text()
        except Exception as e:
            return f"Error reading destination file: {e}"

        # Handle empty file: split("") returns [""], but we want []
        dest_lines = dest_content.split("\n") if dest_content else []

        # Insert into destination (dest_line=1 means insert before line 1)
        insert_idx = max(0, min(dest_line - 1, len(dest_lines)))
        new_dest_lines = dest_lines[:insert_idx] + copy_lines + dest_lines[insert_idx:]

        try:
            dest_full_path.write_text("\n".join(new_dest_lines))
        except Exception as e:
            return f"Error writing destination file: {e}"

        lines_copied = src_end_line - src_start_line + 1
        same_file = src_full_path.resolve() == dest_full_path.resolve()
        if same_file:
            return f"Duplicated {lines_copied} lines ({src_start_line}-{src_end_line}) and inserted before line {dest_line} in {src_path}"
        else:
            return f"Copied {lines_copied} lines ({src_start_line}-{src_end_line}) from {src_path} and inserted before line {dest_line} in {dest_path}"

    def _handle_list_issues(self, args: dict) -> str:
        """List issues with optional filtering and truncation."""
        import yaml

        def parse_issue_raw(content: str) -> dict:
            """Parse issue file as raw text when YAML parsing fails."""
            result = {}
            status_match = re.search(r"^status:\s*(.+)$", content, re.MULTILINE)
            if status_match:
                result["status"] = status_match.group(1).strip()
            desc_match = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)
            if desc_match:
                desc = desc_match.group(1).strip()
                # Remove surrounding quotes if present
                if (desc.startswith("'") and desc.endswith("'")) or (desc.startswith('"') and desc.endswith('"')):
                    desc = desc[1:-1]
                result["description"] = desc
            return result

        status_filter = args.get("status", "open")
        max_desc_len = args.get("max_desc_len", 100)

        root = self._get_root_path()
        issues_dir = root / "issues"

        if not issues_dir.exists():
            return "No issues directory found."
        if not issues_dir.is_dir():
            return "Error: 'issues' is not a directory"

        results = []
        for f in sorted(issues_dir.glob("*.yaml")):
            if not f.is_file():
                continue
            try:
                content = f.read_text()
                try:
                    data = yaml.safe_load(content)
                    if not isinstance(data, dict):
                        data = parse_issue_raw(content)
                except Exception:
                    # YAML parsing failed - fallback to raw text parsing
                    data = parse_issue_raw(content)

                issue_status = data.get("status", "unknown")

                # Filter by status
                if status_filter != "all" and issue_status != status_filter:
                    continue

                desc = data.get("description", "")
                # Normalize whitespace (collapse newlines to spaces)
                desc = " ".join(desc.split())

                # Truncate if needed
                if max_desc_len > 0 and len(desc) > max_desc_len:
                    desc = desc[:max_desc_len] + "..."

                issue_id = f.stem
                results.append(f"{issue_id} | {issue_status} | {desc}")

            except Exception as e:
                results.append(f"{f.stem} | error | [Error reading: {e}]")

        if not results:
            if status_filter == "all":
                return "No issues found."
            return f"No {status_filter} issues found."

        header = f"Found {len(results)} issue(s) (status={status_filter}):\n"
        return header + "\n".join(results)

    def _handle_create_issue(self, args: dict) -> str:
        """Create a new issue with UUID filename."""
        import secrets

        import yaml

        description = args.get("description", "")
        origin = args.get("origin", "agent")

        if not description:
            return "Error: description is required"

        root = self._get_root_path()
        issues_dir = root / "issues"
        issues_dir.mkdir(exist_ok=True)

        issue_id = secrets.token_hex(4)
        issue_data = {
            "status": "open",
            "origin": origin,
            "description": description,
        }

        issue_path = issues_dir / f"{issue_id}.yaml"
        issue_path.write_text(yaml.safe_dump(issue_data, sort_keys=False, allow_unicode=True))

        return f"Created issue {issue_id}: {description}"
