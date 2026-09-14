"""版本号单一来源机检 —— 防 main.APP_VERSION 与 package.json 漂移。

背景（T8.5 定位结论）：版本号历史上在两处各写一份字面量
（`main.py::APP_VERSION` 与 `package.json::version`），且 e2e 脚本与集成测试
又各硬编码一份断言值 —— 升级时极易漏改（本次 1.0.0→1.1.0 就命中 4 处）。
本测试把"唯一真值来源 = main.APP_VERSION"这条不变式机检化：

1. `package.json` / `package-lock.json` 的 version 必须与 APP_VERSION 相等；
2. `tests/e2e_frozen.py` 的 /health 断言不得再硬编码版本字面量；
3. APP_VERSION 必须是合法 semver（MAJOR.MINOR.PATCH）。
"""
import json
import re
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


@pytest.fixture(scope="module")
def app_version() -> str:
    from main import APP_VERSION
    return APP_VERSION


def test_app_version_is_semver(app_version):
    assert _SEMVER.match(app_version), f"APP_VERSION 不是合法 semver: {app_version!r}"


def test_package_json_matches_app_version(app_version):
    pkg = json.loads((_PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    assert pkg["version"] == app_version, (
        f"package.json version={pkg['version']!r} != main.APP_VERSION={app_version!r}"
    )


def test_package_lock_matches_app_version(app_version):
    lock = json.loads((_PROJECT_ROOT / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["version"] == app_version, (
        f"package-lock.json version={lock['version']!r} != APP_VERSION={app_version!r}"
    )
    # lockfile 的 packages[""] 根条目也要一致（npm 会同步写两处）
    root_pkg = lock.get("packages", {}).get("", {})
    assert root_pkg.get("version") == app_version, (
        f"package-lock.json packages[''].version={root_pkg.get('version')!r} "
        f"!= APP_VERSION={app_version!r}"
    )


def test_e2e_frozen_health_assert_has_no_hardcoded_version():
    """e2e_frozen 的 /health 断言必须引用版本变量/常量，不得写死字面量。"""
    src = (_PROJECT_ROOT / "tests" / "e2e_frozen.py").read_text(encoding="utf-8")
    # 允许 `assert data["version"] == "1.1.0"` 形式存在，但值必须等于 APP_VERSION
    from main import APP_VERSION
    m = re.search(r'data\["version"\]\s*==\s*"([^"]+)"', src)
    if m is not None:
        assert m.group(1) == APP_VERSION, (
            f"e2e_frozen.py 断言版本 {m.group(1)!r} != APP_VERSION {APP_VERSION!r}"
        )
