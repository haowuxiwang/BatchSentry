"""Downstream probes — test_provider / test_feishu."""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config import config, load_feishu_config
from core.security import is_local_request, validate_external_url
from db.client import get_db
from api.settings import _PROVIDER_NAME_RE, router
from api.settings.read import _mask, _settings_config_path

logger = logging.getLogger(__name__)


class TestProviderRequest(BaseModel):
    """测试指定 provider 请求。"""
    provider: str


async def _audit_llm_test(provider: str, action: str, detail: str) -> None:
    """审计：provider 测试结果写 audit_log（job_id='system'，与 feishu_test 同模式）。"""
    try:
        db = await get_db()
        await db.execute(
            "INSERT INTO audit_log (job_id, action, detail, created_at) VALUES ('system', ?, ?, datetime(\'now\',\'localtime\'))",
            (action, detail),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"[settings] audit write failed ({action}): {e}")


@router.post("/api/settings/test_provider")
async def test_provider(req: TestProviderRequest, request: Request):
    """测试指定 provider 的连通性 — 用临时构建的 LLMClient 真实调用。

    与 /api/health/downstream 区别：
    - downstream 测试当前 active provider
    - 本端点测试指定 provider（即使非 active）
    用于每个 provider 卡片独立的"测试连接"按钮。
    """
    if not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")

    name = req.provider.strip().lower()
    providers = config["providers"]
    if name not in providers:
        raise HTTPException(404, f"Provider {name!r} not found")

    cfg = providers[name]
    if not cfg.api_key:
        return {
            "ok": False,
            "provider": name,
            "reason": "API 密钥未配置",
        }

    # 用临时 client 测试，不影响全局单例
    from llm.client import LLMClient
    try:
        probe_client = LLMClient(provider=name)
        start = time.time()
        await probe_client.adapter.chat(
            system_prompt="",
            user_content="ping",
            max_tokens=1,
            temperature=0,
            timeout=8,
        )
        elapsed_ms = int((time.time() - start) * 1000)
        await _audit_llm_test(name, "llm_test_ok",
                              f"provider={name} model={cfg.model} "
                              f"latency_ms={elapsed_ms}")
        return {
            "ok": True,
            "provider": name,
            "model": cfg.model,
            "latency_ms": elapsed_ms,
            "reason": "",
        }
    except Exception as e:
        err_str = str(e).lower()
        if any(kw in err_str for kw in ["401", "authentication", "unauthorized"]):
            reason = "API Key 无效或已过期"
        elif any(kw in err_str for kw in ["403", "forbidden"]):
            reason = "访问被拒绝（可能 Key 无此模型权限）"
        elif any(kw in err_str for kw in ["timeout", "timed out"]):
            reason = "请求超时（8s）"
        elif any(kw in err_str for kw in ["connection", "dns", "resolve"]):
            reason = "无法连接到 Base URL"
        else:
            # P0-1/P1-8: fallback 消息经 _mask_secrets 脱敏（防止 URL 签名/
            # 密钥随异常回显到设置页），中文化前缀便于用户理解
            from llm.client import _mask_secrets
            reason = f"测试失败：{e.__class__.__name__}: {_mask_secrets(str(e))[:200]}"
        # P1-8: 测试结果同样写审计（llm_call_audit 走 LLMClient 内部路径，但
        # 设置页直接调 adapter.chat 不经 client — 此处显式记录 provider 测试
        # 的成败，满足 GMP 追溯要求的完整操作记录）
        await _audit_llm_test(name, "llm_test_failed",
                              f"provider={name} model={cfg.model} reason={reason[:200]}")
        return {
            "ok": False,
            "provider": name,
            "reason": reason,
        }


class TestFeishuRequest(BaseModel):
    """飞书通知测试请求（字段可选—缺省用已保存配置）。

    mode: "webhook" 群机器人 | "app_bot" 自建应用私聊。
    """
    mode: Optional[str] = None
    webhook_url: Optional[str] = None
    secret: Optional[str] = None
    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    open_id: Optional[str] = None
    mobile: Optional[str] = None


@router.post("/api/settings/test_feishu")
async def test_feishu(req: TestFeishuRequest, request: Request):
    """发送一条测试消息验证飞书通知可用性（真实调用，非假端点）。

    校验顺序：模式识别 → 必要配置存在 → 私网地址拦截（SSRF）→
    签名 + 发送 → 解析业务 code（HTTP 200 不等于成功）。
    失败原因中文化便于设置页提示。
    """
    if not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")

    saved = load_feishu_config()
    mode = (req.mode or "").strip().lower() or saved.get("mode", "webhook")
    if mode not in ("webhook", "app_bot"):
        return {"ok": False, "reason": f"未知模式 {mode!r}"}

    import asyncio as _asyncio
    from core.notify import (
        _post_app_bot_sync, _post_sync, app_bot_error_zh, build_text_message,
    )
    test_text = "【BatchSentry】飞书通知测试消息 — 配置成功 ✅"

    if mode == "app_bot":
        app_id = (req.app_id or "").strip() or saved.get("app_id", "")
        app_secret = (req.app_secret or "").strip() or saved.get("app_secret", "")
        open_id = (req.open_id or "").strip() or saved.get("open_id", "")
        mobile = (req.mobile or "").strip() or saved.get("mobile", "")
        if not app_id or not app_secret:
            return {"ok": False, "reason": "未配置 App ID / App Secret"}
        if app_secret == _mask(saved.get("app_secret", "")):
            return {"ok": False, "reason": "App Secret 为掩码，请粘贴完整值"}
        if not open_id and not mobile:
            return {"ok": False, "reason": "未配置接收者 open_id 或手机号"}
        ok, detail = await _asyncio.to_thread(
            _post_app_bot_sync, app_id, app_secret, open_id, mobile, test_text
        )
        # 失败时把业务码映射成中文提示
        if not ok:
            for code_str in ("230006", "230013", "230027", "230028", "230034", "230053", "230101", "99991661", "99991663"):
                if f"code={code_str}" in detail:
                    hint = app_bot_error_zh(int(code_str))
                    detail = f"code={code_str} {hint}"
                    break
        try:
            from db.client import get_db
            db = await get_db()
            await db.execute(
                "INSERT INTO audit_log (job_id, action, detail, created_at) VALUES ('system', 'feishu_test', ?, datetime(\'now\',\'localtime\'))",
                (f"mode=app_bot ok={ok} {detail}",),
            )
            await db.commit()
        except Exception:
            pass
        if ok:
            return {"ok": True, "reason": ""}
        return {"ok": False, "reason": f"发送失败: {detail}"}

    url = (req.webhook_url or "").strip() or saved.get("webhook_url", "")
    secret = (req.secret or "").strip() or saved.get("secret", "")
    if not url:
        return {"ok": False, "reason": "未配置 webhook URL"}
    # 掩码回填保护：掩码本身不是合法 URL，先拦截给出明确提示（避免先落入 URL 格式报错）
    if url == _mask(saved.get("webhook_url", "")):
        return {"ok": False, "reason": "填写的 URL 与掩码一致，请粘贴完整 webhook URL"}
    # 与 app_secret 一致（:884）：secret 为掩码时拒绝 — 否则签名校验失败提示具有误导性
    if secret and secret == _mask(saved.get("secret", "")):
        return {"ok": False, "reason": "Secret 为掩码，请粘贴完整值"}
    ok, reason = validate_external_url(url, kind="Feishu webhook")
    if not ok:
        return {"ok": False, "reason": reason}

    payload = build_text_message("（测试消息）", "review", 0, None, "")
    payload["content"]["text"] = test_text
    ok, detail = await _asyncio.to_thread(_post_sync, url, payload, secret)
    try:
        from db.client import get_db
        db = await get_db()
        await db.execute(
            "INSERT INTO audit_log (job_id, action, detail, created_at) VALUES ('system', 'feishu_test', ?, datetime(\'now\',\'localtime\'))",
            (f"mode=webhook ok={ok} {detail}",),
        )
        await db.commit()
    except Exception:
        pass
    if ok:
        return {"ok": True, "reason": ""}
    hint = {
        "code=19021": "签名校验失败（检查 secret 与本机时间）",
        "code=19024": "消息不含群关键词（请在群机器人里加关键词 BatchSentry）",
        "code=19022": "IP 白名单校验失败（改用签名校验）",
        "code=9499": "消息体超过 20KB（请联系开发者）",
        "code=11232": "触发频率限制（稍后再试）",
    }
    for k, v in hint.items():
        if k in detail:
            detail = v
            break
    return {"ok": False, "reason": f"发送失败: {detail}"}
