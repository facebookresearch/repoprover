# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Core types for the multi-file autoformalization system.

Minimal types needed for the PR workflow:
- PRStatus: Status of a PR in its lifecycle
- Review: Result of math or engineering review
- ReviewComment: A specific comment on code
- Enums: ReviewType, ReviewVerdict, AgentType
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


def _utcnow() -> datetime:
    """Return current UTC time (timezone-aware)."""
    return datetime.now(tz=timezone.utc)


# =============================================================================
# Enums
# =============================================================================


class PRStatus(StrEnum):
    """Status of a PR in its lifecycle."""

    PENDING_REVIEW = "pending_review"
    REVIEW_IN_PROGRESS = "review_in_progress"
    NEEDS_REVISION = "needs_revision"
    REVISION_IN_PROGRESS = "revision_in_progress"
    APPROVED = "approved"
    MERGED = "merged"
    FAILED = "failed"


class ReviewType(StrEnum):
    """Type of review being performed."""

    MATH = "math"  # Mathematical correctness, proof validity
    ENGINEERING = "engineering"  # Code quality, style, compilation


class ReviewVerdict(StrEnum):
    """Verdict from a review."""

    APPROVE = "approve"  # PR is good to merge
    REQUEST_CHANGES = "request_changes"  # Agent should revise
    REJECT = "reject"  # PR should not be merged
    ABSTAIN = "abstain"  # Reviewer cannot decide (escalate)


class AgentType(StrEnum):
    """Type of agent in the system."""

    # Unified contributor modes
    CONTRIBUTOR = "contributor"  # Unified agent (use mode to distinguish)
    SKETCH = "sketch"  # Create initial file structure
    PROVE = "prove"  # Prove a specific theorem
    MAINTAIN = "maintain"  # Work on issues
    SCAN = "scan"  # Find and create issues
    TRIAGE = "triage"  # Close resolved issues
    FIX = "fix"  # Agent proposing infrastructure fixes
    PROGRESS = "progress"  # Check target theorem status and blockers


# =============================================================================
# Review
# =============================================================================


@dataclass
class ReviewComment:
    """A comment on a specific location in the code."""

    file_path: str
    line_start: int
    line_end: int
    message: str
    severity: str = "info"  # info, warning, error


@dataclass
class Review:
    """A review of a pull request.

    Reviews are either math (mathematical correctness) or engineering
    (code quality, compilation). Both must approve for a PR to merge.
    """

    review_id: str
    pr_id: str
    review_type: ReviewType
    reviewer_agent_id: str
    verdict: ReviewVerdict
    summary: str
    comments: list[ReviewComment] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)
    reasoning: str = ""  # Internal reasoning (for logging/debugging)


# =============================================================================
# Review Context (for review_pr interface)
# =============================================================================


@dataclass
class ReviewContext:
    """Context passed to reviewers for evaluating a PR.

    This is a read-only view combining PR identity with review-specific data.
    The actual PR state is tracked in SimplePR in the coordinator.
    """

    pr_id: str
    branch_name: str
    agent_type: AgentType  # Type of agent that created this PR
    agent_id: str  # ID of the agent that created this PR
    chapter_id: str
    title: str
    files_changed: list[str]
    source_content: str = ""  # LaTeX source for cross-checking
    description: str = ""  # PR description from contributor
    revision_number: int = 0  # Which revision this is (0 = initial)
    previous_review_feedback: str = ""  # Feedback from previous review (for revisions)
