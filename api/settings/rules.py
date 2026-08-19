"""User compliance rules — GET/PUT /api/settings/rules."""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel

from config import USER_RULES_MAX, USER_RULES_TEXT_MAX, load_user_rules
from core.security import is_local_request
from db.client import get_db
from api.settings import router
from api.settings.read import _settings_config_path

logger = logging.getLogger(__name__)


class UserRuleItem(BaseModel):
    """单条用户合规规则。"""
    id: Optional[str] = None
    text: str
    active: bool = True


class UserRulesUpdate(BaseModel):
    """全量替换用户规则列表请求。"""
    rules: list[UserRuleItem] = []


_USER_RULES_MAX = USER_RULES_MAX
_USER_RULES_TEXT_MAX = USER_RULES_TEXT_MAX


def _read_raw_config() -> dict:
    """读取 config.json 原始内容（不存在则返回空 dict）。"""
    config_path = _settings_config_path()
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read {config_path}: {e}")
        return {}


def _write_raw_config(existing: dict) -> None:
    """原子写入 config.json（临时文件 + replace，防并发半写）。"""
    config_path = _settings_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.parent / f"config.json.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        tmp_path.replace(config_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@router.get("/api/settings/rules")
async def get_user_rules(request: Request):
    """返回用户填写的合规规则 + 各规则历史命中数（source='user_rule' 按 id 统计）。

    hits: {rule_id: 命中次数}，未回填 id 的历史命中归入空串键（GMP 溯源辅助）。
    last_saved_at: 最近一次成功保存时间（audit_log user_rules_update），
    None 表示从未保存成功 — 前端据此提示用户（防呆：规则不生效时一眼可判
    是"没保存"还是"没命中"）。
    """
    if not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")
    try:
        from db.client import get_db
        db = await get_db()
        cursor = await db.execute(
            "SELECT user_rule_id, COUNT(*) AS cnt FROM findings "
            "WHERE source = 'user_rule' AND user_rule_id IS NOT NULL GROUP BY user_rule_id"
        )
        hits = {}
        for row in await cursor.fetchall():
            hits[str(row["user_rule_id"])] = row["cnt"]
        cursor = await db.execute(
            "SELECT created_at FROM audit_log "
            "WHERE action = 'user_rules_update' "
            "ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        last_saved_at = row["created_at"] if row else None
    except Exception as e:
        logger.warning(f"Failed to gather user-rule hit stats: {e}")
        hits = {}
        last_saved_at = None
    return {"rules": load_user_rules(), "hits": hits, "last_saved_at": last_saved_at}


@router.put("/api/settings/rules")
async def update_user_rules(req: UserRulesUpdate, request: Request):
    """全量替换用户合规规则。

    校验：每条 text 非空且 ≤1000 字符，总数 ≤100。校验失败返回 400，
    不写入任何内容。成功后写 audit_log（GMP 追溯规则变更）。
    """
    if not is_local_request(request):
        raise HTTPException(403, "Forbidden (non-local request)")

    errors: list[str] = []
    cleaned: list[dict] = []
    for i, r in enumerate(req.rules):
        text = r.text.strip()
        if not text:
            errors.append(f"规则 #{i + 1}: 内容不能为空")
            continue
        if len(text) > _USER_RULES_TEXT_MAX:
            errors.append(f"规则 #{i + 1}: 内容超过 {_USER_RULES_TEXT_MAX} 字符上限")
            continue
        cleaned.append({
            "id": r.id or uuid.uuid4().hex[:12],
            "text": text,
            "active": bool(r.active),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    if len(cleaned) > _USER_RULES_MAX:
        errors.append(f"规则总数超过 {_USER_RULES_MAX} 条上限")
    if errors:
        # 校验失败也写 audit_log（GMP：规则变更事件需留痕，否则用户保存
        # 失败无任何记录 — 真实故障：用户误以为规则已生效但 config 从未
        # 更新，8-13 后无一条 user_rules_update 记录即为实证）。
        try:
            db = await get_db()
            await db.execute(
                "INSERT INTO audit_log (job_id, action, detail, created_at) VALUES (?, ?, ?, datetime(\'now\',\'localtime\'))",
                ("system", "user_rules_update_failed", "; ".join(errors)[:500]),
            )
            await db.commit()
        except Exception as e:
            logger.warning(f"Failed to write user_rules failure audit log: {e}")
        raise HTTPException(400, detail={"errors": errors})

    existing = _read_raw_config()
    existing["user_rules"] = cleaned
    _write_raw_config(existing)

    try:
        db = await get_db()
        await db.execute(
            "INSERT INTO audit_log (job_id, action, detail, created_at) VALUES (?, ?, ?, datetime(\'now\',\'localtime\'))",
            ("system", "user_rules_update",
             f"{len(cleaned)} rules (active={sum(1 for r in cleaned if r['active'])})"),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"Failed to write user_rules audit log: {e}")

    logger.info(f"User rules updated: {len(cleaned)} rules persisted to {_settings_config_path()}")
    return {
        "ok": True,
        "rules": cleaned,
        "message": f"已保存 {len(cleaned)} 条合规规则，将注入下次跨页分析",
    }
