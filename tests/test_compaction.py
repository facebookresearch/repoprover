# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

#!/usr/bin/env python3
"""Test script for context compaction in the LLM tool loop.

This script tests whether the LLM can preserve important information
through context compaction by:
1. Giving it a task to collect 100 secret code parts
2. Each tool call returns a secret part buried in ~20k tokens of junk
3. After compaction(s), we verify if the LLM remembered all parts

Usage:
    python -m repoprover.tests.test_compaction

Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY environment variable.
"""

import os
import random
import sys

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from repoprover.agents.tools import (
    COMPACTION_THRESHOLD_TOKENS,
    MAX_CONTEXT_TOKENS,
    _estimate_tokens,
    run_tool_loop,
)
from repoprover.agents.base import create_client, AgentConfig


# Generate deterministic secret parts for verification
NUM_SECRET_PARTS = 10  # 10 secrets
SECRET_PARTS = [f"SECRET_{i:02d}_{random.Random(42 + i).randint(1000, 9999)}" for i in range(NUM_SECRET_PARTS)]


def create_junk_payload(size_tokens: int, seed: int) -> str:
    """Create realistic-looking junk text of approximately the specified token size."""
    rng = random.Random(seed)
    chars_needed = size_tokens * 4

    # Use varied patterns to look more realistic
    patterns = [
        "Processing data chunk with metadata fields and configuration options... ",
        "Loading module dependencies from cache storage subsystem... ",
        "Initializing connection pool with default timeout parameters... ",
        "Validating input schema against registered type definitions... ",
        "Executing query plan with optimized index scan strategy... ",
        "Serializing response payload to wire format encoding... ",
        "Dispatching event handlers for registered callbacks... ",
        "Allocating buffer space for incoming request stream... ",
    ]

    parts = []
    while len("".join(parts)) < chars_needed:
        parts.append(rng.choice(patterns))
        # Add some random hex data occasionally
        if rng.random() < 0.3:
            parts.append("".join(rng.choices("0123456789abcdef", k=64)) + " ")

    return "".join(parts)[:chars_needed]


def main():
    print("=" * 70)
    print("Context Compaction Semantic Test")
    print("=" * 70)
    print(f"Max context: {MAX_CONTEXT_TOKENS:,} tokens")
    print(f"Compaction threshold: {COMPACTION_THRESHOLD_TOKENS:,} tokens")
    print(f"Number of secret parts to collect: {NUM_SECRET_PARTS}")
    print(f"Expected secrets: {SECRET_PARTS[:3]} ... {SECRET_PARTS[-1]}")
    print()

    # Create a config - try different providers
    config = None
    for provider in ["anthropic", "openai", "google"]:
        try:
            config = AgentConfig(provider=provider)
            client = create_client(config)
            print(f"Using provider: {provider}, model: {config.model}")
            break
        except ValueError:
            continue

    if config is None:
        print("ERROR: No API key found. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY")
        sys.exit(1)

    system_prompt = f"""You are an assistant helping to collect secret codes.

Your task is to collect ALL {NUM_SECRET_PARTS} secret code parts by calling the retrieve_secret tool
for each index from 0 to {NUM_SECRET_PARTS - 1}.

CRITICAL INSTRUCTIONS:
1. Call retrieve_secret ONE AT A TIME: first index=0, wait for result, then index=1, etc.
2. DO NOT batch multiple retrieve_secret calls - call them ONE BY ONE
3. Each response contains ONE secret code part - you MUST remember ALL of them
4. The secret codes look like: SECRET_XX_YYYY (e.g., SECRET_00_1234)
5. After collecting ALL {NUM_SECRET_PARTS} parts, call report_secrets with the complete list

If you receive a CONTEXT COMPACTION message:
- You MUST include ALL secret codes collected so far in your summary
- List them explicitly: SECRET_00_XXXX, SECRET_01_XXXX, etc.
- This is CRITICAL - losing any secret code is a test failure

After collecting all {NUM_SECRET_PARTS} secrets, call report_secrets with ALL of them.
"""

    user_prompt = f"""Please collect all {NUM_SECRET_PARTS} secret code parts.

Start by calling retrieve_secret(index=0) and continue until you have all {NUM_SECRET_PARTS} parts.
Each secret looks like SECRET_XX_YYYY where XX is the index.

Remember: You MUST preserve ALL secrets through any context compaction events.
When done, call report_secrets with the complete list of all secrets.

Begin now with retrieve_secret(index=0).
"""

    print(f"Initial prompt size: ~{_estimate_tokens(user_prompt):,} tokens")
    print()

    # Define tools
    tools = [
        {
            "type": "function",
            "function": {
                "name": "retrieve_secret",
                "description": "Retrieves a secret code part by index. Returns the secret buried in diagnostic data.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": f"The index of the secret to retrieve (0 to {NUM_SECRET_PARTS - 1})",
                        }
                    },
                    "required": ["index"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "report_secrets",
                "description": "Report all collected secrets. Call this after collecting all secret parts.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "secrets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of all collected secret codes (e.g., ['SECRET_00_1234', 'SECRET_01_5678', ...])",
                        }
                    },
                    "required": ["secrets"],
                },
            },
        },
    ]

    # Track state
    retrieved_indices: set[int] = set()
    reported_secrets: list[str] = []
    test_complete = False

    def handle_tool_call(name: str, args: dict) -> str:
        nonlocal test_complete, reported_secrets

        if name == "retrieve_secret":
            index = args.get("index", 0)
            if index < 0 or index >= NUM_SECRET_PARTS:
                return f"Error: Index {index} out of range. Valid range: 0 to {NUM_SECRET_PARTS - 1}"

            retrieved_indices.add(index)
            secret = SECRET_PARTS[index]

            # Generate junk payload - sized so ~2 retrievals trigger compaction
            # 150k threshold / 2 retrievals = ~75k per retrieval = ~18k tokens per half
            junk_size = 35_000  # ~35k tokens total junk per retrieval
            junk_before = create_junk_payload(junk_size // 2, seed=index * 1000)
            junk_after = create_junk_payload(junk_size // 2, seed=index * 1000 + 500)

            print(f"  retrieve_secret(index={index}) -> {secret} (with ~{junk_size:,} tokens of junk)")

            return f"""=== DIAGNOSTIC OUTPUT START ===
{junk_before}
=== SECRET CODE PART {index:02d} ===
>>> {secret} <<<
=== END SECRET CODE PART ===
{junk_after}
=== DIAGNOSTIC OUTPUT END ===

Successfully retrieved secret part {index + 1}/{NUM_SECRET_PARTS}.
The secret code is: {secret}
{"Continue with retrieve_secret(index=" + str(index + 1) + ")" if index < NUM_SECRET_PARTS - 1 else "All parts collected! Now call report_secrets with ALL secrets."}
"""

        elif name == "report_secrets":
            secrets = args.get("secrets", [])
            reported_secrets = secrets
            test_complete = True

            # Verify results
            correct = set(SECRET_PARTS)
            reported = set(secrets)
            missing = correct - reported
            extra = reported - correct

            print(f"\n  report_secrets called with {len(secrets)} secrets")

            result = f"Received {len(secrets)} secrets.\n"
            if missing:
                result += f"MISSING {len(missing)} secrets: {sorted(missing)}\n"
            if extra:
                result += f"EXTRA {len(extra)} unexpected: {sorted(extra)}\n"
            if not missing and not extra:
                result += "ALL SECRETS CORRECT! Test passed.\n"

            return result

        return f"Unknown tool: {name}"

    def should_stop(_text: str) -> bool:
        return test_complete

    print("Starting secret collection...")
    print("-" * 70)

    try:
        result = run_tool_loop(
            client=client,
            model=config.model,
            system_prompt=system_prompt,
            initial_messages=[{"role": "user", "content": user_prompt}],
            tools=tools,
            tool_handler=handle_tool_call,
            max_iterations=100,  # Enough for all retrievals + some buffer
            max_tokens=4096,
            temperature=0.3,  # Lower temperature for more deterministic behavior
            should_stop=should_stop,
            log_prefix="[test]",
            enable_compaction=True,
            compaction_threshold=COMPACTION_THRESHOLD_TOKENS,
        )

        print("-" * 70)

        # Print assistant messages (non-tool-call text)
        print("\n--- LLM Responses ---")
        for i, msg in enumerate(result.messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if content:
                    # Truncate long responses
                    if len(content) > 500:
                        content = content[:500] + f"... [{len(content)} chars]"
                    print(f"\n[Assistant #{i}]: {content}")
        print("--- End LLM Responses ---\n")

        print("=" * 70)
        print("TEST RESULTS")
        print("=" * 70)

        print("\nExecution Stats:")
        print(f"  Iterations: {result.iteration_count}")
        print(f"  Tool calls: {len(result.tool_calls)}")
        print(f"  Total input tokens: {result.total_input_tokens:,}")
        print(f"  Total output tokens: {result.total_output_tokens:,}")
        print(f"  Compaction count: {result.compaction_count}")
        print(f"  Stop reason: {result.stop_reason}")

        print("\nRetrieval Stats:")
        print(f"  Indices retrieved: {len(retrieved_indices)}/{NUM_SECRET_PARTS}")
        if len(retrieved_indices) < NUM_SECRET_PARTS:
            missing_indices = set(range(NUM_SECRET_PARTS)) - retrieved_indices
            print(f"  Missing indices: {sorted(missing_indices)}")

        print("\nSecret Verification:")
        correct_secrets = set(SECRET_PARTS)
        reported_set = set(reported_secrets)

        correct_count = len(correct_secrets & reported_set)
        missing_secrets = correct_secrets - reported_set
        extra_secrets = reported_set - correct_secrets

        print(f"  Expected: {NUM_SECRET_PARTS} secrets")
        print(f"  Reported: {len(reported_secrets)} secrets")
        print(f"  Correct: {correct_count}/{NUM_SECRET_PARTS}")

        if missing_secrets:
            print(
                f"  MISSING ({len(missing_secrets)}): {sorted(missing_secrets)[:5]}{'...' if len(missing_secrets) > 5 else ''}"
            )
        if extra_secrets:
            print(
                f"  EXTRA ({len(extra_secrets)}): {sorted(extra_secrets)[:5]}{'...' if len(extra_secrets) > 5 else ''}"
            )

        # Final verdict
        print("\n" + "=" * 70)
        if result.compaction_count > 0:
            print(f"✓ Context compaction triggered {result.compaction_count} time(s)")
        else:
            print("✗ Context compaction was NOT triggered")
            print("  (Try increasing NUM_SECRET_PARTS or junk_size)")

        if correct_count == NUM_SECRET_PARTS and not extra_secrets:
            print("✓ ALL SECRETS PRESERVED - Compaction worked correctly!")
            success = True
        else:
            retention_rate = correct_count / NUM_SECRET_PARTS * 100
            print(f"✗ SECRET RETENTION: {retention_rate:.1f}% ({correct_count}/{NUM_SECRET_PARTS})")
            if result.compaction_count > 0:
                print("  Compaction may have lost important information")
            success = False
        print("=" * 70)

        sys.exit(0 if success else 1)

    except Exception as e:
        error_str = str(e)
        print(f"\nError: {error_str[:500]}")
        if len(error_str) > 500:
            print(f"  ... ({len(error_str)} chars total)")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
