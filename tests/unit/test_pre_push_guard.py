"""推送防护（`.githooks/pre-push` → `scripts/pre_push_guard.py`）的行为用例。

**为什么有这条护栏**：R130 实测本地旧 tag 仍有 816 处真实数据命中，一条
`git push --tags` 就能把已删库的内容重新公开。旧 tag 已清除；本用例防的是
"下一条 tag 或工作分支被误推"。
"""
import os
import subprocess
import sys
from pathlib import Path

from scripts.pre_push_guard import decide

ROOT = Path(__file__).resolve().parents[2]
ZERO = "0" * 40


def _line(local_ref: str, remote_ref: str, sha: str = "a" * 40) -> str:
    return f"{local_ref} {sha} {remote_ref} {ZERO}"


class TestDecide:
    def test_tag_is_blocked_by_default(self):
        reasons = decide([_line("refs/tags/v1.2.1", "refs/tags/v1.2.1")])
        assert len(reasons) == 1
        assert "拒绝推送 tag" in reasons[0]
        assert "ALLOW_TAG_PUSH=1" in reasons[0]

    def test_main_is_allowed(self):
        assert decide([_line("refs/heads/main", "refs/heads/main")]) == []

    def test_work_branch_is_blocked(self):
        reasons = decide([_line("refs/heads/feat/x", "refs/heads/feat/x")])
        assert len(reasons) == 1
        assert "非 main 分支" in reasons[0]

    def test_tag_allowed_only_with_explicit_switch(self):
        assert decide([_line("refs/tags/v1.2.1", "refs/tags/v1.2.1")], allow_tags=True) == []

    def test_mixed_push_blocks_only_tag(self):
        reasons = decide([
            _line("refs/heads/main", "refs/heads/main"),
            _line("refs/tags/v9.9.9", "refs/tags/v9.9.9"),
        ])
        assert len(reasons) == 1
        assert "v9.9.9" in reasons[0]

    def test_malformed_lines_are_ignored(self):
        assert decide(["", "only-one-field", "a b c", "a b c d e"]) == []


class TestEntrypoint:
    """真跑进程：证明退出码契约（钩子靠退出码拦推送），而不只是纯函数。"""

    def _run(self, stdin: str, **env_extra: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("ALLOW_TAG_PUSH", None)
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "pre_push_guard.py")],
            input=stdin, capture_output=True, text=True, env=env, cwd=str(ROOT),
        )

    def test_exit_code_is_one_when_tag_pushed(self):
        r = self._run(_line("refs/tags/v1.0.0", "refs/tags/v1.0.0"))
        assert r.returncode == 1
        assert "拒绝推送 tag" in r.stderr

    def test_exit_code_is_zero_for_main(self):
        r = self._run(_line("refs/heads/main", "refs/heads/main"))
        assert r.returncode == 0
        assert r.stderr == ""
