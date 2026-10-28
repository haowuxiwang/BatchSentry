# -*- coding: utf-8 -*-
"""把便携版打成**可分发**的 zip（分发纪律的可执行版本）。

分发形态（``DEPLOYMENT.md`` §便携版分发）：**文件夹便携版**，不产出安装包。
交付物 = 一个 zip，根目录两件套::

    win-unpacked/            ← 解压后双击 BatchSentry.exe 即用
    PORTABLE_README.txt      ← 用户解压后第一份会读的文档

⚠️ README **不在** Electron 包内（``build.files`` 只打包 ``electron/main.js``）
⇒ 必须由本脚本附带。手敲 ``Compress-Archive`` 极易漏掉它，而漏掉是**静默**的
（用户解压后没有第一份文档，交付者不会收到任何报错）。

命名含**版本 + 日期**（``CLAUDE.md``「仓库卫生与发布纪律」规则 2）；zip 与
``.sha256`` 一律落 ``release-archive/``（该目录已在 ``.gitignore`` 登记）。

用法::

    python scripts/pack_portable.py            # dry-run：只报告将要打包什么
    python scripts/pack_portable.py --apply    # 生成 zip + .sha256

为什么要有脚本而不是手敲命令：分发件必须**可自证**（版本 / 成员数 / sha256），
且「zip 根目录两件套」是一条**易漏且静默**的纪律 —— 脚本把它变成可复现动作。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNPACKED = REPO / "dist-electron" / "win-unpacked"
README = REPO / "PORTABLE_README.txt"
OUT_DIR = REPO / "release-archive"
MAIN_PY = REPO / "main.py"

#: 完整产物的三件套（与 ``release_gate.count_complete_artifacts`` 同口径：
#: 缺一即"不是能发的那一份"）。
ENTRY = "BatchSentry.exe"
EMBEDDED = "resources/pbc-server/pbc-server.exe"
PROVENANCE = "PROVENANCE.txt"

#: zip 内必须存在的成员（自校验用）—— 分发契约的两件套。
REQUIRED_MEMBERS = ("win-unpacked/BatchSentry.exe", "PORTABLE_README.txt")


def app_version() -> str:
    """从 ``main.py`` 读 ``APP_VERSION`` —— 与 ``build.ps1`` 取法一致（单一真值）。"""
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', MAIN_PY.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("[FAIL] main.py 里找不到 APP_VERSION")
    return m.group(1)


def check_artifact() -> None:
    """打包前置：产物完整 + README 在位 + 根目录只有一份待发目录。"""
    missing = [n for n in (ENTRY, EMBEDDED, PROVENANCE) if not (UNPACKED / n).is_file()]
    if missing:
        raise SystemExit(f"[FAIL] {UNPACKED} 不是完整产物，缺: {missing}")
    if not README.is_file():
        raise SystemExit(f"[FAIL] 找不到 {README}（zip 根目录必须附它）")
    variants = sorted(p.name for p in REPO.iterdir()
                      if p.is_dir() and p.name.startswith("dist-"))
    if len(variants) > 1:
        raise SystemExit(
            f"[FAIL] 根目录有 {len(variants)} 个 dist-* 目录 {variants} —— "
            f"先跑 `python scripts/clean_dist.py` 收敛到唯一一份再打包"
        )


def _iter_members():
    """产出 (源文件, zip 内路径) —— 顺序稳定，便于对账。"""
    for p in sorted(UNPACKED.rglob("*")):
        if p.is_file():
            yield p, f"win-unpacked/{p.relative_to(UNPACKED).as_posix()}"
    yield README, "PORTABLE_README.txt"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包便携版 zip（win-unpacked + README）")
    ap.add_argument("--apply", action="store_true", help="真正生成 zip（默认 dry-run）")
    ap.add_argument("--force", action="store_true", help="同名 zip 已存在时覆盖")
    args = ap.parse_args(argv)

    check_artifact()
    ver = app_version()
    stamp = date.today().strftime("%Y%m%d")
    out = OUT_DIR / f"BatchSentry-Portable-{ver}-{stamp}.zip"

    members = list(_iter_members())
    total = sum(src.stat().st_size for src, _ in members)
    print(f"  产物   = {UNPACKED}（完整，版本 {ver}）")
    print(f"  成员   = {len(members)} 个文件 / {total / 1024 / 1024:.1f} MB（未压缩）")
    print(f"  输出   = {out}")

    if not args.apply:
        print("\n(dry-run) 加 --apply 才真正生成。")
        return 0

    if out.exists() and not args.force:
        raise SystemExit(f"[FAIL] {out} 已存在（用 --force 覆盖）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # ⚠️ zipfile 会把每个成员的 mtime 写进头 ⇒ 同一目录两次打包**不**逐字节相等；
    # 可复现性靠 `.sha256` 记录**这一份**的指纹（不是"重跑必得同一 sha"）。
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for src, arc in members:
            zf.write(src, arc)

    digest = _sha256(out)
    (out.parent / (out.name + ".sha256")).write_text(
        f"{digest}  {out.name}\n", encoding="utf-8")

    # 自校验：能读回、无坏成员、两件套齐备。
    with zipfile.ZipFile(out) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise SystemExit(f"[FAIL] zip 内有坏成员: {bad}")
        names = set(zf.namelist())
    missing = [m for m in REQUIRED_MEMBERS if m not in names]
    if missing:
        raise SystemExit(f"[FAIL] zip 缺必需成员: {missing}")

    print(f"\n[OK] {out.name}")
    print(f"     {out.stat().st_size / 1024 / 1024:.1f} MB / {len(names)} 成员")
    print(f"     sha256 = {digest}")
    print(f"     两件套齐备: {list(REQUIRED_MEMBERS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
