"""Guards for ``scripts/purge_pii_history.py``.

The script prepares a destructive operation, so the failure modes that matter
are the ones that would either (a) touch the source repository, (b) rewrite
unrelated short substrings, or (c) claim success while a real token survives.

All fixtures use **synthetic** tokens; real values must never appear in the
tracked tree (the mapping file itself is gitignored).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from scripts import purge_pii_history as purge  # noqa: E402

REAL_A = "REALTOKEN123"
FAKE_A = "PLACEHOLDER"
REAL_B = "REALNAME99"
FAKE_B = "PSEUDONYM"
REAL_CJK = "王五"
FAKE_CJK = "李四"


@pytest.fixture()
def mapping_file(tmp_path: Path) -> Path:
    path = tmp_path / "map.json"
    path.write_text(
        json.dumps({"替换": {REAL_A: FAKE_A, REAL_B: FAKE_B, REAL_CJK: FAKE_CJK,
                              "ab": "cd", "same": "same"}},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.invalid",
         "-c", "user.name=t", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    return repo


# ── mapping ─────────────────────────────────────────────────────────────────

def test_load_mapping_drops_short_and_identity_entries(mapping_file: Path):
    mapping = purge.load_mapping(mapping_file)
    assert mapping == {REAL_A: FAKE_A, REAL_B: FAKE_B, REAL_CJK: FAKE_CJK}


def test_load_mapping_keeps_two_char_cjk_names_but_drops_two_char_ascii():
    assert purge._min_token_len("王五") == 2
    assert purge._min_token_len("ab") == 4


def test_load_mapping_missing_file_fails_loudly(tmp_path: Path):
    with pytest.raises(purge.MappingError):
        purge.load_mapping(tmp_path / "absent.json")


def test_load_mapping_rejects_empty_mapping(tmp_path: Path):
    path = tmp_path / "empty.json"
    path.write_text('{"替换": {}}', encoding="utf-8")
    with pytest.raises(purge.MappingError):
        purge.load_mapping(path)


def test_build_replacements_is_literal_and_longest_first():
    short, longer = "SHORT", "MUCHLONGERTOKEN"
    lines = purge.build_replacements({short: "a", longer: "b"}).splitlines()
    assert all(line.startswith("literal:") for line in lines)
    assert lines.index(f"literal:{longer}==>b") < lines.index(f"literal:{short}==>a")


def test_scan_tokens_returns_indices_not_values():
    mapping = {REAL_A: FAKE_A, REAL_B: FAKE_B}
    counts = purge.scan_tokens(f"{REAL_A} {REAL_A} {REAL_B}", mapping)
    assert counts == {1: 2, 2: 1}
    assert all(isinstance(k, int) for k in counts)


# ── dry-run never touches anything ──────────────────────────────────────────

def test_dry_run_creates_no_workdir_and_prints_no_real_values(tmp_path: Path,
                                                              mapping_file: Path,
                                                              capsys):
    repo = _init_repo(tmp_path)
    workdir = tmp_path / "scratch"
    rc = purge.main(["--mapping", str(mapping_file), "--repo", str(repo),
                     "--workdir", str(workdir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert not workdir.exists()
    assert REAL_A not in out and REAL_B not in out


def test_apply_without_filter_repo_fails_closed(tmp_path: Path, mapping_file: Path,
                                                monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(purge, "_filter_repo_bin", lambda: None)
    rc = purge.main(["--mapping", str(mapping_file), "--repo", str(repo),
                     "--workdir", str(tmp_path / "scratch"), "--apply"])
    assert rc == 2


def test_apply_refuses_existing_workdir(tmp_path: Path, mapping_file: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(purge, "_filter_repo_bin", lambda: "git-filter-repo")
    workdir = tmp_path / "scratch"
    workdir.mkdir()
    rc = purge.main(["--mapping", str(mapping_file), "--repo", str(repo),
                     "--workdir", str(workdir), "--apply"])
    assert rc == 2


# ── verification actually sees history ──────────────────────────────────────

def test_verify_history_detects_token_in_a_commit(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "note.txt").write_text(f"x {REAL_A} y", encoding="utf-8")
    assert _git(repo, "add", "note.txt").returncode == 0
    assert _git(repo, "commit", "-qm", "add").returncode == 0

    hits = purge.verify_repo_history(repo, {REAL_A: FAKE_A})
    assert hits == {1: 1}


def test_verify_messages_detects_token_in_a_commit_message(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "note.txt").write_text("clean", encoding="utf-8")
    assert _git(repo, "add", "note.txt").returncode == 0
    assert _git(repo, "commit", "-qm", f"contact {REAL_B}").returncode == 0

    assert purge.verify_repo_messages(repo, {REAL_B: FAKE_B}) == {1: 1}


def test_verify_clean_history_returns_empty(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "note.txt").write_text("nothing sensitive", encoding="utf-8")
    assert _git(repo, "add", "note.txt").returncode == 0
    assert _git(repo, "commit", "-qm", "clean").returncode == 0

    assert purge.verify_repo_history(repo, {REAL_A: FAKE_A}) == {}
    assert purge.verify_repo_messages(repo, {REAL_A: FAKE_A}) == {}


@pytest.mark.skipif(shutil.which("git-filter-repo") is None,
                    reason="git-filter-repo is not installed on this host")
def test_apply_rewrites_and_verifies_scratch_clone(tmp_path: Path, mapping_file: Path):
    repo = _init_repo(tmp_path)
    (repo / "note.txt").write_text(f"x {REAL_A} y", encoding="utf-8")
    assert _git(repo, "add", "note.txt").returncode == 0
    assert _git(repo, "commit", "-qm", f"contact {REAL_B}").returncode == 0

    workdir = tmp_path / "scratch"
    rc = purge.main(["--mapping", str(mapping_file), "--repo", str(repo),
                     "--workdir", str(workdir), "--apply"])
    assert rc == 0
    clone = workdir / "repo"
    assert (clone / "note.txt").read_text(encoding="utf-8") == f"x {FAKE_A} y"
    assert purge.verify_repo_history(clone, purge.load_mapping(mapping_file)) == {}
