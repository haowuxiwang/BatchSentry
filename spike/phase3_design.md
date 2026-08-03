# Phase 3 详细设计 — 前端复核页改造（critical 高亮 + source 分层 + 矩阵 cell 标注）

| 字段 | 值 |
|---|---|
| 版本 | v1（待 user 确认） |
| 日期 | 2026-07-27 |
| 输入 | Phase 2 验证结果（26 findings，含 1 critical time_reversal）|
| 目标文件 | [templates/review.html](file:///d:/learn/claudecode/pharma-batch-checker/templates/review.html) + [api/review.py](file:///d:/learn/claudecode/pharma-batch-checker/api/review.py) + [db/client.py](file:///d:/learn/claudecode/pharma-batch-checker/db/client.py) + [core/pipeline.py](file:///d:/learn/claudecode/pharma-batch-checker/core/pipeline.py) |
| 不动文件 | `core/page_analyzer.py`、`core/cross_page_analyzer.py`、`models/schemas.py` |

---

## 1. 现状分析

### 1.1 当前复核页能力

[templates/review.html](file:///d:/learn/claudecode/pharma-batch-checker/templates/review.html) 已有：
- 左 PDF iframe + 右 OCR/findings 双栏布局
- findings 卡片按 severity 着色（critical 红 / warning 橙 / info 蓝）
- 三按钮（确认/拒绝/修正）+ 状态切换

### 1.2 Phase 2 暴露的 5 个问题

| # | 问题 | 根因 |
|---|---|---|
| P1 | **critical 不显眼** | 只在卡片左边框加 4px 红条，复核员扫一眼可能漏 |
| P2 | **source 字段丢失** | cross-page 产 26 findings 含 `source: rule/llm_page/llm_fallback/llm_cross`，但 [core/pipeline.py:182](file:///d:/learn/claudecode/pharma-batch-checker/core/pipeline.py) INSERT 时只写 7 个字段，source 没入库 |
| P3 | **matrix cell 状态不显示** | page9 measurements 9×8=72 cell 含 `in_spec: true/false/null`，但复核页完全没渲染 |
| P4 | **跨页 finding 看不到上下文** | R1-b 跨页 time_reversal 描述 "第 10 页工序 3 早于第 9 页工序 2 结束"，但复核页只显示当前页 findings，复核员看不到 page 9 的工序 2 时间 |
| P5 | **page2 steps=[] 时 findings 来源混乱** | per-page LLM 产 5 findings + 规则层产 1 finding，用户分不清哪个是 LLM 判的哪个是规则判的 |

---

## 2. 改造目标

| 目标 | 验证方法 |
|---|---|
| G1 critical finding 顶部置顶 + 闪烁 | 复核员打开 page2，第一眼看到红色闪烁 time_reversal |
| G2 source 字段持久化 + UI 显示 | DB findings 表加 source 列；卡片显示徽章 `[rule]` / `[llm_page]` |
| G3 matrix cell in_spec 渲染 | page9 显示 9×8 表格，每个 cell 按合规/不合规/未知着色 |
| G4 跨页 finding 跳转 | page10 R1-b finding 含"跳到 page 9"链接 |
| G5 findings 排序：critical > warning > info，rule > llm_page | 视觉优先级 |

---

## 3. DB 改造（最小改动）

### 3.1 findings 表加 source 列

[db/schema.sql](file:///d:/learn/claudecode/pharma-batch-checker/db/schema.sql) 改：

```sql
CREATE TABLE IF NOT EXISTS findings (
    ...
    source TEXT DEFAULT 'rule',   -- Phase 3: rule | llm_page | llm_fallback | llm_cross
    ...
);
```

[db/client.py](file:///d:/learn/claudecode/pharma-batch-checker/db/client.py) `migrate()` 追加：

```python
# Phase 3: add source column to findings
cursor = await db.execute("PRAGMA table_info(findings)")
finding_cols = {row["name"] for row in await cursor.fetchall()}
if "source" not in finding_cols:
    await db.execute("ALTER TABLE findings ADD COLUMN source TEXT DEFAULT 'rule'")
    logger.info("Migration: added findings.source")
```

### 3.2 pipeline.py INSERT 加 source

[core/pipeline.py:182](file:///d:/learn/claudecode/pharma-batch-checker/core/pipeline.py) 改：

```python
await db.execute(
    "INSERT INTO findings (job_id, page, type, severity, description, ocr_text, operator, source) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    (job_id, f["page"], f["type"], f["severity"], f["description"],
     f.get("ocr_text"), f.get("operator"), f.get("source", "rule")),
)
```

---

## 4. API 改造

### 4.1 [api/review.py](file:///d:/learn/claudecode/pharma-batch-checker/api/review.py) `list_findings` 加排序

```python
@router.get("/jobs/{job_id}/findings")
async def list_findings(job_id: str, status: Optional[str] = None):
    db = await get_db()
    # Phase 3: order by severity (critical first), then source (rule first)
    severity_order = "CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 WHEN 'info' THEN 2 ELSE 3 END"
    source_order = "CASE source WHEN 'rule' THEN 0 WHEN 'llm_fallback' THEN 1 WHEN 'llm_page' THEN 2 WHEN 'llm_cross' THEN 3 ELSE 4 END"
    if status:
        cursor = await db.execute(
            f"SELECT * FROM findings WHERE job_id = ? AND status = ? "
            f"ORDER BY page, {severity_order}, {source_order}, id",
            (job_id, status),
        )
    else:
        cursor = await db.execute(
            f"SELECT * FROM findings WHERE job_id = ? "
            f"ORDER BY page, {severity_order}, {source_order}, id",
            (job_id,),
        )
    ...
```

### 4.2 新增 `/jobs/{job_id}/pages/{page}/measurements` 端点

为 G3 矩阵渲染提供数据：

```python
@router.get("/jobs/{job_id}/pages/{page}/measurements")
async def get_page_measurements(job_id: str, page: int):
    """Return measurement matrix for rendering on review page."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT structured_json FROM page_cache WHERE job_id = ? AND page = ?",
        (job_id, page),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Page not found")
    data = json.loads(row["structured_json"]) if row["structured_json"] else {}
    measurements = []
    for step in data.get("steps", []) or []:
        for m in step.get("measurements", []) or []:
            measurements.append({
                "step_no": step.get("step_no"),
                "time": m.get("time"),
                "values": m.get("values") or {},
            })
    return {"page": page, "measurements": measurements, "count": len(measurements)}
```

---

## 5. 前端 review.html 改造（核心）

### 5.1 critical finding 顶部置顶 + 闪烁（G1）

```html
<style>
    /* Phase 3: critical 闪烁 + 顶部置顶 */
    .findings-list-critical {
        position: sticky;
        top: 0;
        z-index: 100;
        background: #fff5f5;
        border: 2px solid #e74c3c !important;
        animation: critical-pulse 2s ease-in-out infinite;
    }
    @keyframes critical-pulse {
        0%, 100% { box-shadow: 0 0 0 0 rgba(231, 76, 60, 0.7); }
        50%      { box-shadow: 0 0 0 8px rgba(231, 76, 60, 0); }
    }
    .critical-banner {
        background: #e74c3c;
        color: #fff;
        padding: 6px 12px;
        font-weight: bold;
        border-radius: 4px;
        margin-bottom: 8px;
        text-align: center;
        animation: critical-pulse 2s ease-in-out infinite;
    }
</style>

<!-- 顶部 critical 横幅 -->
{% set critical_count = findings | selectattr('severity', 'equalto', 'critical') | list | length %}
{% if critical_count > 0 %}
<div class="critical-banner">
    ⚠️ 本页有 {{ critical_count }} 条 CRITICAL 问题，请优先处理
</div>
{% endif %}

<!-- findings 循环，critical 加 sticky class -->
{% for f in findings %}
<div class="finding-card {{ f.severity }} {{ f.status }} {% if f.severity == 'critical' %}findings-list-critical{% endif %}" id="finding-{{ f.id }}">
    ...
</div>
{% endfor %}
```

### 5.2 source 徽章显示（G2）

```html
<!-- 在 finding-card 内，severity-badge 旁边 -->
<span class="severity-badge {{ f.severity }}">{{ f.severity }}</span>
{% if f.source %}
<span class="source-badge source-{{ f.source }}">{{ f.source }}</span>
{% endif %}
<strong>{{ f.type }}</strong>
```

CSS：

```css
.source-badge {
    font-size: 0.7rem;
    padding: 2px 6px;
    border-radius: 3px;
    color: #fff;
    margin-left: 4px;
}
.source-rule         { background: #34495e; }  /* 深蓝灰 */
.source-llm_page     { background: #9b59b6; }  /* 紫 */
.source-llm_fallback { background: #e67e22; }  /* 深橙 */
.source-llm_cross    { background: #16a085; }  /* 青 */
```

### 5.3 matrix cell 渲染（G3）

```html
<!-- 在 findings 列表下方 -->
{% if measurements %}
<h3>第 {{ page }} 页参数矩阵</h3>
<div class="matrix-container">
    <table class="measurement-matrix">
        <thead>
            <tr>
                <th>时间</th>
                {% for col in matrix_columns %}
                <th>{{ col }}</th>
                {% endfor %}
            </tr>
        </thead>
        <tbody>
            {% for m in measurements %}
            <tr>
                <td>{{ m.time }}</td>
                {% for col in matrix_columns %}
                {% set cell = m.values.get(col, {}) %}
                <td class="cell-{% if cell.in_spec == true %}ok{% elif cell.in_spec == false %}bad{% else %}unknown{% endif %}"
                    title="spec: {{ cell.spec }} | actual: {{ cell.actual }}">
                    {{ cell.actual or '-' }}
                </td>
                {% endfor %}
            </tr>
            {% endfor %}
        </tbody>
    </table>
</div>
{% endif %}
```

CSS：

```css
.measurement-matrix {
    border-collapse: collapse;
    font-size: 0.75rem;
    width: 100%;
}
.measurement-matrix th, .measurement-matrix td {
    border: 1px solid #ddd;
    padding: 4px 6px;
    text-align: center;
}
.cell-ok      { background: #d4edda; }  /* 绿 - in_spec=true */
.cell-bad     { background: #f8d7da; font-weight: bold; }  /* 红 - in_spec=false */
.cell-unknown { background: #fff3cd; }  /* 黄 - in_spec=null */
```

### 5.4 跨页 finding 跳转链接（G4）

在 finding-card 的 description 中，解析"第 X 页"并加跳转链接：

```html
<p>{{ f.description | render_page_links(job_id) }}</p>
```

Jinja2 自定义 filter（注册在 FastAPI app）：

```python
import re
from markupsafe import Markup

def render_page_links(text, job_id):
    """Convert '第N页' in finding description to clickable links."""
    def repl(m):
        page = m.group(1)
        return f'<a href="/jobs/{job_id}/review?page={page}" class="page-link">第{page}页</a>'
    return Markup(re.sub(r"第(\d+)页", repl, text))

# 注册：
app.template_filter('render_page_links')(render_page_links)
```

CSS：

```css
.page-link {
    color: #3498db;
    text-decoration: underline;
    font-weight: bold;
}
```

### 5.5 findings 排序（G5）

排序逻辑放在后端 API（见 4.1），前端直接按返回顺序渲染：

```
排序优先级：
  1. page (asc)
  2. severity: critical > warning > info
  3. source: rule > llm_fallback > llm_page > llm_cross
  4. id (asc)
```

---

## 6. 改造前后对比

### 6.1 page2（critical time_reversal）

**改造前**：
- 6 个 findings 卡片，time_reversal 和 warning 混排
- 没有 source 区分
- 复核员可能跳过 critical

**改造后**：
- 顶部红色闪烁横幅 "⚠️ 本页有 1 条 CRITICAL 问题"
- time_reversal 卡片 sticky 置顶 + 边框闪烁
- 卡片显示 `[llm_page]` 紫色徽章（per-page LLM 产的）
- description 中 "开始生产日期 2015.01.27" 加红色背景

### 6.2 page9（参数矩阵）

**改造前**：
- findings 列表只有 0 个 param_out_of_spec（全 in_spec）
- 复核员看不到 72 个 cell 的合规情况

**改造后**：
- findings 列表下方加 9×8 矩阵表格
- 每个 cell 绿色背景（全 in_spec=true）
- 鼠标 hover 显示 spec/actual
- 任何 cell in_spec=false 自动显示红色 + 加 finding

---

## 7. 实施顺序

| # | 任务 | 文件 | 验证 |
|---|---|---|---|
| 1 | DB schema 加 source 列 + migrate | `db/schema.sql` + `db/client.py` | 启动 server，老 DB 自动 migrate |
| 2 | pipeline.py INSERT 加 source | `core/pipeline.py:182` | 重跑 cross-page，DB findings.source 写入 |
| 3 | API list_findings 排序 | `api/review.py` | curl GET findings，确认排序 |
| 4 | API 新增 measurements 端点 | `api/review.py` | curl 验证 page9 measurements 返回 |
| 5 | review.html 加 critical 闪烁 + 横幅 | `templates/review.html` | 浏览器打开 page2，看闪烁 |
| 6 | review.html 加 source 徽章 | `templates/review.html` | 6 个 findings 显示不同颜色徽章 |
| 7 | review.html 加 matrix 表格 | `templates/review.html` | page9 显示 9×8 表格，绿色 cell |
| 8 | review.html 加 page-link filter | `templates/review.html` + app 注册 | page10 R1-b finding "第9页" 可点击 |
| 9 | 端到端验证 | 浏览器 page2 + page9 + page10 | 5 个目标 G1-G5 全部达成 |

---

## 8. 不在 Phase 3 范围

- 不改 per-page LLM prompt（Phase 1 已定 v3）
- 不改 cross-page 规则（Phase 2 已定 R1-R5 + 透传）
- 不改 DB schema 主表（只加 source 列）
- 不改前端框架（保持 Jinja2 + 原生 JS）
- 不加 WebSocket / 实时更新（保持现有 reload 模式）
- 不加 finding 自动去重（人工判）

---

## 9. 待 user 确认的 3 个问题

1. **critical 闪烁是否过于刺眼**：推荐用 `animation: critical-pulse 2s`（2 秒周期），可调慢到 3s 或关掉。是否保留？
2. **matrix 表格放在 findings 上方还是下方**：推荐下方（findings 优先），但 page9 findings=0 时表格占主导。是否需要"findings 为 0 时自动展开 matrix"？
3. **跨页跳转是否新开窗口**：推荐 `target="_blank"`，避免丢失当前页位置。是否同意？
