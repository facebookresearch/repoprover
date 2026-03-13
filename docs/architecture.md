# RepoProver Architecture

This document explains the architecture of the multi-file autoformalization system.

## Design Principles

1. **Simple Loop** - One main loop that runs until completion
2. **PR = Branch** - A PR is just a branch name submitted for review
3. **Resumable** - State saved after every iteration
4. **Agent Isolation** - Each agent works in its own git worktree
5. **Review Gate** - Every change goes through automated review
6. **Unified Contributor** - All formalization work uses a single agent with different modes
7. **Unified Runner** - All agent types use `_run_contributor()` for consistent execution

## Core Components

### BookCoordinator

The heart of the system. Runs a priority-ordered loop where each step returns
whether it made progress. On progress, the loop restarts from the highest priority:

1. Harvest completed reviews (land review results → unblock merges)
2. Harvest completed agents (land agent results → new PRs)
3. Process merges (merge approved PRs to main)
4. Launch reviews (fire-and-forget for pending PRs)
5. Process revisions (re-launch agents with feedback)
6. Launch provers (for theorems with `sorry`)
7. Launch sketchers (for chapters without sketches)
8. Launch maintain/triage/scan contributors

If no step makes progress, the loop sleeps until a background task completes.

```python
# coordinator.py - simplified
while self._running:
    progress = False
    for step in priority_steps:
        if await step():
            progress = True
            break  # restart from highest priority

    if not progress:
        # Wait for any background task to complete
        all_tasks = agent_tasks | review_tasks
        if all_tasks:
            await asyncio.wait(all_tasks, timeout=poll_interval,
                               return_when=FIRST_COMPLETED)
        else:
            await asyncio.sleep(poll_interval)

    self.save_state()
    if self._is_complete() and no_running_tasks:
        break
```

### Method Responsibilities

The coordinator uses a consistent pattern for all agent types:

#### Launch Methods (`_launch_*`)
- Check launch conditions (capacity, timing, etc.)
- Create `ContributorTask` for the agent type
- Call `_run_contributor()` via `asyncio.to_thread()`
- Add task to appropriate task dictionary
- **Do NOT**: record events (done by `_run_contributor`)

#### Unified Runner (`_run_contributor`)
- Acquire worktree (new or from existing branch for revisions)
- Set up recording (register agent, record launch event)
- Create and run `ContributorAgent` with the task
- Handle special statuses (`fix`, `issue` for prove agents)
- Record PR submission on success
- Return `SimplePR | None`
- **Worktree cleanup**: only cleans up if no PR was submitted (the worktree stays alive
  for the reviewer when a PR is created)

#### Harvest Methods (`_harvest_*`)
- Check for completed tasks in task dictionary
- Extract result from task (`SimplePR | None`)
- Add PR to `state.prs` if successful
- Record completion event
- Clean up task dictionary
- **Do NOT**: record PR submission (done by `_run_contributor`)

#### Process Methods (`_process_*`)
- `_process_revisions`: Find PRs needing revision, build task, call `_run_contributor`
- `_process_reviews`: Launch review tasks for pending PRs
- `_process_merges`: Merge approved PRs to main branch

### Task Dictionaries

| Dictionary | Agent Types | Key → Value |
|------------|-------------|-------------|
| `_agent_tasks` | sketch, prove | `Task → (chapter_id, agent_id)` |
| `_scanner_tasks` | scan | `Task → scanner_id` |
| `_triage_tasks` | triage | `Task → agent_id` |
| `_maintain_tasks` | maintain | `Task → (agent_id, issue_id)` |
| `_review_tasks` | reviews | `Task → (pr_id, revision)` |

### Agents

The system uses a unified **ContributorAgent** with five modes:

**ContributorAgent (mode=SKETCH)**
- Reads LaTeX source files
- Creates Lean files with definitions and theorem statements
- Leaves proofs as `sorry`
- Uses tools: file_read, file_write, file_edit, git_commit, lake build

**ContributorAgent (mode=PROVE)**
- Reads Lean files with `sorry` proofs
- Fills in complete proofs
- Must not leave any `sorry` in its submitted code
- Can create issues (ISSUE) or propose fixes (FIX)
- Same tools as sketch mode

**ContributorAgent (mode=MAINTAIN)**
- Assigned a specific issue from ISSUES.md by the coordinator
- Makes progress on the assigned issue (add lemmas, fix statements, etc.)
- Can close the issue it resolves
- Used for general codebase improvement

**ContributorAgent (mode=SCAN)**
- Scans Lean files for architectural issues
- Creates new issues in ISSUES.md
- Identifies API gaps, naming issues, forward dependencies
- Creates PRs for review

**ContributorAgent (mode=TRIAGE)**
- Reads ISSUES.md to find open issues
- Identifies stale issues (already resolved or obsolete)
- Closes stale issues with explanations
- Does NOT try to fix issues itself (observer only)
- Finding nothing to close is a valid outcome

All modes:
- Work in isolated git worktrees
- Commit changes to their branch
- **Support revision with feedback** (all modes handle `feedback` kwarg)
- Use the same set of tools
- Return unified `ContributorResult`
- **Run through `_run_contributor()`** for consistent execution

### Revision Support

All agent types support revision when their PR fails review:

1. PR gets `needs_revision` status from review or merge failure
2. `_process_revisions()` finds the PR
3. Builds appropriate `ContributorTask` for the agent type
4. Calls `_dispatch_agent()` with:
   - `agent_id`: Same agent_id as before (reuses branch/worktree)
   - `feedback`: The review feedback (conflict info, build errors, or LLM review)
   - `revision_number`: Incremented revision count
5. Agent receives feedback in its prompt via `kwargs.get("feedback")`
6. On completion, `_find_revision_in_progress(agent_id)` matches the PR back by agent_id
7. PR updated back to `pending_review` for another review cycle

### Unified Output Markers

All contributor modes use the same output markers. Everything after the marker line is the **PR description** shown to reviewers.

```
-- DONE
<PR description: what this PR does and why>
Closes #N (if resolving issues)
```

```
-- FIX
<PR description: what was fixed and why>
Original task still TODO: <what remains>
Closes #N (if resolving issues)
```

```
-- ISSUE
<PR description: the issue created>
Issue #N: <title>
```

```
-- BLOCKED
<Reason why no progress could be made>
```

### SimplePR

A PR is just a branch name with status tracking:

```python
@dataclass
class SimplePR:
    pr_id: str
    branch_name: str           # e.g., "sketch-ch1-abc123"
    chapter_id: str
    agent_type: str            # "sketch", "prove", "fix", "scan", "triage"
    theorem_name: str | None   # For prove PRs
    status: str                # pending_review, needs_revision, approved, merged, failed
    revision_count: int
    last_review_feedback: str
    diff_stats: dict[str, int] | None  # {"+": additions, "-": deletions}
```

### RunState

Tracks everything needed for resumability:

```python
@dataclass
class RunState:
    book_id: str
    chapters: dict[str, dict]           # chapter_id -> info
    prs: dict[str, SimplePR]            # pr_id -> SimplePR
    completed_theorems: dict[str, list] # chapter_id -> [theorem_names]
    next_issue_id: int                  # For unique issue IDs
```

Saved to `.repoprover/state.json` after every loop iteration.

### Review System

Every PR goes through the review pipeline. Reviews are fire-and-forget async tasks,
harvested by the main loop just like agent tasks.

#### Review Pipeline (`_run_review`)

The reviewer acquires the contributor's worktree via `worktree_pool.acquire(pr.agent_id)`.
This returns the existing worktree left by the contributor, or recreates it from the branch
if something went wrong. The worktree is released (without cleanup) when the review finishes,
so it remains on disk for potential revision or merge.

> **Important**: Reviewers are **read-only** — they only read the code on the branch to
> perform reviews. Multiple reviewers can safely share the same worktree because they
> don't modify it. The `WorktreePool` uses per-agent locking to ensure the worktree is
> created before any reviewer accesses it (see `git-worktree-tools.md` for details).

- **Step 0: Pre-review rebase-onto-main check** — `_rebase_onto_main_in_worktree(worktree_path)`
    - Runs `git rebase main` in the review's own worktree (concurrent-safe, no locks)
    - **Conflict**: aborts rebase, returns `REQUEST_CHANGES` immediately
        - Skips build and LLM calls entirely (saves ~30s build + 2 LLM calls)
        - Feedback to agent: `"Rebase conflict with main in: Foo.lean, ISSUES.md"`
        - Records: `merge_conflict_detected` event with `conflict_files`
    - **Clean rebase**: proceeds to Step 1

- **Step 1: Lake build** in worktree — must compile or auto-reject
    - Uses centralized `lake_build()` with semaphore for concurrency control
    - See [build.md](build.md) for details on build management

- **Step 2: Math + Engineering LLM reviews** — two parallel LLM calls
    - Math review checks mathematical correctness
    - Engineering review checks code quality

- **Step 3: AND-success** — both reviews must approve

#### Review Verdicts → PR Status

- `APPROVE` → `pr.status = "approved"` (proceeds to merge)
- `REQUEST_CHANGES` → `pr.status = "needs_revision"` (agent revises with feedback)
- `REJECT` → `pr.status = "failed"` (agent relaunched from scratch)

#### Empty Diff Handling

Different agent types have different expectations for whether they produce changes:

| Agent Type | Empty Diff Result | Reason |
|------------|-------------------|--------|
| `triage` | **APPROVE** | Open-ended: may find nothing to close |
| `scan` | **APPROVE** | Open-ended: may find no issues |
| `maintain` | **APPROVE** | Open-ended: may find nothing actionable |
| `sketch` | REQUEST_CHANGES | Task-based: must produce changes |
| `prove` | REQUEST_CHANGES | Task-based: must produce changes |
| `fix` | REQUEST_CHANGES | Task-based: must produce changes |

This is controlled by `_OPEN_ENDED_AGENT_TYPES` in `reviewers.py`.

### Merge Flow (`_process_merges` → `_merge_branch`)

Merges run sequentially on the main worktree (one at a time):

- `git checkout main` + `git pull --ff-only`
- `git merge --no-ff branch_name`
    - **Merge conflict**:
        - Detects conflict files via `git diff --name-only --diff-filter=U`
        - Runs `git merge --abort`
        - `pr.status = "needs_revision"`
        - Feedback to agent: `"Merge conflict with main in: Foo.lean, ISSUES.md"`
        - Records: `merge_completed(success=False, conflict_files=[...])`
        - Agent enters revision cycle
    - **Merge succeeds** → runs `lake build` to verify
        - Uses centralized `lake_build()` with semaphore (see [build.md](build.md))
        - Records: `build(context="merge", passed=..., duration_s=...)`
        - **Build fails**:
            - `git reset --hard HEAD~1` to undo the merge commit
            - `pr.status = "needs_revision"`
            - Feedback: `"Merge failed: Build failed: ..."`
            - Records: `merge_completed(success=False, error="Build failed: ...")`
            - Agent enters revision cycle
        - **Build passes**:
            - `pr.status = "merged"`
            - Worktree cleaned up via `worktree_pool.release(agent_id, cleanup=True)`
            - Chapter state updated (`sketch_merged` or `completed_theorems`)
            - Records: `merge_completed(success=True, commit_hash=..., diff_stats=...)`
            - Records: `proof_stats` snapshot

#### Why two merge-conflict checks?

The pre-review check (Step 0) catches conflicts cheaply before wasting build + LLM time.
But main advances between review and merge (other PRs merge in between), so the final
merge can still conflict. Both paths set `needs_revision` and give the agent specific
conflict file info.

### Revision Cycle (`_process_revisions`)

When a PR has `status == "needs_revision"`:

- If `revision_count >= max_revisions` → `pr.status = "failed"` (agent relaunched from scratch)
- Otherwise:
    - `pr.status = "revision_in_progress"`, `pr.revision_count += 1`
    - Same agent relaunched with `feedback = pr.last_review_feedback` on its existing branch
    - Agent receives feedback in its prompt, makes fixes, commits
    - On completion: `_find_revision_in_progress(agent_id)` matches the PR back to this agent
    - PR updated to `pending_review` → review pipeline restarts

### Git Worktrees & Naming

Each agent gets an isolated workspace. The **agent_id is the branch name** — they are
the same identifier, used consistently throughout the system:

- `agent_id` = unique identifier for the agent (e.g., `sketch-ch1-abc123`)
- `branch_name` = same string, used as the git branch name
- `worktree_path` = `.repoprover/worktrees/{agent_id}/`
- `pr.branch_name` = same string, stored on the PR

Agent ID formats by type:
- Sketch: `sketch-{chapter_id}-{hex6}` (e.g., `sketch-ac-cauchy-binet-a1b2c3`)
- Prove: `prove-{theorem_name_truncated}-{hex6}` (e.g., `prove-thm_det_CB-d4e5f6`)
- Maintain: `maintain-{hex8}` (e.g., `maintain-12345678`)
- Triage: `triage-{hex6}`
- Scan: `scan-{hex6}`

On revision, the **same agent_id** is reused. This means:
- The agent continues working on the same branch (new commits on top)
- `worktree_pool.acquire(agent_id)` reuses the existing branch if it exists
- `_find_revision_in_progress(agent_id)` matches PRs by agent_id only — prevents
  cross-contamination where one agent steals another's PR

#### Worktree Lifecycle

Worktrees are kept alive through the full contributor→review→revision cycle:

```
create → contributor uses → contributor done (worktree stays) →
reviewer acquires → merge-main check → lake build → LLM review →
reviewer releases (worktree stays on disk) →
  if needs_revision: contributor re-acquires → ... (repeat) →
  if approved: merge to main → delete worktree
  if failed permanently: delete worktree
```

Cleanup rules:
- **`_run_contributor`**: Only cleans up if no PR was submitted (error/blocked/issue).
  If a PR was created, the worktree stays for the reviewer.
- **`_run_review`**: Acquires the worktree (reuses the one left by contributor),
  releases without cleanup when done. The worktree stays for revision or merge.
- **`_process_merges`**: Cleans up after successful merge (terminal state).
- **Failure points**: Worktree cleaned up when PR is permanently failed (REJECT verdict,
  max revisions exceeded, dedupe cleanup).

#### Startup Cleanup: Branches Are Safe, Worktrees Are Throwaway

**Core principle**: All important work is committed to **branches**. Worktrees are just
throwaway working copies that can be deleted and recreated at any time.

On `WorktreePool` initialization (e.g., at run startup or resume), aggressive cleanup
ensures a clean state:

1. Remove all `.git/worktrees/*/locked` files
2. Delete the entire `worktrees_root/` directory
3. Run `git worktree prune` to clean up git metadata

**This is safe because**:
- Branches contain all committed work (preserved)
- Worktrees are recreated on-demand via `pool.acquire(agent_id)`
- The branch name equals agent_id, so `acquire()` recreates from the existing branch

**This prevents the "missing but locked worktree" error** that occurs when:
1. Worktrees were created in a previous run
2. Worktree directories were deleted (e.g., filesystem cleanup between runs)
3. Git still has lock files in `.git/worktrees/*/locked`

To manually start fresh before a run:
```bash
# Remove locked worktrees and prune
rm -rf .repoprover/worktrees/
rm -f .git/worktrees/*/locked
git worktree prune
```

Worktree layout:

```
my-project/                    # Base project (main branch)
├── .lake/
│   ├── packages/              # 6.5GB Mathlib (shared)
│   └── build/                 # Base build artifacts
└── MyBook/

.repoprover/worktrees/
├── sketch-ch1-abc123/         # Sketch agent worktree
│   ├── .lake/
│   │   ├── packages -> ../../.lake/packages  (symlink)
│   │   └── build/             # Agent's own build (~150MB)
│   └── MyBook/
└── prove-thm_foo-def456/      # Prove agent worktree
    └── ...
```

- Worktrees stay alive through the contributor→review→revision cycle
- Cleaned up after merge or permanent failure (`worktree_pool.release(agent_id, cleanup=True)`)
- Branches are preserved in git history (not deleted)

## Data Flow

- `manifest.json` → `load_manifest()` → chapters registered in state
- Main loop continuously runs priority-ordered steps:
    - Harvest completed reviews/agents (land results)
    - Process merges (merge approved PRs to main)
    - Launch reviews (fire-and-forget for pending PRs)
    - Process revisions (relaunch agents with feedback)
    - Launch provers (for theorems with `sorry` in merged sketches)
    - Launch sketchers (for chapters without sketches)
    - Launch maintain/triage/scan contributors
- Each agent: gets worktree → runs task → commits → creates PR → review → merge
- Loop terminates when all sorries filled and no open issues

## File Organization

```
repoprover/
├── cli.py              # Command-line interface
├── coordinator.py      # Main loop, SimplePR, RunState
├── git_worktree.py     # WorktreePool, WorktreeManager
├── distributed.py      # ZMQ-based distributed worker support
├── types.py            # PR, Review, enums
├── recording.py        # Session recording
├── safe_shell.py       # Secure command execution
├── agents/
│   ├── base.py         # BaseAgent, AgentConfig, LLM helpers
│   ├── contributor.py  # ContributorAgent (unified, all 5 modes)
│   ├── reviewers.py    # MathReviewer, EngineeringReviewer, review_pr()
│   ├── file_tools.py   # File manipulation tools
│   ├── git_worktree_tools.py  # Git tools for agents
│   ├── lean_tools.py   # lean_check tool
│   └── shell_tools.py  # bash tool
└── docs/
    └── architecture.md
```

## Distributed Execution

The system supports distributed execution across multiple machines using ZMQ for communication. Key design principle: **Workers are RPCs for `_run_contributor()`**.

### Architecture Overview

**Coordinator (rank=0)**
- `_dispatch_agent()` routes to local or distributed mode
  - **Local mode**: calls `_run_contributor()` directly in thread
  - **Distributed mode**: sends `DistributedTask` via ZMQ PUSH
- `_harvest_distributed_results()` polls ZMQ PULL for results
- `_process_contributor_result()` creates SimplePR + recordings (unified for both modes)

**Workers (rank=1..N)**
- `_execute()` receives task, runs `ContributorAgent` in worktree
- Returns `DistributedResult` with status, branch_name, iterations
- Branches persist in shared git repo via NFS

**Communication**
- Coordinator → Workers: ZMQ PUSH/PULL (tasks)
- Workers → Coordinator: ZMQ PUSH/PULL (results)
- Shared filesystem (NFS): branches accessible after worktree release

### RPC Pattern

Workers are treated as RPCs for `_run_contributor()`:

1. **Worker receives** `DistributedTask` with serialized `ContributorTask`
2. **Worker executes** the agent in a worktree (same as local mode)
3. **Worker returns** `DistributedResult` with:
   - `status` (done, fix, issue, blocked, error)
   - `branch_name` (git branch with agent's commits)
   - `iterations` (agent iteration count for metrics)
   - Error/description fields
4. **Coordinator post-processes** via `_process_contributor_result()`:
   - Gets diff from branch (NFS means branch exists in shared repo)
   - Creates `SimplePR`
   - Records events to session

### Key Components

**DistributedTask** - Serializable task sent to workers:
```python
@dataclass
class DistributedTask:
    task_id: str
    agent_type: str           # sketch, prove, triage, scan, maintain
    task_data: dict           # Serialized ContributorTask
    agent_id: str             # Also used as branch name
    chapter_id: str
    feedback: str = ""
    revision_number: int = 0
    run_dir: str | None = None  # For agent recording
```

**DistributedResult** - Mirrors `ContributorResult` + branch info:
```python
@dataclass
class DistributedResult:
    task_id: str
    agent_id: str
    chapter_id: str
    status: str               # done, fix, issue, blocked, error
    branch_name: str          # Coordinator gets diff from this
    iterations: int           # For metrics
    description: str = ""
    error: str | None = None
    fix_request: str | None = None
    issue_text: str | None = None
    theorem_name: str | None = None
```

### Unified Result Processing

Both local and distributed paths use `_process_contributor_result()`:

```python
def _process_contributor_result(
    self,
    status: str,
    branch_name: str,
    agent_id: str,
    agent_type: str,
    chapter_id: str,
    ...
) -> SimplePR | None:
    # Get diff from branch (works via NFS)
    diff_stats, diff_content = self._get_branch_diff(branch_name)

    # Create PR for done/fix statuses
    if status in ("done", "fix"):
        pr = SimplePR(...)
        self.session_recorder.record_pr_submitted(...)

    # Record agent completion
    self.session_recorder.record_agent_done(...)

    return pr
```

### NFS Optimization

Workers and coordinator share an NFS filesystem:
- Worker commits to branch, releases worktree
- Branch still exists in shared git repo
- Coordinator gets diff via `git diff main...{branch_name}`
- No need to transfer diffs over ZMQ

### Recording Architecture

**Session Recording** (coordinator only):
- `session.jsonl` - Session events (start, end, agent_done, pr_submitted, etc.)
- Written by `SessionRecorder`

**Agent Recording** (per-agent, local or distributed):
- `agents/{agent_id}.jsonl` - Dialog events (messages, tool calls)
- Written by `AgentRecorder`
- Workers use `AgentRecorder(run_dir, agent_id, ...)` directly

### Dispatch Flow

```python
async def _dispatch_agent(agent_type, task, agent_id, chapter_id, feedback="", revision_number=0):
    if self._is_distributed:
        # Record launch (coordinator is source of truth)
        self.session_recorder.record_agent_launched(...)

        # Create task and send to worker
        dist_task = DistributedTask(...)
        self._zmq_server.put(dist_task.to_dict())

        # Store pending info for later harvest
        self._pending_distributed[task_id] = (
            chapter_id, agent_id, agent_type, revision_number, future
        )

        return asyncio.create_task(await_future())
    else:
        # Local mode - run in thread
        return asyncio.create_task(
            asyncio.to_thread(self._run_contributor, ...)
        )
```

### Harvest Flow

```python
async def _harvest_distributed_results():
    while result := self._zmq_server.get(block=False):
        # Retrieve pending info
        chapter_id, agent_id, agent_type, revision_number, future = \
            self._pending_distributed.pop(task_id)

        # Unified processing (same as local)
        pr = self._process_contributor_result(
            status=result.status,
            branch_name=result.branch_name,
            agent_id=agent_id,
            agent_type=agent_type,
            iterations=result.iterations,
            ...
        )

        future.set_result(pr)
```

## Extension Points

**Adding a new contributor mode:**
1. Add mode to `ContributorMode` enum in `agents/contributor.py`
2. Create mode-specific prompt (e.g., `NEW_MODE_PROMPT`)
3. Add prompt selection in `_get_mode_prompt()`
4. Add user prompt builder `_build_new_mode_prompt()`
5. Add launch logic to coordinator

**Adding a new review type:**
1. Create reviewer class in `agents/reviewers.py`
2. Add to `review_pr()` coordination function

**Custom state tracking:**
1. Extend `RunState` with new fields
2. Update `save()` and `load()` methods

## Contributor Task & Result Types

### ContributorTask

Specifies what the contributor should do:

```python
@dataclass
class ContributorTask:
    mode: ContributorMode
    chapter_id: str | None = None
    theorem_name: str | None = None
    issue_id: int | None = None
    lean_path: str = ""
    source_tex_path: str = ""

    # Factory methods for convenience
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

### ContributorResult

Unified result for all modes:

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

## Agent Lifecycle & Task Management

The coordinator manages contributor agents using task dictionaries:

| Dictionary | Purpose | Key → Value |
|------------|---------|-------------|
| `_agent_tasks` | Sketch, Prove, Fix agents | `Task → chapter_id` |
| `_scanner_tasks` | Scan agents | `Task → scanner_id` |
| `_triage_tasks` | Triage agents | `Task → agent_id` |
| `_review_tasks` | PR reviews | `Task → (pr_id, revision)` |

### Contributor Lifecycle

| Mode | Task Dict | Returns | Review Type |
|------|-----------|---------|-------------|
| SKETCH | `_agent_tasks` | `SimplePR` | SKETCH |
| PROVE | `_agent_tasks` | `SimplePR` | PROVE |
| FIX | `_agent_tasks` | `SimplePR` | FIX |
| SCAN | `_scanner_tasks` | `ContributorResult` | SCAN |
| TRIAGE | `_triage_tasks` | `SimplePR` or `None` | FIX |

## Agent Launch Logic

The coordinator continuously launches agents until the run is complete. Each agent type has specific launch conditions:

### Completion Condition

The run terminates when ALL of the following are true:
1. All chapters have `sketch_merged = True`
2. All theorems (sorries) in each chapter are in `completed_theorems`
3. No open issues remain (`open_issue_count == 0`)
4. All task queues are empty (no agents running)

```python
def _is_complete(self) -> bool:
    # Must have no open issues
    if self.state.open_issue_count > 0:
        return False

    for chapter_id, chapter_info in self.state.chapters.items():
        # Must have sketch merged
        if not chapter_info.get("sketch_merged"):
            return False

        # All sorries must be completed
        sorries = self._scan_sorries(lean_path)
        completed = self.state.completed_theorems.get(chapter_id, [])
        if any(s not in completed for s in sorries):
            return False

    return True
```

### Sketchers (once per chapter, relaunch on failure)

| Condition | Action |
|-----------|--------|
| Chapter has no `sketch_merged` | Check for existing PR |
| No active/pending PR exists | Launch sketcher |
| PR status is `failed` | Relaunch sketcher |
| PR status is `pending_review`/`approved`/`merged` | Skip (already in progress) |

```python
def _has_sketch_pr(chapter_id) -> bool:
    # Returns True only if there's an active (non-failed) sketch PR
    for pr in self.state.prs.values():
        if pr.chapter_id == chapter_id and pr.agent_type == "sketch":
            if pr.status not in ("merged", "failed"):
                return True  # Active PR exists, don't relaunch
    return False
```

### Provers (once per theorem, relaunch on failure)

| Condition | Action |
|-----------|--------|
| Theorem has `sorry` | Check if already completed |
| Not in `completed_theorems` | Check for existing PR |
| No active/pending PR exists | Launch prover |
| PR status is `failed` | Relaunch prover |
| PR status is `pending_review`/`approved`/`merged` | Skip (already in progress) |

```python
def _has_prover_pr(chapter_id, theorem_name) -> bool:
    # Returns True only if there's an active (non-failed) prove PR
    for pr in self.state.prs.values():
        if (pr.chapter_id == chapter_id and
            pr.theorem_name == theorem_name and
            pr.agent_type == "prove"):
            if pr.status not in ("merged", "failed"):
                return True  # Active PR exists, don't relaunch
    return False
```

### Maintainers (after first sketch, assigned issues)

| Condition | Action |
|-----------|--------|
| At least one sketch merged | Proceed to check issues |
| No sketches merged yet | Skip (no Lean code to work on) |
| Open issues not already assigned | Launch maintainer with specific issue |
| Below `max_concurrent_contributors` | Launch additional maintainers |
| More issues than slots | Randomize which issues to assign |
| No unassigned open issues | Skip |

### Scanners (periodic, every 5 minutes)

| Condition | Action |
|-----------|--------|
| 5 minutes since last scan | Launch scanner |
| Below `max_concurrent_scanners` | Launch scanner |
| Less than 5 minutes elapsed | Skip |

```python
_scanner_interval = 300.0  # 5 minutes
_scanner_last_run = 0.0

def _launch_scanners():
    if time.time() - _scanner_last_run < _scanner_interval:
        return False  # Too soon
    # ... launch scanner ...
    _scanner_last_run = time.time()
```

### Triage (periodic, every 5 minutes)

| Condition | Action |
|-----------|--------|
| 5 minutes since last triage | Launch triage agent |
| Below `max_concurrent_triage` | Launch triage |
| Less than 5 minutes elapsed | Skip |

### Summary Table

| Agent Type | Launch Trigger | Relaunch Condition | Periodic |
|------------|----------------|-------------------|----------|
| Sketcher | Chapter without sketch | PR failed | No |
| Prover | Theorem with sorry | PR failed | No |
| Maintainer | Open issues exist | Always while issues open | No |
| Scanner | Capacity available | Every 5 minutes | Yes |
| Triage | Capacity available | Every 5 minutes | Yes |

### PR Status Flow

PR statuses and transitions:

- `pending_review` → review pipeline runs
    - `APPROVE` → `approved`
    - `REQUEST_CHANGES` → `needs_revision`
    - `REJECT` → `failed`
- `approved` → merge attempt
    - Merge + build pass → `merged` (terminal)
    - Merge conflict or build fail → `needs_revision`
- `needs_revision` → agent revises with feedback
    - Under `max_revisions` → `revision_in_progress` → agent runs → `pending_review`
    - At `max_revisions` → `failed`
- `failed` → agent relaunched from scratch (new agent_id, new branch)

## Issues System

Issues are tracked in `ISSUES.md` with a simple format:

```markdown
# Issues

## Open Issues
- [ ] #1: [chapter-id] Description. Location: <where in file>.

## Closed Issues
- [x] #2: [chapter-id] Description. (resolved: <what resolved it>)
```

**Issue lifecycle:**
1. Created via Contributor(SCAN) or Contributor(PROVE) with ISSUE outcome
2. Worked on by Contributor(MAINTAIN)
3. Closed by Contributor(TRIAGE) when detected as resolved
4. All changes go through PR review before merge

**Design Decision: Agents Edit ISSUES.md Directly**

The agent edits ISSUES.md in its worktree, commits, and creates a PR. The coordinator does NOT manage issue IDs or create issues directly.

Benefits:
- Serialized issue IDs via merge (no race conditions)
- Clear merge conflict handling via git
- Integrated review for quality
- Single source of truth (ISSUES.md)
