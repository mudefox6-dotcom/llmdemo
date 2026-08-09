"""LLM 模型接入层 —— 统一管理 ChatOpenAI 实例。

适配 langchain-openai >= 1.1。
"""

from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI

from app.core.config import get_settings


def _dashscope_extra_body() -> dict | None:
    settings = get_settings()
    base_url = settings.openai_api_base.lower()
    model_name = settings.openai_model_name.lower()
    thinking_model_prefixes = ("qwen3", "glm-5")
    if "dashscope" in base_url and model_name.startswith(thinking_model_prefixes):
        return {"enable_thinking": False}
    return None


def _max_tokens_kwargs() -> dict:
    """当配置了 OPENAI_MAX_TOKENS(>0) 时才显式传 max_tokens，否则用服务商默认。

    显式指定可以争取到更长的单次输出，缓解 Engineer 生成完整技术设计时
    "输出被截断 → 结构化解析拿不到完整工具调用 → 返回 None" 的问题。
    """
    value = get_settings().openai_max_tokens
    return {"max_tokens": value} if value and value > 0 else {}


@lru_cache()
def get_llm(temperature: float = 0.3) -> ChatOpenAI:
    """获取默认 LLM 实例。"""
    settings = get_settings()
    return ChatOpenAI(
        model=settings.openai_model_name,
        api_key=settings.openai_api_key,
        base_url=settings.openai_api_base,
        temperature=temperature,
        timeout=settings.openai_timeout_seconds,
        max_retries=2,
        extra_body=_dashscope_extra_body(),
        **_max_tokens_kwargs(),
    )


def get_creative_llm() -> ChatOpenAI:
    """获取用于创意/方案生成的高 temperature LLM。"""
    settings = get_settings()
    return ChatOpenAI(
        model=settings.openai_model_name,
        api_key=settings.openai_api_key,
        base_url=settings.openai_api_base,
        temperature=0.7,
        timeout=settings.openai_timeout_seconds,
        max_retries=2,
        extra_body=_dashscope_extra_body(),
        **_max_tokens_kwargs(),
    )


def get_structured_llm(schema, *, temperature: float = 0.3, method: str | None = None):
    """获取"结构化输出"的 LLM——各 Agent 产出 PRD/技术设计/评审结论都用它。

    输入：schema=一个 Pydantic 模型类（如 PRDDocument）。
    输出：一个"调用后直接返回该模型实例"的 LLM，不用自己解析文本 JSON。
    原理：with_structured_output 把模型的字段定义转成 function_calling 的 schema，
         让 LLM 按字段填值，框架再校验反序列化成对象。method 默认 function_calling。
    """
    llm = get_llm(temperature)
    settings = get_settings()
    structured_method = method or settings.structured_output_method
    return llm.with_structured_output(schema, method=structured_method)


def invoke_structured(structured_llm, messages, *, node: str, attempts: int = 3):
    """调用结构化输出并对 None 重试。

    为什么需要：`with_structured_output` 在模型没有产出有效工具调用时会返回 **None**
    （输出被截断、模型偶发不按 schema 走等），而各 Agent 拿到就直接 `.model_dump()`，
    于是抛 `AttributeError: 'NoneType' object has no attribute 'model_dump'`，
    整个任务崩掉。模型有随机性，重试几次通常就能拿到结果。

    Args:
        structured_llm: get_structured_llm() 的返回值
        messages: 消息列表
        node: 节点名，仅用于日志
        attempts: 最多尝试次数

    Returns:
        Schema 实例；全部尝试都失败时返回 None，由调用方决定降级策略。
    """
    from app.core.logger import logger

    for i in range(1, max(1, attempts) + 1):
        try:
            result = structured_llm.invoke(messages)
        except Exception as exc:
            logger.warning(
                f"{node} 结构化输出调用异常（第 {i}/{attempts} 次）：{str(exc)[:160]}",
                extra={"node": node},
            )
            result = None
        if result is not None:
            return result
        if i < attempts:
            logger.warning(
                f"{node} 结构化输出返回 None，重试 {i}/{attempts}", extra={"node": node}
            )
    logger.error(f"{node} 结构化输出重试 {attempts} 次后仍失败", extra={"node": node})
    return None


def get_llm_with_tools(tools: list, *, temperature: float = 0.3) -> ChatOpenAI:
    """获取"绑定了工具"的 LLM，供 tool-calling 循环使用（Engineer/Reviewer 预热阶段）。
    让 LLM 能自主决定要不要调 search_memory / validate_coverage 等工具。

    qwen-max 通过 DashScope OpenAI 兼容接口支持 function calling，
    格式与 OpenAI 一致，bind_tools() 可直接使用。
    每次调用返回新的绑定实例（bind_tools 不可缓存，因为 tools 列表可变），
    但底层 ChatOpenAI 实例复用 get_llm() 的 lru_cache。
    """
    return get_llm(temperature).bind_tools(tools)
