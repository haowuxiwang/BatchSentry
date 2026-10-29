"""推送前防护：拒绝把已清除的旧历史（tag / 非 main 分支）重新公开。

**由来（R130 实测）**：旧库删除重建后，本地仍留有 5 个旧 tag（`v1.0.0`…`v1.1.9`，
最多 250 个提交），用 PII 映射表扫描有 **816 处**命中；一条 `git push --tags`
就会把已删库的真实批号/姓名重新公开。旧 tag 已删除且对象已 `gc --prune=now`，
本脚本防的是"下一次"。

**启用**：`git config core.hooksPath .githooks`（钩子文件为 `.githooks/pre-push`）。
**放行**：确需发布 tag 时 `ALLOW_TAG_PUSH=1 git push --tags`，且发布前必须有门禁全绿记录。

输入（stdin，git 的 pre-push 协议，每行 4 段）::

    <local_ref> <local_sha> <remote_ref> <remote_sha>

只做**引用级**判定，不扫描内容（扫描历史见 `devlogs/_verify/history_pii_scan.py`，
它是取证脚本、故意不入库）。
"""
from __future__ import annotations

import os
import sys

ALLOWED_HEADS = {"refs/heads/main"}
TAG_PREFIX = "refs/tags/"


def decide(lines: list[str], allow_tags: bool = False) -> list[str]:
    """返回拒绝原因列表；空列表表示放行。"""
    reasons: list[str] = []
    for raw in lines:
        parts = raw.split()
        if len(parts) != 4:
            continue
        _local_ref, _local_sha, remote_ref, _remote_sha = parts
        if remote_ref.startswith(TAG_PREFIX):
            if not allow_tags:
                reasons.append(
                    f"拒绝推送 tag：{remote_ref}\n"
                    "  原因：本仓库因旧历史泄漏而重建，旧 tag 会把已清除的真实数据重新公开。\n"
                    "  确需发布：先跑门禁全绿，再用 ALLOW_TAG_PUSH=1 git push --tags。"
                )
        elif remote_ref not in ALLOWED_HEADS:
            reasons.append(
                f"拒绝推送非 main 分支：{remote_ref}\n"
                "  原因：公开仓只接受 main；工作分支请走 PR（dependabot 由平台自己推）。"
            )
    return reasons


def main() -> int:
    allow_tags = os.environ.get("ALLOW_TAG_PUSH") == "1"
    reasons = decide(sys.stdin.read().splitlines(), allow_tags=allow_tags)
    for r in reasons:
        print(f"[pre-push] {r}", file=sys.stderr)
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main())
