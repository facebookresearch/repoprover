# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Review agents for math and engineering review.

Two independent reviewer agents:
1. MathReviewer - Reviews mathematical correctness and proof validity
2. EngineeringReviewer - Reviews code quality, style, and compilation

Both inherit from BaseReviewer which inherits from BaseAgent, giving them:
- Shared tool loop via run()
- Read-only tools via mixins (FileReadToolsMixin, MathlibToolsMixin, LeanToolsMixin)

The review_pr() function coordinates:
1. Run lake build once (auto-reject if fails)
2. Call both reviewers in parallel
3. Return combined result (AND-success)
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING

from ..build import lake_build
from ..types import (
    Review,
    ReviewComment,
    ReviewContext,
    ReviewType,
    ReviewVerdict,
)
from .base import AgentConfig, BaseAgent
from .file_tools import FileReadToolsMixin
from .lean_tools import LeanToolsMixin
from .mathlib_tools import MathlibToolsMixin
from .tools import LLM_ERROR_MAX_LEN, truncate_error

if TYPE_CHECKING:
    from ..recording import AgentRecorder, SessionRecorder

logger = getLogger(__name__)


# =============================================================================
# Base Reviewer (inherits from BaseAgent)
# =============================================================================


class BaseReviewer(FileReadToolsMixin, MathlibToolsMixin, LeanToolsMixin, BaseAgent):
    """Base class for all reviewers.

    Inherits from BaseAgent to get the shared tool loop.
    Uses read-only tool mixins: FileReadToolsMixin, MathlibToolsMixin, LeanToolsMixin.

    Tools are registered automatically via the mixin's register_tools chain.

    Subclasses must implement:
    - _build_system_prompt(pr) -> str
    - _build_review_prompt(pr, diff, files) -> str
    - review_type: ReviewType (class attribute)
    """

    agent_type: str = "reviewer"
    review_type: ReviewType = ReviewType.MATH  # Override in subclass

    def __init__(
        self,
        reviewer_id: str,
        config: AgentConfig | None = None,
        recorder: "AgentRecorder | None" = None,
        worktree_path: Path | None = None,
    ):
        # Initialize BaseAgent with repo_root from worktree_path
        super().__init__(
            config=config,
            repo_root=worktree_path,
            recorder=recorder,
        )
        self.reviewer_id = reviewer_id
        self.worktree_path = worktree_path
        # For current review context (set by review())
        self._current_pr: ReviewContext | None = None
        self._current_diff: str = ""
        self._current_files: dict[str, str] = {}

    @property
    def log_prefix(self) -> str:
        return f"[{self.reviewer_id}]"

    # -------------------------------------------------------------------------
    # Abstract methods for subclasses
    # -------------------------------------------------------------------------

    def _build_system_prompt(self, pr: ReviewContext) -> str:
        """Build the system prompt for this reviewer. Override in subclass."""
        raise NotImplementedError

    def _build_review_prompt(self, pr: ReviewContext, diff: str, files: dict[str, str]) -> str:
        """Build the review prompt. Override in subclass."""
        raise NotImplementedError

    # -------------------------------------------------------------------------
    # BaseAgent interface implementation
    # -------------------------------------------------------------------------

    def get_system_prompt(self) -> str:
        """BaseAgent interface - uses current PR context."""
        if self._current_pr is None:
            raise RuntimeError("No PR context set. Call review() instead of run() directly.")
        return self._build_system_prompt(self._current_pr)

    def build_user_prompt(self, **kwargs) -> str:
        """BaseAgent interface - uses current PR context."""
        if self._current_pr is None:
            raise RuntimeError("No PR context set. Call review() instead of run() directly.")
        return self._build_review_prompt(self._current_pr, self._current_diff, self._current_files)

    # -------------------------------------------------------------------------
    # Main review method
    # -------------------------------------------------------------------------

    def review(self, pr: ReviewContext, diff: str, files: dict[str, str]) -> Review:
        """Perform review using the inherited run() method."""
        # Store context for get_system_prompt/build_user_prompt
        self._current_pr = pr
        self._current_diff = diff
        self._current_files = files

        try:
            # Use inherited run() which handles tool loop, recording, etc.
            result = self.run()

            # Parse the final text into Review
            final_text = ""
            if result.dialog:
                # Get last assistant message
                for msg in reversed(result.dialog):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        final_text = msg["content"]
                        break

            verdict, summary, comments = _parse_review_response(final_text)

            return Review(
                review_id=f"review-{uuid.uuid4().hex[:12]}",
                pr_id=pr.pr_id,
                review_type=self.review_type,
                reviewer_agent_id=self.reviewer_id,
                verdict=verdict,
                summary=summary,
                comments=comments,
                reasoning=final_text,
            )

        except Exception as e:
            error_msg = str(e)
            logger.exception(f"Review failed: {truncate_error(error_msg, LLM_ERROR_MAX_LEN)}")
            return Review(
                review_id=f"review-{uuid.uuid4().hex[:12]}",
                pr_id=pr.pr_id,
                review_type=self.review_type,
                reviewer_agent_id=self.reviewer_id,
                verdict=ReviewVerdict.ABSTAIN,
                summary=f"Review failed: {e}",
                comments=[],
                reasoning=str(e),
            )

        finally:
            # Clear context
            self._current_pr = None
            self._current_diff = ""
            self._current_files = {}


# =============================================================================
# Math Reviewer
# =============================================================================


class MathReviewer(BaseReviewer):
    """Reviews mathematical correctness, adapting criteria to agent type.

    For sketchers: focuses on statement accuracy, type correctness, Mathlib alignment.
    For provers: focuses on proof progress, correctness, and structural suggestions.
    """

    agent_type = "MathReviewer"
    review_type = ReviewType.MATH

    def _build_system_prompt(self, pr: ReviewContext) -> str:
        base = """You are a mathematical reviewer for Lean 4 formalization projects.

Your job is to review pull requests for mathematical correctness and faithfulness to source material.

## Tools Available

You have access to these tools for verification:

### File Tools (Read-Only)
- `file_read(path, offset?, limit?)` - Read file content with line numbers
- `file_list(path?)` - List directory contents
- `file_grep(path, pattern, context_lines?)` - Search for patterns in files

### Mathlib Tools
- `mathlib_grep(pattern, kind?, subdir?, max_results?, context_lines?, literal?)` - Search Mathlib source
- `mathlib_find_name(name, exact?, max_results?)` - Find declarations by name
- `mathlib_read_file(file_path, start_line?, end_line?)` - Read Mathlib source files

### Lean Tools
- `lean_check(code)` - Check Lean code snippets for errors

Use these tools to verify claims in the PR (e.g., check if a theorem exists in Mathlib,
verify file structure, test code snippets).

"""
        if pr.agent_type.value == "sketch":
            base += """This PR is from a SKETCHER agent. Sketchers translate LaTeX mathematics into Lean 4
statement-level formalizations. They are expected to use `sorry` for all non-trivial proofs.

## Review Checklist

### Definitions
- Matches mathematical meaning from source
- Uses appropriate Mathlib types (not reinventing existing ones)
- Bundled vs unbundled choice is appropriate
- Naming follows Mathlib conventions
- **Definition equivalence**: Any equivalent formulation may be taken as the PRIMARY definition, provided (1) the equivalence to the source's formulation is proved as a theorem, and (2) docstrings note any discrepancy and reference the equivalence theorem

### Theorem Statements
- Statement is faithful to source theorem
- All hypotheses are explicit (no hidden assumptions)
- Types are appropriate (not too general or too specific)
- **No placeholder conclusions**: Theorems must NOT have placeholder conclusions like `True` — flag as **critical**
- **No tautological theorems**: If the conclusion `P` appears verbatim as a hypothesis `(h : P)`, the theorem proves nothing — flag as **critical**

### `[cited]` and `[exercise]` Correctness

Theorems marked `sorry -- [cited]` have proofs delegated to external sources. Theorems marked `sorry -- [exercise]` are exercises without solutions or results left to the reader.

Rules for `[cited]`:
- Valid only if the source does NOT provide the proof and no tex file for the cited reference is available in `tex/`
- If the source provides proof steps (even a sketch), it must NOT be `[cited]`
- Consequences derived using a cited theorem must be proved, not cited
- Pay attention to theorems the paper proves while noting they are known results — these must NOT be `[cited]`

Rules for `[exercise]`:
- Valid only if the exercise has NO solution in the source
- If a solution is provided, the theorem should be plain `sorry` (to be proved using the solution)

Flag misuse of either marker as **critical**.

### Theorem Completeness
- **ALL named results** from the source must be formalized: theorems, propositions, lemmas, corollaries
- **Logical dependency order**: If Theorem A uses Lemma B, then B must appear before A in the file — flag forward dependencies as **critical**
  - Theorems should be placed where they are PROVED in the source, not where first stated (papers often state a main result early but prove it later)
- **Key applications included**: if the source proof reveals an obvious way of applying a theorem, include that as a separate lemma

### Implicit Intermediate Results
Papers often embed **critical results in prose** — not labeled as Lemma/Theorem but cited by later proofs:
- **Numbered equations cited by theorems**: If the source says "By Theorem X and (n), we have Y", then equation (n) MUST have a corresponding Lean theorem
- Flag missing implicit results as **critical** if a theorem cites an equation number that has no corresponding Lean statement

### API Lemmas
- Are basic API lemmas (`@[simp]`, extensionality, coercions) provided for new definitions?

### Code Quality
- Compiles without errors (sorries are OK)
- Imports are correct
- Structure is logical (definitions → API lemmas → theorems)

## Verdict Policy
`sorry` is EXPECTED and ACCEPTABLE in sketcher PRs. Do NOT reject for `sorry` usage.

Note: APPROVE will immediately merge the PR. If you have any feedback you want addressed (even minor), use REQUEST_CHANGES instead.

APPROVE if the statements are mathematically faithful, complete, and well-structured.
REQUEST_CHANGES for: wrong statements, missing theorems, `[cited]`/`[exercise]` misuse, placeholder/tautological theorems, forward dependencies, or poor structure.

## Severity Levels
- **critical**: Must fix (wrong statements, missing theorems, `[cited]`/`[exercise]` misuse, placeholder conclusions, forward dependencies)
- **warning**: Should fix (suboptimal types, missing API lemmas, naming issues)
- **suggestion**: Nice to have (style improvements, documentation)
"""
        elif pr.agent_type.value == "fix":
            base += """This PR is from a PROVER agent proposing a FIX to the codebase.

The prover identified a specific issue preventing the proof and has directly applied changes to fix it.
This could include: adding missing intermediate theorems, fixing theorem statements, reordering for dependencies.

**Note**: FIX PRs may also include issue file changes (in `issues/` folder) alongside Lean code changes.
This is normal — provers document their analysis in issues while also formalizing what they can in code.

## Your Role
You are evaluating **whether the fix is mathematically correct**, NOT whether proofs are complete.
`sorry` is expected in fix PRs — the changes add infrastructure that will be proved later.

## Review Checklist

### Mathematical Correctness of New/Changed Statements
- Are the new theorem statements mathematically valid?
- Do they faithfully represent results from the source material?
- Are the types and hypotheses correct?

### Fix Validity
- Does the fix address the identified problem?
- Is the proposed change logically coherent?
- Are dependencies now correctly ordered (no forward references)?

### Source Fidelity
- If new theorems are added, are they actually in the source material?
- For numbered equations/results: does equation (n) in the source actually say what the new theorem claims?
- Are `-- [cited]` and `-- [exercise]` markers used correctly?

### Issue File Changes (if present)
- If issues were created or updated, are the descriptions accurate?
- Does the issue documentation match what's actually in the code?

### No Regressions
- Did the changes preserve existing correct statements?
- Are there any unnecessary modifications?

## Verdict Policy
`sorry` is EXPECTED. This PR adds infrastructure, not proofs.

Note: APPROVE will immediately merge the PR. If you have any feedback you want addressed (even minor), use REQUEST_CHANGES instead.

APPROVE if the fix is mathematically sound and addresses the identified issue.
REQUEST_CHANGES if: statements are mathematically wrong, fix doesn't address the underlying issue,
source material doesn't support the claimed results, or changes introduce new problems.

## Severity Levels
- **critical**: Wrong statements, source mismatch, breaks existing correct code
- **warning**: Suboptimal structure, could be organized better
- **suggestion**: Style improvements
"""
        elif pr.agent_type.value == "scan":
            base += """This PR is from a SCAN agent. Scanners identify issues in the codebase and document them in the `issues/` folder.

Scan agents do NOT fill in proofs or modify Lean code. They only analyze the codebase and create issue reports.

## Your Role
You are evaluating **whether the identified issues are valid and well-documented**, NOT proof progress.

## Review Checklist

### Issue Validity
- Are the identified issues real problems (not false positives)?
- Are the issues mathematically accurate (e.g., "forward dependency" claims are actually forward dependencies)?
- Do the file paths and locations referenced actually exist?

### Issue Quality
- Are issue descriptions clear and actionable?
- Is the category appropriate (forward_dependency, api_gap, naming_violation, etc.)?
- Is there enough context for a maintainer to understand and fix the issue?

### No False Positives
- Check that claimed issues are actually problems, not misunderstandings
- Verify that "missing" theorems are actually missing from the source material

### Source Cross-Check (if source available)
- If the issue claims something is "missing from source", verify against the LaTeX
- If the issue claims a statement is "wrong", verify the mathematical claim

## Verdict Policy
Note: APPROVE will immediately merge the PR. If you have any feedback you want addressed (even minor), use REQUEST_CHANGES instead.

APPROVE if all identified issues are valid and well-documented.
REQUEST_CHANGES if: issues are false positives, descriptions are unclear or misleading,
or the scanner made mathematical errors in its analysis.

## Severity Levels
- **critical**: False positive issues, mathematically incorrect analysis
- **warning**: Unclear descriptions, wrong categorization
- **suggestion**: Could be more detailed
"""
        elif pr.agent_type.value == "triage":
            base += """This PR is from a TRIAGE agent. Triagers review open issues and close ones that are already resolved.

Triage agents do NOT fill in proofs or modify Lean code. They only update issue files in `issues/` folder to close resolved issues.

## Your Role
You are evaluating **whether the closure decisions are correct**, NOT proof progress.

## Review Checklist

### Closure Validity
- For each issue marked as closed, verify it is actually resolved in the current codebase
- Check that the justification matches reality (e.g., if it says "theorem now exists", verify it does)
- Ensure no issues are incorrectly closed (still-open issues marked as resolved)

### Closure Completeness
- Are there any obviously-resolved issues that should have been closed but weren't?
- (This is a minor concern — missing closures are less harmful than incorrect closures)

### No False Closures
- The most critical error is closing an issue that is NOT actually resolved
- Verify each closure claim against the actual codebase state

## Verdict Policy
Note: APPROVE will immediately merge the PR. If you have any feedback you want addressed (even minor), use REQUEST_CHANGES instead.

APPROVE if all closure decisions are correct.
REQUEST_CHANGES if: issues are incorrectly marked as closed when they're still open,
or closure justifications are inaccurate.

## Severity Levels
- **critical**: False closure (issue marked closed but still exists)
- **warning**: Questionable closure justification
- **suggestion**: Could have closed additional resolved issues
"""
        elif pr.agent_type.value == "maintain":
            base += """This PR is from a MAINTAIN agent. Maintainers pick up issues from the `issues/` folder and fix them.

Maintain agents fix structural issues (forward dependencies, missing theorems, API gaps, etc.).
They may modify Lean code, and `sorry` is allowed — they may or may not prove things.

**Important**: If the issue was already resolved in the codebase (the problem no longer exists), the maintainer may submit a PR that only closes the issue file with no Lean code changes. This is valid — approve it if the issue is indeed resolved.

## Your Role
You are evaluating **whether the fix correctly addresses the claimed issue**, NOT whether proofs are complete.

## Review Checklist

### Issue Resolution
- Does the PR actually fix the issue it claims to address?
- If the maintainer claims the issue was **already resolved** (no code changes), verify this is true by checking the current codebase state
- If the issue was "forward dependency", is the dependency now resolved?
- If the issue was "missing theorem", is the theorem now present and correctly stated?

### Mathematical Correctness
- Are any new/modified statements mathematically valid?
- Do they faithfully represent results from the source material?
- Are types and hypotheses correct?

### Source Fidelity
- If new theorems are added, are they actually in the source material?
- Are `-- [cited]` and `-- [exercise]` markers used correctly?

### Issue Tracking
- Is the resolved issue properly marked as closed in its issue file (`issues/*.yaml`)?
- Is the closure note accurate?

## Verdict Policy
`sorry` is allowed. This PR fixes infrastructure; proofs are optional.

Note: APPROVE will immediately merge the PR. If you have any feedback you want addressed (even minor), use REQUEST_CHANGES instead.

APPROVE if the fix correctly addresses the issue and is mathematically sound.
APPROVE if the issue was already resolved and the maintainer correctly identified this (even with no code changes).
REQUEST_CHANGES if: the fix doesn't actually resolve the issue, statements are mathematically wrong,
or source material doesn't support the claimed results.

## Severity Levels
- **critical**: Fix doesn't resolve issue, wrong statements, source mismatch
- **warning**: Suboptimal structure, incomplete fix
- **suggestion**: Style improvements
"""
        else:  # prover (default)
            base += """This PR is from a PROVER agent. Provers work on filling in `sorry` placeholders with actual proofs.

## Understanding Prover Outcomes

Provers may submit PRs with various outcomes:
- **Proof completed**: One or more sorries filled with valid proofs
- **Partial progress**: Some proof steps completed, infrastructure added
- **Infrastructure only**: Helper lemmas, restructuring, or API additions needed for the proof
- **No proof possible**: The theorem may be `[cited]` (external), `[exercise]` (unsolvable), or blocked by dependencies

**All of these are valid outcomes.** The prover's job is to make progress OR identify why progress cannot be made.

## Review Checklist

### If Proofs Were Completed
- Are the completed proofs mathematically valid?
- Are Mathlib lemmas and tactics used appropriately?
- Are proofs reasonably concise (not excessively long term-mode when tactics would work)?
- No abuse of `decide`/`native_decide` on large goals

### If No Proofs Were Completed
- Did the prover correctly identify why the proof cannot proceed?
- Are any infrastructure changes (helper lemmas, restructuring) mathematically sound?
- If blocked by dependencies, is the blocker correctly identified?

### No Regressions
- Did the prover preserve existing definitions and statements unchanged?
- No unnecessary modifications to code outside the target theorem

### Structural Suggestions
- If a forward dependency is detected, the prover should escalate (not hack around it)

## Verdict Policy
Note: APPROVE will immediately merge the PR. If you have any feedback you want addressed (even minor), use REQUEST_CHANGES instead.

APPROVE if:
- The prover completed valid proofs, OR
- The prover made sound infrastructure changes, OR
- The prover correctly identified a blocker (with no code changes or just documentation)

REQUEST_CHANGES if proofs are incorrect, the prover introduced regressions, or made unnecessary changes.

## Severity Levels
- **critical**: Must fix (incorrect proofs, regressions, introduced sorry/axiom)
- **warning**: Should fix (poor proof style, unnecessary changes)
- **suggestion**: Nice to have (alternative tactic suggestions)
"""

        base += """
Output format:
VERDICT: approve|request_changes|reject|abstain

**Verdict semantics:**
- REJECT = work is fundamentally misguided, no longer needed, or beyond repair — better to start over from scratch
- REQUEST_CHANGES = work is on the right track but has fixable issues (e.g., compilation errors, style problems) — worth revising
- APPROVE = work is ready to merge
- ABSTAIN = cannot evaluate (e.g., missing context)

SUMMARY:
This is your complete review output. Provide a **detailed** review covering:

1. **Coverage Assessment**: What results from the source material are covered? List specific theorem/lemma names.
2. **Mathematical Fidelity**: How well do the formalizations match the source? Note any deviations or interpretations made.
3. **Critical Issues**: Describe each critical issue in detail, explaining:
   - What the problem is
   - Why it matters
   - What the fix should be
4. **Structure & Dependencies**: Comment on the logical organization and any dependency issues.
5. **Missing Content**: List any theorems, lemmas, or results from the source that are NOT formalized (with section/theorem numbers from source).
6. **Positive Observations**: Note what was done well.

Be thorough. This is the complete review that will be shown to users.

COMMENTS:
- file.lean:10-15: [critical|warning|suggestion] Specific comment about these lines
"""
        return base

    def _build_review_prompt(self, pr: ReviewContext, diff: str, files: dict[str, str]) -> str:
        prompt = f"""Please review this pull request.

## PR Information
- **Title**: {pr.title}
- **Agent**: {pr.agent_id}
- **Agent Type**: {pr.agent_type.value}
- **Branch**: {pr.branch_name}
- **Files Changed**: {", ".join(pr.files_changed)}
- **Revision**: {pr.revision_number} ({"initial submission" if pr.revision_number == 0 else f"revision #{pr.revision_number}"})
"""
        if pr.revision_number > 0 and pr.previous_review_feedback:
            prompt += f"""
## Previous Review Feedback
This is revision #{pr.revision_number}. The contributor was asked to address the following feedback:

{pr.previous_review_feedback}

Please verify whether the previous feedback has been adequately addressed.
If issues from the previous review are still present, mention them again in your review.
If you find new issues, raise them as new comments.
"""

        prompt += f"""
## PR Description
{pr.description if pr.description else "(No description provided)"}

## Changes (Diff)
```diff
{diff[:30000]}
```

## Full File Contents
"""
        for path, content in files.items():
            if path.endswith(".lean"):
                prompt += f"\n### {path}\n```lean\n{content[:20000]}\n```\n"

        if pr.source_content:
            prompt += f"\n## Source Material (LaTeX)\n```latex\n{pr.source_content[:30000]}\n```\n"

        if pr.description and "closes #" in pr.description.lower():
            prompt += """
## Issue Closure Verification
The PR description claims to close one or more issues. Please verify:
1. The issue is actually resolved by the changes in this PR
2. The corresponding issue file in `issues/` folder is marked as closed (`status: closed`)
3. A note is optionally added to the description explaining how it was resolved

If the issue closure claim is invalid, REQUEST_CHANGES.
"""

        prompt += "\nProvide your review verdict, summary, and specific comments.\n"
        return prompt


# =============================================================================
# Engineering Reviewer
# =============================================================================


class EngineeringReviewer(BaseReviewer):
    """Reviews code quality, adapting criteria to agent type.

    For sketchers: focuses on structure, naming, imports (lenient on sorry).
    For provers: focuses on proof style, no regressions, no unnecessary changes.
    """

    agent_type = "EngineeringReviewer"
    review_type = ReviewType.ENGINEERING

    def _build_system_prompt(self, pr: ReviewContext) -> str:
        base = """You are an engineering reviewer for Lean 4 code.

Your job is to review pull requests for code quality and engineering best practices.
Note: Compilation is already verified before this review runs.

"""
        if pr.agent_type.value == "sketch":
            base += """This PR is from a SKETCHER agent. Sketchers create initial formalizations with `sorry` placeholders.

Focus areas:
1. STYLE: Does the code follow Lean/Mathlib style conventions (naming, indentation)?
2. IMPORTS: Are imports appropriate and minimal?
3. ORGANIZATION: Is the file logically structured (definitions, then API lemmas, then theorems)?
4. DOCUMENTATION: Are there docstrings for the main definitions and theorems?

`sorry` is expected in sketcher PRs. Do NOT reject for sorry usage.
Be lenient — the goal is a good skeleton, not polished code.

Note: APPROVE will immediately merge the PR. If you have any feedback you want addressed (even minor), use REQUEST_CHANGES instead.

APPROVE if the code is reasonably well-structured.
"""
        elif pr.agent_type.value == "fix":
            base += """This PR is from a PROVER agent proposing a FIX to the codebase.

The prover identified that the file structure needs modification (missing theorems, reordering, etc.).
This PR adds infrastructure that will be proved later.

Focus areas:
1. STYLE: Do the new/changed statements follow Lean/Mathlib conventions?
2. ORGANIZATION: Do the changes maintain logical file structure?
3. NO REGRESSIONS: Did the changes preserve existing code appropriately?
4. MINIMAL CHANGES: Are the changes focused on what's needed?

`sorry` is expected in fix PRs. Do NOT reject for sorry usage.

Note: APPROVE will immediately merge the PR. If you have any feedback you want addressed (even minor), use REQUEST_CHANGES instead.

APPROVE if the changes are well-structured and focused.
"""
        elif pr.agent_type.value == "scan":
            base += """This PR is from a SCAN agent. Scanners identify issues in the codebase and document them in the `issues/` folder.

Scan agents do NOT modify Lean code. They only create issue files in the `issues/` folder.

Focus areas:
1. ISSUE FORMAT: Are new issues properly formatted YAML files with status, origin, and description?
2. CLARITY: Are issue descriptions clear and actionable?
3. NO LEAN CHANGES: Scanner should NOT modify .lean files (only create issue files)
4. VALID REFERENCES: Do file paths referenced in issue descriptions actually exist?
5. STABLE LOCATIONS: Issues must use semantic anchors (theorem/definition names) NOT line numbers.

Note: APPROVE will immediately merge the PR. If you have any feedback you want addressed (even minor), use REQUEST_CHANGES instead.

APPROVE if issue files are well-formatted and issue descriptions are clear.
REQUEST_CHANGES if: issue format is wrong, descriptions are unclear, or Lean files were modified.
"""
        elif pr.agent_type.value == "triage":
            base += """This PR is from a TRIAGE agent. Triagers review open issues and close resolved ones.

Triage agents do NOT modify Lean code. They only update issue files in `issues/` folder to mark issues as closed.

Focus areas:
1. ISSUE FORMAT: Are closures properly formatted (status changed from `open` to `closed`)?
2. NO LEAN CHANGES: Triager should NOT modify .lean files (only issue files)
3. CLOSURE FORMAT: Each closed issue should have `status: closed` and optionally a note explaining why it's resolved

Note: APPROVE will immediately merge the PR. If you have any feedback you want addressed (even minor), use REQUEST_CHANGES instead.

APPROVE if issue closures are well-formatted.
REQUEST_CHANGES if: closure format is wrong or Lean files were modified.
"""
        elif pr.agent_type.value == "maintain":
            base += """This PR is from a MAINTAIN agent. Maintainers fix issues from the `issues/` folder.

Maintain agents fix structural issues by modifying Lean code. `sorry` is allowed — they may or may not prove things.

**Important**: If the issue was already resolved in the codebase (the problem no longer exists), the maintainer may submit a PR that only closes the issue file with no Lean code changes. This is valid — approve it if the issue is indeed resolved.

Focus areas:
1. STYLE: Do new/changed statements follow Lean/Mathlib conventions?
2. ORGANIZATION: Do changes maintain logical file structure?
3. NO REGRESSIONS: Did changes preserve existing code appropriately?
4. ISSUE TRACKING: Is the resolved issue marked as closed in its issue file (`issues/*.yaml`)?
5. MINIMAL CHANGES: Are changes focused on fixing the specific issue?

`sorry` is allowed in maintain PRs. Do NOT reject for sorry usage.

Note: APPROVE will immediately merge the PR. If you have any feedback you want addressed (even minor), use REQUEST_CHANGES instead.

APPROVE if the fix is well-structured and the issue is properly marked as resolved.
APPROVE if the issue was already resolved and the maintainer correctly identified this (even with no code changes).
REQUEST_CHANGES if: structure is poor, regressions introduced, or issue not marked as resolved.
"""
        else:  # prover (default)
            base += """This PR is from a PROVER agent. Provers fill in sorry placeholders with actual proofs.

Focus areas:
1. PROOF STYLE: Are proofs written in clean tactic style (not excessively long term-mode)?
2. NO REGRESSIONS: Did the prover preserve existing structure and naming?
3. UNNECESSARY CHANGES: Did the prover avoid modifying statements or definitions unnecessarily?
4. TACTIC USAGE: Are tactics used appropriately (not abusing decide/native_decide on large goals)?

Note: APPROVE will immediately merge the PR. If you have any feedback you want addressed (even minor), use REQUEST_CHANGES instead.

APPROVE if proofs are clean and don't regress the codebase.
REQUEST_CHANGES if the prover made unnecessary structural changes or used poor proof style.
"""

        base += """
Output format:
VERDICT: approve|request_changes|reject|abstain

**Verdict semantics:**
- REJECT = work is fundamentally misguided, no longer needed, or beyond repair — better to start over from scratch
- REQUEST_CHANGES = work is on the right track but has fixable issues (e.g., compilation errors, style problems) — worth revising
- APPROVE = work is ready to merge
- ABSTAIN = cannot evaluate (e.g., missing context)

SUMMARY:
This is your complete review output. Provide a **detailed** engineering review covering:

1. **Code Organization**: How well is the code structured? Are definitions, API lemmas, and theorems in logical order?
2. **Style Compliance**: Does the code follow Lean/Mathlib conventions? Note specific deviations.
3. **Import Analysis**: Are imports appropriate and minimal? Any missing or unnecessary imports?
4. **Documentation Quality**: Are docstrings present and useful for main definitions/theorems?
5. **Issues Found**: For each issue, explain:
   - What the problem is
   - Where it occurs (file and approximate location)
   - What the fix should be
6. **Positive Observations**: Note what was done well.

Be thorough. This is the complete review that will be shown to users.

COMMENTS:
- file.lean:10-15: Specific comment about these lines
"""
        return base

    def _build_review_prompt(self, pr: ReviewContext, diff: str, files: dict[str, str]) -> str:
        prompt = f"""Please review this pull request for code quality.

## PR Information
- **Title**: {pr.title}
- **Agent**: {pr.agent_id}
- **Agent Type**: {pr.agent_type.value}
- **Branch**: {pr.branch_name}
- **Files Changed**: {", ".join(pr.files_changed)}

## Changes (Diff)
```diff
{diff[:30000]}
```

## Full File Contents
"""
        for path, content in files.items():
            if path.endswith(".lean"):
                prompt += f"\n### {path}\n```lean\n{content[:20000]}\n```\n"

        prompt += "\nProvide your review verdict, summary, and specific comments.\n"
        return prompt


# =============================================================================
# Review Coordination
# =============================================================================


@dataclass
class ReviewResult:
    """Result of coordinated review."""

    build_passed: bool
    build_error: str | None
    build_output: str | None  # Combined stdout/stderr for failed builds
    math_review: Review | None
    engineering_review: Review | None
    combined_verdict: ReviewVerdict


def _is_empty_diff(diff: str) -> bool:
    """Check if a diff is empty (no actual changes)."""
    if not diff or not diff.strip():
        return True
    # A diff with only headers but no actual changes
    lines = diff.strip().split("\n")
    for line in lines:
        # Actual change lines start with + or - (but not --- or +++)
        if line.startswith("+") and not line.startswith("+++"):
            return False
        if line.startswith("-") and not line.startswith("---"):
            return False
    return True


# Agent types that have open-ended tasks (empty diff = success, nothing needed to be done)
# - triage: closes resolved issues, may find nothing to close
# - scan: finds architectural issues, may find nothing
# - maintain: picks up issues from ISSUES.md, may find nothing actionable
_OPEN_ENDED_AGENT_TYPES = {"triage", "scan", "maintain"}

# Agent types with specific tasks (empty diff = request_changes, task not completed)
_TASK_AGENT_TYPES = {"sketch", "prove", "fix"}


def review_pr(
    pr: ReviewContext,
    diff: str,
    files: dict[str, str],
    worktree_path: Path | None = None,
    config: AgentConfig | None = None,
    session_recorder: "SessionRecorder | None" = None,
) -> ReviewResult:
    """Coordinate review of a PR.

    Steps:
    0. Check for empty diff (handle based on agent type)
    1. Run lake build once (auto-reject if fails)
    2. Call both reviewers
    3. Compute AND-success

    Args:
        pr: The PR to review
        diff: Git diff of the changes
        files: Dict of {path: content} for changed files
        worktree_path: Path to worktree for running lake build (optional)
        config: Agent configuration for LLM calls
        session_recorder: Session recorder for logging reviewer agents

    Returns:
        ReviewResult with both reviews and combined verdict
    """
    logger.info(f"Starting coordinated review of PR {pr.pr_id}")

    # Step 0: Check for empty diff - handle based on agent type
    if _is_empty_diff(diff):
        agent_type = pr.agent_type.value
        if agent_type in _OPEN_ENDED_AGENT_TYPES:
            # Open-ended tasks (triage, scan): empty diff is success (nothing to do)
            logger.info(f"PR {pr.pr_id} has empty diff from {agent_type} agent - treating as success")
            return ReviewResult(
                build_passed=True,
                build_error=None,
                build_output=None,
                math_review=None,
                engineering_review=None,
                combined_verdict=ReviewVerdict.APPROVE,
            )
        else:
            # Task-based agents (sketch, prove, fix): empty diff means task not done
            logger.warning(f"PR {pr.pr_id} has empty diff from {agent_type} agent - requesting changes")
            return ReviewResult(
                build_passed=True,
                build_error="Empty diff: no changes were made. The task was not completed.",
                build_output=None,
                math_review=None,
                engineering_review=None,
                combined_verdict=ReviewVerdict.REQUEST_CHANGES,
            )

    # Step 1: Lake build check
    build_output = None
    if worktree_path:
        build_passed, build_error, build_output, build_duration = _run_lake_build(worktree_path, pr.branch_name)

        # Record the review build result
        if session_recorder:
            session_recorder.record_build(
                context="review",
                pr_id=pr.pr_id,
                branch_name=pr.branch_name,
                passed=build_passed,
                error=build_error,
                duration_s=build_duration,
                stdout=build_output,
            )

        if not build_passed:
            logger.warning(f"PR {pr.pr_id} failed build check")
            return ReviewResult(
                build_passed=False,
                build_error=build_error,
                build_output=build_output,
                math_review=None,
                engineering_review=None,
                combined_verdict=ReviewVerdict.REQUEST_CHANGES,
            )
    else:
        pass  # Skip build check

    logger.info(f"PR {pr.pr_id} passed build check, running LLM reviews")

    # Step 2: Run both reviews in parallel (with recorders if available)
    math_agent_id = f"math-{pr.pr_id}"
    engineering_agent_id = f"engineering-{pr.pr_id}"

    math_recorder = None
    engineering_recorder = None

    if session_recorder:
        math_recorder = session_recorder.register_agent(
            agent_id=math_agent_id,
            agent_type="math_reviewer",
            config={"pr_id": pr.pr_id, "branch": pr.branch_name},
        )
        engineering_recorder = session_recorder.register_agent(
            agent_id=engineering_agent_id,
            agent_type="engineering_reviewer",
            config={"pr_id": pr.pr_id, "branch": pr.branch_name},
        )
        # Record launch events so reviewers appear in the viewer timeline
        session_recorder.record_agent_launched(
            agent_id=math_agent_id,
            agent_type="math_reviewer",
            chapter_id="",  # Reviewers work on PRs, not chapters
            review_target=pr.agent_id,  # The agent whose PR is being reviewed
        )
        session_recorder.record_agent_launched(
            agent_id=engineering_agent_id,
            agent_type="engineering_reviewer",
            chapter_id="",  # Reviewers work on PRs, not chapters
            review_target=pr.agent_id,  # The agent whose PR is being reviewed
        )

    math_reviewer = MathReviewer(
        reviewer_id=math_agent_id,
        config=config,
        recorder=math_recorder,
        worktree_path=worktree_path,
    )
    engineering_reviewer = EngineeringReviewer(
        reviewer_id=engineering_agent_id,
        config=config,
        recorder=engineering_recorder,
        worktree_path=worktree_path,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        math_future = pool.submit(math_reviewer.review, pr, diff, files)
        eng_future = pool.submit(engineering_reviewer.review, pr, diff, files)

        math_review = math_future.result()
        eng_review = eng_future.result()

    # Step 3: Compute AND-success
    combined = _compute_combined_verdict(math_review, eng_review)

    logger.info(
        f"Review complete: math={math_review.verdict.value}, "
        f"engineering={eng_review.verdict.value}, combined={combined.value}"
    )

    return ReviewResult(
        build_passed=True,
        build_error=None,
        build_output=None,
        math_review=math_review,
        engineering_review=eng_review,
        combined_verdict=combined,
    )


def _run_lake_build(worktree_path: Path, branch_name: str) -> tuple[bool, str | None, str | None, float | None]:
    """Run lake build in the worktree using the centralized build function.

    The worktree should already be on the correct branch (set up by WorktreePool).

    Args:
        worktree_path: Path to the git worktree
        branch_name: Branch name (for logging only)

    Returns:
        (success, error_message, build_output, duration_seconds)
        build_output is the combined stdout/stderr for failed builds
    """
    result = lake_build(worktree_path, label=f"review:{branch_name[:20]}")

    # Combine stdout/stderr for failed builds
    build_output = None
    if not result.success:
        parts = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.append(result.stderr)
        if parts:
            build_output = "\n".join(parts)

    if result.timed_out:
        return False, result.error, build_output, None

    return result.success, result.error, build_output, result.duration


def _compute_combined_verdict(sem: Review, eng: Review) -> ReviewVerdict:
    """Compute AND-success verdict.

    Rules:
    - Any REJECT → REJECT
    - Any REQUEST_CHANGES → REQUEST_CHANGES
    - Any ABSTAIN → ABSTAIN
    - Both APPROVE → APPROVE
    """
    if sem.verdict == ReviewVerdict.REJECT or eng.verdict == ReviewVerdict.REJECT:
        return ReviewVerdict.REJECT

    if sem.verdict == ReviewVerdict.REQUEST_CHANGES or eng.verdict == ReviewVerdict.REQUEST_CHANGES:
        return ReviewVerdict.REQUEST_CHANGES

    if sem.verdict == ReviewVerdict.ABSTAIN or eng.verdict == ReviewVerdict.ABSTAIN:
        return ReviewVerdict.ABSTAIN

    return ReviewVerdict.APPROVE


# =============================================================================
# Response Parsing
# =============================================================================


def _parse_review_response(response: str) -> tuple[ReviewVerdict, str, list[ReviewComment]]:
    """Parse LLM response into verdict, summary, and comments."""
    lines = response.strip().split("\n")

    verdict = ReviewVerdict.ABSTAIN
    summary_lines = []
    comments = []
    current_section = None

    for line in lines:
        line_stripped = line.strip()

        if line_stripped.upper().startswith("VERDICT:"):
            verdict_str = line_stripped.split(":", 1)[1].strip().lower()
            if verdict_str in [v.value for v in ReviewVerdict]:
                verdict = ReviewVerdict(verdict_str)

        elif line_stripped.upper().startswith("SUMMARY:"):
            current_section = "summary"

        elif line_stripped.upper().startswith("COMMENTS:"):
            current_section = "comments"

        elif current_section == "summary" and line_stripped:
            summary_lines.append(line_stripped)

        elif current_section == "comments" and line_stripped.startswith("-"):
            comment = _parse_comment(line_stripped[1:].strip())
            if comment:
                comments.append(comment)

    summary = "\n".join(summary_lines)
    return verdict, summary, comments


def _parse_comment(comment_str: str) -> ReviewComment | None:
    """Parse a comment string like 'file.lean:10-15: Comment text'."""
    try:
        if ":" not in comment_str:
            return None

        parts = comment_str.split(":", 2)
        if len(parts) < 2:
            return None

        file_path = parts[0].strip()
        rest = parts[1].strip()

        if "-" in rest.split(":")[0]:
            line_part, message = rest.split(":", 1) if ":" in rest else (rest, "")
            start, end = map(int, line_part.split("-"))
        elif rest and rest[0].isdigit():
            line_part = ""
            message = ""
            for i, c in enumerate(rest):
                if c.isdigit():
                    line_part += c
                elif c == ":":
                    message = rest[i + 1 :].strip()
                    break
            start = end = int(line_part) if line_part else 0
        else:
            return None

        return ReviewComment(
            file_path=file_path,
            line_start=start,
            line_end=end,
            message=message,
        )
    except Exception:
        return None
