"""Planner Agent —— 需求澄清、任务拆解、识别缺失信息。

适配 langchain >= 1.2 / langchain-openai >= 1.1 的 with_structured_output API。
节点函数接收 AgentState (TypedDict) 并返回 dict 更新。
"""

from __future__ import annotations

from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.llm import get_structured_llm, invoke_structured
from app.core.logger import logger
from app.core.prompts import PLANNER_SYSTEM_PROMPT, PLANNER_USER_PROMPT
from app.graph.state import AgentState
from app.memory.short_term import add_entry
from app.schemas.requirement import ClarifiedRequirement


def planner_node(state: AgentState) -> dict:
    """Planner 节点：分析用户需求并输出结构化的需求澄清结果。"""
    logger.info("Planner Agent 开始执行", extra={"node": "planner"})

    # 读入字段：
    #   normalized_input —— input_normalize 规范化后的需求（优先用）；没有则退回原始 user_input
    #   human_feedback   —— 用户在澄清环节补充的信息（首次执行为空；澄清回流后才有值）
    user_input = state.get("normalized_input") or state.get("user_input", "")
    human_feedback = state.get("human_feedback", "")

    # extra_context：拼进 prompt 的"用户补充说明"，只有澄清回流时才非空
    extra_context = ""
    if human_feedback:
        extra_context = f"用户补充说明：\n{human_feedback}"

    # 使用 with_structured_output 绑定 Pydantic Schema
    structured_llm = get_structured_llm(ClarifiedRequirement)

    messages = [
        SystemMessage(content=PLANNER_SYSTEM_PROMPT),
        HumanMessage(
            content=PLANNER_USER_PROMPT.format(
                user_input=user_input,
                extra_context=extra_context,
            )
        ),
    ]

    # 带 None 重试：模型偶发不产出有效工具调用时会返回 None，直接 .model_dump() 会崩
    result: ClarifiedRequirement | None = invoke_structured(structured_llm, messages, node="planner")
    if result is None:
        # 兜底：给一个最小可用的澄清结果，并强制转人工澄清补齐信息，
        # 而不是让整个任务因一次模型抖动而失败。
        logger.error("Planner 结构化输出持续失败，回退为待澄清状态", extra={"node": "planner"})
        result = ClarifiedRequirement(
            goal=user_input[:200] or "（未能解析出目标）",
            open_questions=["自动需求分析失败，请补充说明核心功能、使用对象与主要约束。"],
        )
    result_dict = result.model_dump()

    # 是否需要人工澄清：正常取 Schema 的 needs_clarification 属性
    # （规则：open_questions>3 或 core_features 为空，见 ClarifiedRequirement）。
    # 但若上一轮已人工澄清过（human_clarification_confirmed=True），则强制不再追问，
    # 否则会「补充→仍判缺→再问」形成死循环。
    if state.get("human_clarification_confirmed", False):
        needs_clarification = False
    else:
        needs_clarification = result.needs_clarification

    logger.info(
        "Planner Agent 执行完成",
        extra={
            "node": "planner",
            "needs_clarification": needs_clarification,
            "open_questions_count": len(result.open_questions),
        },
    )

    # 下面几个变量都是为"写短期记忆(task_memory)"做准备的：
    #   core_features —— LLM 识别出的核心功能列表（可能是 str 或 dict 两种形态，都兼容）
    #   feature_names —— 前5个功能名（纯展示/摘要用）
    core_features = result_dict.get("core_features") or []
    feature_names = [f for f in (core_features[:5]) if isinstance(f, str)]
    feature_names += [
        f.get("name", "") for f in core_features[:5] if isinstance(f, dict)
    ]

    #   decisions —— 本轮"关键决策"（优先级、前3条假设），写进短期记忆供回流时回顾
    decisions = [f"优先级={result_dict.get('priority', 'medium')}"]
    if result_dict.get("assumptions"):
        decisions.append(f"假设={', '.join(str(a)[:50] for a in result_dict['assumptions'][:3])}")

    #   metrics —— 本轮量化指标：待澄清问题数、核心功能数、是否需澄清
    metrics = {
        "open_questions": len(result.open_questions),
        "core_features": len(core_features),
        "needs_clarification": needs_clarification,
    }

    task_mem = add_entry(
        state, "planner",
        summary=f"识别了 {len(core_features)} 个核心功能{'，需人工澄清' if needs_clarification else ''}",
        key_decisions=decisions,
        metrics=metrics,
        warnings=(
            [f"待澄清问题：{', '.join(str(q)[:80] for q in result.open_questions[:4])}"]
            if needs_clarification and result.open_questions else []
        ),
    )

    # 本节点改动的字段：
    #   clarified_requirement (replace) —— 结构化需求，下游 solution/engineer 的输入源
    #   needs_human_clarification (replace) —— 澄清开关，route_after_planner 读它
    #   current_node (replace) —— 标记走到了 planner
    #   task_memory (append，来自 **task_mem) —— 追加一条短期记忆摘要
    #   execution_logs (append) —— 追加一条执行日志
    return {
        "clarified_requirement": result_dict,
        "needs_human_clarification": needs_clarification,
        "current_node": "planner",
        **task_mem,
        "execution_logs": [
            {
                "node": "planner",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "needs_clarification": needs_clarification,
            }
        ],
    }
