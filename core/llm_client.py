"""大模型客户端（OpenAI 兼容接口）。

base_url / api_key / model / temperature / max_tokens 全部来自 settings，可随时切换平台。
提供普通对话与「生成策略 DSL」两种调用；后者解析出 JSON 并用 DSL 模型校验。
"""
from __future__ import annotations

import json
import re
from typing import Optional

from openai import OpenAI

from core.config import apply_proxies, get_setting
from core.strategy_dsl import StrategyDSL
from core.strategy_prompt import build_messages


class LLMError(RuntimeError):
    pass


def _client() -> OpenAI:
    apply_proxies()
    base_url = get_setting("llm_base_url") or None
    api_key = get_setting("llm_api_key") or "EMPTY"
    return OpenAI(base_url=base_url, api_key=api_key)


def _params(**overrides):
    params = {
        "model": get_setting("llm_model") or "gpt-4o-mini",
        "temperature": float(get_setting("llm_temperature", "0.2")),
        "max_tokens": int(get_setting("llm_max_tokens", "2000")),
    }
    params.update(overrides)
    return params


def chat(messages: list[dict], system: Optional[str] = None) -> str:
    """普通多轮对话，返回助手文本。"""
    client = _client()
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)
    try:
        resp = client.chat.completions.create(messages=msgs, **_params())
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"大模型请求失败：{e}") from e
    return resp.choices[0].message.content or ""


def test_connection(prompt: str = "请回复：连接正常") -> dict:
    """连通性测试，返回 {ok, reply, error}。"""
    try:
        reply = chat([{"role": "user", "content": prompt}])
        return {"ok": True, "reply": reply.strip(), "error": ""}
    except LLMError as e:
        return {"ok": False, "reply": "", "error": str(e)}


def generate_strategy(dialog_history: list[dict]) -> tuple[str, Optional[StrategyDSL], Optional[str]]:
    """根据对话历史生成策略。

    返回 (raw_text, dsl_or_None, error_or_None)。
    若大模型在追问/澄清（未产出合法 JSON），dsl 为 None 且 error 为 None。
    若产出非法 JSON，error 给出原因。
    """
    messages = build_messages(dialog_history)
    raw = chat(messages)
    obj, err = _extract_json(raw)
    if err:
        return raw, None, err
    if obj is None:
        # 大模型在追问，未输出 JSON
        return raw, None, None
    try:
        dsl = StrategyDSL.model_validate(obj)
    except Exception as e:  # noqa: BLE001
        return raw, None, f"DSL 校验失败：{e}"
    return raw, dsl, None


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_OBJ_RE = re.compile(r"(\{.*\})", re.DOTALL)


def _extract_json(text: str) -> tuple[Optional[dict], Optional[str]]:
    """从模型输出中抽取 JSON 对象。返回 (obj_or_None, error_or_None)。"""
    m = _JSON_BLOCK_RE.search(text)
    candidate = m.group(1) if m else None
    if candidate is None:
        m2 = _BARE_OBJ_RE.search(text)
        candidate = m2.group(1) if m2 else None
    if candidate is None:
        return None, None  # 没有 JSON，视为对话澄清
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败：{e}"
    if not isinstance(obj, dict):
        return None, "顶层不是 JSON 对象"
    return obj, None
