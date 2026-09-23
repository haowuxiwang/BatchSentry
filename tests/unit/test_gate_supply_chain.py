# -*- coding: utf-8 -*-
"""B11-2 / B11-3 护栏：门禁的"供应链"两项 + 产物份数判据。

背景（第五轮对抗性审查 §17.3，2026-09-23）
------------------------------------------
产品逻辑被四轮审得很细，但 **`release_gate.py` 的 9 项里没有任何一项**看着
"我们依赖的东西还安不安全" —— 这是**流程缺口**，不是"忘了跑"。本轮补两项：

  · `dependency_vulns` —— 依赖无已知公告（读**快照**，门禁**不联网**）
  · `runtime_eol`      —— 运行时在安全支持期内（读**产物二进制** vs 仓库内支持线表）

同时把 `dist_variants` 的判据从"数目录个数"改成"数**可分发产物的份数**"：
实测 3 个 `dist-*` 里 2 个是残壳时，旧判据（目录数 > 3）**恰好不触发**
⇒ 最危险的状态落在阈值内侧（PITFALLS §二十六：阈值卡在恰好不触发 = 零判别力）。

本文件用 `tmp_path` 造假目录/假产物 —— 判据必须能在**不依赖本机实际产物**的
条件下被验证（否则"本机恰好有产物"会让用例变成环境依赖）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = REPO / "scripts"


def _load(name: str, filename: str):
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    # ⚠️ 必须先注册进 `sys.modules`：`release_gate.py` 里有 `@dataclass`，
    # 而 dataclass 在解析字段类型注解时会查 `sys.modules[cls.__module__]` ——
    # 不注册就报 `'NoneType' object has no attribute '__dict__'`（实测踩过）。
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rg = _load("release_gate_under_test", "release_gate.py")
ad = _load("audit_deps_under_test", "audit_deps.py")


# ── 造假的产物/文件 ─────────────────────────────────────────────────────────


def _make_artifact(root: Path, name: str, *, complete: bool = True,
                   electron: str = "41.0.0") -> Path:
    """造一个产物目录；`complete=False` 时缺内嵌后端与 PROVENANCE（= 残壳）。"""
    d = root / name
    wu = d / "win-unpacked"
    (wu / "resources" / "pbc-server").mkdir(parents=True, exist_ok=True)
    (wu / "BatchSentry.exe").write_bytes(
        f"x Electron/{electron} y Chrome/130.0.6723.191 z node.js/v20.18.3 w".encode())
    if complete:
        (wu / "PROVENANCE.txt").write_text("git_head=abc1234\nversion=1.2.0")
        (wu / "resources" / "pbc-server" / "pbc-server.exe").write_bytes(b"MZ")
    return d


def _write_requirements(root: Path, pins: dict[str, str]) -> None:
    (root / "requirements.txt").write_text(
        "".join(f"{k}=={v}\n" for k, v in pins.items()), encoding="utf-8")


def _write_snapshot(root: Path, *, locked: dict[str, str],
                    vulns: dict[str, dict] | None = None,
                    generated_at: str = "2026-09-23T00:00:00") -> None:
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "DEPENDENCY_AUDIT.json").write_text(json.dumps({
        "schema": 1, "generated_at": generated_at,
        "locked": locked,
        "packages_with_vulns": vulns or {},
        "totals": {"dependencies_scanned": len(locked),
                   "advisories": sum(v["advisory_count"] for v in (vulns or {}).values()),
                   "packages_with_vulns": len(vulns or {})},
    }, ensure_ascii=False), encoding="utf-8")


def _write_support_table(root: Path, *, supported=(41, 42, 43),
                         updated_at: str = "2026-09-23") -> None:
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "RUNTIME_SUPPORT.json").write_text(json.dumps({
        "schema": 1, "updated_at": updated_at,
        "components": {"electron": {"supported_major": list(supported)}},
    }, ensure_ascii=False), encoding="utf-8")


# ── B11-3：完整产物计数与变体判据 ───────────────────────────────────────────


def test_complete_artifact_counts_only_fully_packed_dirs(tmp_path):
    """1 完整 + 2 残壳 ⇒ 完整产物**只有 1 份**（残壳不算）。"""
    _make_artifact(tmp_path, "dist-electron", complete=False)              # 残壳
    _make_artifact(tmp_path, "dist-electron-out-153526", complete=False)   # 残壳
    _make_artifact(tmp_path, "dist-electron-out-160111")                   # 完整
    assert rg.count_complete_artifacts(tmp_path) == ["dist-electron-out-160111"]


def test_dist_variants_pass_but_discloses_husks(tmp_path):
    """★ 1 完整 + 2 残壳 ⇒ **不报风险**，但残壳必须**具名披露**。

    这就是本机 2026-09-23 的真实状态。旧判据（目录数 3 > 3 为假）连披露都没有；
    新判据至少让"有 2 个不可分发的残壳"**可见**。
    """
    _make_artifact(tmp_path, "dist-electron", complete=False)              # 残壳
    _make_artifact(tmp_path, "dist-electron-out-153526", complete=False)   # 残壳
    _make_artifact(tmp_path, "dist-electron-out-160111")                   # 完整
    res = rg.check_dist_variants(tmp_path)
    assert res.status == rg.PASS
    assert "1 份完整产物" in res.detail
    assert "dist-electron" in res.detail and "153526" in res.detail


def test_dist_variants_warns_when_two_complete_artifacts_coexist(tmp_path):
    """★ 2 份**完整**产物并存 ⇒ WARN（那才是"可能发错版本"的实质风险）。"""
    _make_artifact(tmp_path, "dist-electron-out-aaa")
    _make_artifact(tmp_path, "dist-electron-out-bbb")
    res = rg.check_dist_variants(tmp_path)
    assert res.status == rg.WARN
    assert "2 份完整产物" in res.detail


def test_old_judgement_would_have_missed_the_husk_case(tmp_path):
    """对照：**旧判据**（只看目录个数 > 3）在"1 完整 + 2 残壳"上**零反应**。

    把这条写在测试里，是为了让"判据升级到底改了什么"可被复核 ——
    否则半年后没人说得清为什么要改。
    """
    _make_artifact(tmp_path, "dist-electron", complete=False)              # 残壳
    _make_artifact(tmp_path, "dist-electron-out-153526", complete=False)   # 残壳
    _make_artifact(tmp_path, "dist-electron-out-160111")                   # 完整
    variants = rg.count_dist_variants(tmp_path)
    old_would_warn = len(variants) > rg.DIST_VARIANT_WARN_AT
    assert old_would_warn is False, "旧判据本就不该报警（这正是它的缺陷）"
    assert rg.check_dist_variants(tmp_path).status == rg.PASS, (
        "新判据同样不报'风险'（残壳清不掉），但披露了它们")


# ── B11-2：依赖漏洞（快照） ─────────────────────────────────────────────────


def test_dependency_snapshot_missing_is_fail_closed(tmp_path):
    """快照缺失 ⇒ FAIL（**不得**记 PASS。否则"没扫过"= "没漏洞"）。"""
    _write_requirements(tmp_path, {"fastapi": "0.139.0"})
    res = rg.check_dependency_vulns(tmp_path)
    assert res.status == rg.FAIL
    assert "快照缺失" in res.detail


def test_dependency_snapshot_broken_json_is_fail_closed(tmp_path):
    _write_requirements(tmp_path, {"fastapi": "0.139.0"})
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "DEPENDENCY_AUDIT.json").write_text("{不是 json")
    assert rg.check_dependency_vulns(tmp_path).status == rg.FAIL


def test_dependency_drift_between_snapshot_and_pins_is_fail(tmp_path):
    """★ 依赖改了但没重扫 ⇒ FAIL（强制"改依赖必须重扫"的流程）。"""
    _write_requirements(tmp_path, {"pillow": "12.3.0"})          # 已升级
    _write_snapshot(tmp_path, locked={"pillow": "10.1.0"})        # 快照还是旧的
    res = rg.check_dependency_vulns(tmp_path)
    assert res.status == rg.FAIL
    assert "脱节" in res.detail and "pillow" in res.detail


def test_dependency_advisories_are_named_fail(tmp_path):
    """有公告 ⇒ FAIL，且 detail 必须**具名**给出包与最低安全版本。"""
    _write_requirements(tmp_path, {"pillow": "10.1.0"})
    _write_snapshot(tmp_path, locked={"pillow": "10.1.0"}, vulns={
        "pillow": {"version": "10.1.0", "advisory_count": 31,
                   "minimum_safe_version": "12.3.0", "vulns": []}})
    res = rg.check_dependency_vulns(tmp_path)
    assert res.status == rg.FAIL
    assert "pillow" in res.detail and "12.3.0" in res.detail


def test_dependency_clean_and_fresh_is_pass(tmp_path):
    _write_requirements(tmp_path, {"pillow": "12.3.0"})
    _write_snapshot(tmp_path, locked={"pillow": "12.3.0"},
                    generated_at="2026-09-23T00:00:00")
    assert rg.check_dependency_vulns(tmp_path).status == rg.PASS


def test_dependency_stale_snapshot_warns(tmp_path):
    """无公告但快照过老 ⇒ WARN（新 CVE 未必已收录 —— 这是快照方案的固有代价）。"""
    _write_requirements(tmp_path, {"pillow": "12.3.0"})
    _write_snapshot(tmp_path, locked={"pillow": "12.3.0"},
                    generated_at="2020-01-01T00:00:00")
    res = rg.check_dependency_vulns(tmp_path)
    assert res.status == rg.WARN
    assert "未刷新" in res.detail


# ── B11-2：运行时支持期 ─────────────────────────────────────────────────────


def test_runtime_eol_flags_out_of_support_version(tmp_path):
    """★ 产物里的 Electron 33 不在支持线 ⇒ FAIL（具名 + 附支持线）。"""
    _make_artifact(tmp_path, "dist-electron-out-x", electron="33.4.11")
    _write_support_table(tmp_path, supported=(41, 42, 43))
    res = rg.check_runtime_eol(tmp_path)
    assert res.status == rg.FAIL
    assert "33.4.11" in res.detail and "41" in res.detail


def test_runtime_eol_passes_for_supported_version(tmp_path):
    _make_artifact(tmp_path, "dist-electron-out-x", electron="43.1.0")
    _write_support_table(tmp_path, supported=(41, 42, 43))
    assert rg.check_runtime_eol(tmp_path).status == rg.PASS


def test_runtime_eol_skips_when_no_artifact(tmp_path):
    """无完整产物 ⇒ SKIP（如实记），**不冒充** PASS。"""
    _write_support_table(tmp_path)
    res = rg.check_runtime_eol(tmp_path)
    assert res.status == rg.SKIP
    assert "不冒充" in res.detail or "读不到" in res.detail


def test_runtime_eol_unreadable_version_is_fail_closed(tmp_path):
    """产物在但读不出 Electron 版本 ⇒ FAIL（fail-closed，不得记通过）。"""
    d = _make_artifact(tmp_path, "dist-electron-out-x")
    # 抹掉版本串，但保留"完整产物"的三件套
    (d / "win-unpacked" / "BatchSentry.exe").write_bytes(b"no version string here")
    _write_support_table(tmp_path)
    res = rg.check_runtime_eol(tmp_path)
    assert res.status == rg.FAIL
    assert "读不到" in res.detail


def test_runtime_eol_missing_table_is_fail_closed(tmp_path):
    _make_artifact(tmp_path, "dist-electron-out-x")
    assert rg.check_runtime_eol(tmp_path).status == rg.FAIL


def test_runtime_eol_reads_versions_from_binary(tmp_path):
    """版本必须来自**产物二进制**，而不是 `package.json` 的范围。"""
    d = _make_artifact(tmp_path, "dist-electron-out-x", electron="42.0.1")
    got = rg._read_runtime_versions(d / "win-unpacked" / "BatchSentry.exe")
    assert got["electron"] == "42.0.1"
    assert got["chrome"] == "130.0.6723.191"
    assert got["node"] == "20.18.3"


# ── 最低安全版本的计算（升错版本 = 白升） ───────────────────────────────────


def test_minimum_safe_version_takes_the_maximum():
    """★ 必须取**最大**修复版本：各公告修复版本不同，取最小会留下后面的漏洞。

    例：某包 A 漏洞修在 10.2.0、B 漏洞修在 12.2.0 ⇒ 升到 10.2.0 **仍然**中 B。
    """
    assert ad.minimum_safe_version(["10.2.0", "10.3.0", "12.2.0"]) == "12.2.0"
    assert ad.minimum_safe_version(["0.0.22", "0.0.31"]) == "0.0.31"


def test_minimum_safe_version_none_when_no_fix_exists():
    """无修复版本 ⇒ None（门禁据此显示"无"，比悄悄跳过更安全）。"""
    assert ad.minimum_safe_version([]) is None
    assert ad.minimum_safe_version(["", None]) is None


def test_locked_requirements_parsing():
    """`包==版本` 的解析口径（注释/空行/无 pin 行都要忽略）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "requirements.txt"
        p.write_text(
            "# 注释\n"
            "fastapi==0.139.0\n"
            "uvicorn[standard]==0.48.0   # 行内注释\n"
            "\n"
            "eval-type-backport>=0.2\n"      # 无 == ⇒ 忽略
            "Pillow==10.1.0\n",
            encoding="utf-8")
        got = ad.locked_requirements(p)
    assert got["fastapi"] == "0.139.0"
    assert got["pillow"] == "10.1.0"         # 名字统一小写
    assert "eval-type-backport" not in got


# ── 公告去重（判据失真防护）────────────────────────────────────────────────
# 背景：pip-audit 会在同一个包的 vulns 里把**同一条公告重复列出**（实测每条 2 份），
# 直接 len(vulns) 会把 24 条唯一公告报成 47 条，严重度虚高 ~2×。


def test_dedup_vulns_removes_repeated_advisory_ids():
    """同一 id 重复出现 ⇒ 只保留一份，并回报原始条目数。"""
    vulns = [
        {"id": "A", "aliases": ["CVE-1"], "fix_versions": ["1.0"]},
        {"id": "B", "aliases": ["CVE-2"], "fix_versions": ["2.0"]},
        {"id": "A", "aliases": ["CVE-1"], "fix_versions": ["1.0"]},
        {"id": "B", "aliases": ["CVE-2"], "fix_versions": ["2.0"]},
    ]
    uniq, raw_n = ad.dedup_vulns(vulns)
    assert [u["id"] for u in uniq] == ["A", "B"]   # 保持首次出现顺序
    assert raw_n == 4


def test_dedup_vulns_unions_fix_versions_and_aliases():
    """同一条公告在不同来源里修复版本/别名可能不全 ⇒ 取并集（更保守，不丢信息）。"""
    vulns = [
        {"id": "A", "aliases": ["CVE-1"], "fix_versions": ["1.0"]},
        {"id": "A", "aliases": ["GHSA-x"], "fix_versions": ["1.2"]},
    ]
    uniq, raw_n = ad.dedup_vulns(vulns)
    assert len(uniq) == 1
    assert set(uniq[0]["aliases"]) == {"CVE-1", "GHSA-x"}
    assert set(uniq[0]["fix_versions"]) == {"1.0", "1.2"}
    assert raw_n == 2


def test_build_snapshot_counts_unique_not_raw():
    """★ 核心判据：快照的 advisory_count 必须是**唯一**数，且与 totals 自洽。"""
    raw = {"dependencies": [
        {"name": "pkgdup", "version": "1.0",
         "vulns": [
             {"id": "PYSEC-1", "aliases": ["CVE-1"], "fix_versions": ["1.1"]},
             {"id": "PYSEC-1", "aliases": ["CVE-1"], "fix_versions": ["1.1"]},
             {"id": "PYSEC-2", "aliases": ["CVE-2"], "fix_versions": ["1.2"]},
             {"id": "PYSEC-2", "aliases": ["CVE-2"], "fix_versions": ["1.2"]},
         ]},
    ]}
    snap = ad.build_snapshot(raw)
    pkg = snap["packages_with_vulns"]["pkgdup"]
    assert pkg["advisory_count"] == 2          # 唯一
    assert pkg["raw_entry_count"] == 4         # 原始
    assert len(pkg["vulns"]) == 2
    assert snap["totals"]["advisories"] == 2
    assert snap["totals"]["raw_advisory_entries"] == 4
    # minimum_safe_version 不受重复影响（取最大修复版本）
    assert pkg["minimum_safe_version"] == "1.2"


def test_committed_snapshot_has_no_duplicate_advisory_ids():
    """★ 真值护栏：仓库里**已提交**的快照不得含重复 id。

    这条直接守"交付物里的数字"，而不是只测函数 —— 若有人手工编辑快照或旧脚本
    回退，这条会红。与 `test_product_...` 一类的 e2e 判据同一思路：测产物，不测意图。
    """
    import collections

    snap = json.loads((REPO / "docs" / "DEPENDENCY_AUDIT.json").read_text("utf-8"))
    bad = {}
    for name, p in (snap.get("packages_with_vulns") or {}).items():
        ids = [v["id"] for v in p.get("vulns", [])]
        dups = {k: c for k, c in collections.Counter(ids).items() if c > 1}
        if dups:
            bad[name] = dups
        # 该包的计数必须等于 Vulns 的唯一条数
        assert p["advisory_count"] == len(set(ids)), f"{name} advisory_count 与唯一数不符"
    assert not bad, f"快照里仍有重复公告 id（判据失真）：{bad}"
    t = snap["totals"]
    assert t["advisories"] == sum(
        p["advisory_count"] for p in snap["packages_with_vulns"].values())
    assert t.get("raw_advisory_entries", 0) >= t["advisories"]


@pytest.mark.parametrize("stamp,expect_none", [
    (None, True), ("", True), ("不是日期", True), ("2026-09-23T00:00:00", False),
])
def test_age_days_degrades_gracefully(stamp, expect_none):
    """时间戳解析不出来 ⇒ None（判据随之降级，而不是抛异常打断门禁）。"""
    got = rg._age_days(stamp)
    assert (got is None) is expect_none


# ── CVE 可达性表与快照的一致性（防"表在腐烂而没人发现"）────────────────────

import re  # noqa: E402

_ADV_ID_RE = re.compile(r"PYSEC-\d{4}-\d+")


def _reachability_ids() -> set[str]:
    """从 `docs/CVE_REACHABILITY.md` 的**表格行**里抽公告 id（忽略散文）。"""
    doc = (REPO / "docs" / "CVE_REACHABILITY.md").read_text("utf-8")
    ids: set[str] = set()
    for line in doc.splitlines():
        if not line.lstrip().startswith("|"):
            continue                      # 只看表格行，散文里的举例不算
        ids.update(_ADV_ID_RE.findall(line))
    return ids


def _snapshot_ids() -> set[str]:
    snap = json.loads((REPO / "docs" / "DEPENDENCY_AUDIT.json").read_text("utf-8"))
    return {v["id"] for p in (snap.get("packages_with_vulns") or {}).values()
            for v in p.get("vulns", [])}


def test_cve_reachability_table_covers_snapshot():
    """★ 双向一致：快照里的每条公告都要在可达性表里被判定过，反之亦然。

    为什么要有这条：可达性是**人工证据**，快照却会随 `audit_deps.py` 自动刷新。
    没有这条护栏，"表"会静静腐烂成"看起来有效"（PITFALLS §二十六 的恒真退化）。
    有了它，重扫后一旦出现新公告，测试立刻红 ⇒ 强制重新判定。
    """
    snap_ids = _snapshot_ids()
    table_ids = _reachability_ids()
    assert table_ids == snap_ids, (
        "可达性表与依赖快照不一致（必须人工复核后同步）：\n"
        f"  仅快照有（新增/未判定）：{sorted(snap_ids - table_ids)}\n"
        f"  仅表里有（已修/写错）：  {sorted(table_ids - snap_ids)}"
    )
    # 空集合会让上面的相等断言**恒真**（两边都空）⇒ 显式挡住这种退化
    assert snap_ids, "快照里一条公告都没有，一致性断言失去判别力"
