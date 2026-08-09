"""Engineer Agent —— 生成技术设计、DB Schema、API 与代码骨架。

基于 PRD 文档和需求澄清结果，产出完整的技术设计方案。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from app.core.llm import get_llm_with_tools, get_structured_llm
from app.core.logger import logger
from app.core.config import get_settings
from app.core.prompts import ENGINEER_SYSTEM_PROMPT, ENGINEER_USER_PROMPT
from app.graph.state import AgentState
from app.memory.short_term import add_entry, format_for_prompt
from app.memory.few_shot import build_few_shot_for_task
from app.memory.self_refine import build_refined_feedback, engineer_self_critique
from app.agents.engineer_batch import generate_design_in_batches
from app.schemas.design import TechnicalDesign
from app.tools.agent_tools import ENGINEER_PREP_TOOLS
from app.storage.repo_memory import search_memory_sync
from app.tools.coverage_metrics import (
    missing_feature_mappings,
    repair_feature_mappings,
    score_api_feature_coverage,
    score_db_feature_coverage,
    score_prd_tech_coverage,
)


def engineer_node(state: AgentState) -> dict:
    """Engineer 节点：基于 PRD 生成技术设计方案。"""
    logger.info("Engineer Agent 开始执行", extra={"node": "engineer"})

    # 读入字段：
    #   prd_doc              —— Solution 产出的 PRD（主输入，据它设计架构/API/DB）
    #   clarified_requirement —— 需求澄清（辅助上下文）
    #   review_result        —— 上轮评审（仅回流时非空，取 target==engineer 的意见）
    #   metadata             —— 指标容器 + memory 开关（是否启用 RAG 检索）
    prd_doc = state.get("prd_doc", {})
    clarified_req = state.get("clarified_requirement", {})
    review_result = state.get("review_result")
    metadata = state.get("metadata", {}) or {}

    extra_context = ""
    if review_result and not review_result.get("passed", False):
        issues = [
            i for i in review_result.get("issues", [])
            if i.get("target") == "engineer"
        ]
        suggestions = review_result.get("suggestions", [])
        if suggestions or issues:
            extra_context += "评审修订建议：\n"
            for s in suggestions:
                extra_context += f"- {s}\n"
            for issue in issues:
                extra_context += f"- [{issue.get('severity')}] {issue.get('description')}: {issue.get('suggestion')}\n"

    # Tool calling 预热阶段：让 LLM 主动决定是否检索 memory，而非每次强制注入
    # memory_enabled 时才启用，否则跳过，memory_context 保持空字符串
    memory_hits: list[dict] = []
    memory_context = ""
    if _memory_enabled(metadata):
        memory_context, memory_hits = _run_tool_calling_phase(
            prd_doc, clarified_req, extra_context
        )

    structured_llm = get_structured_llm(TechnicalDesign)

    task_memory_text = format_for_prompt(
        state.get("task_memory"),
        max_chars=get_settings().short_term_memory_max_chars,
    )

    few_shot_text = build_few_shot_for_task(prd_doc, clarified_req)

    messages = [
        SystemMessage(content=ENGINEER_SYSTEM_PROMPT),
        HumanMessage(
            content=ENGINEER_USER_PROMPT.format(
                prd_doc=json.dumps(prd_doc, ensure_ascii=False, indent=2),
                clarified_requirement=json.dumps(clarified_req, ensure_ascii=False, indent=2),
                extra_context=extra_context,
                few_shot_examples=few_shot_text,
                task_memory=task_memory_text,
                memory_context=memory_context,
            )
        ),
    ]

    _COVERAGE_THRESHOLD = 0.8
    _MAX_REACT_ITERATIONS = 3
    result_dict: dict = {}
    react_iterations = 0
    batch_stats: dict = {}

    # ── 分批生成路径 ──────────────────────────────────────────
    # 功能模块较多时，一次性生成整份设计会撞模型输出上限（截断→返回 None→重试/降级）。
    # 改为"先出架构骨架，再按模块分批出 API/DB"，每次调用都小而稳，覆盖率也更高。
    # 模块数不超过 batch_size 时不分批——那种规模单次生成更省一次调用。
    settings = get_settings()
    module_count = len([m for m in (prd_doc.get("feature_modules") or []) if isinstance(m, dict)])
    if settings.engineer_batch_enabled and module_count > settings.engineer_batch_size:
        logger.info(
            f"Engineer 采用分批生成（{module_count} 个功能模块，每批 {settings.engineer_batch_size} 个）",
            extra={"node": "engineer"},
        )
        result_dict, batch_stats = generate_design_in_batches(
            prd_doc,
            clarified_req,
            extra_context=extra_context,
            few_shot_text=few_shot_text,
            task_memory_text=task_memory_text,
            memory_context=memory_context,
            batch_size=settings.engineer_batch_size,
        )
        api_cov = score_api_feature_coverage(prd_doc, result_dict)
        db_cov = score_db_feature_coverage(prd_doc, result_dict)
        logger.info(
            "Engineer 分批生成完成",
            extra={
                "node": "engineer",
                "llm_calls": batch_stats.get("llm_calls"),
                "batches": batch_stats.get("batches"),
                "failed_batches": batch_stats.get("failed_batches"),
                "api_coverage": round(api_cov, 3),
                "db_coverage": round(db_cov, 3),
            },
        )
        return _finalize(
            state, prd_doc, result_dict, metadata, memory_hits, memory_context,
            react_iterations=0, batch_stats=batch_stats,
        )

    # ── 单次生成 + ReAct 循环（模块少时走这条，或分批被关闭）──────
    # 每轮先让 LLM 产出一版完整技术设计，再用规则算"覆盖率"——
    # 覆盖率 = PRD 的每个功能模块，在技术设计里是否有对应 API 端点 / DB 表。
    # 两个覆盖率都 >=0.8 就退出；否则把"缺哪些模块"反馈给 LLM 要求补齐后重出。
    for react_iterations in range(1, _MAX_REACT_ITERATIONS + 1):
        # 复杂设计可能因模型单次输出上限(如 DeepSeek 8192 token)被截断，导致结构化解析
        # 拿不到完整工具调用而返回 None。模型有随机性，对 None 重试几次通常能落在上限内。
        result: TechnicalDesign | None = None
        for _attempt in range(1, 4):
            result = structured_llm.invoke(messages)  # 生成一版技术设计
            if result is not None:
                break
            logger.warning(
                f"Engineer 结构化输出返回 None（可能被截断），重试 {_attempt}/3",
                extra={"node": "engineer"},
            )
        if result is None:
            # 3 次仍 None：多半是本次设计太大、超出模型单次输出上限被截断（非随机抖动）。
            # 先追加"极简、省略代码骨架"指令再试一次，尽量保住 LLM 生成的质量。
            logger.warning(
                "Engineer 结构化输出持续为 None，改用精简请求（省略代码骨架）再试一次",
                extra={"node": "engineer"},
            )
            reduced_messages = messages + [
                HumanMessage(content=(
                    "上次输出因过长被截断，请重新输出并务必在长度预算内完成：\n"
                    "- code_scaffold 只填 dependencies 和最多 3 条 directory_structure，key_files 留空；\n"
                    "- services/tech_risks 各最多 4 条，描述从简；\n"
                    "- 最优先保证每个功能模块都有对应的 API 端点和数据库表。"
                ))
            ]
            result = structured_llm.invoke(reduced_messages)
        if result is None:
            # 精简版仍失败：回退到"规则生成的最小技术设计"，保证整条流程不因单节点截断而中断。
            logger.error(
                "Engineer 仍返回 None，回退到规则生成的最小技术设计以保证流程继续",
                extra={"node": "engineer"},
            )
            result_dict = _build_fallback_design(prd_doc)
            break
        result_dict = result.model_dump()

        # api_coverage / db_coverage：本版设计对 PRD 功能模块的覆盖率(0~1)。
        # 含义：每个功能模块是否都有对应的 API 端点 / 数据库表。>=0.8 视为达标。
        api_coverage = score_api_feature_coverage(prd_doc, result_dict)
        db_coverage = score_db_feature_coverage(prd_doc, result_dict)

        logger.info(
            f"Engineer ReAct 第 {react_iterations} 轮",
            extra={
                "node": "engineer",
                "api_coverage": round(api_coverage, 3),
                "db_coverage": round(db_coverage, 3),
            },
        )

        if api_coverage >= _COVERAGE_THRESHOLD and db_coverage >= _COVERAGE_THRESHOLD:
            break  # 覆盖率达标（API 和 DB 都 >=0.8），退出循环，本版即最终版

        if react_iterations == _MAX_REACT_ITERATIONS:
            break  # 已是最后一轮，不再追加反馈（后面交给规则兜底补齐）

        # missing：不达标时算出的缺口清单 {"api":[缺API的模块名...], "db":[缺DB的模块名...]}
        # coverage_info：给 LLM 看的覆盖率现状文字
        missing = missing_feature_mappings(prd_doc, result_dict)
        coverage_info = (
            f"当前 API 覆盖率 {api_coverage:.0%}，DB 覆盖率 {db_coverage:.0%}，未达到 {_COVERAGE_THRESHOLD:.0%} 要求。"
        )

        # A self-critique adds a second model call. Only use it when the
        # coverage analysis found an actionable API or database gap.
        if get_settings().self_refine_enabled and (missing["api"] or missing["db"]):
            critique = engineer_self_critique(
                design_json=json.dumps(result_dict, ensure_ascii=False),
                prd_json=json.dumps(prd_doc, ensure_ascii=False),
                missing_api=missing["api"],
                missing_db=missing["db"],
                few_shot_text=few_shot_text,
            )
            missing_lines = []
            if missing["api"]:
                missing_lines.append(f"缺少 API 的模块：{', '.join(missing['api'])}")
            if missing["db"]:
                missing_lines.append(f"缺少 DB 表的模块：{', '.join(missing['db'])}")
            feedback = build_refined_feedback(
                self_critique=critique,
                coverage_info=coverage_info,
                missing_modules="\n".join(missing_lines),
            )
        else:
            feedback_lines = [
                coverage_info,
                "请补充以下功能模块的 API 端点和数据库表设计：",
            ]
            if missing["api"]:
                feedback_lines.append(f"缺少 API 的模块：{', '.join(missing['api'])}")
            if missing["db"]:
                feedback_lines.append(f"缺少 DB 表的模块：{', '.join(missing['db'])}")
            feedback_lines.append("请在原有设计基础上补充，保持其他内容不变，重新输出完整的技术设计。")
            feedback = "\n".join(feedback_lines)

        messages.append(HumanMessage(content=feedback))

    return _finalize(
        state, prd_doc, result_dict, metadata, memory_hits, memory_context,
        react_iterations=react_iterations, batch_stats=batch_stats,
    )


def _finalize(
    state: AgentState,
    prd_doc: dict,
    result_dict: dict,
    metadata: dict,
    memory_hits: list[dict],
    memory_context: str,
    *,
    react_iterations: int,
    batch_stats: dict,
) -> dict:
    """两条生成路径（分批 / 单次 ReAct）共用的收尾：覆盖率兜底 + 指标 + 状态更新。

    先记录 LLM 自己达到的"原始覆盖率"（raw_*，用于观测真实能力），
    再用规则做确定性兜底修补——把仍缺的模块用模板化 API/DB 直接补上，保证可追溯。
    """
    raw_prd_tech_coverage = score_prd_tech_coverage(prd_doc, result_dict)
    raw_api_coverage = score_api_feature_coverage(prd_doc, result_dict)
    raw_db_coverage = score_db_feature_coverage(prd_doc, result_dict)
    result_dict, coverage_repair = repair_feature_mappings(prd_doc, result_dict)  # 规则兜底
    # 用 result_dict 取代 result（兜底降级时 result 为 None，不能再点 .code_scaffold）
    code_scaffold = result_dict.get("code_scaffold") or {}
    api_count = len(result_dict.get("api_endpoints") or [])
    table_count = len((result_dict.get("db_schema") or {}).get("tables") or [])

    logger.info(
        "Engineer Agent 执行完成",
        extra={
            "node": "engineer",
            "api_count": api_count,
            "table_count": table_count,
            "react_iterations": react_iterations,
            "batches": batch_stats.get("batches", 0),
            "coverage_repair_api_count": len(coverage_repair["api"]),
            "coverage_repair_db_count": len(coverage_repair["db"]),
        },
    )

    warnings_list: list[str] = []
    if missing := coverage_repair.get("api") or []:
        warnings_list.append(f"修复了 {len(missing)} 个 API 覆盖率缺口")
    if missing := coverage_repair.get("db") or []:
        warnings_list.append(f"修复了 {len(missing)} 个 DB 覆盖率缺口")
    if react_iterations > 1:
        warnings_list.append(f"经过 {react_iterations} 轮 ReAct 才达标")
    if batch_stats.get("failed_batches"):
        warnings_list.append(f"{batch_stats['failed_batches']} 个批次生成失败，已由规则兜底")

    mode = "分批" if batch_stats else "单次"
    task_mem = add_entry(
        state, "engineer",
        summary=f"{mode}生成：{api_count} 个 API 端点、{table_count} 张 DB 表",
        key_decisions=[
            f"架构风格={result_dict.get('architecture_style', '')}",
            f"服务数={len(result_dict.get('services') or [])}",
        ],
        metrics={
            "api_count": api_count,
            "table_count": table_count,
            "react_iterations": react_iterations,
            "batches": batch_stats.get("batches", 0),
            "api_coverage": round(raw_api_coverage, 3),
            "db_coverage": round(raw_db_coverage, 3),
        },
        warnings=warnings_list,
    )

    # 本节点改动的字段：
    #   technical_design (replace) —— 整份技术设计，reviewer 的评审对象
    #   code_scaffold (replace) —— 从技术设计里抽出的代码骨架
    #   memory_hits (append) —— 本轮 RAG 命中的历史记忆（供观测/统计）
    #   metadata (replace) —— 覆盖率等大量指标（raw_*_coverage、react_iterations、coverage_repair…）
    #   task_memory(append) / current_node / execution_logs(append)
    return {
        "technical_design": result_dict,
        **task_mem,
        "code_scaffold": code_scaffold,
        "current_node": "engineer",
        "execution_logs": [
            {
                "node": "engineer",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "api_count": api_count,
                "table_count": table_count,
                "memory_hit_count": len(memory_hits),
                "coverage_repair": coverage_repair,
                "generation_mode": mode,
            }
        ],
        "memory_hits": memory_hits,
        "metadata": {
            **metadata,
            "memory_context_used": bool(memory_context),
            "memory_hit_count": len(memory_hits),
            "react_iterations": react_iterations,
            "engineer_generation_mode": mode,
            "engineer_batch_stats": batch_stats,
            "raw_prd_tech_coverage_rate": raw_prd_tech_coverage,
            "raw_api_feature_coverage_rate": raw_api_coverage,
            "raw_db_feature_coverage_rate": raw_db_coverage,
            "coverage_repair_applied": bool(coverage_repair["api"] or coverage_repair["db"]),
            "coverage_repair": coverage_repair,
            "self_refine_engineer": int(react_iterations > 1 and get_settings().self_refine_enabled),
            "self_refine_coverage_delta": max(
                (
                    raw_api_coverage - (float(metadata.get("raw_api_feature_coverage_rate") or 0)),
                    raw_db_coverage - (float(metadata.get("raw_db_feature_coverage_rate") or 0)),
                ),
                default=0.0,
            ),
            "retrieved_case_similarity": max(
                [float(item.get("score") or 0) for item in memory_hits],
                default=None,
            ),
        },
    }


def _build_fallback_design(prd_doc: dict) -> dict:
    """LLM 因输出超限持续失败时，用规则生成一份"最小可用"技术设计，保证流程不中断。

    做法：给一个空设计骨架，交给 repair_feature_mappings 为 PRD 每个功能模块
    补齐 API 端点 + DB 表（确定性、不调 LLM）。质量一般，但覆盖率达标、流程能继续走到评审。
    """
    base = {
        "architecture_overview": "（自动回退生成的最小技术设计：因模型单次输出超限，改用规则按功能模块补齐 API/DB。）",
        "architecture_style": "模块化单体",
        "services": [],
        "db_schema": {"database_type": "PostgreSQL", "tables": [], "relationships": []},
        "api_endpoints": [],
        "tech_risks": [],
        "code_scaffold": {"directory_structure": [], "key_files": [], "dependencies": []},
    }
    design, _ = repair_feature_mappings(prd_doc, base)
    return design


def _memory_enabled(metadata: dict) -> bool:
    if "memory_enabled" in metadata:
        return bool(metadata.get("memory_enabled"))
    return bool(get_settings().memory_enabled)


def _run_tool_calling_phase(
    prd_doc: dict,
    clarified_req: dict,
    extra_context: str,
) -> tuple[str, list[dict]]:
    """Tool calling 预热阶段：让 LLM 主动决定调用哪些工具收集上下文。

    返回 (memory_context_str, memory_hits_list)。
    最多执行 5 轮工具调用，防止无限循环。
    """
    tool_map = {t.name: t for t in ENGINEER_PREP_TOOLS}
    llm_with_tools = get_llm_with_tools(ENGINEER_PREP_TOOLS)

    feature_names = ", ".join(
        str(m.get("name", "")) for m in (prd_doc.get("feature_modules") or [])
        if isinstance(m, dict)
    )
    prep_prompt = (
        "你是一名技术架构师，即将为以下需求生成技术设计方案。\n"
        f"产品目标：{prd_doc.get('product_goal') or clarified_req.get('goal') or ''}\n"
        f"核心功能模块：{feature_names}\n"
        "请先使用工具检索相关历史案例或架构模式，为后续设计提供参考。"
        "如果不需要额外信息，直接回复'无需检索'即可。"
    )
    messages = [HumanMessage(content=prep_prompt)]

    all_memory_hits: list[dict] = []
    context_parts: list[str] = []

    for _ in range(5):
        response = llm_with_tools.invoke(messages)
        messages.append(response)

        if not response.tool_calls:
            break

        for tc in response.tool_calls:
            tool_name = tc["name"]
            tool_args = tc["args"]
            tool_id = tc["id"]

            if tool_name not in tool_map:
                result_content = f"未知工具：{tool_name}"
            else:
                try:
                    result_content = tool_map[tool_name].invoke(tool_args)
                except Exception as exc:
                    result_content = f"工具调用失败：{exc}"

            messages.append(ToolMessage(content=result_content, tool_call_id=tool_id))

            # 收集 search_memory 的原始 hits，用于 metadata 统计
            if tool_name == "search_memory" and result_content != "未找到相关记忆。":
                context_parts.append(result_content)
                raw_hits = search_memory_sync(
                    tool_args.get("query", ""),
                    top_k=get_settings().memory_top_k,
                    memory_types=[tool_args.get("memory_type", "case")],
                )
                all_memory_hits.extend(raw_hits)

    memory_context = "\n\n".join(context_parts)
    settings = get_settings()
    if len(memory_context) > settings.memory_context_max_chars:
        memory_context = memory_context[: settings.memory_context_max_chars].rstrip() + "\n...[truncated]"

    return memory_context, all_memory_hits
