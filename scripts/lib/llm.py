"""知识稿撰写模型（任何 OpenAI 兼容的 /chat/completions 接口）。"""

from __future__ import annotations

import json
import re
import time

import httpx

from .settings import Settings

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRY_DELAYS = (2.0, 6.0, 15.0)


class LlmError(RuntimeError):
    pass


def chat(
    messages: list[dict],
    settings: Settings,
    *,
    max_tokens: int | None = None,
    temperature: float = 0.3,
    log=None,
) -> str:
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload: dict = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or settings.llm_max_tokens,
    }
    if settings.llm_enable_thinking is not None:
        # 千问系接口认这个开关；不支持它的网关会忽略，不影响调用。
        payload["enable_thinking"] = settings.llm_enable_thinking

    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }

    last: Exception | None = None
    for delay in (0.0, *RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            with httpx.Client(timeout=settings.request_timeout_seconds) as client:
                response = client.post(url, headers=headers, json=payload)
            if response.status_code in RETRYABLE_STATUS:
                last = LlmError(f"模型网关 {response.status_code}：{response.text[:200]}")
                continue
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            last = exc
            continue

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"模型返回结构异常：{json.dumps(data, ensure_ascii=False)[:300]}") from exc

        usage = data.get("usage") or {}
        completion = usage.get("completion_tokens")
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if log and completion is not None:
            log(
                f"    token：prompt={usage.get('prompt_tokens')} "
                f"completion={completion}（其中推理 {reasoning}）"
            )

        # 思考型模型会把输出额度用在推理上。截断必须显式报错，
        # 否则下游只会拿到一段残缺的 JSON，白白浪费一次调用。
        if finish == "length":
            raise LlmError(
                f"模型输出被截断（max_tokens={payload['max_tokens']}，"
                f"推理已用 {reasoning}，正文只剩 {(completion or 0) - (reasoning or 0)}）；"
                "请调大 .env 的 LLM_MAX_TOKENS"
            )
        if not isinstance(content, str) or not content.strip():
            raise LlmError(
                f"模型未返回正文（finish_reason={finish}，推理用掉 {reasoning} token）；"
                "通常是输出额度被推理耗尽，请调大 .env 的 LLM_MAX_TOKENS"
            )
        return content
    raise LlmError(f"模型调用失败（已重试 {len(RETRY_DELAYS)} 次）：{last}")


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def extract_json(text: str) -> dict:
    """从模型输出里取出 JSON 对象。

    模型偶尔会用 ```json 包裹，或在前后多说一句，这里做兜底而不是直接失败。
    """
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LlmError(f"模型输出不是合法 JSON：{exc}\n原文片段：{cleaned[:400]}") from exc
    raise LlmError(f"模型输出中找不到 JSON 对象：{cleaned[:400]}")


def chat_json(messages: list[dict], settings: Settings, **kwargs) -> dict:
    return extract_json(chat(messages, settings, **kwargs))