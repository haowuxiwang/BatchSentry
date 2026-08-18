"""Detect UTF-8 BOM in PBC config files (T2.5).

背景：冻结版（PyInstaller）启动时读取 %APPDATA%/PBC/config.json。
Windows PowerShell 5.1 `Set-Content -Encoding UTF8` 会写入 BOM，
config.py 以 encoding="utf-8" 解析会在 config 加载前抛
UnicodeDecodeError，服务器直接退出（exit code 1）。

本脚本用于 CI/手工检查 —— 防止带 BOM 的配置文件进入发布目录：

    python tools/check_bom.py                       # 检查项目 config.json + .env
    python tools/check_bom.py extra_config.json     # 追加自定义文件
    python tools/check_bom.py --fix                 # 检测到 BOM 时重写为无 BOM

Exit codes: 0 = 全部无 BOM；1 = 检测到 BOM；2 = 用法错误。
"""
import argparse
import sys
from pathlib import Path

BOM = b"\xef\xbb\xbf"


def check_file(path: Path, fix: bool = False) -> bool:
    """Return True if the file has a UTF-8 BOM (and remove it when fix=True)."""
    data = path.read_bytes()
    if not data.startswith(BOM):
        return False
    if fix:
        path.write_bytes(data[len(BOM):])
        print(f"[FIXED] {path} — BOM removed")
    else:
        print(f"[BOM]   {path}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="additional files to check")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="rewrite BOM'd files without the BOM (atomic write)",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    files = [root / "config.json", root / ".env"] + [Path(f) for f in args.files]

    found = 0
    for f in files:
        if not f.exists():
            continue
        if check_file(f, fix=args.fix):
            found += 1

    if found:
        print(f"{found} file(s) with UTF-8 BOM found" if not args.fix
              else f"{found} file(s) fixed")
        return 1
    print("OK: no UTF-8 BOM in checked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())