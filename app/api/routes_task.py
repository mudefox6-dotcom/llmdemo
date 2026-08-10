"""FastAPI task routes with async queue and checkpoint-aware resume."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.logger import logger
from app.core.observability import extract_metrics, merge_task_metric
from app.core.task_queue import (
    enqueue_resume,
    enqueue_start,
    forget_task,
    get_queue_info,
    make_config,
)
from app.graph.builder import get_graph
from app.graph.checkpoints import get_checkpointer, has_checkpoint
from app.storage import repo_task
from app.tools.export_tool import export_deliverable

router = APIRouter(prefix="/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    user_input: str = Field(..., min_length=1, description="User requirement")


class CreateTaskResponse(BaseModel):
    task_id: str
    status: str
    message: str
    task_submit_latency_ms: float | None = None


class FeedbackRequest(BaseModel):
    feedback: str = Field("", description="Human feedback")
    approved: bool | None = Field(None, description="Approval decision")


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    current_node: str
    reflow_count: int
    interrupt_info: dict | list | str | None = None
    clarified_requirement: dict | None = None
    user_input: str = ""
    review_result: dict | None = None
    prd_doc: dict | None = None
    technical_design: dict | None = None
    metrics: dict | None = None
    # 排队信息：{running_count, pending_count, queue_position}
    # 让前端能提示"前面还有 N 个任务在执行"，而不是干巴巴一个"排队中"
    queue_info: dict | None = None


@router.get("")
async def list_tasks_endpoint(limit: int = 50, offset: int = 0):
    return await repo_task.list_tasks(limit=limit, offset=offset)


@router.post("", response_model=CreateTaskResponse)
async def create_task(req: CreateTaskRequest):
    """创建任务并入队后台执行（非阻塞，立即返回 task_id）。

    输入：{user_input}。输出：{task_id, status="queued", task_submit_latency_ms}。
    数据流：建 DB 记录 → 构造初始 AgentState(initial_state) → 落 tasks.state_snapshot
            → enqueue_start 入内存队列 → 立即返回，图由后台 worker 异步跑。
    """
    started = time.perf_counter()
    task_id = await repo_task.create_task(req.user_input)
    settings = get_settings()
    # 演示模式下不允许回流：评审不通过时会重跑 engineer/repairer + 再评审一次，
    # 实测这会让整轮从 358 秒涨到 524 秒，且是否触发带随机性——演示时长不可控。
    # 设为 0 后仍会完整展示评审结论（含发现的问题），只是不等它返工，直接交人工审批。
    max_reflow = 0 if settings.demo_mode else settings.max_reflow_count
    initial_state = {
        "user_input": req.user_input,
        "task_id": task_id,
        "max_reflow_count": max_reflow,
        "metadata": {
            "task_id": task_id,
            "metrics": {
                "api_timeout": False,
                "background_task_success": None,
            },
        },
    }
    initial_state = merge_task_metric(
        initial_state,
        api_timeout=False,
    )
    await repo_task.update_task(
        task_id,
        status="queued",
        current_node="queued",
        state_snapshot=initial_state,
    )
    await enqueue_start(task_id, initial_state)
    submit_latency_ms = round((time.perf_counter() - started) * 1000, 3)
    initial_state = merge_task_metric(
        initial_state,
        task_submit_latency_ms=submit_latency_ms,
    )
    await repo_task.update_task(task_id, state_snapshot=initial_state)
    logger.info("Task queued", extra={"task_id": task_id, "latency_ms": submit_latency_ms})
    return CreateTaskResponse(
        task_id=task_id,
        status="queued",
        message="Task created and queued for background execution.",
        task_submit_latency_ms=submit_latency_ms,
    )


@router.get("/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    task = await repo_task.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    state_snapshot = task.get("state_snapshot") or {}
    interrupt_info = state_snapshot.get("interrupt_info")
    checkpoint_seen = False
    # DB 里的 current_node 只在 graph.invoke 返回时才写（中断/结束），任务跑到一半时
    # 它还停在入队时的 "queued"，前端进度条会一直显示"排队中"。所以优先用实时检查点
    # 推断当前节点：graph_state.next 就是"接下来/正在跑"的节点。
    live_node = ""
    live_values: dict[str, Any] = {}   # 检查点里的实时产物，比 DB 快照新
    try:
        # 读检查点必须放线程池：get_state / has_checkpoint 都是同步调用，且要和正在执行
        # 的 worker 抢 PersistentMemorySaver 的线程锁（_persist 会 pickle 全量检查点再写
        # SQLite）。直接在事件循环里调，抢锁期间整个 API 都会冻住——并发跑多个任务时
        # 尤其明显（曾出现状态查询卡满 120s 超时）。
        graph_state, checkpoint_exists = await asyncio.to_thread(_read_graph_state, task_id)
        checkpoint_seen = _has_materialized_graph_state(graph_state) or checkpoint_exists
        values = graph_state.values if hasattr(graph_state, "values") else {}
        if isinstance(values, dict):
            live_values = values
            if values.get("__interrupt__"):
                interrupt_info = values["__interrupt__"]
        # next 是"下一个要执行的节点"，正在跑的时候它就是当前节点；
        # 图已跑完时 next 为空，退回用状态里最后记录的 current_node。
        next_nodes = getattr(graph_state, "next", None) or ()
        if next_nodes:
            live_node = str(next_nodes[0])
        elif isinstance(values, dict) and values.get("current_node"):
            live_node = str(values["current_node"])
    except Exception as exc:
        logger.warning(f"Could not read graph checkpoint for task {task_id}: {exc}")

    if task["status"] in {"waiting_human", "running", "queued"}:
        state_snapshot = merge_task_metric(
            state_snapshot,
            checkpoint_recovered=checkpoint_seen,
            state_persistence_coverage=1.0 if state_snapshot else 0.0,
        )
        await repo_task.update_task(task_id, state_snapshot=state_snapshot)

    return TaskStatusResponse(
        task_id=task["task_id"],
        status=task["status"],
        # 实时节点优先；只有读不到检查点时才退回 DB 里那个可能滞后的值
        current_node=live_node or task["current_node"],
        reflow_count=task["reflow_count"],
        interrupt_info=interrupt_info,
        # 产物同样优先取检查点：DB 快照只在 invoke 返回时才写，而一次 invoke 会连跑
        # 多个节点（可能十几分钟）。取实时值能让 PRD 一生成就可见，不必等整轮跑完。
        clarified_requirement=_pick_live(live_values, state_snapshot, "clarified_requirement"),
        user_input=task.get("user_input", ""),
        review_result=_pick_live(live_values, state_snapshot, "review_result"),
        prd_doc=_pick_live(live_values, state_snapshot, "prd_doc"),
        technical_design=_pick_live(live_values, state_snapshot, "technical_design"),
        metrics=extract_metrics(state_snapshot),
        queue_info=get_queue_info(task_id),
    )


def _pick_live(live: dict, snapshot: dict, key: str):
    """产物取值：优先用检查点里的实时值，为空才退回 DB 快照。

    "为空"包括 None 和空 dict —— 未产出的字段在状态里是 `{}`，不能当成有值，
    否则会把已经落库的旧产物覆盖成空。
    """
    value = live.get(key)
    if value:
        return value
    return snapshot.get(key)


def _read_graph_state(task_id: str) -> tuple[Any, bool]:
    """同步读取图检查点，供 asyncio.to_thread 调用。

    返回 (graph_state, 检查点是否存在)。两个调用都涉及 checkpointer 的线程锁，
    合并到一次线程切换里完成，避免在事件循环里阻塞。
    """
    graph_state = get_graph().get_state(make_config(task_id))
    return graph_state, has_checkpoint(task_id)


def _has_materialized_graph_state(graph_state: Any) -> bool:
    if graph_state is None:
        return False
    values = getattr(graph_state, "values", None)
    tasks = getattr(graph_state, "tasks", None)
    next_nodes = getattr(graph_state, "next", None)
    created_at = getattr(graph_state, "created_at", None)
    return bool(created_at or values or tasks or next_nodes)


@router.post("/{task_id}/feedback")
async def submit_feedback(task_id: str, req: FeedbackRequest):
    """提交人工反馈以恢复中断的图（澄清/审批）。

    输入：{feedback, approved?}。approved 非空=审批场景(→dict)，为空=澄清场景(→纯文本)。
    两道前置闸门：① 任务必须处于 waiting_human；② 检查点必须还在(has_checkpoint)否则无法续跑。
    数据流：存反馈审计 → 组装 resume_data → 用 Command(resume=...) enqueue_resume 重入队。
    """
    task = await repo_task.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "waiting_human":
        raise HTTPException(status_code=400, detail="Task is not waiting for human feedback")
    if not has_checkpoint(task_id):
        raise HTTPException(status_code=409, detail="Graph checkpoint is missing for this task")

    await repo_task.save_feedback(
        task_id,
        content=req.feedback,
        node=task["current_node"],
        feedback_type="approval" if req.approved is not None else "clarification",
    )
    # resume_data 的形态要和中断节点解析方式对应：
    #   审批(approved 非空) → dict，human_approval_node 取 .approved/.feedback
    #   澄清(approved 为空) → 纯字符串，human_clarification_node 直接当 human_feedback
    resume_data: Any = (
        {"approved": req.approved, "feedback": req.feedback}
        if req.approved is not None
        else req.feedback
    )
    state_snapshot = merge_task_metric(
        task.get("state_snapshot") or {},
        resume_requested=True,
        resume_success=None,
    )
    await repo_task.update_task(
        task_id,
        status="queued",
        current_node="queued",
        state_snapshot=state_snapshot,
    )
    await enqueue_resume(task_id, resume_data)
    return {
        "task_id": task_id,
        "status": "queued",
        "current_node": "queued",
        "message": "Feedback accepted and queued for background resume.",
    }


@router.get("/{task_id}/download/project")
async def download_project(task_id: str):
    """下载生成的可运行项目 zip。

    export 只把 zip 写到服务器本地（output/{task_id}/project.zip），
    浏览器拿不到本地路径，所以需要这个接口把文件流回去。
    """
    task = await repo_task.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    zip_path = get_settings().output_dir / task_id / "project.zip"
    if not zip_path.exists():
        raise HTTPException(
            status_code=404,
            detail="项目包尚未生成，请先调用 /result 完成交付导出",
        )
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=f"{task_id}-project.zip",
    )


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    """删除任务：清掉业务记录、反馈、队列登记和 LangGraph 检查点。

    关于"正在执行中"的任务：图跑在线程里，无法中途打断。这里先删数据行，
    worker 跑完后写回时 UPDATE 命中 0 行、自然作废；若任务还在队列里没开跑，
    `_run_job` 开头 `get_task()` 拿不到记录会直接跳过，等价于取消。
    响应里的 was_running 用于提示用户"后台那一轮会自己结束"。
    """
    task = await repo_task.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    was_running = forget_task(task_id)

    # 清理检查点（失败不阻断删除，仅告警——否则会残留删不掉的任务）
    try:
        saver = get_checkpointer()
        if hasattr(saver, "delete_thread"):
            await asyncio.to_thread(saver.delete_thread, task_id)
    except Exception as exc:
        logger.warning(f"Failed to delete checkpoint for task {task_id}: {exc}")

    deleted = await repo_task.delete_task(task_id)
    logger.info("Task deleted", extra={"task_id": task_id, "was_running": was_running})
    return {
        "task_id": task_id,
        "deleted": deleted,
        "was_running": was_running,
        "message": (
            "任务已删除；后台正在执行的那一轮会自行结束，结果不再写回。"
            if was_running else "任务已删除。"
        ),
    }


@router.get("/{task_id}/result")
async def get_task_result(task_id: str):
    task = await repo_task.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    result_data = task.get("result", {})
    if not result_data or task["status"] != "completed":
        raise HTTPException(status_code=400, detail="Task is not completed")

    try:
        exported = export_deliverable(result_data, task_id)
    except Exception as exc:
        logger.error(f"Export failed: {exc}", extra={"task_id": task_id})
        exported = {}

    return {
        "task_id": task_id,
        "deliverable": result_data,
        "exported_files": exported,
    }
