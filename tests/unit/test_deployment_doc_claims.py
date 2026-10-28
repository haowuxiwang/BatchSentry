"""DEPLOYMENT.md 的事实性断言护栏（R86 第二十六批新增）。

由来（2026-10-10 复审）：`DEPLOYMENT.md` 是开发/运维文档（不入包），但其中两条会被
当成事实引用 —— **数据保留**与**安全描述**。实测查出 5 处与实物/代码不符：

* 「压缩后的样式（~14KB）」而 `static/app.css` 是 **20,016 字节**；
* 「上传文件…（**OCR 完成后自动清理**）」—— 与实现**相反**：`core/` 内**没有**任何
  删除已上传 PDF 的逻辑，删除只发生在 `api/jobs/actions.py::delete_job`，且**必须
  保留**（复核页的逐页渲染 `api/jobs/page_image.py` 依赖它）。这是**合规相关**的错误陈述；
* 「有 4 个日志文件」而实为 3 个 + 控制台；
* `/health` 示例硬编码 `"version":"1.0.0"`（早已是 1.2.x）⇒ 示例里的具体版本号必腐坏；
* 上传限额只写 200MB、**漏页数上限**。

本文件把上述断言**绑定到真值**（能复算的复算，不能复算的钉住方向 + 给正向对照）。
"""
import re
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))


def _doc() -> str:
    return (_ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8", errors="replace")


# ── 1. 日志轮转表 == logging_config.py ───────────────────────────────


def _handlers_from_source():
    """从 `logging_config.py` 解析 (文件名, maxBytes 的 MB 数, backupCount)。"""
    src = (_ROOT / "logging_config.py").read_text(encoding="utf-8")
    pat = re.compile(
        r'\{log_dir\}/(\w+)\.log",\s*'
        r"maxBytes=(\d+)\s*\*\s*1024\s*\*\s*1024,\s*"
        r"backupCount=(\d+)"
    )
    return [(m.group(1), int(m.group(2)), int(m.group(3))) for m in pat.finditer(src)]


def test_log_rotation_table_matches_logging_config():
    """文档的轮转策略必须等于 `logging_config.py` 的实际配置（逐文件对应）。

    日志是排障时唯一的现场；轮转策略写错会让人按错误的量级判断"日志怎么没了"。
    """
    handlers = _handlers_from_source()
    assert len(handlers) >= 3, (
        f"只从 logging_config.py 解析到 {len(handlers)} 个 RotatingFileHandler"
        f"（解析方式可能已过时）：{handlers}"
    )
    doc = _doc()
    problems = []
    for name, mb, backups in handlers:
        row = next(
            (ln for ln in doc.splitlines() if ln.startswith(f"| `{name}.log` |")), None
        )
        if row is None:
            problems.append(f"{name}.log：文档表格里没有这一行")
            continue
        if f"{mb}MB × {backups} 份" not in row:
            problems.append(
                f"{name}.log：文档写 {row!r}，代码是 {mb}MB × {backups} 份"
            )
    assert not problems, "DEPLOYMENT.md 日志轮转表与 logging_config.py 不一致：\n  " + "\n  ".join(problems)


# ── 2. CSS 体积声明 ≈ 实物 ──────────────────────────────────────────


def test_app_css_size_claim_is_close_to_reality():
    """文档声明的 app.css 体积必须与实物同量级（±30%）。

    不要求精确：Tailwind 输出随类名增删浮动。但"~14KB" vs 实测 20KB 已超出任何
    合理近似 —— 它会让读者误判"CSS 是否被换成了未压缩版"。
    """
    m = re.search(r"`static/app\.css`[^|]*\|\s*[^|]*（~(\d+)KB", _doc())
    assert m, "DEPLOYMENT.md 未给出 app.css 的体积量级（或写法已变，请同步本测试）"
    claimed = int(m.group(1))
    actual_kb = (_ROOT / "static" / "app.css").stat().st_size / 1024
    assert abs(claimed - actual_kb) / actual_kb <= 0.30, (
        f"DEPLOYMENT.md 写 ~{claimed}KB，实物 {actual_kb:.1f}KB（差 "
        f"{abs(claimed - actual_kb) / actual_kb:.0%}）"
    )


# ── 3. 上传文件的保留策略（合规相关）────────────────────────────────


def test_upload_file_retention_is_documented_and_code_backed():
    """上传的 PDF **不在 OCR 完成后删除**；文档必须如实说明，且删除路径真实存在。

    这条是**合规相关**的：受监管用户会依据"文件何时被销毁"做数据留存判断。
    原文档写「OCR 完成后自动清理」，与实现相反（且复核页依赖该 PDF）。
    """
    doc = _doc()
    assert "保留至任务删除" in doc, "DEPLOYMENT.md 未说明上传文件的真实保留策略"
    assert "OCR 完成后自动清理" not in doc, (
        "DEPLOYMENT.md 又出现了已被否证的说法「OCR 完成后自动清理」—— "
        "core/ 内没有任何删除已上传 PDF 的逻辑"
    )
    # 正向对照：删除路径必须真实存在，否则"保留至任务删除"也是空话
    actions = (_ROOT / "api" / "jobs" / "actions.py").read_text(encoding="utf-8")
    assert "shutil.rmtree(job_dir" in actions, (
        "delete_job 里找不到对 job 目录的删除 —— 「保留至任务删除」失去依据"
    )


def test_core_package_does_not_delete_the_uploaded_pdf():
    """`core/` 不得出现"删掉上传 PDF"的逻辑 —— 它是上面那条文档断言的代码侧对照。

    只扫**同一行同时出现 `pdf_path` 与删除调用**的情况：既能抓住真实回流，
    又不会误伤 `core/mineru_client.py` 里清理自家临时目录的 `rmtree(d)`。
    """
    offenders = []
    for p in sorted((_ROOT / "core").rglob("*.py")):
        for i, line in enumerate(
            p.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if "pdf_path" in line and re.search(r"\.unlink\(|rmtree\(|os\.remove\(", line):
                offenders.append(f"{p.relative_to(_ROOT)}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "core/ 中出现了删除已上传 PDF 的逻辑 —— 复核页的逐页渲染依赖它；"
        "若确实要改保留策略，必须同步 DEPLOYMENT.md 与 PORTABLE_README.txt：\n  "
        + "\n  ".join(offenders)
    )


# ── 4. 示例里不得硬编码会自增的值 ───────────────────────────────────


def test_health_example_does_not_hardcode_a_version():
    """`/health` 示例输出不得写死版本号 —— 它必然随发版腐坏（实测残留 1.0.0）。

    改为引用单一真值名（`main.APP_VERSION`），示例就不再需要维护。
    ⚠️ 只扫 `# →` 形式的**示例输出**行：正文里的历史实测记录（如
    "v1.1.9 基线实测 `/health` → …1.1.9"）是**当时的事实**，不得当作腐坏。
    """
    doc = _doc()
    examples = [ln for ln in doc.splitlines() if ln.lstrip().startswith("# →")]
    assert examples, "未找到 `# →` 形式的示例输出行（写法已变，请同步本测试）"
    offenders = [
        ln for ln in examples if re.search(r'"version"\s*:\s*"\d+\.\d+\.\d+"', ln)
    ]
    assert not offenders, (
        "DEPLOYMENT.md 的示例输出硬编码了版本号（必然腐坏）：\n  "
        + "\n  ".join(offenders)
        + "\n改为引用 main.APP_VERSION"
    )
    assert "<main.APP_VERSION>" in doc, "未找到引用单一真值的版本示例"
