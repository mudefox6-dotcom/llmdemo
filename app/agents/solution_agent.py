"""Solution Agent —— 生成产品方案、PRD 草稿与功能模块。

基于 Planner 输出的需求澄清结果，生成完整的 PRD 文档。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.llm import get_structured_llm, invoke_structured
from app.core.logger import logger
from app.core.config import get_settings
from app.core.prompts import SOLUTION_SYSTEM_PROMPT, SOLUTION_USER_PROMPT
from app.graph.state import AgentState
from app.memory.short_term import add_entry, format_for_prompt
from app.memory.few_shot import build_few_shot_for_task
from app.schemas.prd import PRDDocument


def solution_node(state: AgentState) -> dict:
    """Solution 节点：基于需求澄清结果生成 PRD 文档。"""
    logger.info("Solution Agent 开始执行", extra={"node": "solution"})

    # 读入字段：
    #   clarified_requirement —— Planner 产出的结构化需求（本节点的主输入，据它写 PRD）
    #   human_feedback        —— 用户反馈（澄清或审批驳回时的补充）
    #   review_result         —— 上一轮评审结论（仅回流时非空，用于按 target 取修订意见）
    clarified_req = state.get("clarified_requirement", {})
    human_feedback = state.get("human_feedback", "")
    review_result = state.get("review_result")

    # 组装附加上下文：用户反馈 + 回流时的评审意见。
    # 关键：从 review_result.issues 里 **只挑 target=="solution" 的问题**，
    # 即"该由方案层修的问题"，engineer 的问题不管——这就是回流时各改各的机制。
    extra_context = ""
    if human_feedback:
        extra_context += f"用户反馈：\n{human_feedback}\n\n"
    if review_result and not review_result.get("passed", False):
        suggestions = review_result.get("suggestions", [])
        issues = [
            i for i in review_result.get("issues", [])
            if i.get("target") == "solution"
        ]
        if suggestions or issues:
            extra_context += "评审修订建议：\n"
            for s in suggestions:
                extra_context += f"- {s}\n"
            for issue in issues:
                extra_context += f"- [{issue.get('severity')}] {issue.get('description')}: {issue.get('suggestion')}\n"

    # structured_llm：绑定 PRDDocument Schema 的 LLM，temperature=0.5（略高，方案需一点发散）
    structured_llm = get_structured_llm(PRDDocument, temperature=0.5)

    # task_memory_text：把黑板上的 task_memory（各节点摘要）压成一段文本注入 prompt，
    #                   让 Solution 知道之前发生了什么（尤其回流时）
    task_memory_text = format_for_prompt(
        state.get("task_memory"),
        max_chars=get_settings().short_term_memory_max_chars,
    )

    # few_shot_text：从 ChromaDB 检索的历史高分案例文本，作为写 PRD 的参考范例
    few_shot_text = build_few_shot_for_task(
        prd_doc={}, clarified_req=clarified_req
    )

    # 演示模式：限制功能模块数量。实测这是整条流程耗时的支配因素——
    # 模块数超过 engineer_batch_size 时 Engineer 会转入分批生成，
    # 同一需求下 Engineer 耗时从 110s 涨到 263s。
    settings = get_settings()
    system_prompt = SOLUTION_SYSTEM_PROMPT
    if settings.demo_mode:
        cap = settings.demo_max_feature_modules
        system_prompt += (
            f"\n\n【演示模式约束】feature_modules 最多输出 {cap} 个，"
            "只保留最能代表该产品核心价值的模块；其余想法合并进这些模块的 sub_features，"
            "或直接省略。用户故事、用户流程同样从简。"
        )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(
            content=SOLUTION_USER_PROMPT.format(
                clarified_requirement=json.dumps(clarified_req, ensure_ascii=False, indent=2),
                extra_context=extra_context,
                few_shot_examples=few_shot_text,
                task_memory=task_memory_text,
            )
        ),
    ]

    # 带 None 重试，避免一次模型抖动就让任务崩在 .model_dump()
    result: PRDDocument | None = invoke_structured(structured_llm, messages, node="solution")
    if result is None:
        # 兜底：用需求澄清结果拼一份最小 PRD，让流程能继续到技术设计与评审，
        # 评审会如实反映质量问题，而不是整个任务失败。
        logger.error("Solution 结构化输出持续失败，回退为最小 PRD", extra={"node": "solution"})
        goal = str(clarified_req.get("goal") or "").strip()
        result = PRDDocument(
            product_name=(goal[:60] or "未命名产品"),
            # positioning 是必填字段，兜底时也必须给值，否则 Pydantic 校验直接抛错
            positioning=goal or "（自动方案生成失败，此为最小占位 PRD，请人工补充产品定位）",
            feature_modules=[
                {"name": str(f), "description": str(f)}
                for f in (clarified_req.get("core_features") or [])[:5]
            ],
        )
    result_dict = result.model_dump()

    logger.info(
        "Solution Agent 执行完成",
        extra={
            "node": "solution",
            "feature_count": len(result.feature_modules),
        },
    )

    decisions = [
        f"产品定位={result.positioning[:60]}" if result.positioning else "",
        f"优先级最高的功能={result.feature_modules[0].name}" if result.feature_modules else "",
    ]
    decisions = [d for d in decisions if d]
    task_mem = add_entry(
        state, "solution",
        summary=f"产出了 {len(result.feature_modules)} 个功能模块、{len(result.user_stories)} 个用户故事",
        key_decisions=decisions,
        metrics={
            "feature_count": len(result.feature_modules),
            "user_stories": len(result.user_stories),
            "user_flows": len(result.user_flows or []),
            "success_metrics": len(result.success_metrics or []),
        },
    )

    # 本节点改动的字段：
    #   prd_doc (replace) —— 整份 PRD，engineer 的输入源
    #   current_node / task_memory(append) / execution_logs(append)
    return {
        "prd_doc": result_dict,
        "current_node": "solution",
        **task_mem,
        "execution_logs": [
            {
                "node": "solution",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "feature_count": len(result.feature_modules),
            }
        ],
    }
