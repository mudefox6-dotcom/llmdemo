"""本地调试入口：直接构建并同步执行 LangGraph 工作流，方便在各智能体节点打断点单步跟踪。

用法：
  - VS Code：选 "直接跑图（单步跟踪各智能体）" 调试配置，F5 启动。
  - 命令行：python debug_graph.py

与走 API/队列不同，这里在主线程同步 invoke，断点会直接命中
planner_node / solution_node / engineer_node / reviewer_node 等函数。
遇到 human_clarification / human_approval 的 interrupt 时会返回 __interrupt__，
下面演示了如何用 Command(resume=...) 恢复。
"""

from __future__ import annotations

import json

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.graph.builder import build_graph

# 用内存检查点即可，避免调试时把状态写进磁盘 SQLite
graph = build_graph(checkpointer=MemorySaver())
config = {"configurable": {"thread_id": "debug-1"}}

initial_state = {
    "user_input": "做一个在线教育平台，支持选课、直播上课和作业批改",
    "task_id": "debug-1",
    "max_reflow_count": 2,
}

# 第一次执行：在这一行打断点，可 step into 进入各节点
result = graph.invoke(initial_state, config)
print("当前节点:", result.get("current_node"))

# 循环 resume 直到没有 __interrupt__（图真正跑完）。
# 一次 resume 只清一个中断；本图有 human_clarification 和 human_approval 两处，需循环处理。
_guard = 0
while "__interrupt__" in result:
    _guard += 1
    if _guard > 6:
        print("!! 中断次数过多，可能陷入循环，停止"); break
    intr = result["__interrupt__"]
    # 取出中断负载，判断是澄清还是审批，给不同回复
    payload = intr[0].value if isinstance(intr, (list, tuple)) and intr else intr
    itype = payload.get("type") if isinstance(payload, dict) else ""
    print(f"命中中断[{itype}]:", json.dumps(payload, ensure_ascii=False, default=str)[:400])
    if itype == "approval":
        resume_value = {"approved": True, "feedback": "同意交付"}   # 审批：批准
    else:
        resume_value = "第三方直播(声网)；仅人工批改；需要回放；需付费；需考试；需社区。"  # 澄清：补充答复
    result = graph.invoke(Command(resume=resume_value), config)
    print("恢复后当前节点:", result.get("current_node"))

print("最终节点:", result.get("current_node"), "| next_action:", result.get("next_action"))

# 打印关键产物供检查
for key in ("clarified_requirement", "prd_doc", "technical_design", "review_result"):
    val = result.get(key)
    if val:
        print(f"\n===== {key} =====")
        print(json.dumps(val, ensure_ascii=False, indent=2, default=str)[:1500])
