# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Base agent class with LLM API integration and tool handling.

This module provides the foundation for all agents in repoprover:
- LLM API integration (OpenAI-compatible, supports multiple providers)
- Tool registration and dispatch
- Message history management
- Chapter/worktree context

Tool definitions and the core tool loop are centralized in tools.py.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any

from openai import OpenAI

from .tools import DEFAULT_MAX_ITERATIONS, LLM_ERROR_MAX_LEN, ToolCallRecord, run_tool_loop, truncate_error

if TYPE_CHECKING:
    from ..git_worktree import WorktreeManager
    from ..recording import AgentRecorder

logger = getLogger(__name__)


# =============================================================================
# Provider Configuration
# =============================================================================

PROVIDER_BASE_URLS = {
    "anthropic": "https://api.anthropic.com/v1/",
    "openai": "https://api.openai.com/v1/",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/",
}

PROVIDER_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
}

PROVIDER_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-20250514",
    "openai": "gpt-4o",
    "google": "gemini-2.5-flash",
}


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class AgentConfig:
    """Configuration for agents."""

    provider: str = ""  # anthropic, openai, google (must be set)
    model: str = ""  # If empty, uses provider default
    temperature: float = 0.7
    max_tokens: int = 8192
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    api_key: str = ""  # If empty, uses env var
    base_url: str = ""  # If empty, uses provider default
    # Mathlib grep tools configuration
    mathlib_grep: bool = True  # Enable mathlib_grep tools for searching Mathlib source

    def __post_init__(self) -> None:
        if not self.model:
            self.model = PROVIDER_DEFAULT_MODELS.get(self.provider, "claude-sonnet-4-20250514")


@dataclass
class ToolCall:
    """Record of a single tool invocation.

    Deprecated: Use ToolCallRecord from tools.py instead.
    Kept for backward compatibility.
    """

    name: str
    arguments: dict[str, Any]
    result: str
    duration_seconds: float = 0.0

    @classmethod
    def from_record(cls, record: ToolCallRecord) -> "ToolCall":
        """Create from a ToolCallRecord."""
        return cls(
            name=record.name,
            arguments=record.arguments,
            result=record.result,
            duration_seconds=record.duration_seconds,
        )


@dataclass
class AgentRun:
    """Record of a single agent run."""

    run_id: str
    agent_type: str
    started_at: float = field(default_factory=time)
    ended_at: float | None = None
    status: str = "running"  # running, done, error, escalation
    tool_calls: list[ToolCall] = field(default_factory=list)
    dialog: list[dict[str, Any]] = field(default_factory=list)
    iteration_count: int = 0
    error: str | None = None


@dataclass
class AgentResult:
    """Result from an agent run."""

    dialog: list[dict[str, Any]]
    learnings: list[str] = field(default_factory=list)
    run: AgentRun | None = None
    status: str = "done"


# =============================================================================
# LLM API Helpers
# =============================================================================


def create_client(config: AgentConfig) -> OpenAI:
    """Create an OpenAI-compatible client for the configured provider."""
    base_url = config.base_url or PROVIDER_BASE_URLS.get(config.provider, "")
    if not base_url:
        raise ValueError(f"No base URL for provider '{config.provider}'. Provide base_url in AgentConfig.")

    api_key = config.api_key
    if not api_key:
        env_var = PROVIDER_API_KEY_ENV.get(config.provider, "")
        api_key = os.environ.get(env_var, "")
        if not api_key:
            raise ValueError(f"API key not found. Set {env_var} environment variable or pass api_key in AgentConfig.")

    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        default_headers={"anthropic-beta": ""},
    )


def get_text_content(response) -> str:
    """Extract text content from OpenAI response."""
    message = response.choices[0].message
    return message.content or ""


def get_stop_reason(response) -> str:
    """Get the stop reason from OpenAI response."""
    return response.choices[0].finish_reason or ""


def call_llm_simple(
    system: str,
    user_message: str,
    config: AgentConfig | None = None,
) -> str:
    """Simple one-shot LLM call. Creates client, calls API, returns text.

    Use this for simple cases that don't need tool handling.
    """
    config = config or AgentConfig()
    client = create_client(config)

    # Use run_tool_loop with no tools for consistency
    result = run_tool_loop(
        client=client,
        model=config.model,
        system_prompt=system,
        initial_messages=[{"role": "user", "content": user_message}],
        tools=None,
        tool_handler=lambda name, args: "Error: No tools available",
        max_tokens=config.max_tokens,
        temperature=config.temperature,
    )
    return result.final_text


# =============================================================================
# Learnings Store
# =============================================================================


class LearningsStore:
    """Simple storage for agent learnings."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._learnings: list[dict[str, str]] = []
        if path and path.exists():
            self._load()

    def _load(self) -> None:
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text())
            self._learnings = data.get("learnings", [])
        except Exception as e:
            logger.warning(f"Failed to load learnings: {e}")

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"learnings": self._learnings}, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save learnings: {e}")

    def add(self, category: str, problem: str, solution: str) -> None:
        self._learnings.append(
            {
                "category": category,
                "problem": problem,
                "solution": solution,
            }
        )
        self.save()

    def to_prompt_context(self, max_learnings: int = 10) -> str:
        """Format learnings for inclusion in a prompt."""
        if not self._learnings:
            return "(No learnings yet)"

        recent = self._learnings[-max_learnings:]
        lines = []
        for learning in recent:
            lines.append(f"- [{learning['category']}] {learning['problem']} → {learning['solution']}")
        return "\n".join(lines)


# =============================================================================
# Base Agent
# =============================================================================


class BaseAgent(ABC):
    """Abstract base class for all agents.

    Provides:
    - LLM API integration
    - Tool registration and dispatch
    - Message history management
    - Run recording

    Subclasses must implement:
    - get_system_prompt() -> str
    - build_user_prompt(**kwargs) -> str
    - handle_tool_call(name, arguments) -> str
    """

    agent_type: str = "base"

    def __init__(
        self,
        config: AgentConfig | None = None,
        worktree_manager: "WorktreeManager | None" = None,
        repo_root: Path | None = None,
        learnings: LearningsStore | None = None,
        recorder: "AgentRecorder | None" = None,
    ):
        self.config = config or AgentConfig()
        self.worktree_manager = worktree_manager
        self.repo_root = repo_root or (worktree_manager.worktree_path if worktree_manager else None)
        self.learnings = learnings or LearningsStore()
        self.recorder = recorder

        # API client (lazy init)
        self._client: Any = None

        # Current run tracking
        self._current_run: AgentRun | None = None
        self._messages: list[dict[str, Any]] = []

        # Tool registration (populated by mixin register_tools via super() chain)
        self._tool_defs: dict[str, dict] = {}
        self._tool_handlers: dict[str, Any] = {}
        self.register_tools(self._tool_defs, self._tool_handlers)

    @property
    def client(self):
        """Get or create the API client."""
        if self._client is None:
            self._client = create_client(self.config)
        return self._client

    @property
    def log_prefix(self) -> str:
        """Log prefix for this agent."""
        return f"[{self.agent_type}]"

    # -------------------------------------------------------------------------
    # Abstract methods
    # -------------------------------------------------------------------------

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Build the system prompt for this agent."""
        pass

    @abstractmethod
    def build_user_prompt(self, **kwargs) -> str:
        """Build the initial user prompt for this agent."""
        pass

    def register_tools(self, defs: dict[str, dict], handlers: dict[str, Any]) -> None:
        """Register tools from mixins. Override in mixins using super() pattern.

        Each mixin should call super().register_tools() then self._register_tools_from_list().
        Handlers must be named _handle_{tool_name}.

        Example mixin:
            TOOLS = [...]  # Tool definitions

            def register_tools(self, defs, handlers):
                super().register_tools(defs, handlers)
                self._register_tools_from_list(self.TOOLS, defs, handlers)

            def _handle_my_tool(self, args): ...
        """
        pass  # Base case - do nothing

    def _register_tools_from_list(self, tools: list[dict], defs: dict[str, dict], handlers: dict[str, Any]) -> None:
        """Register tools using naming convention: handler is _handle_{tool_name}.

        Args:
            tools: List of tool definitions (each has function.name)
            defs: Tool definitions dict to populate
            handlers: Handler dict to populate
        """
        for tool_def in tools:
            name = tool_def["function"]["name"]
            if name in defs:
                raise ValueError(f"Tool collision: {name}")
            defs[name] = tool_def
            handler = getattr(self, f"_handle_{name}", None)
            if handler is None:
                raise ValueError(f"Missing handler: _handle_{name} for tool {name}")
            handlers[name] = handler

    def get_tools(self) -> list[dict[str, Any]]:
        """Return list of tool definitions from all registered mixins."""
        return list(self._tool_defs.values())

    def handle_tool_call(self, name: str, arguments: dict[str, Any]) -> str:
        """Dispatch tool call to registered handler."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}

        handler = self._tool_handlers.get(name)
        if handler is None:
            return f"Unknown tool: {name}"

        try:
            return handler(arguments)
        except Exception as e:
            logger.exception(f"Tool {name} failed")
            return f"Error: {e}"

    def should_stop(self, _text: str) -> bool:
        """Check if agent output indicates completion.

        Override in subclass for custom stop conditions.
        """
        return False

    # -------------------------------------------------------------------------
    # Main run loop
    # -------------------------------------------------------------------------

    def run(self, **kwargs) -> AgentResult:
        """Run the agent with the given initial context.

        Uses the shared run_tool_loop() from tools.py for the core loop.

        Args:
            **kwargs: Arguments passed to build_user_prompt()

        Returns:
            AgentResult with dialog, learnings, and run metadata
        """
        import uuid

        # Initialize run
        run_id = str(uuid.uuid4())[:8]
        self._current_run = AgentRun(
            run_id=run_id,
            agent_type=self.agent_type,
        )

        # Build initial messages
        system_prompt = self.get_system_prompt()
        user_prompt = self.build_user_prompt(**kwargs)

        self._messages = [{"role": "user", "content": user_prompt}]
        self._current_run.dialog.append({"role": "user", "content": user_prompt})

        tools = self.get_tools()

        # Track errors for recorder finalization
        error_occurred = False
        error_msg = ""
        final_text = ""

        try:
            # Use the shared tool loop
            result = run_tool_loop(
                client=self.client,
                model=self.config.model,
                system_prompt=system_prompt,
                initial_messages=[{"role": "user", "content": user_prompt}],
                tools=tools if tools else None,
                tool_handler=self.handle_tool_call,
                max_iterations=self.config.max_iterations,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
                should_stop=self.should_stop,
                recorder=self.recorder,
                log_prefix=self.log_prefix,
            )

            # Update run metadata from result
            self._current_run.iteration_count = result.iteration_count
            self._messages = result.messages
            final_text = result.final_text

            # Convert tool call records to the legacy format
            for tc_record in result.tool_calls:
                self._current_run.tool_calls.append(ToolCall.from_record(tc_record))

            # Build dialog from messages for backward compatibility
            self._current_run.dialog = []
            for msg in result.messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                dialog_entry: dict[str, Any] = {"role": role, "content": content}

                if role == "assistant" and msg.get("tool_calls"):
                    # Extract tool call info for dialog
                    tool_calls_info = []
                    for tc in msg["tool_calls"]:
                        if isinstance(tc, dict) and "function" in tc:
                            tc_name = tc["function"].get("name", "")
                            tc_args_str = tc["function"].get("arguments", "{}")
                            try:
                                tc_args = json.loads(tc_args_str)
                            except json.JSONDecodeError:
                                tc_args = {}
                            tool_calls_info.append((tc_name, tc_args))
                    dialog_entry["tool_calls"] = tool_calls_info

                self._current_run.dialog.append(dialog_entry)

            # Map stop_reason to status
            if result.stop_reason == "max_iterations":
                self._current_run.status = "max_iterations"
                logger.warning(f"Agent {self.agent_type} hit max iterations")
            else:
                self._current_run.status = "done"

        except Exception as e:
            self._current_run.status = "error"
            self._current_run.error = str(e)
            error_occurred = True
            error_msg = str(e)
            logger.exception(f"Agent {self.agent_type} error: {truncate_error(error_msg, LLM_ERROR_MAX_LEN)}")
            raise

        finally:
            self._current_run.ended_at = time()

            # Finalize recorder
            if self.recorder:
                if error_occurred:
                    self.recorder.done("error", error_msg)
                else:
                    self.recorder.done(self._current_run.status)

        # Extract learnings from final output
        learnings = self._extract_learnings(final_text)

        return AgentResult(
            dialog=self._current_run.dialog,
            learnings=learnings,
            run=self._current_run,
            status=self._current_run.status,
        )

    def _extract_learnings(self, text: str) -> list[str]:
        """Extract LEARNING blocks from agent output."""
        pattern = r"-- LEARNING: (\w+)\n-- Problem: ([^\n]+)\n-- Solution: ([^\n]+)"
        matches = re.findall(pattern, text)
        learnings = []
        for category, problem, solution in matches:
            self.learnings.add(category, problem, solution)
            learnings.append(f"[{category}] {problem} → {solution}")
        return learnings


# =============================================================================
# Utility Functions
# =============================================================================


def dialog_to_text(dialog: list[dict[str, Any]]) -> str:
    """Convert dialog history to plain text for analysis."""
    parts = []
    for msg in dialog:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(c) for c in content)
        parts.append(f"[{role}] {content}")
    return "\n\n".join(parts)
