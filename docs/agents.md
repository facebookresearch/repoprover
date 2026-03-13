# Agent System

This document describes the agent architecture in RepoProver.

## Overview

RepoProver uses LLM-powered agents to translate and prove mathematics. The primary agent is the **ContributorAgent**, which operates in five different modes to handle all formalization work.

## Architecture

### Core Design Principles

1. **Unified Tool Loop** — All agents use `run_tool_loop()` from `tools.py` for LLM interaction
2. **Tools as Mixins** — Tool capabilities are added via mixin classes (e.g., `FileToolsMixin`, `LeanToolsMixin`)
3. **Convention-Based Handlers** — Tool handlers are automatically discovered by naming convention (`_handle_{tool_name}`)
4. **Read-Only vs Full Access** — Reviewers get read-only tools; contributors get full access

### Tool Registration Pattern

Tools are registered via a mixin chain using `register_tools()`:

```python
class MyToolsMixin:
    """Each mixin defines its tools and handlers."""

    def register_tools(self, defs: dict, handlers: dict) -> None:
        super().register_tools(defs, handlers)  # Chain to other mixins
        self._register_tools_from_list(MY_TOOLS, defs, handlers)

    def _handle_my_tool(self, args: dict) -> str:
        """Handler discovered automatically by naming convention."""
        ...
```

The base class `_register_tools_from_list()` method:
1. Takes a list of tool definitions
2. Extracts each tool's `name` from the definition
3. Looks up `_handle_{name}` method on `self`
4. Registers both the definition and handler

### Agent Class Hierarchy

```
BaseAgent (abstract)
├── ContributorAgent (FileToolsMixin, GitWorktreeToolsMixin, LeanToolsMixin,
│                     MathlibToolsMixin, ShellToolsMixin)
│   └── All 5 modes: sketch, prove, maintain, scan, triage
│
└── BaseReviewer (FileReadToolsMixin, MathlibToolsMixin, LeanToolsMixin)
    ├── MathReviewer
    └── EngineeringReviewer
```

**Key difference:** Contributors use `FileToolsMixin` (read + write), reviewers use `FileReadToolsMixin` (read-only).

### Tool Mixin Hierarchy

```
FileReadToolsMixin          # file_read, file_list, file_grep (read-only)
    └── FileToolsMixin      # + file_write, file_edit, file_edit_lines

GitWorktreeToolsMixin       # git_status, git_add, git_commit, git_diff, etc.

LeanToolsMixin              # lean_check

MathlibToolsMixin           # mathlib_grep, mathlib_find_name, mathlib_read_file
                            # (conditional on config.mathlib_grep)

ShellToolsMixin             # bash
```

## ContributorAgent

A unified agent that handles all formalization work. The mode determines the task and behavior.

### Mode: SKETCH

Creates initial Lean formalizations from LaTeX source.

**Responsibilities:**
- Read source LaTeX files
- Create Lean files with definitions, structures, and theorem statements
- Use Mathlib conventions and existing definitions
- Leave proofs as `sorry` (provers fill these in)
- Fix issues raised by reviewers

**Input:**
- `chapter_id`: Chapter identifier
- `source_tex_path`: Path to LaTeX source file
- `lean_path`: Path to target Lean file

**Output:**
- `ContributorResult` with status (done/blocked/failure)

### Mode: PROVE

Fills in `sorry` proofs with complete Lean proofs.

**Responsibilities:**
- Read the Lean file to understand available lemmas
- Consult source LaTeX for proof strategies
- Develop and verify complete proofs
- Create issues when theorem statement is problematic (via ISSUE)
- Propose fixes directly when the problem is clear (via FIX)

**Key Rule:** Can use any theorem that appears BEFORE the target in the file, even if those theorems still have `sorry`.

**Input:**
- `chapter_id`: Chapter identifier
- `theorem_name`: Name of theorem to prove
- `lean_path`: Path to Lean file
- `source_tex_path`: Optional, for proof hints

**Output:**
- `ContributorResult` with status (done/fix/issue/blocked/failure)

### Mode: MAINTAIN

Picks up open issues and makes progress on them.

**Responsibilities:**
- Read ISSUES.md to find open issues
- Select or work on a specific issue
- Make progress (add lemmas, fix statements, refactor)
- Close issues that are resolved by the work

**Input:**
- `issue_id`: Optional issue ID to work on (if None, agent picks one)

**Output:**
- `ContributorResult` with status (done/blocked/failure)

### Mode: SCAN

Scans the codebase for architectural issues and creates issues in ISSUES.md.

**Responsibilities:**
- Scan Lean files for API gaps, forward dependencies, naming issues
- Create well-formatted issues in ISSUES.md
- Avoid duplicates by checking existing issues
- Commit changes and create PR for review

**Input:**
- `lean_paths`: Optional list of Lean files to scan

**Output:**
- `ContributorResult` with status (done/blocked/failure)

### Mode: TRIAGE

Scans ISSUES.md to identify and close resolved or obsolete issues.

**Responsibilities:**
- Read ISSUES.md to identify open issues
- Check codebase state for each issue
- Identify **resolved** issues (work already done, no `sorry`)
- Identify **discarded** issues (codebase took different direction)
- Close stale issues with clear explanations
- Does NOT modify any files other than ISSUES.md

**Key Design Decisions:**
- Finding nothing to close is a valid, successful outcome
- Only edits ISSUES.md — no code changes
- Creates PRs for human review before merge
- Discarded issues get annotated with reason: `(discarded: reason)`

**Output:**
- `ContributorResult` with status (done/blocked/failure)

## Output Markers

All contributor modes use unified output markers:

### DONE - Successful completion
```
-- DONE
-- Summary: <what was accomplished>
```

### FIX - Fixed a blocker
```
-- FIX
-- Fixed: <what was fixed>
-- Original task: <still needs to be done>
-- END FIX
```

### ISSUE - Created an issue
```
-- ISSUE
-- Issue created: #N
-- Problem: <description>
-- Action required: <what needs to be done>
-- END ISSUE
```

### BLOCKED - Cannot make progress
```
-- BLOCKED
-- Reason: <why no progress could be made>
```

## ContributorTask

The task specification for ContributorAgent:

```python
@dataclass
class ContributorTask:
    mode: ContributorMode
    chapter_id: str | None = None
    theorem_name: str | None = None
    issue_id: int | None = None
    lean_path: str = ""
    source_tex_path: str = ""

    # Factory methods
    @classmethod
    def sketch(cls, chapter_id, source_tex_path, lean_path): ...
    @classmethod
    def prove(cls, chapter_id, theorem_name, lean_path, source_tex_path): ...
    @classmethod
    def maintain(cls, issue_id=None): ...
    @classmethod
    def scan(cls): ...
    @classmethod
    def triage(cls): ...
```

## ContributorResult

Unified result type for all modes:

```python
@dataclass
class ContributorResult:
    task: ContributorTask
    status: str  # "done", "fix", "issue", "blocked", "failure"
    mergeable_code: str | None = None
    fix_request: str | None = None
    issue_text: str | None = None
    learnings: list[str] = field(default_factory=list)
    run: Any = None
    error: str | None = None
```

## Reviewers

Two independent reviewers that inherit from `BaseReviewer`, which itself inherits from `BaseAgent`:

```python
class BaseReviewer(FileReadToolsMixin, MathlibToolsMixin, LeanToolsMixin, BaseAgent):
    """Base class for reviewers with read-only tools."""

    def review(self, pr: ReviewContext, diff: str, files: dict) -> Review:
        """Perform review using inherited run() method."""
        self._current_pr = pr  # Set context for prompts
        result = self.run()    # Uses shared tool loop
        return self._parse_review(result)
```

**MathReviewer**
- Agent-type-aware review (sorry is expected for sketch mode, not for prove mode)
- Logical correctness and mathematical accuracy
- Proper use of Mathlib lemmas
- Mathematical clarity
- Can use `file_read`, `file_grep`, `mathlib_grep` to verify claims

**EngineeringReviewer**
- Code style and formatting
- Naming conventions
- Module organization
- Documentation
- Can use `file_read`, `file_grep`, `mathlib_grep` to verify claims

### Reviewer Tools

Reviewers have access to **read-only** tools for verification:

| Tool | Purpose |
|------|---------|
| `file_read` | Read file content to verify claims |
| `file_list` | List directory structure |
| `file_grep` | Search for patterns in files |
| `mathlib_grep` | Search Mathlib for API usage |
| `mathlib_find_name` | Find Mathlib declarations |
| `mathlib_read_file` | Read Mathlib source |
| `lean_check` | Verify Lean code snippets |

This allows reviewers to verify that:
- File paths and locations referenced in issues actually exist
- Mathlib APIs are used correctly
- Code compiles as claimed

## Base Agent Architecture

All agents extend `BaseAgent` which provides:

```python
class BaseAgent(ABC):
    def __init__(self, config, repo_root, recorder, ...):
        # Tool registration happens automatically via mixin chain
        self._tool_defs: dict[str, dict] = {}
        self._tool_handlers: dict[str, Any] = {}
        self.register_tools(self._tool_defs, self._tool_handlers)

    def run(self, **kwargs) -> AgentResult:
        """Main entry point - uses shared run_tool_loop()."""

    def get_tools(self) -> list[dict]:
        """Returns all registered tool definitions."""
        return list(self._tool_defs.values())

    def handle_tool_call(self, name: str, arguments: dict) -> str:
        """Dispatches to registered handler by name."""
        handler = self._tool_handlers.get(name)
        return handler(arguments) if handler else f"Unknown tool: {name}"

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Build the system prompt."""

    @abstractmethod
    def build_user_prompt(self, **kwargs) -> str:
        """Build the initial user message."""

    def register_tools(self, defs: dict, handlers: dict) -> None:
        """Override in mixins using super() chain."""
        pass  # Base case

    def should_stop(self, text: str) -> bool:
        """Custom stop condition (e.g., '-- DONE' marker)."""
        return False
```

### Unified Tool Loop (`tools.py`)

The `run_tool_loop()` function is THE single implementation used by all agents:

```python
def run_tool_loop(
    client: OpenAI,
    model: str,
    system_prompt: str,
    initial_messages: list[dict],
    tools: list[dict] | None,
    tool_handler: Callable[[str, dict], str],
    *,
    max_iterations: int = 128,
    should_stop: Callable[[str], bool] | None = None,
    recorder: AgentRecorder | None = None,
) -> ToolLoopResult:
    """
    Core loop:
    1. Call LLM with system prompt + messages
    2. If finish_reason == "stop" or should_stop(text): done
    3. If tool_calls: execute each, append results, continue
    4. If max_iterations reached: done with "max_iterations" status

    Returns: ToolLoopResult with final_text, messages, tool_calls, stop_reason
    """
```

Benefits:
- Single implementation eliminates code duplication
- Consistent retry logic with exponential backoff
- Unified recording interface
- Same behavior for contributors and reviewers

### AgentConfig

```python
@dataclass
class AgentConfig:
    model: str = "claude-sonnet-4-20250514"
    temperature: float = 0.7
    max_tokens: int = 8192
    max_iterations: int = 128
    api_timeout: int = 120
    provider: str = ""  # anthropic, openai, google
```

## Tool System

Tools are organized into mixins that agents inherit. The mixin chain automatically registers all tools.

### File Tools (`file_tools.py`)

**FileReadToolsMixin** (read-only, for reviewers):
- `file_read(path, offset?, limit?)` — Read files with line numbers
- `file_list(path?)` — List directory contents
- `file_grep(path, pattern, context_lines?)` — Search with regex

**FileToolsMixin** (inherits from FileReadToolsMixin, for contributors):
- All read tools above, plus:
- `file_write(path, content)` — Write/overwrite files
- `file_edit(path, old_string, new_string)` — Replace exact text
- `file_edit_lines(path, start_line, end_line, new_content)` — Replace by line range

### Git Tools (`git_worktree_tools.py`)

**GitWorktreeToolsMixin** (for contributors in worktrees):
- `git_status()` — Show changed files
- `git_add(paths)` — Stage files
- `git_commit(message)` — Commit staged changes
- `git_diff(paths?, staged?)` — Show differences
- `git_log(n?)` — Show recent commits
- `git_unstage(paths)` — Unstage files
- `git_restore(paths)` — Discard changes
- `git_checkout_file(ref, paths)` — Checkout files from a specific ref (e.g., main)
- `git_rebase(branch?)` — Rebase onto main (default) to sync with latest
- `git_rebase_continue()` — Continue after resolving rebase conflicts
- `git_rebase_abort()` — Abort a rebase in progress
- `git_conflicts()` — Show conflict markers with line numbers

### Lean Tools (`lean_tools.py`)

**LeanToolsMixin**:
- `lean_check(code)` — Check arbitrary Lean code snippet using the global checker pool

### Mathlib Tools (`mathlib_tools.py`)

**MathlibToolsMixin** (conditional on `config.mathlib_grep`):
- `mathlib_grep(pattern, kind?, subdir?, max_results?, context_lines?, literal?)` — Search Mathlib source
- `mathlib_find_name(name, exact?, max_results?)` — Find declarations by name
- `mathlib_read_file(file_path, start_line?, end_line?)` — Read Mathlib source files

### Shell Tools (`shell_tools.py`)

**ShellToolsMixin**:
- `bash(command)` — Run shell commands (e.g., `lake build`)
- Commands are validated by `SafeShell` based on agent role

### Adding New Tools

To add a new tool:

1. **Define the tool schema** in the mixin module:
```python
MY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "my_tool",
            "description": "What the tool does...",
            "parameters": { ... }
        }
    }
]
```

2. **Create the mixin class**:
```python
class MyToolsMixin:
    def register_tools(self, defs: dict, handlers: dict) -> None:
        super().register_tools(defs, handlers)
        self._register_tools_from_list(MY_TOOLS, defs, handlers)

    def _handle_my_tool(self, args: dict) -> str:
        """Handler name MUST be _handle_{tool_name}."""
        return "result"
```

3. **Add the mixin to the agent class**:
```python
class ContributorAgent(MyToolsMixin, FileToolsMixin, ..., BaseAgent):
    ...
```

### File Protection

Source files are always read-only:
- `.tex`, `.md`, `.pdf`, `.txt` files cannot be written
- This is enforced in `FileToolsMixin._validate_path()` regardless of agent type

## Learnings System

Agents can emit learnings that persist across runs:

```
-- LEARNING: mathlib_api
-- Problem: Need to use Ring.neg_one_pow for (-1)^n
-- Solution: Use Ring.neg_one_pow instead of direct ^ notation
```

Categories: `mathlib_api`, `tactic`, `type_coercion`, `proof_strategy`, `lemma_search`, `notation`, `naming`, `structure`, `hypothesis`

Learnings are stored in `.repoprover/learnings.json` and included in future agent prompts.

## Recording

When enabled, all agent interactions are recorded:
- System prompts
- User messages
- Assistant responses
- Tool calls and results
- Completion status

See `recording.py` for details.
