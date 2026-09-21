"""产物入包清单：**构建时**生成，**门禁时**校验 —— 产物新鲜度的唯一判据。

## 为什么需要它（B7-3 实测）

「版本号一致」**不蕴含**「产物是新构建的」。2026-09-20 实测铁证（字节级）：

    源码 static/settings.js      77547 B  sha256 af254343…  含 auto_activated
    产物 _internal/static/settings.js 77231 B  sha256 fa9701e9…  仍含早已删除的 firstConfigured

两侧版本号**都是 1.1.9**，而 `release_gate.py --skip-tests` 照样 7/7 全绿 ——
因为机制上三层叠加都漏：① `pytest.ini` 的 `testpaths=tests` + `python_files=test_*.py`
使 `tests/e2e_*.py` **不在收集范围**；② 唯一依赖产物的 `test_frozen_smoke.py`
只验「能启动」；③ 唯一含版本断言的 `tests/e2e_frozen.py` 恰在收集范围之外。
⇒ **改了进产物的模块却不重建，可以静默通过全部门禁**。

## 做法

构建收尾时把「进包的每一个源文件」的 sha256 写进清单
（`<artifact>/build_manifest.json`），门禁再拿当前工作树的同一批文件重新算一遍
**逐字节**比对。清单随产物分发（electron 的 `extraResources` 会把它一起拷进
`resources/pbc-server/`），故**任何一份产物都能自证**它由哪次提交、哪些字节构建而成。

## 三层可验证性 —— ⚠️ **不是所有文件都能从产物里读回来**

| 处置 | 文件 | 产物内可读 | 门禁怎么验 |
| --- | --- | --- | --- |
| `internal` | `static/**`、`templates/**`、`db/schema.sql`、`core/kb/data/*.json` | ✅ `_internal/<rel>` 直接可读 | 清单 hash == 工作树 hash == **产物字节** hash |
| `asar` | `electron/main.js` | ✅ `app.asar` 成员可 seek 读出 | 同上（走 :func:`read_asar_member`） |
| `pyz` | `main.py`/`server.py`/`config.py`/`logging_config.py`/`api|core|db|models|llm/**/*.py` | ❌ 编译进 `pbc-server.exe` 内的 PYZ，**产物里没有对应字节** | 只能验「工作树 hash == 清单 hash」 |

**盲区（如实声明，不假装覆盖）**：`pyz` 那层无法从产物字节反查 ⇒ 本清单能发现
**「源码改了却没重建」**，**不能**发现**「重建后又改了源码并手工重新生成清单」**
（后者需要主动伪造清单，属**绕过**而非**漏检**）。判据边界见 `docs/PROJECT_PITFALLS.md` §三十一。

**为什么「工作树 == 清单」就等于「HEAD == 清单」**：门禁的 `worktree_clean` 项
要求工作树干净（无未提交改动）⇒ 工作树 ≡ HEAD ⇒ 两者与清单比对是同一件事。
两项**互补**：单有 `worktree_clean` 抓不到「改动已提交但没重建」，单有本项
抓不到「源码脏着但恰好没碰入包文件」。

## 与 `clean_dist.asar_version` 的关系

两者共用同一份 asar 读取实现（:func:`read_asar_index` / :func:`read_asar_member`），
`clean_dist.asar_version` 已改为委托本模块 —— 格式解析只允许有**一处**实现
（asar 的「数据基址是 `8 + headerSize`（headerSize 含对齐填充），不能用 JSON 文本
长度推算」这个坑，重复实现必然有一份写错）。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MANIFEST_SCHEMA = 1
MANIFEST_NAME = "build_manifest.json"

# PyInstaller one-dir（COLLECT）把 datas 落在 `_internal/`，源码相对路径原样保留。
INTERNAL_DIR = "_internal"

# 处置（disposition）
D_INTERNAL = "internal"   # 产物内 `_internal/<rel>` 有可读副本
D_ASAR = "asar"           # 产物内 `app.asar` 的成员（成员名 == 仓库相对路径）
D_PYZ = "pyz"             # 只存在于 exe 内的 PYZ —— 产物侧不可读

#: **入包集合的唯一真值**。与 `pbc-server.spec`（datas / hiddenimports）和
#: `package.json` 的 `build.files` 对应。新增「进产物的源文件」时改这里，
#: 门禁会因「集合不一致」变红，直到重新构建 —— 这正是设计意图。
BUNDLE_SOURCES: tuple[tuple[str, str], ...] = (
    ("static/**/*", D_INTERNAL),
    ("templates/**/*", D_INTERNAL),
    ("db/schema.sql", D_INTERNAL),
    ("core/kb/data/*.json", D_INTERNAL),
    ("main.py", D_PYZ),
    ("server.py", D_PYZ),
    ("config.py", D_PYZ),
    ("logging_config.py", D_PYZ),
    ("api/**/*.py", D_PYZ),
    ("core/**/*.py", D_PYZ),
    ("db/**/*.py", D_PYZ),
    ("models/**/*.py", D_PYZ),
    ("llm/**/*.py", D_PYZ),
    ("electron/main.js", D_ASAR),
)

#: 后端产物入口（也是 electron `extraResources` 拷进 `resources/pbc-server/` 的那份）
EXE_NAME = "pbc-server.exe"

#: 默认产物目录（可用 `--artifact` 覆盖）
DEFAULT_ARTIFACT = Path("dist") / "pbc-server"

#: 读 asar 头部时一次读入的上限。实测本项目索引仅 ~53 KB（全体成员名 + integrity），
#: 4 MB 有 75 倍余量，同时避免把 380 MB 的 asar 整个读进内存。
_ASAR_HEAD_READ = 1 << 22


# ── 入包集合 ────────────────────────────────────────────────────────────────


def iter_bundle_entries(root) -> list[tuple[str, str]]:
    """枚举入包源文件 → ``[(仓库相对 posix 路径, 处置)]``（按路径排序）。

    ``**`` 在 pathlib 里**只匹配目录**（实测：`glob("static/**")` 返回 0 个文件），
    故目录级 glob 必须写成 `static/**/*`。
    """
    root = Path(root)
    found: dict[str, str] = {}
    conflicts: list[str] = []
    for pattern, disp in BUNDLE_SOURCES:
        for p in sorted(root.glob(pattern)):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            prev = found.get(rel)
            if prev is not None and prev != disp:
                conflicts.append(f"{rel}: {prev} vs {disp}")
                continue
            found[rel] = disp
    if conflicts:
        # 规则冲突会让「同一文件该在产物哪里」变得不确定 ⇒ 必须 fail-closed
        raise ValueError("入包清单规则冲突：" + "; ".join(conflicts))
    return sorted(found.items())


def iter_bundle_sources(root) -> list[str]:
    """只取路径（不含处置）。"""
    return [rel for rel, _ in iter_bundle_entries(root)]


def disposition_of(rel: str, root) -> str | None:
    """单文件的处置（不在入包集合内 → ``None``）。"""
    return dict(iter_bundle_entries(root)).get(rel)


# ── hash ────────────────────────────────────────────────────────────────────


def sha256_file(path) -> str:
    """文件**原始字节**的 sha256（分块读，20MB 的 exe 与 6MB 的 asar 都不吃内存）。

    ⚠️ 必须走 `read_bytes` 路径、**不得**经 `read_text`（那会做换行/编码规范化，
    而本仓库是混合行尾：`main.py` 是 CRLF+BOM、`scripts/*.py` 是 LF 无 BOM）。
    """
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def read_app_version(root) -> str | None:
    """用 **AST** 从 `main.py` 读 `APP_VERSION` 字面量（不导入模块，零副作用）。

    不导入的原因：`main.py` 顶层会建 FastAPI app、读配置，门禁不该为此承担
    副作用与依赖（`--skip-tests` 的秒级路径尤其不能）。
    """
    src = Path(root) / "main.py"
    try:
        tree = ast.parse(src.read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == "APP_VERSION":
                val = node.value
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    return val.value
    return None


# ── asar 读取（与 clean_dist 共用，唯一实现）─────────────────────────────────


def _read_asar_layout(asar) -> tuple[dict, int]:
    """读 asar 头部，返回 ``(索引 dict, 数据区起点)``。失败抛 ``ValueError``。

    **格式（2026-09-20 实测十六进制钉死，不是猜的）** —— 本机 `app.asar` 头部::

        00: 04 00 00 00            # Pickle 头长度
        04: 58 ce 00 00 = 52824    # headerSize ⇒ 数据区起点 = 8 + headerSize = 52832
        08: 54 ce 00 00 = 52820    # （未使用）
        0c: 4e ce 00 00 = 52814    # JSON 文本长度（字符数 = 字节数，纯 ASCII）
        10: {"files":...           # ← 索引 JSON 从 **偏移 16** 开始
        ...
        索引结束 52830 → 数据区 52832（**2 字节 4 对齐填充**）

    两个坑（任一写错都会**静默**读出错误字节，故这里都做了自校验）：
    1. 索引 JSON 起点是 **16**，不是 8（`8 + headerSize` 是**数据区**起点，不是索引起点）；
    2. 索引后用**对齐填充**补齐 ⇒ 数据区起点 **不能**用 "JSON 文本长度" 推算
       （实测差 2 字节）。这里用完整场景：`raw_decode` 自行定界（不依赖长度字段），
       再用「数据区必须紧邻索引、填充 < 4 字节」这条不变式**验证**基址。
    """
    p = Path(asar)
    with p.open("rb") as fh:
        head = fh.read(_ASAR_HEAD_READ)
    if len(head) < 16:
        raise ValueError("asar 头过短（<16 字节）")
    json_start = head.find(b'{"files":')
    if json_start < 0:
        raise ValueError("未找到 asar 索引起点（不是合法 asar）")
    try:
        obj, consumed = json.JSONDecoder().raw_decode(
            head[json_start:].decode("utf-8", "replace"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"asar 索引 JSON 解析失败：{e}") from e
    json_end = json_start + consumed
    data_base = 8 + struct.unpack("<I", head[4:8])[0]
    if not json_end <= data_base < json_end + 4:
        # 字段含义与本实现不符 ⇒ 宁可 fail-closed，也不要按**错误基址**读出一段
        # 看起来合理的字节（那是"判据自己骗自己"，见 PITFALLS §三十一）
        raise ValueError(f"asar 数据区起点异常：json_end={json_end} base={data_base}")
    if not isinstance(obj, dict) or not isinstance(obj.get("files"), dict):
        raise ValueError("asar 索引缺少 files 节点")
    return obj, data_base


def read_asar_index(asar) -> dict:
    """只取 asar 索引（``{"files": {...}}``）。失败抛 ``ValueError``。"""
    return _read_asar_layout(asar)[0]


def read_asar_member(asar, member: str) -> bytes:
    """按 posix 路径读出 asar **成员内容**（如 ``package.json``、``electron/main.js``）。

    走本进程的 `open()` 读、读完即关 —— **不产生**宿主那条「把 .asar 当包打开」
    的持久句柄（那条通道会锁死产物目录，见 `clean_dist` 的模块 docstring）。
    只读头部并 seek 到成员，**不**把整包读成字符串。
    """
    index, data_base = _read_asar_layout(asar)
    node: object = index
    for seg in member.split("/"):
        if not isinstance(node, dict):
            raise KeyError(f"{member} 不是文件成员（中途遇到非对象节点）")
        node = node["files"][seg]
    if not isinstance(node, dict) or "offset" not in node or "size" not in node:
        raise KeyError(f"{member} 不是文件成员")
    # ⚠️ 索引里的 offset 是**字符串**（JS 数字精度考虑），必须显式 int()
    with Path(asar).open("rb") as fh:
        fh.seek(data_base + int(node["offset"]))
        return fh.read(int(node["size"]))


def read_asar_version(asar) -> str | None:
    """读 asar 内 `package.json` 的 ``version``（任何失败返回 ``None``，不抛）。

    ⚠️ **不要**把 asar 里的 `package.json` 与源码 `package.json` 做**字节**比对：
    electron-builder 会**重写**它（剥掉 `build`/devDependencies，实测源 ~1.6KB
    而包内仅 255 B）⇒ 字节比对是**必假**的判据。可比的只有 `version`。
    """
    try:
        blob = read_asar_member(asar, "package.json")
        return json.loads(blob.decode("utf-8", "replace")).get("version")
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None


def asar_path_for_artifact(artifact_dir) -> Path | None:
    """Electron 布局下，嵌入后端 `resources/pbc-server/` 的 asar 在兄弟位置 `resources/app.asar`。"""
    cand = Path(artifact_dir).parent / "app.asar"
    return cand if cand.is_file() else None


# ── git ─────────────────────────────────────────────────────────────────────


def _git(root, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


# ── 清单读写 ────────────────────────────────────────────────────────────────


def compute_manifest(root, artifact_dir=None) -> dict:
    """算一份清单（不落盘）。``artifact_dir`` 给定时额外绑定该产物内的 exe 字节。"""
    root = Path(root)
    files = {rel: sha256_file(root / rel) for rel, _ in iter_bundle_entries(root)}
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "app_version": read_app_version(root),
        "git_head": _git(root, "rev-parse", "HEAD"),
        "git_dirty": bool(_git(root, "status", "--porcelain")),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files": files,
    }
    if artifact_dir is not None:
        exe = Path(artifact_dir) / EXE_NAME
        if exe.is_file():
            manifest["artifact"] = {EXE_NAME: sha256_file(exe)}
    return manifest


def manifest_path(artifact_dir) -> Path:
    return Path(artifact_dir) / MANIFEST_NAME


def write_manifest(root, artifact_dir=None, out: Path | None = None) -> Path:
    """生成清单并落盘（默认 ``<artifact>/build_manifest.json``）。

    ⚠️ 产物目录必须**已存在** —— 拒绝凭清单创建目录（否则一次误调用会在仓库里
    留下一个「只有清单、没有产物」的空壳，而门禁会把它当成真产物去校验）。
    """
    root = Path(root)
    artifact = Path(artifact_dir) if artifact_dir is not None else root / DEFAULT_ARTIFACT
    if not artifact.is_dir():
        raise FileNotFoundError(f"产物目录不存在：{artifact}（先构建，再写清单）")
    if out is None:
        out = manifest_path(artifact)
    manifest = compute_manifest(root, artifact)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 生成物：固定 LF（不继承本仓库的混合行尾约定），带尾换行便于 diff/审计
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                   + "\n", encoding="utf-8", newline="\n")
    return out


def read_manifest(path) -> dict:
    """读清单（失败抛 ``ValueError``/``OSError``，由调用方决定如何呈现）。"""
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(obj, dict) or not isinstance(obj.get("files"), dict):
        raise ValueError("清单格式非法（缺少 files 映射）")
    return obj


# ── 校验（门禁与本模块 CLI 共用的唯一实现）──────────────────────────────────


def verify_artifact(artifact_dir, root) -> list[str]:
    """校验一份产物是否**新鲜**。返回问题清单（空 = 新鲜）。

    **纯判断**，不做分级：把 FAIL/WARN 的取舍留给门禁（`release_gate`），
    这样「判据」与「呈现」各自只有一处实现。
    """
    artifact_dir, root = Path(artifact_dir), Path(root)
    problems: list[str] = []

    mpath = manifest_path(artifact_dir)
    if not mpath.is_file():
        return [f"缺少 {MANIFEST_NAME} —— 产物存在但无清单，无法证明它由当前源码构建"
                f"（重建并跑 `python scripts/bundle_manifest.py --write`）"]
    try:
        manifest = read_manifest(mpath)
    except (OSError, ValueError) as e:
        return [f"{MANIFEST_NAME} 不可读：{type(e).__name__}: {e}"]

    files = manifest["files"]

    # 1. 版本：清单（= 构建时的 main.APP_VERSION）vs 当前源码
    src_ver = read_app_version(root)
    if src_ver is None:
        problems.append("无法从 main.py 读出 APP_VERSION（AST 解析失败）")
    elif manifest.get("app_version") != src_ver:
        problems.append(f"版本不一致：清单 {manifest.get('app_version')!r} "
                        f"vs 源码 {src_ver!r}")

    # 2. 集合覆盖：入包集合变了（新增/删除了源文件）必须让门禁变红，
    #    否则**新加入包的文件就是永久盲区**。
    try:
        expected = dict(iter_bundle_entries(root))
    except ValueError as e:
        problems.append(f"入包清单规则冲突：{e}")
        return problems
    missing = sorted(set(expected) - set(files))
    extra = sorted(set(files) - set(expected))
    if missing:
        problems.append(f"{len(missing)} 个入包源文件不在清单里（构建后新增？）：{missing[:5]}")
    if extra:
        problems.append(f"{len(extra)} 个清单条目已不在入包集合（规则变更？）：{extra[:5]}")

    common = sorted(set(files) & set(expected))

    # 3. 工作树逐字节（抓「源码改了却没重建」）
    diverged_src = [rel for rel in common if sha256_file(root / rel) != files[rel]]
    if diverged_src:
        problems.append(f"{len(diverged_src)} 个源文件与清单不符"
                        f"（**源码改动后未重建**）：{diverged_src[:5]}")

    # 4. 产物内可读副本逐字节（抓「产物陈旧」—— 这是唯一的字节级铁证）
    asar = asar_path_for_artifact(artifact_dir)
    absent: list[str] = []
    diverged_art: list[str] = []
    for rel in common:
        want = files[rel]
        if expected[rel] == D_INTERNAL:
            copy = artifact_dir / INTERNAL_DIR / rel
            if not copy.is_file():
                absent.append(rel)
            elif sha256_file(copy) != want:
                diverged_art.append(rel)
        elif expected[rel] == D_ASAR:
            if asar is None:
                continue          # 后端产物没有 asar ⇒ 该层不适用，不算问题
            try:
                blob = read_asar_member(asar, rel)
            except (OSError, ValueError, KeyError, TypeError):
                absent.append(rel)
                continue
            if sha256_bytes(blob) != want:
                diverged_art.append(rel)
    if absent:
        problems.append(f"{len(absent)} 个入包副本在产物内缺失：{absent[:5]}")
    if diverged_art:
        problems.append(f"{len(diverged_art)} 个产物副本与源码不一致"
                        f"（**产物陈旧**）：{diverged_art[:5]}")

    # 5. asar 的 version 必须与清单一致（asar 与嵌入后端是否同一次构建）
    if asar is not None:
        asar_ver = read_asar_version(asar)
        if asar_ver is not None and asar_ver != manifest.get("app_version"):
            problems.append(f"app.asar 版本 {asar_ver!r} 与清单 "
                            f"{manifest.get('app_version')!r} 不一致")

    # 6. exe 绑定：清单记了 exe 字节就得对得上（防「清单被挪到另一份产物旁边」）
    recorded = (manifest.get("artifact") or {}).get(EXE_NAME)
    if recorded:
        exe = artifact_dir / EXE_NAME
        if not exe.is_file():
            problems.append(f"清单记录了 {EXE_NAME} 的 sha256，但产物内没有该文件")
        elif sha256_file(exe) != recorded:
            problems.append(f"{EXE_NAME} 与清单记录不符（清单与产物不是同一次构建）")

    return problems


def discover_artifacts(root) -> list[Path]:
    """发现所有**后端产物根**（含 electron 经 extraResources 嵌入的那一份）。

    覆盖 `dist/`、`dist-electron/`、以及 electron-builder 漂移出的
    `dist-electron-out-<ts>/` 等变体；不存在的路径不返回。
    """
    root = Path(root)
    out: list[Path] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("dist")):
        backend = d / "pbc-server"
        if backend.is_dir():
            out.append(backend)
        embedded = d / "win-unpacked" / "resources" / "pbc-server"
        if embedded.is_dir():
            out.append(embedded)
    # `dist/pbc-server` 已由上面第一条覆盖；去重保序
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    ap = argparse.ArgumentParser(description="产物入包清单：构建时生成 / 门禁时校验")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                    help="仓库根（默认本文件上级）")
    ap.add_argument("--artifact", default=None,
                    help=f"产物目录（默认 {DEFAULT_ARTIFACT}）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true", help="生成并写入清单")
    g.add_argument("--check", action="store_true", help="校验清单（有问题退出码 1）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    artifact = Path(args.artifact) if args.artifact else root / DEFAULT_ARTIFACT

    if args.write:
        try:
            out = write_manifest(root, artifact)
        except (OSError, ValueError) as e:
            print(f"[FAIL] 写清单失败：{type(e).__name__}: {e}", file=sys.stderr)
            return 1
        man = read_manifest(out)
        n = len(man["files"])
        print(f"[OK] 清单已写入 {out}")
        print(f"     version={man.get('app_version')} files={n} "
              f"head={(man.get('git_head') or '?')[:12]} dirty={man.get('git_dirty')}")
        return 0

    problems = verify_artifact(artifact, root)
    if args.json:
        print(json.dumps({"artifact": str(artifact), "problems": problems},
                         ensure_ascii=False, indent=2))
    elif problems:
        print(f"[FAIL] {artifact} 产物新鲜度校验未通过：")
        for p in problems:
            print(f"  - {p}")
    else:
        print(f"[OK] {artifact} 产物新鲜：清单与源码/产物逐字节一致")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
