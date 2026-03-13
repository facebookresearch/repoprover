# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Tests for safe_shell.py - Safe command execution with pipes."""

import tempfile
from pathlib import Path

import pytest

from repoprover.safe_shell import (
    AgentRole,
    SafeShell,
    SafeShellConfig,
    ShellResult,
)


@pytest.fixture
def temp_repo():
    """Create a temporary directory to act as a repo root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir)
        # Create some test files
        (repo / "file1.txt").write_text("hello world\nfoo bar\nbaz qux\n")
        (repo / "file2.txt").write_text("line one\nline two\nline three\n")
        (repo / "subdir").mkdir()
        (repo / "subdir" / "nested.txt").write_text("nested content\nwith sorry\n")
        yield repo


@pytest.fixture
def shell(temp_repo):
    """Create a SafeShell instance with WORKER role."""
    config = SafeShellConfig(repo_root=temp_repo, role=AgentRole.WORKER)
    return SafeShell(config)


@pytest.fixture
def reader_shell(temp_repo):
    """Create a SafeShell instance with READER role."""
    config = SafeShellConfig(repo_root=temp_repo, role=AgentRole.READER)
    return SafeShell(config)


@pytest.fixture
def merger_shell(temp_repo):
    """Create a SafeShell instance with MERGER role."""
    config = SafeShellConfig(repo_root=temp_repo, role=AgentRole.MERGER)
    return SafeShell(config)


# =============================================================================
# Basic Command Execution
# =============================================================================


class TestBasicCommands:
    """Test basic allowed commands."""

    def test_cat(self, shell, temp_repo):  # noqa: ARG002
        result = shell.run("cat file1.txt")
        assert result.success
        assert "hello world" in result.stdout
        assert "foo bar" in result.stdout

    def test_ls(self, shell):
        result = shell.run("ls")
        assert result.success
        assert "file1.txt" in result.stdout
        assert "file2.txt" in result.stdout
        assert "subdir" in result.stdout

    def test_echo(self, shell):
        result = shell.run("echo hello")
        assert result.success
        assert result.stdout.strip() == "hello"

    def test_wc(self, shell):
        result = shell.run("wc -l file1.txt")
        assert result.success
        assert "3" in result.stdout or "file1.txt" in result.stdout

    def test_head(self, shell):
        result = shell.run("head -1 file1.txt")
        assert result.success
        assert "hello world" in result.stdout

    def test_tail(self, shell):
        result = shell.run("tail -1 file1.txt")
        assert result.success
        assert "baz qux" in result.stdout

    def test_grep(self, shell):
        result = shell.run("grep foo file1.txt")
        assert result.success
        assert "foo bar" in result.stdout

    def test_grep_not_found(self, shell):
        result = shell.run("grep notfound file1.txt")
        assert not result.success  # grep returns 1 when no match
        assert result.return_code == 1

    def test_find(self, shell):
        result = shell.run("find . -name '*.txt'")
        assert result.success
        assert "file1.txt" in result.stdout

    def test_sort(self, shell, temp_repo):
        (temp_repo / "unsorted.txt").write_text("c\na\nb\n")
        result = shell.run("sort unsorted.txt")
        assert result.success
        assert result.stdout.strip() == "a\nb\nc"


# =============================================================================
# Pipe Support
# =============================================================================


class TestPipes:
    """Test pipe support."""

    def test_simple_pipe(self, shell):
        result = shell.run("cat file1.txt | wc -l")
        assert result.success
        assert "3" in result.stdout

    def test_grep_pipe_sort(self, shell, temp_repo):
        (temp_repo / "data.txt").write_text("banana\napple\ncherry\napricot\n")
        result = shell.run("grep ap data.txt | sort")
        assert result.success
        # Should have apple and apricot, sorted
        lines = result.stdout.strip().split("\n")
        assert lines == ["apple", "apricot"]

    def test_multiple_pipes(self, shell, temp_repo):
        (temp_repo / "data.txt").write_text("a\nb\na\nc\na\nb\n")
        result = shell.run("cat data.txt | sort | uniq -c")
        assert result.success
        assert "3" in result.stdout  # 'a' appears 3 times

    def test_grep_pipe_wc(self, shell):
        result = shell.run("grep -r txt . | wc -l")
        assert result.success

    def test_find_pipe_xargs_grep(self, shell):
        result = shell.run("find . -name '*.txt' | xargs grep sorry")
        assert result.success
        assert "sorry" in result.stdout

    def test_cut_pipe(self, shell, temp_repo):
        (temp_repo / "csv.txt").write_text("a,b,c\n1,2,3\n")
        result = shell.run("cat csv.txt | cut -d',' -f2")
        assert result.success
        assert "b" in result.stdout
        assert "2" in result.stdout


# =============================================================================
# Forbidden Commands
# =============================================================================


class TestForbiddenCommands:
    """Test that forbidden commands are blocked."""

    def test_rm_blocked(self, shell):
        result = shell.run("rm file1.txt")
        assert not result.success
        assert "not allowed" in result.error.lower()

    def test_mv_blocked(self, shell):
        result = shell.run("mv file1.txt file3.txt")
        assert not result.success
        assert "not allowed" in result.error.lower()

    def test_cp_blocked(self, shell):
        result = shell.run("cp file1.txt file3.txt")
        assert not result.success
        assert "not allowed" in result.error.lower()

    def test_curl_blocked(self, shell):
        result = shell.run("curl https://example.com")
        assert not result.success
        assert "not allowed" in result.error.lower()

    def test_wget_blocked(self, shell):
        result = shell.run("wget https://example.com")
        assert not result.success
        assert "not allowed" in result.error.lower()

    def test_python_blocked(self, shell):
        result = shell.run("python -c 'print(1)'")
        assert not result.success
        assert "not allowed" in result.error.lower()

    def test_sudo_blocked(self, shell):
        result = shell.run("sudo ls")
        assert not result.success
        assert "not allowed" in result.error.lower()

    def test_unknown_command_blocked(self, shell):
        result = shell.run("my_custom_script.sh")
        assert not result.success
        assert "not allowed" in result.error.lower()

    def test_sed_i_blocked(self, shell):
        """In-place sed editing should be blocked."""
        result = shell.run("sed -i 's/foo/bar/' file1.txt")
        assert not result.success
        assert "sed -i" in result.error.lower() or "not allowed" in result.error.lower()


# =============================================================================
# Forbidden Shell Constructs
# =============================================================================


class TestForbiddenShellConstructs:
    """Test that dangerous shell constructs are blocked."""

    def test_semicolon_blocked(self, shell):
        result = shell.run("echo foo; rm file1.txt")
        assert not result.success
        assert "semicolon" in result.error.lower()

    def test_ampersand_background_blocked(self, shell):
        result = shell.run("sleep 10 &")
        assert not result.success
        assert "background" in result.error.lower()

    def test_command_substitution_dollar_paren_blocked(self, shell):
        result = shell.run("echo $(whoami)")
        assert not result.success
        assert "substitution" in result.error.lower()

    def test_command_substitution_backtick_blocked(self, shell):
        result = shell.run("echo `whoami`")
        assert not result.success
        assert "substitution" in result.error.lower()

    def test_redirect_blocked(self, shell):
        result = shell.run("echo foo > output.txt")
        assert not result.success
        assert "redirect" in result.error.lower()

    def test_append_redirect_blocked(self, shell):
        result = shell.run("echo foo >> output.txt")
        assert not result.success
        assert "redirect" in result.error.lower()

    def test_variable_expansion_blocked(self, shell):
        result = shell.run("echo $HOME")
        assert not result.success
        assert "variable" in result.error.lower()

    def test_variable_expansion_braces_blocked(self, shell):
        result = shell.run("echo ${HOME}")
        assert not result.success
        assert "variable" in result.error.lower()


# =============================================================================
# FD Redirect Support (2>&1, >&2, etc.)
# =============================================================================


class TestFDRedirects:
    """Test that file descriptor redirects are allowed."""

    def test_stderr_to_stdout(self, shell):
        """2>&1 should be allowed - very common pattern."""
        result = shell.run("echo test 2>&1")
        assert result.success
        assert "test" in result.stdout

    def test_stderr_to_stdout_with_pipe(self, shell):
        """2>&1 | head should work - the pattern that was blocked."""
        result = shell.run("echo test 2>&1 | head -1")
        assert result.success
        assert "test" in result.stdout

    def test_stdout_to_stderr(self, shell):
        """>&2 should be allowed."""
        result = shell.run("echo test >&2")
        assert result.success
        # Output goes to stderr
        assert "test" in result.stderr

    def test_fd_close(self, shell):
        """2>&- should be allowed (close fd)."""
        result = shell.run("echo test 2>&-")
        assert result.success

    def test_fd_redirect_with_conditional(self, shell):
        """FD redirects combined with && should work."""
        result = shell.run("echo foo 2>&1 && echo bar")
        assert result.success
        assert "foo" in result.stdout
        assert "bar" in result.stdout

    def test_complex_pipeline_with_fd_redirect(self, shell, temp_repo):
        """Real-world pattern: lake build 2>&1 | head."""
        # We can't run lake, but we can test the pattern with echo
        result = shell.run("echo 'build output' 2>&1 | head -1")
        assert result.success
        assert "build output" in result.stdout

    def test_stderr_to_devnull(self, shell):
        """2>/dev/null should be allowed - very common pattern."""
        result = shell.run("echo test 2>/dev/null")
        assert result.success
        assert "test" in result.stdout

    def test_stderr_to_devnull_with_pipe(self, shell):
        """2>/dev/null | head should work."""
        result = shell.run("echo test 2>/dev/null | head -1")
        assert result.success
        assert "test" in result.stdout

    def test_stdout_to_devnull(self, shell):
        """>/dev/null should be allowed."""
        result = shell.run("echo test >/dev/null")
        assert result.success
        # Output goes to /dev/null, so stdout should be empty
        assert "test" not in result.stdout

    def test_both_to_devnull(self, shell):
        """>/dev/null 2>&1 should be allowed."""
        result = shell.run("echo test >/dev/null 2>&1")
        assert result.success

    def test_append_to_devnull(self, shell):
        """>>/dev/null should be allowed."""
        result = shell.run("echo test >>/dev/null")
        assert result.success

    def test_background_still_blocked(self, shell):
        """Background & at end of command should still be blocked."""
        result = shell.run("echo test &")
        assert not result.success
        assert "background" in result.error.lower()

    def test_background_in_middle_blocked(self, shell):
        """Background & between commands should still be blocked."""
        result = shell.run("sleep 1 & echo test")
        assert not result.success
        assert "background" in result.error.lower()


# =============================================================================
# Conditional Operators (&&, ||)
# =============================================================================


class TestConditionalOperators:
    """Test that && and || are now properly supported."""

    def test_and_operator_allowed(self, shell):
        """&& operator should work."""
        result = shell.run("echo foo && echo bar")
        assert result.success
        assert "foo" in result.stdout
        assert "bar" in result.stdout

    def test_or_operator_allowed(self, shell):
        """|| operator should work."""
        result = shell.run("false || echo fallback")
        assert result.success
        assert "fallback" in result.stdout

    def test_and_short_circuit(self, shell):
        """&& should short-circuit on failure."""
        result = shell.run("false && echo should_not_appear")
        assert not result.success  # false returns 1
        assert "should_not_appear" not in result.stdout

    def test_or_short_circuit(self, shell):
        """|| should short-circuit on success."""
        result = shell.run("echo first || echo should_not_appear")
        assert result.success
        assert "first" in result.stdout
        # The second echo might still run depending on shell, but first should be there

    def test_chained_conditionals(self, shell):
        """Multiple && and || should work."""
        result = shell.run("echo a && echo b && echo c")
        assert result.success
        assert "a" in result.stdout
        assert "b" in result.stdout
        assert "c" in result.stdout

    def test_mixed_pipe_and_conditional(self, shell, temp_repo):
        """Mix of pipes and conditionals."""
        (temp_repo / "test.txt").write_text("hello\nworld\n")
        result = shell.run("cat test.txt | grep hello && echo found")
        assert result.success
        assert "hello" in result.stdout
        assert "found" in result.stdout

    def test_quoted_and_operator_is_argument(self, shell):
        """'&&' in quotes should be treated as a string argument, not operator."""
        result = shell.run("echo '&&'")
        assert result.success
        assert "&&" in result.stdout

    def test_quoted_or_operator_is_argument(self, shell):
        """'||' in quotes should be treated as a string argument, not operator."""
        result = shell.run("echo '||'")
        assert result.success
        assert "||" in result.stdout

    def test_conditional_with_forbidden_command_blocked(self, shell):
        """Conditionals don't bypass command validation."""
        result = shell.run("echo foo && rm file.txt")
        assert not result.success
        assert "not allowed" in result.error.lower()

    def test_conditional_with_path_escape_blocked(self, shell):
        """Conditionals don't bypass path validation."""
        result = shell.run("echo foo && cat /etc/passwd")
        assert not result.success
        assert "escapes" in result.error.lower()


# =============================================================================
# Git Commands and Roles
# =============================================================================


class TestGitCommands:
    """Test git command access by role."""

    def test_git_status_allowed_reader(self, reader_shell):
        # This will fail because we're not in a git repo, but should pass validation
        result = reader_shell.run("git status")
        # The error should be from git, not from our validation
        assert "not allowed" not in result.error.lower()

    def test_git_log_allowed_reader(self, reader_shell):
        result = reader_shell.run("git log --oneline -5")
        assert "not allowed" not in result.error.lower()

    def test_git_add_blocked_for_reader(self, reader_shell):
        result = reader_shell.run("git add file1.txt")
        assert not result.success
        assert "permissions" in result.error.lower() or "not allowed" in result.error.lower()

    def test_git_commit_blocked_for_reader(self, reader_shell):
        result = reader_shell.run("git commit -m 'test'")
        assert not result.success
        assert "permissions" in result.error.lower() or "not allowed" in result.error.lower()

    def test_git_add_allowed_for_worker(self, shell):
        result = shell.run("git add file1.txt")
        # Should pass validation (may fail on git execution if not a repo)
        assert "permissions" not in result.error.lower()

    def test_git_merge_blocked_for_worker(self, shell):
        result = shell.run("git merge some-branch")
        assert not result.success
        assert "permissions" in result.error.lower() or "not allowed" in result.error.lower()

    def test_git_merge_allowed_for_merger(self, merger_shell):
        result = merger_shell.run("git merge some-branch")
        # Should pass validation
        assert "permissions" not in result.error.lower()

    def test_git_push_always_blocked(self, merger_shell):
        result = merger_shell.run("git push origin main")
        assert not result.success
        assert "not allowed" in result.error.lower()

    def test_git_pull_always_blocked(self, shell):
        result = shell.run("git pull origin main")
        assert not result.success
        assert "not allowed" in result.error.lower()

    def test_git_fetch_always_blocked(self, shell):
        result = shell.run("git fetch origin")
        assert not result.success
        assert "not allowed" in result.error.lower()

    def test_git_clone_always_blocked(self, shell):
        result = shell.run("git clone https://github.com/foo/bar")
        assert not result.success
        assert "not allowed" in result.error.lower()

    def test_git_worktree_blocked(self, shell):
        """git worktree should be blocked (managed by WorktreePool)."""
        result = shell.run("git worktree add ../new-worktree")
        assert not result.success
        assert "not allowed" in result.error.lower()


# =============================================================================
# Path Validation
# =============================================================================


class TestPathValidation:
    """Test path escape prevention."""

    def test_absolute_path_outside_repo_blocked(self, shell):
        result = shell.run("cat /etc/passwd")
        assert not result.success
        assert "escapes" in result.error.lower()

    def test_relative_path_escape_blocked(self, shell):
        result = shell.run("cat ../../../etc/passwd")
        assert not result.success
        assert "escapes" in result.error.lower()

    def test_path_within_repo_allowed(self, shell):
        result = shell.run("cat subdir/nested.txt")
        assert result.success
        assert "nested content" in result.stdout

    def test_dotdot_within_repo_allowed(self, shell, temp_repo):
        # Navigate into subdir then back out - should be allowed if we stay in repo
        (temp_repo / "subdir" / "test.txt").write_text("subdir test")
        result = shell.run("cat subdir/../file1.txt")
        assert result.success
        assert "hello world" in result.stdout


# =============================================================================
# xargs Validation
# =============================================================================


class TestXargsValidation:
    """Test xargs command validation."""

    def test_xargs_with_allowed_command(self, shell):
        result = shell.run("echo file1.txt | xargs cat")
        assert result.success
        assert "hello world" in result.stdout

    def test_xargs_with_grep(self, shell):
        result = shell.run("echo file1.txt | xargs grep foo")
        assert result.success
        assert "foo bar" in result.stdout

    def test_xargs_with_forbidden_command_blocked(self, shell):
        result = shell.run("echo file1.txt | xargs rm")
        assert not result.success
        assert "xargs cannot run" in result.error.lower() or "not allowed" in result.error.lower()

    def test_xargs_with_unknown_command_blocked(self, shell):
        result = shell.run("echo arg | xargs my_script")
        assert not result.success
        assert "xargs cannot run" in result.error.lower() or "not allowed" in result.error.lower()


# =============================================================================
# Output Handling
# =============================================================================


class TestOutputHandling:
    """Test output formatting and limits."""

    def test_format_for_agent_success(self):
        result = ShellResult(success=True, stdout="output", stderr="", return_code=0)
        formatted = result.format_for_agent()
        assert formatted == "output"

    def test_format_for_agent_with_stderr(self):
        result = ShellResult(success=False, stdout="out", stderr="err", return_code=1)
        formatted = result.format_for_agent()
        assert "out" in formatted
        assert "stderr:" in formatted
        assert "err" in formatted
        assert "exit code: 1" in formatted

    def test_format_for_agent_error(self):
        result = ShellResult(success=False, error="Validation failed")
        formatted = result.format_for_agent()
        assert "Error: Validation failed" == formatted

    def test_format_for_agent_no_output(self):
        result = ShellResult(success=True, stdout="", stderr="", return_code=0)
        formatted = result.format_for_agent()
        assert formatted == "(no output)"

    def test_output_truncation(self, shell, temp_repo):
        # Create a large file
        large_content = "x" * 100 + "\n"
        (temp_repo / "large.txt").write_text(large_content * 50000)  # ~5MB

        # Configure small limit for test
        shell.config.max_output_bytes = 1000

        result = shell.run("cat large.txt")
        assert result.success
        assert len(result.stdout) <= 1100  # Allow for truncation message
        assert "truncated" in result.stdout


# =============================================================================
# Timeout Handling
# =============================================================================


class TestTimeout:
    """Test timeout handling."""

    def test_timeout(self, temp_repo):
        config = SafeShellConfig(
            repo_root=temp_repo,
            role=AgentRole.WORKER,
            timeout_seconds=1,
        )
        shell = SafeShell(config)
        result = shell.run("sleep 10")
        assert not result.success
        assert "timed out" in result.error.lower()


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases and special scenarios."""

    def test_empty_command(self, shell):
        result = shell.run("")
        assert not result.success
        assert "empty" in result.error.lower()

    def test_whitespace_only_command(self, shell):
        result = shell.run("   ")
        assert not result.success

    def test_quoted_arguments(self, shell):
        result = shell.run("echo 'hello world'")
        assert result.success
        assert "hello world" in result.stdout

    def test_double_quoted_arguments(self, shell):
        result = shell.run('echo "hello world"')
        assert result.success
        assert "hello world" in result.stdout

    def test_grep_with_regex(self, shell):
        result = shell.run("grep 'foo.*' file1.txt")
        assert result.success
        assert "foo bar" in result.stdout

    def test_pipe_to_true(self, shell):
        """Pipe to true should succeed."""
        result = shell.run("echo test | true")
        assert result.success

    def test_special_characters_in_grep(self, shell, temp_repo):
        (temp_repo / "special.txt").write_text("foo.bar\nfoo-bar\nfoo_bar\n")
        result = shell.run(r"grep 'foo\.bar' special.txt")
        assert result.success
        assert "foo.bar" in result.stdout
        # Should NOT match foo-bar due to regex escaping

    def test_awk_command(self, shell, temp_repo):
        (temp_repo / "data.txt").write_text("1 2 3\n4 5 6\n")
        result = shell.run("awk '{print $2}' data.txt")
        assert result.success
        assert "2" in result.stdout
        assert "5" in result.stdout


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegration:
    """Integration tests with realistic scenarios."""

    def test_find_sorry_in_lean_files(self, shell, temp_repo):
        """Simulate finding sorry statements in Lean files."""
        (temp_repo / "Chapter1.lean").write_text("theorem foo : True := by sorry\ntheorem bar : False := by sorry\n")
        (temp_repo / "Chapter2.lean").write_text("theorem baz : True := trivial\n")

        result = shell.run("grep -r sorry *.lean | wc -l")
        assert result.success
        assert "2" in result.stdout

    def test_count_theorems(self, shell, temp_repo):
        """Count theorems in Lean files."""
        (temp_repo / "test.lean").write_text(
            "theorem foo : True := trivial\ntheorem bar : False := sorry\nlemma baz : True := trivial\n"
        )

        result = shell.run("grep -E '^(theorem|lemma)' test.lean | wc -l")
        assert result.success
        assert "3" in result.stdout

    def test_list_lean_files(self, shell, temp_repo):
        """List all Lean files."""
        (temp_repo / "A.lean").write_text("")
        (temp_repo / "B.lean").write_text("")
        (temp_repo / "subdir" / "C.lean").write_text("")

        result = shell.run("find . -name '*.lean' | sort")
        assert result.success
        assert "A.lean" in result.stdout
        assert "B.lean" in result.stdout
        assert "C.lean" in result.stdout


# =============================================================================
# Pipeline Splitting Edge Cases
# =============================================================================


class TestPipelineSplitting:
    """Test edge cases for pipeline splitting with quotes."""

    def test_pipe_in_double_quotes(self, shell):
        """Pipe inside double quotes should not split."""
        result = shell.run('echo "foo|bar"')
        assert result.success
        assert "foo|bar" in result.stdout

    def test_pipe_in_single_quotes(self, shell):
        """Pipe inside single quotes should not split."""
        result = shell.run("echo 'foo|bar'")
        assert result.success
        assert "foo|bar" in result.stdout

    def test_pipe_in_single_quotes_regex(self, shell, temp_repo):
        """Regex with alternation in single quotes."""
        (temp_repo / "test.txt").write_text("theorem\nlemma\nproof\n")
        result = shell.run("grep -E 'theorem|lemma' test.txt | wc -l")
        assert result.success
        assert result.stdout.strip() == "2"

    def test_pipe_in_double_quotes_regex(self, shell, temp_repo):
        """Regex with alternation in double quotes."""
        (temp_repo / "test.txt").write_text("foo\nbar\nbaz\n")
        result = shell.run('grep -E "foo|bar" test.txt | wc -l')
        assert result.success
        assert result.stdout.strip() == "2"

    def test_multiple_pipes(self, shell, temp_repo):
        """Multiple pipe segments."""
        (temp_repo / "data.txt").write_text("b\na\nc\na\nb\n")
        result = shell.run("cat data.txt | sort | uniq")
        assert result.success
        assert "a" in result.stdout
        assert "b" in result.stdout
        assert "c" in result.stdout

    def test_nested_quotes_with_pipe(self, shell):
        """Nested quotes with pipe character."""
        result = shell.run('''echo "it's a | test"''')
        assert result.success
        assert "|" in result.stdout

    def test_complex_regex_pattern(self, shell, temp_repo):
        """Complex regex with multiple special chars in quotes."""
        (temp_repo / "code.lean").write_text(
            "theorem foo : True := trivial\nlemma bar : False := sorry\ndef baz := 42\n"
        )
        result = shell.run("grep -E '^(theorem|lemma|def)' code.lean | wc -l")
        assert result.success
        assert result.stdout.strip() == "3"

    def test_pipe_with_awk_pattern(self, shell, temp_repo):
        """Awk with pipe character in pattern."""
        (temp_repo / "log.txt").write_text("ERROR|something\nINFO|other\nERROR|again\n")
        result = shell.run("cat log.txt | grep '^ERROR|' | wc -l")
        assert result.success
        assert result.stdout.strip() == "2"

    def test_sed_with_pipe_in_pattern(self, shell, temp_repo):
        """Sed with pipe in regex (extended regex)."""
        (temp_repo / "input.txt").write_text("foo\nbar\nbaz\n")
        result = shell.run("cat input.txt | sed -E 's/foo|bar/X/g'")
        assert result.success
        assert "X" in result.stdout
        assert "baz" in result.stdout


class TestInputRedirectParsing:
    """Tests for input redirect detection - especially edge cases with < characters."""

    @pytest.fixture
    def shell(self, temp_repo):
        config = SafeShellConfig(repo_root=temp_repo, role=AgentRole.WORKER)
        return SafeShell(config)

    def test_merge_conflict_markers_allowed(self, shell, temp_repo):
        """Grep for merge conflict markers (<<<<<<) should be allowed."""
        (temp_repo / "conflict.txt").write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")
        result = shell.run('grep -n "<<<<<<" conflict.txt')
        assert result.success
        assert "<<<<<<< HEAD" in result.stdout

    def test_merge_conflict_marker_seven_chars(self, shell, temp_repo):
        """Seven < characters should also work."""
        (temp_repo / "file.txt").write_text("<<<<<<<\n")
        result = shell.run('grep "<<<<<<<"  file.txt')
        assert result.success

    def test_single_input_redirect_blocked(self, shell):
        """Single < redirect should be blocked."""
        result = shell.run("cat < file.txt")
        assert not result.success
        assert "Input redirect" in result.error or "not allowed" in result.error

    def test_less_than_in_awk_allowed(self, shell, temp_repo):
        """Less-than comparison in awk should be allowed."""
        (temp_repo / "nums.txt").write_text("1\n5\n10\n")
        result = shell.run("awk '$1 < 5' nums.txt")
        assert result.success
        assert "1" in result.stdout

    def test_less_than_in_quoted_string_grep(self, shell, temp_repo):
        """Searching for < in quoted strings should work."""
        (temp_repo / "html.txt").write_text("<div>hello</div>\n")
        result = shell.run('grep "<div>" html.txt')
        assert result.success
        assert "<div>" in result.stdout

    def test_multiple_less_than_symbols(self, shell, temp_repo):
        """Multiple < symbols in a pattern should work."""
        (temp_repo / "test.txt").write_text("a << b\nx <<< y\n")
        result = shell.run('grep "<<" test.txt')
        assert result.success
        assert "<<" in result.stdout

    def test_angle_brackets_in_lean_code(self, shell, temp_repo):
        """Angle brackets common in Lean code should work."""
        (temp_repo / "test.lean").write_text("theorem foo : ∀ x < 5, x ≥ 0 := by sorry\n")
        result = shell.run('grep "<" test.lean')
        assert result.success
        assert "<" in result.stdout

    def test_xml_tags_searchable(self, shell, temp_repo):
        """Should be able to grep for XML/HTML tags."""
        (temp_repo / "doc.xml").write_text("<root><child>text</child></root>\n")
        result = shell.run('grep "<child>" doc.xml')
        assert result.success
        assert "<child>" in result.stdout

    def test_greater_than_in_quotes_allowed(self, shell, temp_repo):
        """Greater-than in quoted strings should work."""
        (temp_repo / "html.txt").write_text("<div>test</div>\n")
        result = shell.run('grep "</div>" html.txt')
        assert result.success

    def test_awk_comparison_operators(self, shell, temp_repo):
        """Both < and > in awk expressions should work."""
        (temp_repo / "data.txt").write_text("1\n5\n10\n15\n")
        result = shell.run("awk '$1 > 3 && $1 < 12' data.txt")
        assert result.success
        assert "5" in result.stdout
        assert "10" in result.stdout

    def test_unquoted_redirect_still_blocked(self, shell):
        """Unquoted > redirect should still be blocked."""
        result = shell.run("echo test > output.txt")
        assert not result.success
        assert "redirect" in result.error.lower()

    def test_mixed_quoted_unquoted(self, shell, temp_repo):
        """Quoted < followed by unquoted redirect should be blocked."""
        result = shell.run('grep "<" file.txt > output.txt')
        assert not result.success
        assert "redirect" in result.error.lower()

    # === Edge cases for quote handling ===

    def test_nested_quotes_single_in_double(self, shell, temp_repo):
        """Single quotes inside double quotes with redirect chars."""
        (temp_repo / "test.txt").write_text("it's > than\n")
        result = shell.run('grep "it\'s > than" test.txt')
        assert result.success

    def test_nested_quotes_double_in_single(self, shell, temp_repo):
        """Double quotes inside single quotes with redirect chars."""
        (temp_repo / "test.txt").write_text('he said "<hello>"\n')
        result = shell.run("grep 'said \"<' test.txt")
        assert result.success

    def test_escaped_quote_in_double_quotes(self, shell, temp_repo):
        """Escaped quote inside double quotes with redirect."""
        (temp_repo / "test.txt").write_text('test ">" here\n')
        result = shell.run('grep "\\">\\"" test.txt')
        assert result.success

    def test_redirect_no_space(self, shell):
        """Redirect without space should still be blocked."""
        result = shell.run("echo test>output.txt")
        assert not result.success
        assert "redirect" in result.error.lower()

    def test_heredoc_unquoted_blocked(self, shell):
        """Unquoted heredoc << should be blocked."""
        result = shell.run("cat << EOF")
        assert not result.success
        assert "Heredoc" in result.error or "here-string" in result.error

    def test_heredoc_in_quotes_allowed(self, shell, temp_repo):
        """Heredoc marker inside quotes should be allowed."""
        (temp_repo / "test.txt").write_text("cat << EOF\n")
        result = shell.run('grep "<<" test.txt')
        assert result.success

    def test_process_substitution_pattern(self, shell, temp_repo):
        """<( pattern (process substitution) should not trigger input redirect error."""
        # Process substitution itself may not work in sh, but it shouldn't be
        # blocked as "input redirect" - the regex allows <(
        (temp_repo / "test.txt").write_text("test\n")
        # This tests that <( doesn't trigger "Input redirects not allowed"
        # The command may fail for other reasons (not bash), but not redirect blocking
        result = shell.run("cat <(echo test)")
        # Either succeeds or fails for non-redirect reason
        assert "Input redirect" not in (result.error or "")

    def test_empty_quotes(self, shell, temp_repo):
        """Empty quotes should be handled correctly."""
        (temp_repo / "test.txt").write_text("test\n")
        result = shell.run('grep "" test.txt')
        assert result.success

    def test_multiple_redirect_chars_in_pattern(self, shell, temp_repo):
        """Multiple redirect chars in a single quoted pattern."""
        (temp_repo / "test.txt").write_text("a < b > c << d >> e\n")
        result = shell.run('grep "< b > c <<" test.txt')
        assert result.success

    def test_redirect_at_end_of_double_quote(self, shell, temp_repo):
        """Redirect char at end of double-quoted string."""
        (temp_repo / "test.txt").write_text("test>\n")
        result = shell.run('grep ">" test.txt')
        assert result.success

    def test_redirect_at_start_of_double_quote(self, shell, temp_repo):
        """Redirect char at start of double-quoted string."""
        (temp_repo / "test.txt").write_text("<test\n")
        result = shell.run('grep "<test" test.txt')
        assert result.success

    def test_only_redirect_in_quotes(self, shell, temp_repo):
        """Just a redirect char in quotes."""
        (temp_repo / "test.txt").write_text("<\n>\n")
        result = shell.run('grep ">" test.txt')
        assert result.success
        result2 = shell.run("grep '<' test.txt")
        assert result2.success

    def test_adjacent_quotes_with_redirects(self, shell, temp_repo):
        """Adjacent quoted strings each containing redirects."""
        (temp_repo / "test.txt").write_text("<>\n")
        result = shell.run('grep "<"">" test.txt')
        assert result.success

    def test_unbalanced_quotes_rejected(self, shell):
        """Unbalanced quotes should be rejected by shlex."""
        result = shell.run('grep "< test.txt')
        assert not result.success
        assert "parse" in result.error.lower() or "quotation" in result.error.lower()


class TestAllowedPaths:
    """Tests for allowed paths like /dev/null, /dev/stdin, etc."""

    @pytest.fixture
    def shell(self, temp_repo):
        config = SafeShellConfig(repo_root=temp_repo, role=AgentRole.WORKER)
        return SafeShell(config)

    def test_dev_null_allowed_as_argument(self, shell):
        """Using /dev/null as a command argument should be allowed."""
        result = shell.run("cat /dev/null")
        assert result.success

    def test_dev_null_redirect_allowed(self, shell):
        """Redirecting to /dev/null should be allowed."""
        result = shell.run("echo test >/dev/null")
        assert result.success

    def test_dev_null_append_redirect_allowed(self, shell):
        """Appending to /dev/null should be allowed."""
        result = shell.run("echo test >>/dev/null")
        assert result.success

    def test_dev_null_stderr_redirect(self, shell, temp_repo):
        """Redirecting stderr to /dev/null should work."""
        result = shell.run("ls /nonexistent 2>/dev/null")
        # Command may fail but shouldn't be blocked
        assert "redirect" not in (result.error or "").lower()

    def test_dev_stdin_allowed(self, shell):
        """/dev/stdin should be allowed."""
        result = shell.run("cat /dev/stdin < /dev/null")
        # This will be blocked by input redirect, but /dev/stdin itself is fine
        # Let's test it differently
        result = shell.run("ls -la /dev/stdin")
        assert result.success or "Path escapes" not in (result.error or "")

    def test_dev_stdout_allowed(self, shell):
        """/dev/stdout should be allowed."""
        result = shell.run("echo test > /dev/stdout")
        assert result.success

    def test_dev_stderr_allowed(self, shell):
        """/dev/stderr should be allowed."""
        result = shell.run("echo error > /dev/stderr")
        assert result.success

    def test_arbitrary_dev_path_blocked(self, shell):
        """Arbitrary /dev paths should be blocked (path escapes repo)."""
        result = shell.run("cat /dev/random")
        assert not result.success
        assert "escape" in result.error.lower() or "not allowed" in result.error.lower()

    def test_etc_path_blocked(self, shell):
        """Paths outside repo like /etc should be blocked."""
        result = shell.run("cat /etc/passwd")
        assert not result.success
        assert "escape" in result.error.lower()

    def test_dev_null_traversal_blocked(self, shell):
        """Path traversal via /dev/null/../../etc should be blocked."""
        result = shell.run("cat /dev/null/../../etc/passwd")
        assert not result.success
        assert "escape" in result.error.lower()

    def test_dev_null_parent_traversal_blocked(self, shell):
        """Path traversal via /dev/null/../ should be blocked."""
        result = shell.run("cat /dev/null/../zero")
        assert not result.success
        assert "escape" in result.error.lower()

    def test_allowed_path_exact_match_only(self, shell):
        """ALLOWED_PATHS should only match exact paths, not subpaths."""
        # /dev/null is allowed, but /dev/null/foo is not
        result = shell.run("cat /dev/null/foo")
        assert not result.success
        # Either blocked as path escape or the file doesn't exist
        # Both are acceptable outcomes


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
