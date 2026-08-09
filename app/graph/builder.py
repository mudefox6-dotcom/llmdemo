"""LangGraph 工作流构建器。

使用 LangGraph 1.0 StateGraph API 将所有节点和路由组装为完整工作流：
START -> input_normalize -> planner -> [route] -> solution -> engineer
      -> reviewer -> [route] -> human_approval -> [route] -> package_output -> END
"""

from __future__ import annotations

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from app.core.observability import trace_node
from app.graph.checkpoints import get_checkpointer
from app.graph.state import AgentState
from app.graph.router import (
    route_after_planner,
    route_after_reviewer,
    route_after_human_approval,
    route_after_dialogue_reviewer,
)
from app.agents.orchestrator_agent import (
    input_normalize_node,
    human_clarification_node,
    human_approval_node,
    package_output_node,
)
from app.agents.planner_agent import planner_node
from app.agents.solution_agent import solution_node
from app.agents.engineer_agent import engineer_node
from app.agents.reviewer_agent import reviewer_node
from app.agents.dialogue_agent import (
    dialogue_reviewer_node,
    dialogue_engineer_node,
    dialogue_solution_node,
)
from app.agents.repairer_agent import repairer_node
from app.agents.plan_evaluator_agent import plan_evaluator_node


def build_graph(checkpointer: MemorySaver | None = None):
    """构建并编译 LangGraph 工作流（整个调度拓扑在这里定义）。

    输入：
        checkpointer —— 检查点存档器；不传则用 get_checkpointer() 的进程单例
        （持久化到 SQLite，支撑断点续跑与人工中断恢复）。
    输出：
        编译后的 CompiledGraph；后续 graph.invoke(state, config) 就按这里定义的
        节点+边来调度。

    专有名词：
        StateGraph —— "状态图"，节点(node)=处理函数，边(edge)=节点间跳转规则，
                       所有节点共读共写同一个 AgentState（黑板）。
        节点注册 add_node、固定边 add_edge、条件边 add_conditional_edges。
    """
    builder = StateGraph(AgentState)

    # ── 注册所有节点 ──────────────────────────────────────────
    builder.add_node("input_normalize", trace_node("input_normalize")(input_normalize_node))
    builder.add_node("planner", trace_node("planner")(planner_node))
    builder.add_node("plan_evaluator", trace_node("plan_evaluator")(plan_evaluator_node))
    builder.add_node("human_clarification", trace_node("human_clarification", llm_calls=0)(human_clarification_node))
    builder.add_node("solution", trace_node("solution")(solution_node))
    builder.add_node("engineer", trace_node("engineer")(engineer_node))
    builder.add_node("reviewer", trace_node("reviewer")(reviewer_node))
    builder.add_node("repairer", trace_node("repairer")(repairer_node))
    builder.add_node("human_approval", trace_node("human_approval", llm_calls=0)(human_approval_node))
    builder.add_node("package_output", trace_node("package_output", llm_calls=0)(package_output_node))

    # ── Dialogue 节点 ──────────────────────────────────────────
    builder.add_node("dialogue_reviewer", trace_node("dialogue_reviewer")(dialogue_reviewer_node))
    builder.add_node("dialogue_engineer", trace_node("dialogue_engineer")(dialogue_engineer_node))
    builder.add_node("dialogue_solution", trace_node("dialogue_solution")(dialogue_solution_node))

    # ── 固定边 ────────────────────────────────────────────────
    # START -> input_normalize -> planner -> plan_evaluator
    builder.add_edge(START, "input_normalize")
    builder.add_edge("input_normalize", "planner")
    builder.add_edge("planner", "plan_evaluator")

    # human_clarification -> planner（人工澄清后重新规划）
    builder.add_edge("human_clarification", "planner")

    # solution -> engineer -> reviewer
    builder.add_edge("solution", "engineer")
    builder.add_edge("engineer", "reviewer")

    # repairer -> reviewer (re-evaluate after patch)
    builder.add_edge("repairer", "reviewer")

    # package_output -> END
    builder.add_edge("package_output", END)

    # ── 条件边 ────────────────────────────────────────────────
    # 条件边 = 节点跑完后，调一个"路由函数"读 state 算出下一个节点名（或列表=并行）。
    # 这是调度的"分叉点"，四处都是流程能不能前进/回流/分流的关键。
    # 注意：plan_evaluator 的条件边复用 route_after_planner（读 needs_human_clarification）。
    builder.add_conditional_edges("plan_evaluator", route_after_planner)

    # reviewer -> human_approval | solution | engineer | repairer | dialogue_reviewer
    builder.add_conditional_edges("reviewer", route_after_reviewer)

    # dialogue_reviewer -> dialogue_engineer | dialogue_solution | [两者并行] | human_approval
    builder.add_conditional_edges("dialogue_reviewer", route_after_dialogue_reviewer)

    # dialogue -> back to reviewer for re-evaluation
    builder.add_edge("dialogue_engineer", "reviewer")
    builder.add_edge("dialogue_solution", "reviewer")

    # human_approval -> package_output | solution
    builder.add_conditional_edges("human_approval", route_after_human_approval)

    # ── 编译 ──────────────────────────────────────────────────
    # compile 把上面的节点+边固化成可执行图，并"挂上"检查点存档器。
    # 挂了 checkpointer 后，图每跑完一个节点就自动落一次检查点 —— 这是断点续跑的基础。
    if checkpointer is None:
        checkpointer = get_checkpointer()

    graph = builder.compile(checkpointer=checkpointer)
    return graph


# 便捷的默认实例（懒加载）
_default_graph = None


def get_graph():
    """获取默认的编译后工作流实例。"""
    global _default_graph
    if _default_graph is None:
        _default_graph = build_graph()
    return _default_graph


def reset_graph_for_tests() -> None:
    """Reset cached graph so tests can simulate process restart."""
    global _default_graph
    _default_graph = None
