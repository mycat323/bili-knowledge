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
    if settings.llm_disable_thinking:
        # 关掉思考型模型的长推理。实测有效的是 `thinking`，
        # 而 `enable_thinking` / `reasoning_effort` / `effort` 在该网关上都是空操作。
        # 两个都发：不同厂商的 OpenAI 兼容网关各认其中一个，不认的会忽略。
        payload["thinking"] = {"type": "disabled"}
        payload["enable_thinking"] = False

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
_JSON_DECODER = json.JSONDecoder()


def extract_json(text: str) -> dict:
    """从模型输出里取出第一个完整的 JSON 对象。

    模型偶尔会用 ```json 包裹、前后多说一句，或者把同一个对象连着输出两遍。
    实测后者会让 json.loads 报 `Extra data`，所以兜底改成用 raw_decode 从第一个
    `{` 开始只解一个对象。不能用 rfind('}') 去截：两个对象连在一起时，
    从第一个 { 到最后一个 } 的切片依然不是合法 JSON。

    两处 strict=False 是刚需：文章正文是多行 Markdown，模型有时会在 JSON 字符串里
    直接输出真实换行而不是转义的 \n，严格模式会报 Invalid control character。
    """
    cleaned = _FENCE.sub("", text.strip())
    try:
        value = json.loads(cleaned, strict=False)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    if start != -1:
        try:
            value, _ = _JSON_DECODER.raw_decode(cleaned[start:])
        except json.JSONDecodeError as exc:
            raise LlmError(f"模型输出不是合法 JSON：{exc}\n原文片段：{cleaned[:400]}") from exc
        if isinstance(value, dict):
            return value
    raise LlmError(f"模型输出中找不到 JSON 对象：{cleaned[:400]}")


def chat_json(messages: list[dict], settings: Settings, **kwargs) -> dict:
    """取一轮 JSON，解析失败时带上失败原因纠错重试一次。

    批量处理时单条视频失败会浪费掉前面已经花过钱的转写，一次纠错重试
    远比事后全量重跑便宜。
    """
    text = chat(messages, settings, **kwargs)
    try:
        return extract_json(text)
    except LlmError as first:
        log = kwargs.get("log")
        if log:
            log(f"    JSON 解析失败，纠错重试一次（{str(first).splitlines()[0][:60]}）")
        retry_messages = [
            *messages,
            {"role": "assistant", "content": text[:2000]},
            {
                "role": "user",
                "content": (
                    "上面的输出不是合法 JSON。请重新输出，只输出一个 JSON 对象，"
                    "不要任何解释、不要重复输出，确保字符串内的换行与引号都正确转义。"
                ),
            },
        ]
        return extract_json(chat(retry_messages, settings, **kwargs))
