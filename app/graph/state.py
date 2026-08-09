"""LangGraph 共享状态定义。

状态字段按"业务产物"组织，而非绑定到单一 Agent，
以便后续插入新 Agent、并行分支或更多审批点时不需要整体推翻。

LangGraph 1.0 要求 State 使用 TypedDict + Annotated reducer。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict


# ── reducer（字段合并策略）────────────────────────────────────
# LangGraph 每跑完一个节点，会拿节点 return 的"局部更新 dict"和黑板里的旧值，
# 按字段声明的 reducer 合并。reducer 决定"写这个字段"到底是覆盖还是追加。


def _replace(existing: Any, new: Any) -> Any:
    """替换策略：直接用新值覆盖旧值。

    用于"整份产物"字段（prd_doc / technical_design / review_result 等）——
    节点每次都 return 完整对象，直接覆盖上一版。
    """
    return new


def _append_list(existing: list, new: list) -> list:
    """追加策略：将新列表追加到旧列表。

    用于"日志/记录"字段（task_memory / execution_logs / dialogue_history 等）——
    节点每次只 return 一条(或几条)新记录 [entry]，reducer 自动拼到旧列表尾部，
    所以这些字段会随流程不断累积，而不是被覆盖。
    """
    return existing + new


class AgentState(TypedDict, total=False):
    """工作流共享状态。

    所有节点通过读写此状态进行数据传递。
    LangGraph 使用 Annotated 类型来决定字段的合并策略。
    """

    # ── 用户输入 ──────────────────────────────────────────────
    user_input: Annotated[str, _replace]            # 用户原始需求（create_task 写入）
    normalized_input: Annotated[str, _replace]      # 规范化后的需求（input_normalize 写入）

    # ── 业务产物（以 dict 存储，便于 JSON 序列化）──────────────
    # 这五个是最终交付包的核心，全部 _replace：谁产出谁整份覆盖。
    clarified_requirement: Annotated[dict, _replace]  # 结构化需求（planner 写；plan_evaluator 可能改写）
    prd_doc: Annotated[dict, _replace]                # PRD（solution 写；dialogue_solution 可改写）
    technical_design: Annotated[dict, _replace]       # 技术设计（engineer 写；repairer/dialogue_engineer 可改写）
    code_scaffold: Annotated[dict, _replace]          # 代码骨架（engineer 从技术设计里抽出）
    review_result: Annotated[dict, _replace]          # 评审结论（reviewer 写，含 passed/reflow_target）

    # ── 人工交互 ──────────────────────────────────────────────
    human_feedback: Annotated[str, _replace]                 # 用户提交的澄清/审批文字（人工节点写）
    needs_human_clarification: Annotated[bool, _replace]     # 是否要暂停问用户（planner 写，route_after_planner 读）
    human_clarification_confirmed: Annotated[bool, _replace] # 已澄清过标记，防止反复追问（human_clarification 写）
    human_approved: Annotated[bool, _replace]                # 审批是否通过（human_approval 写，route_after_human_approval 读）

    # ── 流程控制 ──────────────────────────────────────────────
    current_node: Annotated[str, _replace]        # 当前/最后执行的节点名（每个节点都会写）
    next_action: Annotated[str, _replace]         # 终态标记，如 "completed"（package_output 写）
    reflow_count: Annotated[int, _replace]        # 已回流次数（reviewer 累加）
    max_reflow_count: Annotated[int, _replace]    # 回流上限，超了转人工（create_task 初始化=2）
    error_message: Annotated[str, _replace]       # 错误原因（异常/崩溃无检查点时写）

    # ── 短期记忆（任务内上下文）────────────────────────────────
    # _append_list：每个节点 add_entry 追加一条 {node,summary,metrics}，供下游(尤其回流)回顾
    task_memory: Annotated[list[dict], _append_list]

    # ── Dialogue（多轮对话）────────────────────────────────────
    dialogue_round: Annotated[int, _replace]              # 当前对话轮次（dialogue_reviewer +1）
    dialogue_active: Annotated[bool, _replace]            # 对话是否进行中
    dialogue_history: Annotated[list[dict], _append_list] # 历轮问答记录（append 累积）
    dialogue_targets: Annotated[list[str], _replace]      # 本轮找谁对话 engineer/solution（路由据此 fan-out）
    dialogue_questions: Annotated[list[dict], _replace]   # 本轮生成的问题清单

    # ── Repairer ───────────────────────────────────────────────
    repair_attempted: Annotated[bool, _replace]   # 是否已尝试过精准修复，防止路由反复选 repairer

    # ── Plan-Verify ────────────────────────────────────────────
    plan_evaluation: Annotated[dict, _replace]    # 方案择优结果（plan_evaluator 写）

    # ── 预留扩展 ──────────────────────────────────────────────
    memory_hits: Annotated[list[dict], _append_list]     # RAG 命中的历史记忆（engineer/reviewer 追加）
    execution_logs: Annotated[list[dict], _append_list]  # 逐节点执行日志（append，含时间戳/耗时）
    metadata: Annotated[dict, _replace]                  # 指标与最终交付包容器（trace_node 埋点、package_output 汇总）
    task_id: Annotated[str, _replace]                    # 任务号（= LangGraph 检查点 thread_id）
