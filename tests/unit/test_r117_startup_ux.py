"""R117：首屏等待与"回主页慢"的**可回归**约束。

两个现象都实测过，根因都不是"后端慢"：

① 首屏 30s 白屏
   `backend-boot.log`：`[PBC] Starting server` 00:04:26.994 →
   `Started server process` 00:04:42.431 = **15.4s 全在模块 import**；
   应用自身初始化（`Starting BatchSentry` → `Application startup complete`）
   只有 **51ms**。源码 `import main` 只要 1.65s，冻结包是 15.4s（7–9×）。
   ⇒ 这段等待**消不掉**（不重构打包的前提下），能改的是"别让用户对着白屏猜"。

② 点面包屑回主页"有点缓慢"
   `GET /` 5ms、`GET /api/jobs` 9ms —— 服务端没问题。慢的是级联入场动画：
   `--i = 序号 + 3`，第 20 行拿到 22 × 40ms = 880ms 延迟 + 200ms 动画 ≈ 1.08s。

这两个都是**文案/常量**层面的不变式，适合静态钉住：改坏了不会报错，只会让用户
重新看到"白屏"和"慢"，没有异常可抓。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "electron" / "main.js"
TOKENS = ROOT / "static" / "design-tokens.css"


class TestSplashTellsTheTruth:
    def test_shows_real_elapsed_seconds(self):
        js = MAIN.read_text(encoding="utf-8")
        assert "已用 ${elapsed} 秒" in js, (
            "首屏必须显示真实已用秒数 —— 一个不动的水波纹与白屏没有区别"
        )

    def test_does_not_blame_the_wrong_stage(self):
        """旧文案说「仍在等待后端就绪」，会让人去查后端/DB —— 真正在跑的是 import。"""
        js = MAIN.read_text(encoding="utf-8")
        assert "仍在等待后端就绪" not in js
        assert "正在加载分析引擎" in js

    def test_hint_threshold_is_not_trigger_happy(self):
        """热启动实测 0.6–5s；阈值太小会让本来很快的启动显得有问题。"""
        js = MAIN.read_text(encoding="utf-8")
        m = re.search(r"const SPLASH_HINT_AFTER_S = (\d+);", js)
        assert m, "缺少 SPLASH_HINT_AFTER_S 常量"
        assert int(m.group(1)) >= 5

    def test_splash_is_refreshed_every_poll_not_every_20(self):
        """按秒跳动的秒数必须每轮都写；日志才按 20 轮一次。"""
        js = MAIN.read_text(encoding="utf-8")
        loop = js[js.index("const elapsed = Math.max(1"):]
        loop = loop[:loop.index("setTimeout(check, 500)")]
        # `setSplashStatus` 必须在 `% 20` 的日志块**之外**
        guard = loop.index("checks % 20")
        assert loop.index("setSplashStatus(") < guard


class TestStaggerIsCapped:
    def test_delay_is_capped(self):
        css = TOKENS.read_text(encoding="utf-8")
        block = css[css.index(".stagger-in"):]
        block = block[:block.index("}")]
        assert "min(var(--i" in block, (
            "级联延迟必须有上限 —— 否则第 20 行要等 880ms 才出现，"
            "用户感知就是「回主页有点慢」"
        )

    def test_cap_is_small_enough_to_feel_instant(self):
        css = TOKENS.read_text(encoding="utf-8")
        block = css[css.index(".stagger-in"):]
        block = block[:block.index("}")]
        m = re.search(r"min\(var\(--i[^,]*,\s*(\d+)\)", block)
        assert m, "上限必须是显式数字，不能只写 min()"
        cap = int(m.group(1))
        # --stagger-step 实测 40ms ⇒ 上限 ≤ 300ms，加上 200ms 动画仍在一瞬之内
        assert cap * 40 <= 300, f"上限 {cap} 级 × 40ms 太长"
