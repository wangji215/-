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
from core.strategy_compiler import validate_draft_coverage
from core.strategy_draft import StrategyGenerationResult
from core.strategy_dsl import StrategyDSL
from core.strategy_prompt import build_messages
from core.strategy_ref import build_sample_reference


_WINDOW_INDS = {"HHV", "LLV", "HHVBARS", "LLVBARS", "RISEBARS"}
_TEMPORAL_INDS = _WINDOW_INDS | {"COUNT", "EXIST", "EVERY", "BARSLAST"}


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


def chat(messages: list[dict], system: Optional[str] = None, **param_overrides) -> str:
    """普通多轮对话，返回助手文本。"""
    client = _client()
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)
    try:
        resp = client.chat.completions.create(messages=msgs, **_params(**param_overrides))
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


def generate_strategy(dialog_history: list[dict],
                      sample_codes: Optional[list[str]] = None,
                      ref_start: Optional[str] = None,
                      ref_end: Optional[str] = None,
                      ref_buy: Optional[str] = None
                      ) -> tuple[str, Optional[StrategyDSL], Optional[StrategyGenerationResult], Optional[str]]:
    """根据对话历史生成策略。

    返回 (raw_text, dsl_or_None, generation_or_None, error_or_None)。
    若大模型在追问/澄清（未产出合法 JSON），dsl 为 None 且 error 为 None。
    若产出非法 JSON，error 给出原因。

    sample_codes 非空时，先构建样本股参考文本注入 prompt，使策略贴合真实样本。
    ref_start/ref_end/ref_buy（YYYYMMDD）给出时进入「窗口+买点」模式：以买点当日为目标态、
    窗口走势作校准，供模型以标注样本优化策略。
    """
    reference = (
        build_sample_reference(sample_codes, start=ref_start, end=ref_end, buy_date=ref_buy)
        if sample_codes else None
    )
    messages = build_messages(dialog_history, reference)
    prompt_text = _user_text(dialog_history)
    raw = chat(messages, temperature=0)
    dsl, generation, parse_err = _parse_strategy_raw(raw)
    if dsl is None:
        return raw, dsl, generation, parse_err

    coverage_issues = _validate_text_dsl_coverage(prompt_text, dsl)
    if coverage_issues:
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    "上一次 JSON 没有完整覆盖自然语言策略："
                    + "；".join(coverage_issues)
                    + "。请只重新输出一个 JSON 代码块，保留用户原始语义，把缺失语义补进 compiled_dsl 的可执行 rules。"
                ),
            },
        ]
        retry_raw = chat(retry_messages, temperature=0)
        retry_dsl, retry_generation, retry_err = _parse_strategy_raw(retry_raw)
        if retry_dsl is not None:
            retry_issues = _validate_text_dsl_coverage(prompt_text, retry_dsl)
            if not retry_issues:
                return retry_raw, retry_dsl, retry_generation, retry_err
            retry_warning = "覆盖检查提示：" + "；".join(retry_issues)
            return retry_raw, retry_dsl, retry_generation, _merge_errors(retry_err, retry_warning)

        retry_parse_err = retry_err or "重试后未生成可执行 DSL"
        coverage_warning = "覆盖检查提示：" + "；".join(coverage_issues)
        return raw, dsl, generation, _merge_errors(parse_err, coverage_warning, retry_parse_err)

    return raw, dsl, generation, parse_err


def _parse_strategy_raw(raw: str) -> tuple[Optional[StrategyDSL], Optional[StrategyGenerationResult], Optional[str]]:
    obj, err = _extract_json(raw)
    if err:
        return None, None, err
    if obj is None:
        return None, None, None
    return _parse_strategy_object(obj)


def _parse_strategy_object(obj: dict) -> tuple[Optional[StrategyDSL], Optional[StrategyGenerationResult], Optional[str]]:
    """解析模型输出对象，兼容新 generation envelope 和旧版直接 DSL。"""
    if "draft" in obj or "compiled_dsl" in obj:
        fallback_dsl = None
        if isinstance(obj.get("compiled_dsl"), dict):
            try:
                fallback_dsl = StrategyDSL.model_validate(obj["compiled_dsl"])
            except Exception:
                fallback_dsl = None
        try:
            generation = StrategyGenerationResult.model_validate(obj)
        except Exception as e:  # noqa: BLE001
            if fallback_dsl is not None:
                return fallback_dsl, None, f"分段策略校验失败，但已保留可执行 DSL：{e}"
            return None, None, f"分段策略校验失败：{e}"
        issues = validate_draft_coverage(generation.draft, generation.compiled_dsl)
        if issues:
            return generation.compiled_dsl, generation, "覆盖检查提示：" + "；".join(issues)
        return generation.compiled_dsl, generation, None

    try:
        dsl = StrategyDSL.model_validate(obj)
    except Exception as e:  # noqa: BLE001
        return None, None, f"DSL 校验失败：{e}"
    return dsl, None, None


def _user_text(dialog_history: list[dict]) -> str:
    return "\n".join(str(msg.get("content", "")) for msg in dialog_history if msg.get("role") == "user")


def _merge_errors(*errors: Optional[str]) -> Optional[str]:
    parts = [err for err in errors if err]
    return "；".join(parts) if parts else None


def _dsl_indicator_names(dsl: StrategyDSL) -> set[str]:
    names: set[str] = set()

    def visit(ind) -> None:
        names.add(ind.ind.upper())
        if ind.expr:
            try:
                nested = StrategyDSL.model_validate({
                    "name": "_expr",
                    "lookback": 5,
                    "conditions": [{"logic": "and", "rules": [ind.expr]}],
                })
            except Exception:
                return
            names.update(_dsl_indicator_names(nested))

    for group in dsl.conditions:
        for rule in group.rules:
            visit(rule.left)
            if rule.right is not None:
                visit(rule.right)
    return names


def _validate_text_dsl_coverage(text: str, dsl: StrategyDSL) -> list[str]:
    """按用户原文检查 DSL 是否漏掉关键时序语义。

    这不是股票或策略模板定制，而是防止模型把“前期/回看/拉升/回踩/曾经/未形成”
    这类时间语义退化成只看当前 K 线的一组扁平条件。
    """
    normalized = re.sub(r"\s+", "", text).lower()
    if not normalized:
        return []

    inds = _dsl_indicator_names(dsl)
    issues: list[str] = []

    has_rise_text = any(token in normalized for token in ("拉升", "上涨", "升幅", "涨幅"))
    if has_rise_text and not ("RISEBARS" in inds or {"HHV", "LLV"} <= inds):
        issues.append("描述包含拉升/涨幅，但 DSL 缺少 RISEBARS 或 HHV/LLV 等阶段涨幅规则")

    has_pullback_text = any(token in normalized for token in ("回踩", "回落", "回调", "整理"))
    if has_pullback_text and not (inds & _TEMPORAL_INDS):
        issues.append("描述包含回踩/回落/整理，但 DSL 缺少窗口或条件聚合规则")
    if has_pullback_text and "回踩贴近" in normalized and not (inds & {"EXIST", "COUNT", "BARSLAST"}):
        issues.append("描述包含回踩贴近，但 DSL 只看当前贴近，缺少 EXIST/COUNT/BARSLAST 等曾经贴近规则")

    if "未形成死叉" in normalized and "EVERY" not in inds:
        issues.append("描述包含未形成死叉，但 DSL 缺少 EVERY 等持续不跌破规则")

    trigger_tokens = ("买点", "触发", "再次站上", "刚上穿", "首次站上")
    if any(token in normalized for token in trigger_tokens):
        has_cross = any(rule.op in {"cross_up", "cross_down"} for group in dsl.conditions for rule in group.rules)
        if not has_cross:
            issues.append("描述包含买点/触发/再次站上，但 DSL 缺少 cross_up/cross_down 触发规则")

    return issues


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
