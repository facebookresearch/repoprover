# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Core tool loop infrastructure shared by all agents.

This module provides:
- ToolLoopResult, ToolCallRecord: Result types
- run_tool_loop(): THE core tool loop implementation
- _call_with_retry(): LLM call with exponential backoff
- truncate_error(): Error message truncation helper
- Context compaction: Automatic summarization when context exceeds threshold
"""

from __future__ import annotations

import json
import time as time_module
from dataclasses import dataclass, field
from logging import getLogger
from time import time
from typing import TYPE_CHECKING, Any, Callable

from openai import APIConnectionError, InternalServerError, RateLimitError

if TYPE_CHECKING:
    from openai import OpenAI

    from ..recording import AgentRecorder

logger = getLogger(__name__)

# Default max iterations for tool loops (same for all agents)
DEFAULT_MAX_ITERATIONS = 512

# =============================================================================
# Context Compaction Configuration
# =============================================================================

# Maximum context window size (in tokens)
MAX_CONTEXT_TOKENS = 200_000

# Threshold at which to trigger compaction (in tokens)
COMPACTION_THRESHOLD_TOKENS = 150_000

# Buffer to keep from max context (in tokens)
CONTEXT_BUFFER_TOKENS = 10_000

# Round token budgets down to this multiple for cleaner numbers
TOKEN_BUDGET_ROUND_TO = 5_000


# =============================================================================
# Context Compaction Prompt
# =============================================================================

COMPACTION_PROMPT_TEMPLATE = """
**CONTEXT COMPACTION REQUIRED**

The conversation context has grown large and must be compacted to continue.
You have approximately **{remaining_tokens_k}k tokens** remaining until the 200k context limit.

Please provide a detailed summary that preserves everything needed to continue your work effectively.
Structure your response EXACTLY as follows:

## Original Task Assignment
[Restate the complete original task/goal you were given. Include all requirements, constraints, and success criteria.]

## Key File Paths & Locations
[List all important file paths, directories, and code locations you've discovered or worked with:]
- /path/to/file1.py - description of what it contains/does
- /path/to/file2.py - description

## Useful Code Extracts
[Include verbatim any critical code snippets, function signatures, type definitions, or patterns you'll need:]
```language
code here
```

## Work Completed So Far
[Describe what you've already accomplished:]
1. Step 1 completed: description
2. Step 2 completed: description

## Work In Progress
[Describe any partially completed work:]
- Current state of implementation
- What was being worked on when compaction triggered

## Key Learnings & Discoveries
[Document important insights, gotchas, or discoveries:]
- Learning 1: explanation
- Learning 2: explanation

## Remaining Tasks
[List what still needs to be done:]
1. Task 1: description
2. Task 2: description

## Critical Context
[Any other essential information that must not be lost:]
- Important decisions made and why
- Dependencies or constraints discovered
- Error patterns encountered and solutions found

---
After this summary, the conversation will continue with your summary as the new context.
Respond with ONLY the summary in the format above, nothing else.
"""


def _estimate_tokens(text: str) -> int:
    """Rough estimate of token count for a string.

    Uses ~2.5 characters per token as a conservative estimate.
    This is safer for code (especially with unicode/special chars) where
    tokenization can be denser than typical English text.
    """
    return int(len(text) / 2.5)


def _estimate_tool_results_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate tokens for tool results added since last LLM call.

    Looks at trailing tool messages that haven't been sent to the LLM yet.
    """
    total = 0
    # Count backwards from the end, summing tool messages
    for msg in reversed(messages):
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if isinstance(content, str):
                total += _estimate_tokens(content)
        else:
            # Stop at first non-tool message (the previous assistant message)
            break
    return total


def _perform_compaction(
    client: "OpenAI",
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    temperature: float,
    log_prefix: str,
    current_context_tokens: int,
) -> tuple[list[dict[str, Any]], int, int, str]:
    """Perform context compaction by adding compaction prompt to the existing conversation.

    This keeps the same dialog - we add the compaction prompt as a user message,
    get the summary response, then respawn with just the summary.

    Args:
        current_context_tokens: Actual token count of current context (from API usage)
        tools: Tool definitions (passed through to maintain same context)

    Returns:
        Tuple of (new_messages, input_tokens_used, output_tokens_used, summary_text)
    """
    # Calculate remaining budget: (max - buffer) - current_size, rounded down to 5k
    max_usable = MAX_CONTEXT_TOKENS - CONTEXT_BUFFER_TOKENS  # 190k
    remaining_raw = max_usable - current_context_tokens
    # Clamp to at least 1k to ensure LLM has room for a meaningful summary
    remaining_k = max(1, (remaining_raw // TOKEN_BUDGET_ROUND_TO) * TOKEN_BUDGET_ROUND_TO // 1000)

    # Build the compaction prompt
    compaction_prompt = COMPACTION_PROMPT_TEMPLATE.format(remaining_tokens_k=remaining_k)

    # Add compaction prompt to the EXISTING messages (same dialog, not a separate one)
    # Sanitize messages to ensure no None content values (AWS Bedrock rejects null)
    sanitized_messages = []
    for msg in messages:
        sanitized_msg = dict(msg)
        if sanitized_msg.get("content") is None:
            sanitized_msg["content"] = ""
        sanitized_messages.append(sanitized_msg)
    messages_with_compaction = sanitized_messages + [{"role": "user", "content": compaction_prompt}]
    full_messages = [{"role": "system", "content": system_prompt or ""}] + messages_with_compaction

    logger.info(
        f"{log_prefix} Triggering context compaction (context at {current_context_tokens:,} tokens, threshold {COMPACTION_THRESHOLD_TOKENS:,})"
    )

    # Call LLM for compaction - keep the same tools so context size matches
    response = _call_with_retry(
        client,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=full_messages,
        tools=tools,
        log_prefix=f"{log_prefix}[compaction]",
    )

    choice = response.choices[0]
    message = choice.message

    # Handle case where LLM returns tool calls instead of summary text
    # This shouldn't happen with a good compaction prompt, but be defensive
    if message.tool_calls:
        logger.warning(f"{log_prefix} Compaction LLM returned tool calls instead of summary, using empty summary")
        summary_text = message.content or "[Compaction failed - LLM returned tool calls instead of summary]"
    else:
        summary_text = message.content or ""

    # Extract token usage
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0 if usage else 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0 if usage else 0

    logger.info(
        f"{log_prefix} Compaction complete. Summary length: {len(summary_text)} chars (~{_estimate_tokens(summary_text)} tokens)"
    )

    # Respawn: Create new message list with just the summary as a user message
    new_messages = [
        {
            "role": "user",
            "content": f"[COMPACTED CONTEXT - Previous conversation was summarized due to length]\n\n{summary_text}\n\n---\n\nPlease continue with the remaining tasks based on this summary.",
        }
    ]

    return new_messages, input_tokens, output_tokens, summary_text


# =============================================================================
# Error Handling Helpers
# =============================================================================

# Truncation limits for different error types
LLM_ERROR_MAX_LEN = 100  # LLM API errors (often contain huge request bodies)
DEFAULT_ERROR_MAX_LEN = 5000  # Other errors (tool failures, etc.)


def truncate_error(error_msg: str | None, max_len: int = DEFAULT_ERROR_MAX_LEN) -> str:
    """Truncate error message for logging.

    Use max_len=LLM_ERROR_MAX_LEN for LLM API errors (they often contain huge request bodies).
    Use default (5000) for other errors like tool failures.

    Handles None gracefully by returning "<no error message>".
    """
    if error_msg is None:
        return "<no error message>"
    if not isinstance(error_msg, str):
        error_msg = str(error_msg)
    if len(error_msg) > max_len:
        return error_msg[:max_len] + f"... [{len(error_msg)} chars]"
    return error_msg


# =============================================================================
# Tool Loop Result Types
# =============================================================================


@dataclass
class ToolCallRecord:
    """Record of a single tool invocation."""

    name: str
    arguments: dict[str, Any]
    result: str
    duration_seconds: float = 0.0


@dataclass
class ToolLoopResult:
    """Result from running a tool loop.

    This is the rich return type from run_tool_loop() containing all
    information about what happened during execution.
    """

    final_text: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    iteration_count: int = 0
    stop_reason: str = "unknown"  # "stop", "no_tool_calls", "max_iterations", "custom_stop"
    # Token usage tracking (from API response)
    total_input_tokens: int = 0  # Cumulative prompt tokens across all iterations
    total_output_tokens: int = 0  # Cumulative completion tokens across all iterations
    # Context compaction tracking
    compaction_count: int = 0  # Number of times context was compacted


# =============================================================================
# LLM Call with Retry
# =============================================================================


def _call_with_retry(
    client: "OpenAI",
    model: str,
    max_tokens: int,
    temperature: float,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    log_prefix: str = "",
    max_retries: int = 100,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> Any:
    """Call the OpenAI API with exponential backoff retry for rate limits.

    This is THE retry implementation used by all agents.
    """
    if not messages:
        raise ValueError(f"{log_prefix} Cannot call API with null/empty messages list")

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools

    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except (RateLimitError, APIConnectionError, InternalServerError) as e:
            last_exception = e
            if attempt == max_retries:
                error_type = type(e).__name__
                logger.error(f"{log_prefix} {error_type} after {max_retries} retries")
                raise

            delay = min(base_delay * (2**attempt), max_delay)
            error_str = str(e)
            error_preview = error_str[:300] if len(error_str) > 300 else error_str

            if isinstance(e, RateLimitError):
                logger.warning(
                    f"{log_prefix} Rate limited (429), retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})"
                )
            elif isinstance(e, InternalServerError):
                logger.warning(
                    f"{log_prefix} Server error (500), retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries}): {error_preview}"
                )
            else:
                logger.warning(
                    f"{log_prefix} Connection error, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries}): {error_preview}"
                )
            time_module.sleep(delay)

    raise last_exception  # Should never reach here


# =============================================================================
# Core Tool Loop - THE implementation used by all agents
# =============================================================================


def run_tool_loop(
    client: "OpenAI",
    model: str,
    system_prompt: str,
    initial_messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_handler: Callable[[str, dict[str, Any]], str],
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_tokens: int = 16384,
    temperature: float = 0.0,
    should_stop: Callable[[str], bool] | None = None,
    recorder: "AgentRecorder | None" = None,
    log_prefix: str = "",
    enable_compaction: bool = True,
    compaction_threshold: int = COMPACTION_THRESHOLD_TOKENS,
) -> ToolLoopResult:
    """Core tool loop - single implementation used by all agents.

    This is THE loop. BaseAgent, reviewers, and any other agent all use this.

    Recording contract:
    - Records initial user message(s) from initial_messages
    - Records each assistant response
    - Records each tool call and result
    - Does NOT call recorder.done() - that's the caller's responsibility

    Context Compaction:
    - When enable_compaction=True (default), monitors token usage
    - If estimated context exceeds compaction_threshold (default 150k), triggers compaction
    - Compaction asks the LLM to summarize everything needed to continue
    - This allows long-running agents to work beyond context limits

    Args:
        client: OpenAI-compatible client
        model: Model name to use
        system_prompt: System prompt for the LLM
        initial_messages: Initial messages (typically [{"role": "user", "content": "..."}])
        tools: List of tool definitions (or None for no tools)
        tool_handler: Function to handle tool calls: (name, args) -> result
        max_iterations: Maximum number of tool call iterations
        max_tokens: Max tokens for LLM response
        temperature: Temperature for LLM
        should_stop: Optional custom stop condition: (text) -> bool
        recorder: Optional recorder for logging
        log_prefix: Prefix for log messages
        enable_compaction: Enable automatic context compaction (default True)
        compaction_threshold: Token threshold for triggering compaction (default 150k)

      Returns:
        ToolLoopResult with final_text, messages, tool_calls, iteration_count, stop_reason
    """
    messages = list(initial_messages)
    all_tool_calls: list[ToolCallRecord] = []
    final_text = ""
    stop_reason = "unknown"
    iteration = 0

    # Token tracking (populated from API response usage)
    total_input_tokens = 0
    total_output_tokens = 0

    # Track last iteration's token counts for compaction check
    # Next input ≈ last_input + last_output + tool_results
    last_iteration_input_tokens = 0
    last_iteration_output_tokens = 0

    # Track compaction events
    compaction_count = 0

    # Record initial user message(s)
    if recorder:
        for msg in initial_messages:
            if msg.get("role") == "user":
                recorder.record("user", msg.get("content", ""))

    for iteration in range(max_iterations):
        logger.debug(f"{log_prefix} Tool loop iteration {iteration + 1}/{max_iterations}")

        # Check for context compaction need BEFORE the LLM call
        # Next input ≈ last_input + last_output + new_tool_results
        if enable_compaction and last_iteration_input_tokens > 0:
            tool_results_estimate = _estimate_tool_results_tokens(messages)
            estimated_next_input = last_iteration_input_tokens + last_iteration_output_tokens + tool_results_estimate

            if estimated_next_input > compaction_threshold:
                logger.warning(
                    f"{log_prefix} Estimated next context ({estimated_next_input:,} tokens = "
                    f"{last_iteration_input_tokens:,} prev_input + {last_iteration_output_tokens:,} prev_output + "
                    f"{tool_results_estimate:,} tool_results) exceeds threshold ({compaction_threshold:,}). "
                    "Triggering compaction."
                )

                # Perform compaction
                messages, compaction_input, compaction_output, summary_text = _perform_compaction(
                    client=client,
                    model=model,
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    log_prefix=log_prefix,
                    current_context_tokens=estimated_next_input,
                )

                # Track compaction token usage
                total_input_tokens += compaction_input
                total_output_tokens += compaction_output
                compaction_count += 1

                # Reset last iteration tokens since we compacted
                last_iteration_input_tokens = 0
                last_iteration_output_tokens = 0

                # Estimate new context size
                new_context_estimate = _estimate_tokens(system_prompt)
                for msg in messages:
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        new_context_estimate += _estimate_tokens(content)

                # Record compaction event
                if recorder:
                    recorder.record_compaction(
                        compaction_number=compaction_count,
                        context_tokens_before=estimated_next_input,
                        context_tokens_after=new_context_estimate,
                        input_tokens=compaction_input,
                        output_tokens=compaction_output,
                        summary=summary_text,
                    )

                logger.info(
                    f"{log_prefix} Compaction #{compaction_count} complete. "
                    f"Context: {estimated_next_input:,} -> ~{new_context_estimate:,} tokens"
                )

        # Call LLM with retry
        # Ensure no None values in messages (some APIs like AWS Bedrock reject null content)
        sanitized_messages = []
        for msg in messages:
            sanitized_msg = dict(msg)
            if sanitized_msg.get("content") is None:
                sanitized_msg["content"] = ""
            sanitized_messages.append(sanitized_msg)
        full_messages = [{"role": "system", "content": system_prompt or ""}] + sanitized_messages

        response = _call_with_retry(
            client,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=full_messages,
            tools=tools,
            log_prefix=log_prefix,
        )

        choice = response.choices[0]
        message = choice.message
        text = message.content or ""
        final_text = text
        finish_reason = choice.finish_reason or ""

        # Extract token usage from API response
        usage = getattr(response, "usage", None)
        if usage:
            iteration_input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            iteration_output_tokens = getattr(usage, "completion_tokens", 0) or 0
            total_input_tokens += iteration_input_tokens
            total_output_tokens += iteration_output_tokens
            # Store for next iteration's compaction check
            last_iteration_input_tokens = iteration_input_tokens
            last_iteration_output_tokens = iteration_output_tokens

        # Build assistant message for history (OpenAI format)
        assistant_message: dict[str, Any] = {"role": "assistant", "content": text}

        tool_calls_in_response = []
        if message.tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in message.tool_calls
            ]
            tool_calls_in_response = [(tc.id, tc.function.name, tc.function.arguments) for tc in message.tool_calls]

        messages.append(assistant_message)

        # Record assistant message
        if recorder:
            recorded_tool_calls = None
            if tool_calls_in_response:
                recorded_tool_calls = []
                for tc_id, tc_name, tc_args_str in tool_calls_in_response:
                    try:
                        tc_args = json.loads(tc_args_str)
                    except json.JSONDecodeError:
                        tc_args = {}
                    recorded_tool_calls.append({"name": tc_name, "args": tc_args})
            recorder.record(
                "assistant",
                text,
                recorded_tool_calls,
                iteration_input_tokens if usage else None,
                iteration_output_tokens if usage else None,
            )

        # Check stop conditions
        if finish_reason == "stop":
            stop_reason = "stop"
            break

        if should_stop and should_stop(text):
            stop_reason = "custom_stop"
            break

        # Handle tool calls
        if not message.tool_calls:
            stop_reason = "no_tool_calls"
            break

        for tc in message.tool_calls:
            tc_name = tc.function.name
            try:
                tc_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                tc_args = {}

            logger.info(f"{log_prefix} Tool call: {tc_name}")
            start = time()
            try:
                result = tool_handler(tc_name, tc_args)
            except Exception as e:
                error_msg = str(e)
                result = f"Error: {error_msg}"
                logger.exception(f"{log_prefix} Tool {tc_name} failed: {truncate_error(error_msg)}")
            duration = time() - start

            # Guard against tool handlers returning None
            if result is None:
                result = ""

            # Truncate result to manage context size
            MAX_DIALOG_RESULT_LEN = 10000
            if len(result) > MAX_DIALOG_RESULT_LEN:
                result = result[:MAX_DIALOG_RESULT_LEN] + f"\n\n... [truncated, {len(result)} chars total]"

            # Track tool call (with truncated preview for display)
            all_tool_calls.append(
                ToolCallRecord(
                    name=tc_name,
                    arguments=tc_args,
                    result=result[:500] if len(result) > 500 else result,
                    duration_seconds=duration,
                )
            )

            # Add tool result to messages (OpenAI format) - use truncated version for LLM
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

            # Record tool call - use FULL result for debugging/viewer
            if recorder:
                recorder.record_tool(
                    tc_name,
                    tc_args,
                    result,
                    duration * 1000,
                )

        # Increment iteration in recorder
        if recorder:
            recorder.increment_iteration()

    else:
        stop_reason = "max_iterations"

    # Log final token summary
    compaction_msg = f", compactions={compaction_count}" if compaction_count > 0 else ""
    logger.info(
        f"{log_prefix} Token summary: total_input={total_input_tokens:,} total_output={total_output_tokens:,}{compaction_msg}"
    )

    return ToolLoopResult(
        final_text=final_text,
        messages=messages,
        tool_calls=all_tool_calls,
        iteration_count=iteration + 1,
        stop_reason=stop_reason,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        compaction_count=compaction_count,
    )
