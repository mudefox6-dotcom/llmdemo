"""Orchestrator Agent —— 输入规范化、人工交互节点、打包输出。

包含不需要独立 LLM 调用的流程控制节点：
- input_normalize: 输入规范化
- human_clarification: 人工澄清（使用 interrupt）
- human_approval: 人工审批（使用 interrupt）
- package_output: 最终交付包打包
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage
from langgraph.types import interrupt

from app.core.config import get_settings
from app.core.llm import get_llm
from app.core.logger import logger
from app.core.prompts import INPUT_NORMALIZE_PROMPT
from app.graph.state import AgentState
from app.storage.repo_memory import save_memory_sync
from app.memory.consolidation import (
    detect_conflict,
    increment_task_counter,
    should_consolidate,
    consolidate_memories,
)


def input_normalize_node(state: AgentState) -> dict:
    """输入规范化节点：清理和规范化用户输入。"""
    logger.info("Input Normalize 开始执行", extra={"node": "input_normalize"})

    raw_input = state.get("user_input", "")
    if not raw_input.strip():
        return {
            "normalized_input": "",
            "current_node": "input_normalize",
            "error_message": "用户输入为空",
        }

    # 演示模式：跳过这次 LLM 调用直接用原文。规范化只是把口语化输入理顺，
    # 而演示用的模板需求本来就是规整文本，省下的是一整轮调用（实测约 10 秒）。
    if get_settings().demo_mode:
        logger.info("演示模式：跳过输入规范化的 LLM 调用", extra={"node": "input_normalize"})
        normalized = raw_input.strip()
    else:
        llm = get_llm(temperature=0.1)
        messages = [
            HumanMessage(
                content=INPUT_NORMALIZE_PROMPT.format(raw_input=raw_input)
            ),
        ]
        response = llm.invoke(messages)
        normalized = response.content.strip()

    logger.info(
        "Input Normalize 执行完成",
        extra={"node": "input_normalize"},
    )

    return {
        "normalized_input": normalized,
        "current_node": "input_normalize",
        "execution_logs": [
            {
                "node": "input_normalize",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }


def human_clarification_node(state: AgentState) -> dict:
    """人工澄清节点：通过 interrupt 暂停等待用户补充信息。"""
    logger.info("Human Clarification 等待用户输入", extra={"node": "human_clarification"})

    clarified_req = state.get("clarified_requirement", {})
    open_questions = clarified_req.get("open_questions", [])

    # 用 interrupt 冻结整张图并把待澄清问题抛给外部（前端）。
    # 第一次执行到这里会抛 GraphInterrupt、图暂停、任务状态转 waiting_human；
    # 用户提交反馈后 worker 用 Command(resume=反馈文本) 恢复，这行才"返回"该文本。
    human_input = interrupt({
        "type": "clarification",
        "message": "需求存在待澄清的问题，请补充信息：",
        "open_questions": open_questions,
    })

    logger.info(
        "Human Clarification 收到用户反馈",
        extra={"node": "human_clarification"},
    )

    return {
        "human_feedback": human_input if isinstance(human_input, str) else json.dumps(human_input, ensure_ascii=False),
        "needs_human_clarification": False,
        "human_clarification_confirmed": True,
        "current_node": "human_clarification",
        "execution_logs": [
            {
                "node": "human_clarification",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "received_feedback",
            }
        ],
    }


def human_approval_node(state: AgentState) -> dict:
    """人工审批节点：通过 interrupt 暂停等待人工审批。"""
    logger.info("Human Approval 等待人工审批", extra={"node": "human_approval"})

    # 只读 review_result：审批卡片展示评审摘要、总分与是否通过。
    # 产物本身不在这里读——前端会另外从 /tasks/{id} 拿完整的 prd_doc/technical_design。
    review_result = state.get("review_result", {})

    # 用 interrupt 暂停等人工审批；恢复时 Command(resume=...) 的值成为 approval。
    approval = interrupt({
        "type": "approval",
        "message": "请审批以下交付产物：",
        "review_summary": review_result.get("summary", ""),
        "overall_score": review_result.get("overall_score", 0),
        "passed": review_result.get("passed", False),
    })

    # 反馈可能有三种形态，都归一成 approved(bool) + feedback(str)：
    #   dict  {"approved":True,"feedback":"..."}（正规路径，submit_feedback 构造）
    #   bool  直接 True/False
    #   str   文本，按关键词判是否批准
    approved = False
    feedback = ""
    if isinstance(approval, dict):
        approved = approval.get("approved", False)
        feedback = approval.get("feedback", "")
    elif isinstance(approval, bool):
        approved = approval
    elif isinstance(approval, str):
        approved = approval.lower() in ("yes", "true", "approve", "approved", "通过", "批准")
        feedback = approval

    logger.info(
        "Human Approval 收到审批结果",
        extra={"node": "human_approval", "approved": approved},
    )

    return {
        "human_approved": approved,
        "human_feedback": feedback,
        "current_node": "human_approval",
        "execution_logs": [
            {
                "node": "human_approval",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "approved": approved,
            }
        ],
    }


def package_output_node(state: AgentState) -> dict:
    """打包输出节点：汇总所有产物生成最终交付包。"""
    logger.info("Package Output 开始打包", extra={"node": "package_output"})

    # final_output：把黑板上五大产物 + 元信息汇总成"最终交付包"。
    # 各字段来源：clarified_requirement(planner)、prd_doc(solution)、technical_design/code_scaffold(engineer)、
    #             review_result(reviewer)；metadata 记 task_id/回流次数/生成时间。
    final_output = {
        "clarified_requirement": state.get("clarified_requirement", {}),
        "prd_doc": state.get("prd_doc", {}),
        "technical_design": state.get("technical_design", {}),
        "code_scaffold": state.get("code_scaffold", {}),
        "review_result": state.get("review_result", {}),
        "metadata": {
            "task_id": state.get("task_id", ""),
            "reflow_count": state.get("reflow_count", 0),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    logger.info(
        "Package Output 打包完成",
        extra={"node": "package_output"},
    )

    _save_task_to_memory(state)

    return {
        "current_node": "package_output",
        "next_action": "completed",
        "metadata": final_output,
        "execution_logs": [
            {
                "node": "package_output",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "packaged",
            }
        ],
    }


def _save_task_to_memory(state: AgentState) -> None:
    """任务完成后将高质量产物写入 memory，供后续任务检索复用。

    写入前进行冲突检测（detect_conflict），写入后检查是否需要触发
    记忆巩固（consolidation）。
    """
    review = state.get("review_result") or {}
    # 质量门槛：只有"评审通过 且 总分>=7"的成果才值得沉淀为长期记忆，
    # 供以后相似需求做 few-shot 参考；不达标直接返回，不写记忆。
    if not review.get("passed", False) or (review.get("overall_score") or 0) < 7:
        return

    task_id = state.get("task_id", "")
    prd = state.get("prd_doc") or {}
    design = state.get("technical_design") or {}
    clarified_req = state.get("clarified_requirement") or {}
    domain = str((state.get("metadata") or {}).get("domain") or prd.get("domain") or "")
    product_name = str(prd.get("product_name") or "")
    tags = ",".join(str(f) for f in (clarified_req.get("core_features") or [])[:5])

    # 1. 写入 case：结构化摘要，供 Engineer 检索相似案例
    api_endpoints = design.get("api_endpoints") or []
    tables = (design.get("db_schema") or {}).get("tables") or []
    feature_modules = prd.get("feature_modules") or []

    case_summary_parts = [
        f"产品：{product_name}",
        f"领域：{domain}",
        f"目标：{prd.get('product_goal') or clarified_req.get('goal') or ''}",
        f"核心功能：{', '.join(str(f.get('name', '')) for f in feature_modules[:6] if isinstance(f, dict))}",
        f"API 端点数：{len(api_endpoints)}，示例：{', '.join(str(e.get('path', '')) for e in api_endpoints[:5])}",
        f"DB 表数：{len(tables)}，示例：{', '.join(str(t.get('table_name', '')) for t in tables[:5])}",
        f"评审评分：{review.get('overall_score')}",
    ]
    case_summary = "\n".join(line for line in case_summary_parts if line.split("：", 1)[-1].strip())

    _checked_save(
        content=case_summary,
        metadata={
            "memory_type": "case",
            "task_id": task_id,
            "domain": domain,
            "product_name": product_name,
            "overall_score": review.get("overall_score"),
            "source": "runtime",
            "tags": tags,
        },
        memory_id=f"case_{task_id}",
    )

    # 2. 写入 architecture_pattern：架构关键决策摘要
    arch_decisions = design.get("architecture_decisions") or design.get("tech_stack") or {}
    if arch_decisions:
        arch_lines = []
        for k, v in arch_decisions.items():
            v_str = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            arch_lines.append(f"{k}: {v_str[:120]}")
        arch_summary = f"产品：{product_name}，领域：{domain}\n" + "\n".join(arch_lines[:10])
        _checked_save(
            content=arch_summary,
            metadata={
                "memory_type": "architecture_pattern",
                "task_id": task_id,
                "domain": domain,
                "product_name": product_name,
                "source": "runtime",
                "tags": tags,
            },
            memory_id=f"arch_{task_id}",
        )

    # 3. 写入 review：评审摘要
    review_summary_parts = [
        f"产品：{product_name}，领域：{domain}",
        f"评分：{review.get('overall_score')}，通过：{review.get('passed')}",
        f"总结：{str(review.get('summary') or '')[:200]}",
    ]
    issues = review.get("issues") or []
    if issues:
        issue_lines = [
            f"- [{i.get('severity')}] {i.get('description', '')[:80]}"
            for i in issues[:5]
        ]
        review_summary_parts.append("主要问题：\n" + "\n".join(issue_lines))
    suggestions = review.get("suggestions") or []
    if suggestions:
        review_summary_parts.append(
            "建议：" + "；".join(str(s)[:60] for s in suggestions[:4])
        )

    _checked_save(
        content="\n".join(review_summary_parts),
        metadata={
            "memory_type": "review",
            "task_id": task_id,
            "domain": domain,
            "product_name": product_name,
            "overall_score": review.get("overall_score"),
            "source": "runtime",
            "tags": tags,
        },
        memory_id=f"review_{task_id}",
    )

    # 4. 检查是否需要触发记忆巩固
    _maybe_consolidate(domain)

    logger.info(
        "Memory 写入完成",
        extra={"node": "package_output", "task_id": task_id, "domain": domain},
    )


def _checked_save(
    content: str,
    metadata: dict,
    memory_id: str,
) -> None:
    """写入 memory，写入前进行冲突检测。"""
    conflict = detect_conflict(content, metadata)
    if conflict and conflict.get("verdict") == "confirms":
        logger.info(
            "Memory write skipped (confirms existing)",
            extra={"memory_id": memory_id, "reason": conflict.get("reason", "")},
        )
        return
    if conflict:
        metadata["conflict_verdict"] = conflict.get("verdict", "")
        metadata["conflict_reason"] = conflict.get("reason", "")[:200]
        if conflict.get("conflict_point"):
            metadata["conflict_point"] = conflict["conflict_point"][:200]
    save_memory_sync(content=content, metadata=metadata, memory_id=memory_id)


def _maybe_consolidate(domain: str) -> None:
    """任务计数 + 条件触发记忆巩固。"""
    count = increment_task_counter()
    if should_consolidate():
        logger.info(
            "触发记忆巩固",
            extra={"node": "package_output", "task_count": count, "domain": domain},
        )
        result = consolidate_memories(domain=domain or None)
        logger.info(
            "记忆巩固结果",
            extra={"node": "package_output", "result": result},
        )
