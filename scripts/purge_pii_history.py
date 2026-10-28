"""Prepare a **non-destructive** git-history rewrite for real PII.

Why this script exists
----------------------
The working tree was scrubbed by ``scripts/scrub_pii.py`` (R127), but a forward
commit does not remove real identifiers from *history*.  Rewriting history is a
destructive, force-push operation, so this script deliberately does **not**
touch the source repository:

* default = dry-run: print what would be rewritten (token **indices** only,
  never the real values) and exit 0;
* ``--apply`` = clone the repo into a scratch directory (under the gitignored
  ``devlogs/_verify/`` by default), run ``git filter-repo`` **on the clone**,
  verify that no real token survives in any commit or commit message, and print
  the force-push steps.  It never pushes.

Prerequisites
-------------
``git filter-repo`` (``pip install git-filter-repo``) and the local mapping
file ``devlogs/_verify/pii_map.json`` (gitignored: its left column is the real
value, so it must never be committed).

Exit codes
----------
0  dry-run OK / rewrite verified
1  rewrite verification failed (tokens still present)
2  missing tool, missing mapping, unsafe workdir, or bad arguments

Usage
-----
    python scripts/purge_pii_history.py                 # dry-run
    python scripts/purge_pii_history.py --apply         # rewrite a scratch clone
    python scripts/purge_pii_history.py --apply --workdir D:/tmp/pbc_clean

After a verified ``--apply`` the operator still has to (manually):
    git -C <clone> push --force --all
    git -C <clone> push --force --tags
and then ask GitHub Support to purge cached views/forks.  Treat the old history
as already public: rotate any leaked credential (see docs/TODO.md 0-66).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAPPING = REPO_ROOT / "devlogs" / "_verify" / "pii_map.json"
ASCII_MIN_TOKEN_LEN = 4
CJK_MIN_TOKEN_LEN = 2


def _min_token_len(token: str) -> int:
    """Chinese names are often 2-3 chars, so a blanket 4-char floor would
    silently skip exactly the PII we need to remove.  ASCII substrings need a
    higher floor to avoid rewriting unrelated words (``id``, ``on``, ...)."""
    has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in token)
    return CJK_MIN_TOKEN_LEN if has_cjk else ASCII_MIN_TOKEN_LEN


class MappingError(ValueError):
    """The mapping file is absent, empty, or not a real->placeholder dict."""


def load_mapping(path: Path) -> dict[str, str]:
    """Read the gitignored replacement map and return ``real -> placeholder``.

    Very short entry handling is CJK-aware: 2-3 char Chinese names are kept
    (they are the sensitive data), while 1-3 char ASCII tokens are dropped to
    avoid corrupting unrelated substrings.
    """
    if not path.is_file():
        raise MappingError(
            f"mapping file not found: {path} (gitignored; restore it from the "
            "local evidence directory before rewriting history)"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MappingError(f"mapping file is not valid JSON: {exc}") from exc
    mapping = raw.get("替换", raw) if isinstance(raw, dict) else None
    if not isinstance(mapping, dict) or not mapping:
        raise MappingError("mapping must be a non-empty {'real': 'placeholder'} object")
    out: dict[str, str] = {}
    for real, placeholder in mapping.items():
        if not isinstance(real, str) or not isinstance(placeholder, str):
            continue
        if len(real) < _min_token_len(real) or real == placeholder:
            continue
        out[real] = placeholder
    if not out:
        raise MappingError("mapping has no usable entries after filtering")
    return out


def build_replacements(mapping: dict[str, str]) -> str:
    """Render a ``git filter-repo --replace-text`` file.

    ``literal:`` is mandatory: identifiers and names contain regex
    metacharacters (``.``, ``(``, ``+``) that would silently widen the match.
    Longest-first keeps a short token from partially rewriting a longer one.
    """
    lines = [
        f"literal:{real}==>{placeholder}"
        for real, placeholder in sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True)
    ]
    return "\n".join(lines) + "\n"


def scan_tokens(text: str, mapping: dict[str, str]) -> dict[int, int]:
    """Count occurrences per token **index** (never return the real values)."""
    counts: dict[int, int] = {}
    for index, real in enumerate(mapping, start=1):
        n = text.count(real)
        if n:
            counts[index] = n
    return counts


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def verify_repo_history(repo: Path, mapping: dict[str, str]) -> dict[int, int]:
    """Return ``{token_index: commits}`` for tokens still present in any commit.

    ``git log -S`` is used instead of ``git grep <token> $(git rev-list --all)``
    because the latter can exceed the Windows command-line limit on a real
    history.  A hit means the token appears in at least one commit tree.
    """
    hits: dict[int, int] = {}
    for index, real in enumerate(mapping, start=1):
        proc = _run(["git", "-C", str(repo), "log", "--all", "--oneline", "-S", real])
        n = len([line for line in proc.stdout.splitlines() if line.strip()])
        if n:
            hits[index] = n
    return hits


def verify_repo_messages(repo: Path, mapping: dict[str, str]) -> dict[int, int]:
    """Return ``{token_index: hits}`` for tokens still present in commit messages."""
    proc = _run(["git", "-C", str(repo), "log", "--all", "--format=%B"])
    return scan_tokens(proc.stdout, mapping)


def _filter_repo_bin() -> str | None:
    return shutil.which("git-filter-repo")


def _supports_replace_message(binary: str) -> bool:
    proc = _run([binary, "--help"])
    return "--replace-message" in (proc.stdout + proc.stderr)


def _default_workdir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return REPO_ROOT / "devlogs" / "_verify" / f"history_clean_{stamp}"


def _print_plan(mapping: dict[str, str], repo: Path, workdir: Path) -> None:
    print("[dry-run] no repository was modified")
    print(f"[dry-run] source repo      : {repo}")
    print(f"[dry-run] scratch workdir  : {workdir}")
    print(f"[dry-run] mapping entries  : {len(mapping)} (values intentionally not printed)")
    print(f"[dry-run] token indices    : {list(range(1, len(mapping) + 1))}")
    print("[dry-run] next step        : re-run with --apply to rewrite a scratch clone")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--apply", action="store_true",
                        help="rewrite a scratch clone (never the source repo)")
    args = parser.parse_args(argv)

    try:
        mapping = load_mapping(args.mapping)
    except MappingError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    repo = args.repo.resolve()
    workdir = (args.workdir or _default_workdir()).resolve()
    if not args.apply:
        _print_plan(mapping, repo, workdir)
        return 0

    if workdir.exists():
        print(f"[error] workdir already exists (refusing to overwrite): {workdir}", file=sys.stderr)
        return 2
    if repo == workdir or repo in workdir.parents:
        print("[error] workdir must live outside the source repository", file=sys.stderr)
        return 2

    binary = _filter_repo_bin()
    if not binary:
        print("[error] git-filter-repo not found; install it with: pip install git-filter-repo",
              file=sys.stderr)
        return 2
    if not _supports_replace_message(binary):
        print("[error] this git-filter-repo lacks --replace-message; upgrade it first",
              file=sys.stderr)
        return 2

    workdir.mkdir(parents=True)
    clone = workdir / "repo"
    # --no-local: a real copy, so rewriting the clone cannot touch the source.
    clone_proc = _run(["git", "clone", "--no-local", str(repo), str(clone)])
    if clone_proc.returncode != 0:
        print(f"[error] clone failed: {clone_proc.stderr[:400]}", file=sys.stderr)
        return 2

    replace_text = workdir / "replace-text.txt"
    replace_text.write_text(build_replacements(mapping), encoding="utf-8")
    replace_message = workdir / "replace-message.txt"
    replace_message.write_text(build_replacements(mapping), encoding="utf-8")

    proc = _run([binary, "--replace-text", str(replace_text),
                 "--replace-message", str(replace_message), "--force"], cwd=clone)
    if proc.returncode != 0:
        print(f"[error] git-filter-repo failed: {proc.stderr[:600]}", file=sys.stderr)
        return 1

    blob_hits = verify_repo_history(clone, mapping)
    msg_hits = verify_repo_messages(clone, mapping)
    if blob_hits or msg_hits:
        print(f"[fail] residual tokens — blobs: {sorted(blob_hits)} messages: {sorted(msg_hits)}",
              file=sys.stderr)
        print(f"[fail] inspect the scratch clone at {clone}; source repo untouched", file=sys.stderr)
        return 1

    print("[ok] scratch clone rewritten and verified: no mapped token remains")
    print(f"[ok] clone: {clone}")
    print("[next] review the clone, then push manually:")
    print(f"       git -C \"{clone}\" push --force --all")
    print(f"       git -C \"{clone}\" push --force --tags")
    print("[next] ask GitHub Support to purge cached views/forks; old history was public")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
