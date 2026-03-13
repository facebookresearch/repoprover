# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Standalone Lean checker with REPL management and response parsing.

Provides CheckResult, LeanChecker and LeanCheckerConfig — the same external
API as the orchestrator version, but without dependencies on services/ or
orchestrator/.
"""

from __future__ import annotations

import enum
import json
import os
import queue
import re
import selectors
import subprocess
import threading
import time as time_module
from concurrent.futures import Future, InvalidStateError
from contextlib import ExitStack
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any

logger = getLogger(__name__)

# =============================================================================
# Header splitting
# =============================================================================


def _split_imports_and_body(code: str) -> tuple[str, str]:
    """Split Lean code into header (imports) and body."""
    lines = code.splitlines()
    header_lines: list[str] = []
    body_start = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if stripped.startswith("import "):
            header_lines.append(stripped)
        else:
            body_start = i
            break
    header = "\n".join(sorted(set(header_lines)))
    body = "\n".join(lines[body_start:]).lstrip("\n")
    return header, body


# =============================================================================
# REPL response parsing
# =============================================================================


@enum.unique
class MessageSeverity(enum.StrEnum):
    ERROR = enum.auto()
    WARNING = enum.auto()
    INFO = enum.auto()

    @property
    def rank(self) -> int:
        return {
            MessageSeverity.INFO: 0,
            MessageSeverity.WARNING: 1,
            MessageSeverity.ERROR: 2,
        }[self]


@dataclass(frozen=True, slots=True, order=True)
class Pos:
    line: int
    column: int

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Pos:
        if d is None:
            return cls(line=0, column=0)
        return cls(line=int(d["line"]), column=int(d["column"]))


@dataclass(frozen=True, slots=True, order=True)
class LeanMessage:
    sort_key: tuple[int, int, int] = field(init=False, repr=False)
    severity: MessageSeverity
    pos: Pos
    endPos: Pos | None
    data: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sort_key",
            (-self.severity.rank, self.pos.line, self.pos.column),
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LeanMessage:
        return cls(
            severity=MessageSeverity(d["severity"]),
            pos=Pos.from_dict(d["pos"]),
            endPos=Pos.from_dict(d["endPos"]) if d.get("endPos") else None,
            data=str(d.get("data", "")),
        )


@dataclass(frozen=True, slots=True, order=True)
class SorryInfo:
    sort_key: tuple[int, int, int] = field(init=False, repr=False)
    pos: Pos
    endPos: Pos
    goal: str
    proofState: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sort_key",
            (self.pos.line, self.pos.column, self.proofState),
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SorryInfo:
        return cls(
            pos=Pos.from_dict(d["pos"]),
            endPos=Pos.from_dict(d["endPos"]),
            goal=str(d.get("goal", "")),
            proofState=int(d["proofState"]),
        )


@dataclass(frozen=True, slots=True, order=True)
class TacticInfo:
    sort_key: tuple[int, int] = field(init=False, repr=False)
    pos: Pos
    endPos: Pos
    goals: str
    tactic: str
    proofState: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "sort_key", (self.pos.line, self.pos.column)
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TacticInfo:
        return cls(
            pos=Pos.from_dict(d["pos"]),
            endPos=Pos.from_dict(d["endPos"]),
            goals=str(d.get("goals", "")),
            tactic=str(d.get("tactic", "")),
            proofState=int(d["proofState"])
            if d.get("proofState") is not None
            else None,
        )


@dataclass(slots=True)
class CommandResponse:
    env: int | None = None
    messages: list[LeanMessage] = field(default_factory=list)
    sorries: list[SorryInfo] = field(default_factory=list)
    tactics: list[TacticInfo] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CommandResponse:
        return cls(
            env=int(d["env"]) if d.get("env") is not None else None,
            messages=[LeanMessage.from_dict(m) for m in d.get("messages", [])],
            sorries=[SorryInfo.from_dict(s) for s in d.get("sorries", [])],
            tactics=[TacticInfo.from_dict(t) for t in d.get("tactics", [])],
        )


@enum.unique
class ReplOutcome(enum.StrEnum):
    SUCCESS = enum.auto()
    SUCCESS_WITH_FEEDBACK = enum.auto()
    HAS_SORRY = enum.auto()
    ERROR = enum.auto()
    REPL_ERROR = enum.auto()


def _parse_repl_response(response: dict[str, Any]) -> CommandResponse:
    cmd = CommandResponse.from_dict(response)
    cmd.messages.sort()
    cmd.sorries.sort()
    cmd.tactics.sort()
    return cmd


def _parse_repl_response_outcome(response: dict[str, Any]) -> ReplOutcome:
    if response.get("repl_error") is not None:
        return ReplOutcome.REPL_ERROR
    cmd = _parse_repl_response(response)
    if cmd.messages and cmd.messages[0].severity is MessageSeverity.ERROR:
        return ReplOutcome.ERROR
    if cmd.sorries:
        return ReplOutcome.HAS_SORRY
    if cmd.messages:
        return ReplOutcome.SUCCESS_WITH_FEEDBACK
    return ReplOutcome.SUCCESS


# =============================================================================
# Lean REPL process
# =============================================================================


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a process and all its children."""
    try:
        import signal

        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


def _make_mem_limit_preexec(mem_limit_gb: int):
    """Return a preexec_fn that sets RLIMIT_AS for the child process."""
    if mem_limit_gb <= 0:
        return None
    try:
        import resource
        limit_bytes = mem_limit_gb * 1024 * 1024 * 1024

        def _set_limit() -> None:
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))

        return _set_limit
    except ImportError:
        return None


class _LeanRepl:
    """Lean REPL process manager."""

    def __init__(self, cwd: str, request_timeout: float = 30.0,
                 header_timeout: float = 180.0, max_retries: int = 1,
                 mem_limit_gb: int = 24):
        self.cwd = cwd
        self.request_timeout = request_timeout
        self.header_timeout = header_timeout
        self.max_retries = max_retries
        self.mem_limit_gb = mem_limit_gb
        self.process: subprocess.Popen | None = None
        self._header_to_env_id: dict[str, int] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        if not os.path.isdir(self.cwd):
            raise FileNotFoundError(f"REPL cwd does not exist: {self.cwd}")
        preexec_fn = _make_mem_limit_preexec(self.mem_limit_gb)
        self.process = subprocess.Popen(
            ["lake", "exe", "repl"],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=preexec_fn,
        )

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            _kill_process_tree(self.process)
        self.process = None
        self._header_to_env_id.clear()

    def restart(self) -> None:
        self.close()
        self.start()

    def __enter__(self) -> _LeanRepl:
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def run(self, code: str, env_id: int | None = None,
            timeout: float | None = None,
            all_tactics: bool = False) -> dict[str, Any]:
        timeout = timeout or self.request_timeout
        run_from_env = env_id is not None
        max_retries = 0 if run_from_env else self.max_retries

        if not run_from_env:
            header, code = _split_imports_and_body(code)

        with self._lock:
            for i in range(max_retries + 1):
                try:
                    if not run_from_env:
                        env_id, output = self._get_env_for_header(header)
                        if env_id is None:
                            return output
                    return self._run(code, env_id, timeout, all_tactics)
                except (TimeoutError, RuntimeError, json.JSONDecodeError) as e:
                    logger.error(
                        "REPL error: %s (attempt %d/%d)", e, i + 1,
                        max_retries + 1,
                    )
                    self.restart()
            return {"repl_error": "Exceeded maximum retries"}

    def _get_env_for_header(
        self, header: str
    ) -> tuple[int | None, dict[str, Any]]:
        test_cmd = "example (n : Nat) : n = n := by rfl"

        if header in self._header_to_env_id:
            env_id = self._header_to_env_id[header]
            response = self._run(test_cmd, env_id, self.request_timeout)
            if "messages" not in response:
                return env_id, response

        code = header + "\n\n" + test_cmd
        response = self._run(code, None, self.header_timeout)
        if "messages" in response:
            return None, {
                "env": None,
                "messages": [{
                    "severity": "error",
                    "pos": {"line": 0, "column": 0},
                    "endPos": None,
                    "data": f"Import failed. Header:\n{header}",
                }],
            }
        env_id = response["env"]
        self._header_to_env_id[header] = env_id
        return env_id, response

    def _run(self, code: str, env_id: int | None, timeout: float,
             all_tactics: bool = False) -> dict[str, Any]:
        cmd_obj: dict[str, Any] = {"cmd": code}
        if env_id is not None:
            cmd_obj["env"] = env_id
        if all_tactics:
            cmd_obj["allTactics"] = True
        command = json.dumps(cmd_obj) + "\n\n"

        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("REPL process not running")

        self.process.stdin.write(command.encode("utf-8"))
        self.process.stdin.flush()

        response_buffer = ""
        end_time = time_module.monotonic() + timeout

        sel = selectors.DefaultSelector()
        try:
            sel.register(self.process.stdout, selectors.EVENT_READ, "stdout")
            sel.register(self.process.stderr, selectors.EVENT_READ, "stderr")

            while True:
                remaining = end_time - time_module.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"REPL timed out after {timeout}s"
                    )
                ready = sel.select(timeout=remaining)
                if not ready:
                    raise TimeoutError(
                        f"REPL timed out after {timeout}s"
                    )
                for key, _ in ready:
                    if key.data == "stdout":
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if not chunk:
                            raise RuntimeError("REPL process terminated")
                        response_buffer += chunk.decode("utf-8", errors="replace")
                        if "\n\n" in response_buffer:
                            raw, _ = response_buffer.split("\n\n", 1)
                            return json.loads(raw.strip())
                    elif key.data == "stderr":
                        err = os.read(key.fileobj.fileno(), 4096)
                        if err:
                            logger.error(
                                "REPL stderr: %s",
                                err.decode("utf-8", errors="replace").rstrip(),
                            )
        finally:
            sel.close()


# =============================================================================
# REPL pool
# =============================================================================


class _LeanReplPool:
    """Thread-based pool of _LeanRepl instances."""

    def __init__(self, cwd: str, capacity: int, **repl_kwargs: Any):
        self.capacity = capacity
        self._cwd = cwd
        self._repl_kwargs = repl_kwargs
        self._tasks: queue.Queue[
            tuple[tuple[Any, ...], dict[str, Any], Future]
        ] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._shutdown = False

        for i in range(capacity):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"lean-repl-pool-{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    def _worker_loop(self) -> None:
        repl: _LeanRepl | None = None
        try:
            while True:
                try:
                    args, kwargs, fut = self._tasks.get(timeout=0.5)
                except queue.Empty:
                    if self._shutdown:
                        break
                    continue
                if fut.cancelled():
                    self._tasks.task_done()
                    continue
                if repl is None:
                    repl = _LeanRepl(self._cwd, **self._repl_kwargs)
                    repl.start()
                try:
                    result = repl.run(*args, **kwargs)
                except Exception as e:
                    if not fut.cancelled():
                        try:
                            fut.set_exception(e)
                        except InvalidStateError:
                            pass
                else:
                    if not fut.cancelled():
                        try:
                            fut.set_result(result)
                        except InvalidStateError:
                            pass
                finally:
                    self._tasks.task_done()
        finally:
            if repl is not None:
                repl.close()

    def submit(self, *args: Any, **kwargs: Any) -> Future:
        if self._shutdown:
            raise RuntimeError("Pool is shut down")
        fut: Future = Future()
        self._tasks.put((args, kwargs, fut))
        return fut

    def shutdown(self, wait: bool = True) -> None:
        self._shutdown = True
        if wait:
            for t in self._threads:
                t.join()

    def __enter__(self) -> _LeanReplPool:
        return self

    def __exit__(self, *args: Any) -> None:
        self.shutdown(wait=True)


# =============================================================================
# Public API: CheckResult, LeanCheckerConfig, LeanChecker
# =============================================================================


@dataclass
class CheckResult:
    """Result from checking Lean code."""

    success: bool
    outcome: ReplOutcome
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sorries: list[dict[str, Any]] = field(default_factory=list)
    raw_response: dict[str, Any] = field(default_factory=dict)
    error_contexts: dict[int, str] = field(default_factory=dict)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def has_sorries(self) -> bool:
        return bool(self.sorries)

    def format_errors(self) -> str:
        if not self.errors:
            return "No errors."
        return "\n".join(f"- {e}" for e in self.errors)

    def format_sorries(self) -> str:
        if not self.sorries:
            return "No sorries."
        lines = []
        for sorry in self.sorries:
            lines.append(
                f"- Line {sorry.get('line', '?')}: "
                f"{sorry.get('goal', 'unknown goal')}"
            )
        return "\n".join(lines)

    def format_for_agent(self) -> str:
        parts = []
        if self.has_errors:
            parts.append(f"## Compilation Errors ({len(self.errors)})\n")
            parts.append(self.format_errors())
        else:
            parts.append("Compiles successfully")
        if self.has_sorries:
            parts.append(f"\n\n## Sorries ({len(self.sorries)})\n")
            parts.append(self.format_sorries())
        if self.warnings:
            parts.append(f"\n\n## Warnings ({len(self.warnings)})\n")
            parts.append("\n".join(f"- {w}" for w in self.warnings[:5]))
            if len(self.warnings) > 5:
                parts.append(
                    f"\n... and {len(self.warnings) - 5} more warnings"
                )
        return "".join(parts)


@dataclass
class LeanCheckerConfig:
    """Configuration for the Lean checker."""

    workspace: str = ""
    timeout: float = 120.0
    header_timeout: float = 180.0
    pool_size: int = 0
    instance_mem_limit_gb: int = 24

    def __post_init__(self) -> None:
        if not self.workspace:
            self.workspace = str(Path(__file__).parent.parent.parent.parent)


class LeanChecker:
    """Wrapper for checking Lean code via the REPL.

    When pool_size > 0, uses a thread pool of REPL instances for concurrent
    checks. Otherwise uses a single REPL with a lock.
    """

    def __init__(self, config: LeanCheckerConfig | None = None):
        self.config = config or LeanCheckerConfig()
        self._exit_stack: ExitStack | None = None
        self._repl: _LeanRepl | None = None
        self._pool: _LeanReplPool | None = None

    def start(self) -> None:
        if self._repl is not None or self._pool is not None:
            return
        self._exit_stack = ExitStack()
        if self.config.pool_size > 0:
            self._pool = _LeanReplPool(
                cwd=self.config.workspace,
                capacity=self.config.pool_size,
                request_timeout=self.config.timeout,
                header_timeout=self.config.header_timeout,
                mem_limit_gb=self.config.instance_mem_limit_gb,
            )
            self._exit_stack.enter_context(self._pool)
        else:
            self._repl = _LeanRepl(
                cwd=self.config.workspace,
                request_timeout=self.config.timeout,
                header_timeout=self.config.header_timeout,
                mem_limit_gb=self.config.instance_mem_limit_gb,
            )
            self._exit_stack.enter_context(self._repl)

    def close(self) -> None:
        if self._exit_stack:
            self._exit_stack.close()
            self._exit_stack = None
            self._repl = None
            self._pool = None

    def __enter__(self) -> LeanChecker:
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _run_code(self, code: str, timeout: float | None = None,
                  all_tactics: bool = False) -> dict[str, Any]:
        timeout = timeout or self.config.timeout
        if self._pool is not None:
            future = self._pool.submit(code, timeout=timeout,
                                       all_tactics=all_tactics)
            return future.result()
        else:
            if self._repl is None:
                self.start()
            assert self._repl is not None
            return self._repl.run(code, timeout=timeout,
                                  all_tactics=all_tactics)

    def check_code(self, code: str, timeout: float | None = None,
                   all_tactics: bool = False) -> CheckResult:
        """Check Lean code and return structured result."""
        if self._repl is None and self._pool is None:
            self.start()

        raw_response = self._run_code(code, timeout=timeout,
                                       all_tactics=all_tactics)

        if raw_response.get("repl_error") is not None:
            return CheckResult(
                success=False,
                outcome=ReplOutcome.REPL_ERROR,
                errors=[f"REPL error: {raw_response['repl_error']}"],
                raw_response=raw_response,
            )

        outcome = _parse_repl_response_outcome(raw_response)
        cmd = _parse_repl_response(raw_response)

        errors = []
        warnings = []
        for msg in cmd.messages:
            formatted = f"Line {msg.pos.line}:{msg.pos.column}: {msg.data}"
            if msg.severity == MessageSeverity.ERROR:
                errors.append(formatted)
            elif msg.severity == MessageSeverity.WARNING:
                warnings.append(formatted)

        sorries = []
        for sorry in cmd.sorries:
            sorries.append({
                "line": sorry.pos.line,
                "column": sorry.pos.column,
                "goal": sorry.goal,
                "proof_state": sorry.proofState,
            })

        tactics = []
        for tactic in cmd.tactics:
            tactics.append({
                "line": tactic.pos.line,
                "column": tactic.pos.column,
                "end_line": tactic.endPos.line,
                "end_column": tactic.endPos.column,
                "goals": tactic.goals,
                "tactic": tactic.tactic,
            })

        success = outcome in (
            ReplOutcome.SUCCESS,
            ReplOutcome.SUCCESS_WITH_FEEDBACK,
            ReplOutcome.HAS_SORRY,
        )
        if errors:
            success = False

        error_contexts: dict[int, str] = {}
        if errors and tactics:
            error_line_re = re.compile(r"^Line (\d+):(\d+):")
            for err in errors:
                m = error_line_re.match(err)
                if not m:
                    continue
                err_line = int(m.group(1))
                err_col = int(m.group(2))
                best_goals: str | None = None
                best_pos: tuple[int, int] = (-1, -1)
                for t in tactics:
                    t_line, t_col = t["line"], t["column"]
                    if (t_line, t_col) <= (err_line, err_col):
                        if (t_line, t_col) > best_pos:
                            best_pos = (t_line, t_col)
                            best_goals = t["goals"]
                if best_goals:
                    error_contexts[err_line] = best_goals

        return CheckResult(
            success=success,
            outcome=outcome,
            errors=errors,
            warnings=warnings,
            sorries=sorries,
            raw_response=raw_response,
            error_contexts=error_contexts,
        )

    def check_code_with_context(self, code: str,
                                 timeout: float | None = None) -> CheckResult:
        """Check code with allTactics to get goal states at error locations."""
        return self.check_code(code, timeout=timeout, all_tactics=True)

    def verify_compilation(self, code: str) -> tuple[bool, str]:
        """Verify that Lean code compiles (possibly with sorries)."""
        result = self.check_code(code)
        if result.has_errors:
            return False, result.format_errors()
        if result.has_sorries:
            return True, f"Compiles with {len(result.sorries)} sorry(s)"
        return True, "Compiles cleanly"
