"""`scripts/bundle_manifest.py` 的护栏 —— 产物新鲜度判据的**唯一**事实源。

为什么值得单独一组护栏：这个模块的结论是「产物是否由当前源码构建」，
它**没有上游**可以互相印证（不像业务代码有 golden 数据）。所以每一条判据都要有
**反向用例**（能被证伪），尤其是 asar 的字节偏移 —— 差 2 字节会**静默**读出
错误字节，那时"校验通过"是彻底的假绿。

`_make_asar` 是照**实测十六进制**复刻的最小 asar 写者（本机 app.asar 的
`headerSize = 52824 = json_len(52814) + 8 + pad(2)`，本文件用它做对照）。
"""
from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from scripts.bundle_manifest import (  # noqa: E402
    D_ASAR, D_INTERNAL, D_PYZ, MANIFEST_NAME, MANIFEST_SCHEMA,
    asar_path_for_artifact, discover_artifacts, disposition_of, iter_bundle_entries,
    iter_bundle_sources, read_app_version, read_asar_index, read_asar_member,
    read_asar_version, read_manifest, sha256_bytes, sha256_file, verify_artifact,
    write_manifest,
)


# ── 合成夹具 ────────────────────────────────────────────────────────────────


def _seed_repo(root: Path, *, version: str = "1.1.9") -> Path:
    """造一个最小「仓库」：覆盖三种处置各至少一个文件。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "main.py").write_text(f'APP_VERSION = "{version}"\n', encoding="utf-8")
    (root / "server.py").write_text("# server\n", encoding="utf-8")
    (root / "config.py").write_text("# config\n", encoding="utf-8")
    (root / "static").mkdir(parents=True, exist_ok=True)
    (root / "static" / "app.js").write_text("let a = 1;\n", encoding="utf-8")
    (root / "templates").mkdir(parents=True, exist_ok=True)
    (root / "templates" / "upload.html").write_text("<html></html>\n", encoding="utf-8")
    (root / "electron").mkdir(parents=True, exist_ok=True)
    (root / "electron" / "main.js").write_text("// electron\n", encoding="utf-8")
    (root / "db").mkdir(parents=True, exist_ok=True)
    (root / "db" / "schema.sql").write_text("CREATE TABLE t(a);\n", encoding="utf-8")
    return root


def _make_asar(path: Path, members: dict[str, bytes]) -> Path:
    """照实测格式写一个最小合法 asar（16 字节头 + 索引 + 4 字节对齐 + 数据区）。

    头部四个 uint32 的取法由本机 `app.asar` 实数反推（见 `bundle_manifest`
    模块 docstring 的十六进制转储）::

        [0:4]   = 4                    # pickle 头长度
        [4:8]   = headerSize           # ⇒ 数据区起点 = 8 + headerSize
        [8:12]  = headerSize - 4       # 实测 52820 = 52824 - 4
        [12:16] = len(json)            # 实测 52814
        [16:]   = JSON

    于是 ``headerSize = len(json) + 8 + pad``（pad 为 4 字节对齐填充）——
    此处**必须**与真实文件同式：写错这个量正是"差 2 字节静默错位"的成因，
    由 :func:`test_data_base_invariant_rejects_wrong_header_size` 反证。
    """
    index: dict = {"files": {}}
    blob = b""
    for member in sorted(members):
        segs = member.split("/")
        cur = index["files"]
        for seg in segs[:-1]:
            cur = cur.setdefault(seg, {"files": {}})["files"]
        cur[segs[-1]] = {"size": len(members[member]), "offset": str(len(blob))}
        blob += members[member]
    js = json.dumps(index, separators=(",", ":")).encode("utf-8")
    pad = (-len(js)) % 4
    header_size = len(js) + 8 + pad
    path.write_bytes(
        struct.pack("<I", 4)
        + struct.pack("<I", header_size)
        + struct.pack("<I", header_size - 4)
        + struct.pack("<I", len(js))
        + js + b" " * pad + blob)
    return path


def _build_artifact(root: Path, artifact: Path) -> Path:
    """按入包集合造出产物侧可读副本 + exe，并写清单（模拟一次真实构建的收尾）。"""
    for rel, disp in iter_bundle_entries(root):
        if disp == D_INTERNAL:
            dst = artifact / "_internal" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes((root / rel).read_bytes())
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "pbc-server.exe").write_bytes(b"MZ" + b"\x00" * 64)
    write_manifest(root, artifact)
    return artifact


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _seed_repo(root)
    return root


@pytest.fixture()
def built(tmp_path, repo):
    """一份「源码与产物已同步」的仓库 + 产物。"""
    return repo, _build_artifact(repo, repo / "dist" / "pbc-server")


# ── 入包集合 ────────────────────────────────────────────────────────────────


class TestBundleSet:
    def test_glob_semantics_recursive_and_pyc_excluded(self, repo):
        """`**` 在 pathlib 里**只匹配目录** ⇒ 必须写 `static/**/*`；`.pyc` 不得入包。"""
        (repo / "static" / "sub").mkdir()
        (repo / "static" / "sub" / "deep.js").write_text("x", encoding="utf-8")
        (repo / "__pycache__").mkdir()
        (repo / "__pycache__" / "junk.pyc").write_bytes(b"\x00")
        (repo / "api").mkdir()
        (repo / "api" / "__init__.py").write_text("", encoding="utf-8")
        (repo / "api" / "__pycache__").mkdir()
        (repo / "api" / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\x00")
        srcs = iter_bundle_sources(repo)
        assert "static/sub/deep.js" in srcs          # 递归生效
        assert "api/__init__.py" in srcs
        assert not [s for s in srcs if s.endswith(".pyc")]

    def test_each_disposition_is_represented(self, repo):
        disp = dict(iter_bundle_entries(repo))
        assert disp["static/app.js"] == D_INTERNAL
        assert disp["electron/main.js"] == D_ASAR
        assert disp["main.py"] == D_PYZ
        assert disposition_of("static/app.js", repo) == D_INTERNAL
        assert disposition_of("nope.js", repo) is None

    def test_rule_conflict_is_fail_closed(self, tmp_path, monkeypatch):
        """同一文件命中两种处置 ⇒ 必须报错，不能"任取一个"（产物位置会不确定）。"""
        import scripts.bundle_manifest as bm
        root = tmp_path / "r"
        (root / "static").mkdir(parents=True)
        (root / "static" / "x.js").write_text("x", encoding="utf-8")
        monkeypatch.setattr(bm, "BUNDLE_SOURCES",
                            (("static/**/*", D_INTERNAL), ("static/x.js", D_PYZ)))
        with pytest.raises(ValueError, match="冲突"):
            bm.iter_bundle_entries(root)

    def test_real_repo_set_is_non_trivial(self):
        """对**真实仓库**的派生集合（不 mock）：三种处置都非空。"""
        disp = dict(iter_bundle_entries(_ROOT))
        kinds = {d: sum(1 for v in disp.values() if v == d)
                 for d in (D_INTERNAL, D_ASAR, D_PYZ)}
        assert kinds[D_INTERNAL] > 10 and kinds[D_ASAR] >= 1 and kinds[D_PYZ] > 30
        assert "static/settings.js" in disp and "main.py" in disp


# ── hash / 版本 ─────────────────────────────────────────────────────────────


class TestHashAndVersion:
    def test_sha256_file_is_raw_bytes(self, tmp_path):
        """必须按**原始字节**算：CRLF 与 LF 的同一段文本 hash 必须不同。"""
        f = tmp_path / "a.py"
        f.write_bytes(b"a = 1\r\n")
        g = tmp_path / "b.py"
        g.write_bytes(b"a = 1\n")
        assert sha256_file(f) == hashlib.sha256(b"a = 1\r\n").hexdigest()
        assert sha256_file(f) != sha256_file(g)

    def test_sha256_bytes_matches_file(self, tmp_path):
        f = tmp_path / "x"
        f.write_bytes(b"hello")
        assert sha256_bytes(b"hello") == sha256_file(f)

    def test_read_app_version_ast(self, tmp_path):
        (tmp_path / "main.py").write_text('APP_VERSION = "9.9.9"\n', encoding="utf-8")
        assert read_app_version(tmp_path) == "9.9.9"

    def test_read_app_version_handles_bom_and_crlf(self, tmp_path):
        """真实 `main.py` 是 **BOM + CRLF** ⇒ 必须能读（`utf-8-sig`）。"""
        (tmp_path / "main.py").write_bytes(
            b'\xef\xbb\xbfAPP_VERSION = "1.2.3"\r\n')
        assert read_app_version(tmp_path) == "1.2.3"

    def test_read_app_version_none_when_not_literal_or_missing(self, tmp_path):
        (tmp_path / "main.py").write_text("APP_VERSION = _v()\n", encoding="utf-8")
        assert read_app_version(tmp_path) is None
        (tmp_path / "main.py").write_text("APP_VERSION = 3\n", encoding="utf-8")
        assert read_app_version(tmp_path) is None
        (tmp_path / "main.py").unlink()
        assert read_app_version(tmp_path) is None

    def test_read_app_version_syntax_error_is_none(self, tmp_path):
        (tmp_path / "main.py").write_text("def (:\n", encoding="utf-8")
        assert read_app_version(tmp_path) is None


# ── asar 读取 ───────────────────────────────────────────────────────────────


class TestAsarReader:
    def test_reads_nested_member_bytes(self, tmp_path):
        asar = _make_asar(tmp_path / "app.asar",
                          {"package.json": b'{"version":"7.7.7"}',
                           "electron/main.js": b"// nested\n"})
        assert read_asar_version(asar) == "7.7.7"
        assert read_asar_member(asar, "electron/main.js") == b"// nested\n"

    def test_index_has_files_node(self, tmp_path):
        asar = _make_asar(tmp_path / "app.asar", {"a.txt": b"hi"})
        assert read_asar_index(asar)["files"]["a.txt"]["size"] == 2

    def test_data_base_invariant_rejects_wrong_header_size(self, tmp_path):
        """**反证数据基址不变式**：把头里 headerSize 写成本该是 `json_len` 的值
        （= 差 8 字节）⇒ 读取必须**报错**，而不是按错基址读出一段看似合理的字节。"""
        asar = _make_asar(tmp_path / "app.asar", {"a.txt": b"hello"})
        raw = bytearray(asar.read_bytes())
        good = struct.unpack("<I", bytes(raw[4:8]))[0]
        raw[4:8] = struct.pack("<I", good - 8)      # 模拟"按 8 字节头算基址"的错法
        asar.write_bytes(bytes(raw))
        with pytest.raises(ValueError, match="数据区起点异常"):
            read_asar_index(asar)

    def test_garbage_and_missing_return_none(self, tmp_path):
        bad = tmp_path / "bad.asar"
        bad.write_bytes(b"not an asar at all")
        assert read_asar_version(bad) is None
        assert read_asar_version(tmp_path / "nope.asar") is None
        assert read_asar_version(_ROOT / "scripts" / "release_gate.py") is None

    def test_non_member_raises_keyerror(self, tmp_path):
        asar = _make_asar(tmp_path / "app.asar", {"a.txt": b"hi"})
        with pytest.raises(KeyError):
            read_asar_member(asar, "electron/nope.js")

    def test_real_asar_matches_source_for_electron_main(self):
        """对**真实产物**（存在时）：asar 里的 `electron/main.js` 必须能被读出。"""
        asar = _ROOT / "dist-electron" / "win-unpacked" / "resources" / "app.asar"
        if not asar.is_file():
            pytest.skip("无 dist-electron 产物")
        assert read_asar_version(asar) == read_app_version(_ROOT)
        src = (_ROOT / "electron" / "main.js").read_bytes()
        assert read_asar_member(asar, "electron/main.js") == src

    def test_asar_path_lookup_is_sibling_of_embedded_server(self, tmp_path):
        wu = tmp_path / "win-unpacked" / "resources"
        (wu / "pbc-server").mkdir(parents=True)
        (wu / "app.asar").write_bytes(b"x")
        assert asar_path_for_artifact(wu / "pbc-server") == wu / "app.asar"
        assert asar_path_for_artifact(tmp_path / "dist" / "pbc-server") is None


# ── 清单读写 ────────────────────────────────────────────────────────────────


class TestManifestIo:
    def test_write_then_read_round_trip(self, tmp_path):
        root = _seed_repo(tmp_path / "r")
        artifact = root / "dist" / "pbc-server"
        artifact.mkdir(parents=True)
        out = write_manifest(root, artifact)
        assert out == artifact / MANIFEST_NAME
        man = read_manifest(out)
        assert man["schema"] == MANIFEST_SCHEMA
        assert man["app_version"] == "1.1.9"
        assert set(man["files"]) == set(iter_bundle_sources(root))

    def test_write_is_lf_and_records_exe_binding(self, tmp_path):
        root = _seed_repo(tmp_path / "r")
        artifact = _build_artifact(root, root / "dist" / "pbc-server")
        blob = (artifact / MANIFEST_NAME).read_bytes()
        assert b"\r\n" not in blob, "生成的清单必须是 LF（生成物，不继承仓库混合行尾）"
        assert blob.endswith(b"\n")
        man = json.loads(blob.decode("utf-8"))
        assert man["artifact"]["pbc-server.exe"] == sha256_file(artifact / "pbc-server.exe")

    def test_write_refuses_missing_artifact_dir(self, tmp_path):
        """产物目录不存在时**拒绝**写 —— 否则会留下"只有清单没有产物"的空壳，
        而门禁会把它当成真产物去校验。"""
        root = _seed_repo(tmp_path / "r")
        with pytest.raises(FileNotFoundError):
            write_manifest(root, root / "dist" / "nope")

    def test_read_rejects_bad_shapes(self, tmp_path):
        p = tmp_path / "m.json"
        p.write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError):
            read_manifest(p)
        p.write_text('{"files": "not-a-map"}', encoding="utf-8")
        with pytest.raises(ValueError):
            read_manifest(p)


# ── 校验器 ──────────────────────────────────────────────────────────────────


class TestVerifyArtifact:
    def test_synced_passes(self, built):
        root, artifact = built
        assert verify_artifact(artifact, root) == []

    def test_stale_copy_is_caught(self, built):
        """**B7-3 主判据**：产物副本字节与源码不符 ⇒ 命中（这就是"产物陈旧"）。"""
        root, artifact = built
        stale = artifact / "_internal" / "static" / "app.js"
        stale.write_bytes(b"let a = 1;  // old build\n")
        problems = verify_artifact(artifact, root)
        assert any("产物陈旧" in p and "static/app.js" in p for p in problems)

    def test_source_changed_after_build_is_caught(self, built):
        root, artifact = built
        (root / "static" / "app.js").write_text("let a = 2;\n", encoding="utf-8")
        problems = verify_artifact(artifact, root)
        assert any("源码改动后未重建" in p for p in problems)

    def test_new_bundled_file_is_caught(self, built):
        """构建后新增一个入包源文件 ⇒ 它不在清单里 ⇒ 必须红（否则是永久盲区）。"""
        root, artifact = built
        (root / "static" / "brand-new.js").write_text("new\n", encoding="utf-8")
        problems = verify_artifact(artifact, root)
        assert any("不在清单里" in p and "brand-new.js" in p for p in problems)

    def test_version_mismatch_is_caught(self, built):
        root, artifact = built
        (root / "main.py").write_text('APP_VERSION = "1.2.0"\n', encoding="utf-8")
        problems = verify_artifact(artifact, root)
        assert any("版本不一致" in p for p in problems)

    def test_missing_internal_copy_is_caught(self, built):
        root, artifact = built
        (artifact / "_internal" / "templates" / "upload.html").unlink()
        problems = verify_artifact(artifact, root)
        assert any("在产物内缺失" in p for p in problems)

    def test_swapped_exe_is_caught(self, built):
        """清单被挪到另一份产物旁边 ⇒ exe 字节对不上 ⇒ 红。"""
        root, artifact = built
        (artifact / "pbc-server.exe").write_bytes(b"MZ" + b"\x01" * 64)
        problems = verify_artifact(artifact, root)
        assert any("pbc-server.exe" in p for p in problems)

    def test_missing_manifest_is_a_problem_not_a_pass(self, built):
        root, artifact = built
        (artifact / MANIFEST_NAME).unlink()
        problems = verify_artifact(artifact, root)
        assert len(problems) == 1 and MANIFEST_NAME in problems[0]

    def test_corrupt_manifest_is_reported(self, built):
        root, artifact = built
        (artifact / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        problems = verify_artifact(artifact, root)
        assert problems and "不可读" in problems[0]

    def test_lag_one_commit_then_sync(self, repo, tmp_path):
        """**TODO B7-3 的验收口径**：产物落后一个提交 ⇒ 红；同步 ⇒ 绿。

        "落后一个提交"用等价的可观测形态构造：源码已改，产物仍是上一次构建的字节。
        """
        artifact = _build_artifact(repo, repo / "dist" / "pbc-server")
        assert verify_artifact(artifact, repo) == []          # 基线：同步 ⇒ 绿
        # 提交一次改动（只改源码，不重建产物）
        (repo / "static" / "app.js").write_text("let a = 99;\n", encoding="utf-8")
        assert verify_artifact(artifact, repo), "产物落后一个提交必须红"
        # 同步：重建产物字节 + 重新生成清单
        _build_artifact(repo, artifact)
        assert verify_artifact(artifact, repo) == [], "同步后必须绿"


class TestDiscoverArtifacts:
    def test_finds_backend_and_embedded_without_duplicates(self, tmp_path):
        (tmp_path / "dist" / "pbc-server").mkdir(parents=True)
        emb = tmp_path / "dist-electron" / "win-unpacked" / "resources" / "pbc-server"
        emb.mkdir(parents=True)
        found = [p.relative_to(tmp_path).as_posix() for p in discover_artifacts(tmp_path)]
        assert found == ["dist/pbc-server",
                         "dist-electron/win-unpacked/resources/pbc-server"]
        assert len(found) == len(set(found))

    def test_empty_when_nothing_built(self, tmp_path):
        assert discover_artifacts(tmp_path) == []
