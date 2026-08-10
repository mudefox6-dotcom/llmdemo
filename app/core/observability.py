"""Lightweight task and node observability helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app.core.logger import logger


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def trace_node(node_name: str, llm_calls: int = 1):
    """给 LangGraph 节点套一层"埋点装饰器"，在节点输出里追加运行指标。

    builder.py 里每个节点都用它包了一层：`trace_node("planner")(planner_node)`。
    作用：不侵入业务代码，就能统一记录每个节点的耗时、LLM 调用数、错误数。

    参数：
      node_name: 节点名，用于指标归属。
      llm_calls: 该节点预计的 LLM 调用次数（纯流程节点如人工/打包传 0）。

    三层结构：trace_node(外层拿配置) → decorator(拿原函数) → wrapper(真正执行时跑)。
    """

    def decorator(func: Callable[[dict], dict]):
        def wrapper(state: dict) -> dict:
            started = time.perf_counter()          # 记开始时间
            try:
                result = func(state)               # 跑真正的节点函数
            except Exception:
                # 节点抛异常：记一次 node_error 并原样抛出（交给上层 worker 处理）
                duration = time.perf_counter() - started
                _record_error(state, node_name, duration)
                raise

            duration = time.perf_counter() - started
            if not isinstance(result, dict):
                return result

            # 把本节点耗时/LLM 次数追加进 metadata.metrics，再合并回节点输出。
            # 注意：metadata 是 _replace 字段，所以这里先取旧 metadata 再 update，避免丢历史指标。
            metadata = dict(state.get("metadata") or {})
            metadata.update(result.get("metadata") or {})
            # metrics：可观测指标容器（嵌套在 metadata 里）。逐字段业务含义如下：
            metrics = dict(metadata.get("metrics") or {})

            # node_latencies：逐节点耗时明细列表，每条 {node: 节点名, duration_seconds: 本次耗时秒数}。
            #   用途：看哪个节点慢、每个节点各跑了多久（累积追加，一个节点跑多次会有多条）。
            node_latencies = list(metrics.get("node_latencies") or [])
            node_latencies.append({"node": node_name, "duration_seconds": round(duration, 4)})
            metrics["node_latencies"] = node_latencies

            # node_latency_seconds：所有节点的"平均耗时"（= node_latencies 各项 duration 的均值）。
            #   用途：一个整体性能概览数字，衡量单节点平均要跑多久。
            metrics["node_latency_seconds"] = round(
                sum(item["duration_seconds"] for item in node_latencies) / len(node_latencies),
                4,
            )

            # llm_call_count：累计 LLM 调用次数。每个节点按其声明的 llm_calls（默认1，纯流程节点=0）累加。
            #   用途：估算成本/调用量。
            metrics["llm_call_count"] = int(metrics.get("llm_call_count") or 0) + llm_calls

            # node_error_count：节点执行出错的累计次数（异常时由 _record_error +1）。这里保证键存在，默认0。
            metrics.setdefault("node_error_count", 0)
            metadata["metrics"] = metrics

            # execution_logs：逐节点执行日志（_append_list 字段，累积）。每条含：
            #   node             —— 节点名
            #   timestamp        —— 完成时刻(UTC ISO)
            #   duration_seconds —— 本次耗时
            #   event            —— 事件类型，这里固定 "node_completed"
            logs = list(result.get("execution_logs") or [])
            logs.append(
                {
                    "node": node_name,
                    "timestamp": utc_now_iso(),
                    "duration_seconds": round(duration, 4),
                    "event": "node_completed",
                }
            )
            result["metadata"] = metadata
            result["execution_logs"] = logs
            return result

        return wrapper

    return decorator


def merge_task_metric(state_snapshot: dict[str, Any] | None, **metrics: Any) -> dict[str, Any]:
    state = dict(state_snapshot or {})
    metadata = dict(state.get("metadata") or {})
    current = dict(metadata.get("metrics") or {})
    current.update({key: value for key, value in metrics.items() if value is not None})
    metadata["metrics"] = current
    state["metadata"] = metadata
    return state


def extract_metrics(state_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    return dict(((state_snapshot or {}).get("metadata") or {}).get("metrics") or {})


def _record_error(state: dict, node_name: str, duration: float) -> None:
    metadata = dict(state.get("metadata") or {})
    metrics = dict(metadata.get("metrics") or {})
    metrics["node_error_count"] = int(metrics.get("node_error_count") or 0) + 1
    metadata["metrics"] = metrics
    logger.error(
        "Node execution failed",
        extra={
            "node": node_name,
            "duration_seconds": round(duration, 4),
            "task_id": state.get("task_id", ""),
        },
    )
