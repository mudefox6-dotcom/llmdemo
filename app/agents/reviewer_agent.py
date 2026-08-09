"""Reviewer Agent: LLM review plus rule-based API/DB feature coverage checks."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from app.core.config import get_settings
from app.core.llm import get_llm_with_tools, get_structured_llm, invoke_structured
from app.core.logger import logger
from app.core.prompts import REVIEWER_SYSTEM_PROMPT, REVIEWER_USER_PROMPT
from app.graph.state import AgentState
from app.memory.short_term import add_entry, format_for_prompt
from app.memory.few_shot import build_few_shot_for_task
from app.memory.self_refine import reviewer_self_critique
from app.schemas.review import ReviewResult, ReviewTargetType
from app.tools.agent_tools import REVIEWER_TOOLS
from app.tools.coverage_metrics import (
    collect_feature_terms,
    missing_feature_mappings,
    score_api_feature_coverage,
    score_db_feature_coverage,
    score_prd_tech_coverage,
)


def reviewer_node(state: AgentState) -> dict:
    """Review PRD and technical design, then enforce coverage gates."""
    logger.info("Reviewer Agent started", extra={"node": "reviewer"})

    # 读入字段：
    #   prd_doc          —— 待评审的 PRD（Solution 产物）
    #   technical_design —— 待评审的技术设计（Engineer 产物）
    #   clarified_requirement —— 需求澄清（判断产物是否满足原始需求）
    #   metadata         —— 指标容器 + memory 开关
    prd_doc = state.get("prd_doc", {})
    technical_design = state.get("technical_design", {})
    clarified_req = state.get("clarified_requirement", {})
    metadata = state.get("metadata", {}) or {}

    memory_hits = []
    memory_context = ""
    if _memory_enabled(metadata):
        memory_context, memory_hits = _run_reviewer_tool_calling(
            prd_doc, technical_design, clarified_req
        )

    structured_llm = get_structured_llm(ReviewResult)
    task_memory_text = format_for_prompt(
        state.get("task_memory"),
        max_chars=get_settings().short_term_memory_max_chars,
    )
    few_shot_text = build_few_shot_for_task(prd_doc, clarified_req)
    messages = [
        SystemMessage(content=REVIEWER_SYSTEM_PROMPT),
        HumanMessage(
            content=REVIEWER_USER_PROMPT.format(
                prd_doc=json.dumps(prd_doc, ensure_ascii=False, indent=2),
                technical_design=json.dumps(technical_design, ensure_ascii=False, indent=2),
                clarified_requirement=json.dumps(clarified_req, ensure_ascii=False, indent=2),
                extra_context="",
                few_shot_examples=few_shot_text,
                task_memory=task_memory_text,
                memory_context=memory_context,
            )
        ),
    ]

    # 第一步：LLM 主观评审，得到一个"通过/不通过"结论 llm_passed
    # 带 None 重试；彻底失败时给一份中性评审，让规则闸门继续判定并交人工，
    # 而不是让任务崩在 .model_dump()。
    result: ReviewResult | None = invoke_structured(structured_llm, messages, node="reviewer")
    if result is None:
        logger.error("Reviewer 结构化输出持续失败，回退为中性评审交人工判断", extra={"node": "reviewer"})
        result = ReviewResult(
            overall_score=6.0, prd_score=6.0, tech_score=6.0, passed=False,
            summary="自动评审失败（模型未返回有效结构化结果），请人工复核交付产物。",
        )
    result_dict = result.model_dump()
    llm_passed = result_dict.get("passed", False)

    # 第二步：规则化覆盖率闸门。可能推翻 LLM 的乐观结论——
    # 若功能模块缺 API/DB 映射严重，强制 passed=False、reflow_target=engineer 并压分。
    result_dict = _apply_rule_based_adjustment(result_dict, prd_doc, technical_design)
    rule_passed = result_dict.get("passed", False)

    # 第三步 ReAct 冲突修正：若"LLM 说通过、规则说不通过"，把冲突点（+可选自我反思）
    # 反馈给 LLM 重评一次，让它的措辞与规则结论一致（最终仍以规则为准）。
    if llm_passed and not rule_passed:
        conflict_issues = [
            i for i in result_dict.get("issues", [])
            if i.get("severity") in ("high", "medium")
        ]
        conflict_lines = [
            "你的评审结论为'通过'，但以下覆盖率问题不满足交付标准，请重新评审并给出'未通过'结论："
        ]
        for issue in conflict_issues[:5]:
            conflict_lines.append(f"- [{issue.get('severity')}] {issue.get('description', '')}")
        conflict_lines.append("请基于以上问题重新输出完整的评审结论。")

        if get_settings().self_refine_enabled:
            critique = reviewer_self_critique(
                review_json=json.dumps(result_dict, ensure_ascii=False),
                prd_json=json.dumps(prd_doc, ensure_ascii=False),
                design_json=json.dumps(technical_design, ensure_ascii=False),
                conflict_issues=conflict_issues,
            )
            full_feedback = (
                "## 自我反思\n\n" + critique + "\n\n"
                "## 规则检测到的具体问题\n\n" + "\n".join(conflict_lines)
            )
        else:
            full_feedback = "\n".join(conflict_lines)

        messages.append(HumanMessage(content=full_feedback))
        # 同样带 None 重试；重评失败就保留第一轮经规则调整后的结论（规则本就是最终依据）
        result2 = invoke_structured(structured_llm, messages, node="reviewer(重评)", attempts=2)
        if result2 is not None:
            result_dict = _apply_rule_based_adjustment(
                result2.model_dump(), prd_doc, technical_design
            )

        logger.info(
            "Reviewer ReAct 第 2 轮（规则冲突修正）",
            extra={
                "node": "reviewer",
                "passed": result_dict.get("passed"),
                "overall_score": result_dict.get("overall_score"),
            },
        )

    # 未通过且指定了回流目标时，回流计数 +1（达到 max_reflow_count 后路由会转人工，防死循环）
    # reflow_count：已回流次数（读旧值）；reflow_target：本次评审建议回流到哪个节点(solution/engineer/none)
    # 未通过且有明确回流目标时 +1；达到 max_reflow_count 后路由会转人工，防死循环
    reflow_count = int(state.get("reflow_count", 0) or 0)
    reflow_target = _target_value(result_dict.get("reflow_target"))
    if not result_dict.get("passed", False) and reflow_target != "none":
        reflow_count += 1

    logger.info(
        "Reviewer Agent completed",
        extra={
            "node": "reviewer",
            "passed": result_dict.get("passed"),
            "overall_score": result_dict.get("overall_score"),
            "reflow_target": reflow_target,
            "reflow_count": reflow_count,
        },
    )

    passed = result_dict.get("passed", False)
    issues_count = len(result_dict.get("issues") or [])
    high_issues = sum(
        1 for i in (result_dict.get("issues") or [])
        if i.get("severity") in ("high", "critical")
    )
    warnings_list = (
        [f"发现 {high_issues} 个高严重性问题"] if high_issues else []
    )
    if reflow_target != "none" and not passed:
        warnings_list.append(f"建议回流到 {reflow_target}")

    task_mem = add_entry(
        state, "reviewer",
        summary=f"评审{'通过' if passed else '未通过'}，总分={result_dict.get('overall_score')}",
        key_decisions=[
            f"PRD评分={result_dict.get('prd_score')}",
            f"技术评分={result_dict.get('tech_score')}",
            f"回流目标={reflow_target}",
        ],
        metrics={
            "overall_score": result_dict.get("overall_score"),
            "passed": passed,
            "issues_count": issues_count,
            "reflow_count": reflow_count,
        },
        warnings=warnings_list,
    )

    # 本节点改动的字段：
    #   review_result (replace) —— 评审结论，含 passed / reflow_target，是流程分流的依据
    #   reflow_count (replace) —— 更新后的回流次数
    #   memory_hits (append) / metadata (replace) / task_memory(append) / current_node / execution_logs
    return {
        "review_result": result_dict,
        **task_mem,
        "reflow_count": reflow_count,
        "current_node": "reviewer",
        "memory_hits": memory_hits,
        "metadata": {
            **metadata,
            "review_memory_context_used": bool(memory_context),
            "self_refine_reviewer": int(
                llm_passed and not rule_passed and get_settings().self_refine_enabled
            ),
        },
        "execution_logs": [
            {
                "node": "reviewer",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "passed": result_dict.get("passed"),
                "overall_score": result_dict.get("overall_score"),
                "reflow_target": reflow_target,
                "memory_hit_count": len(memory_hits),
            }
        ],
    }


def _apply_rule_based_adjustment(
    review: dict[str, Any],
    prd_doc: dict[str, Any],
    technical_design: dict[str, Any],
) -> dict[str, Any]:
    api_coverage = score_api_feature_coverage(prd_doc, technical_design)
    db_coverage = score_db_feature_coverage(prd_doc, technical_design)
    prd_tech_coverage = score_prd_tech_coverage(prd_doc, technical_design)
    missing = missing_feature_mappings(prd_doc, technical_design)
    has_features = bool(collect_feature_terms(prd_doc or {}))

    issues = list(review.get("issues") or [])
    suggestions = list(review.get("suggestions") or [])
    severe_mapping_gap = has_features and (api_coverage < 0.5 or db_coverage < 0.5)

    if missing["api"]:
        issues.append(
            {
                "category": "completeness",
                "severity": "high" if severe_mapping_gap else "medium",
                "description": "PRD feature modules missing API mapping: "
                + ", ".join(missing["api"]),
                "suggestion": "Add domain-specific endpoints whose path or description explicitly covers these feature modules.",
                "target": ReviewTargetType.ENGINEER,
            }
        )
    if missing["db"]:
        issues.append(
            {
                "category": "completeness",
                "severity": "high" if severe_mapping_gap else "medium",
                "description": "PRD feature modules missing DB schema mapping: "
                + ", ".join(missing["db"]),
                "suggestion": "Add tables or columns whose names/descriptions explicitly persist these feature modules.",
                "target": ReviewTargetType.ENGINEER,
            }
        )

    if severe_mapping_gap:
        # 严重缺口（有功能却 API 或 DB 覆盖率 <0.5）：硬闸门直接判不通过，
        # 强制回流给 engineer，并把技术分/总分压到 6 左右——无论 LLM 打了多高分。
        suggestions.append(
            "Rework the technical design so every PRD feature has traceable service/API/DB coverage."
        )
        review["passed"] = False
        review["reflow_target"] = ReviewTargetType.ENGINEER
        review["tech_score"] = min(float(review.get("tech_score") or 0), 6.0)
        review["overall_score"] = min(float(review.get("overall_score") or 0), 6.5)
    elif missing["api"] or missing["db"]:
        feature_count = max(1, len(collect_feature_terms(prd_doc or {})))
        missing_ratio = (len(missing["api"]) + len(missing["db"])) / (feature_count * 2)
        coverage_gap = max(1.0 - api_coverage, 1.0 - db_coverage)
        penalty = max(0.5, min(1.8, coverage_gap * 2.0 + missing_ratio))
        review["tech_score"] = max(7.0, round(float(review.get("tech_score") or 0) - penalty, 2))
        review["overall_score"] = max(
            7.0,
            round(float(review.get("overall_score") or 0) - penalty, 2),
        )
    elif issues and prd_tech_coverage < 0.8:
        review["overall_score"] = min(float(review.get("overall_score") or 0), 7.0)

    review["issues"] = issues
    review["suggestions"] = suggestions
    summary = review.get("summary") or ""
    review["summary"] = (
        f"{summary}\nRule coverage: PRD-Tech={prd_tech_coverage:.2f}, "
        f"API-Feature={api_coverage:.2f}, DB-Feature={db_coverage:.2f}."
    ).strip()
    return review


def _memory_enabled(metadata: dict[str, Any]) -> bool:
    if "memory_enabled" in metadata:
        return bool(metadata.get("memory_enabled"))
    return bool(get_settings().memory_enabled)


def _run_reviewer_tool_calling(
    prd_doc: dict[str, Any],
    technical_design: dict[str, Any],
    clarified_req: dict[str, Any],
) -> tuple[str, list[dict]]:
    """Tool calling 预热阶段：让 Reviewer LLM 主动检索历史评审和架构模式。"""
    tool_map = {t.name: t for t in REVIEWER_TOOLS}
    llm_with_tools = get_llm_with_tools(REVIEWER_TOOLS)

    feature_names = ", ".join(
        str(m.get("name", "")) for m in (prd_doc.get("feature_modules") or [])
        if isinstance(m, dict)
    )
    prep_prompt = (
        "你是一名技术评审专家，即将评审以下产品的 PRD 和技术设计。\n"
        f"产品目标：{prd_doc.get('product_goal') or clarified_req.get('goal') or ''}\n"
        f"核心功能模块：{feature_names}\n"
        "请先检索相关历史评审记录或架构模式，为本次评审提供参考标准。"
        "如果不需要额外信息，直接回复'无需检索'即可。"
    )
    messages = [HumanMessage(content=prep_prompt)]
    context_parts: list[str] = []

    for _ in range(3):
        response = llm_with_tools.invoke(messages)
        messages.append(response)

        if not response.tool_calls:
            break

        for tc in response.tool_calls:
            tool_name = tc["name"]
            tool_id = tc["id"]
            if tool_name not in tool_map:
                result_content = f"未知工具：{tool_name}"
            else:
                try:
                    result_content = tool_map[tool_name].invoke(tc["args"])
                except Exception as exc:
                    result_content = f"工具调用失败：{exc}"

            messages.append(ToolMessage(content=result_content, tool_call_id=tool_id))
            if tool_name == "search_memory" and result_content != "未找到相关记忆。":
                context_parts.append(result_content)

    memory_context = "\n\n".join(context_parts)
    settings = get_settings()
    if len(memory_context) > settings.memory_context_max_chars:
        memory_context = memory_context[: settings.memory_context_max_chars].rstrip() + "\n...[truncated]"
    return memory_context, []


def _target_value(target: Any) -> str:
    return str(getattr(target, "value", target or "none"))
