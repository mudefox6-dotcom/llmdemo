"""In-process asyncio task queue for non-blocking Agent workflow execution."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from langgraph.types import Command

from app.core.config import get_settings
from app.core.logger import logger
from app.core.observability import merge_task_metric, utc_now_iso
from app.graph.builder import get_graph
from app.graph.checkpoints import has_checkpoint
from app.storage import repo_task


@dataclass
class WorkflowJob:
    task_id: str
    payload: dict[str, Any] | Command
    kind: str = "start"
    enqueued_at: float = field(default_factory=time.perf_counter)


_queue: asyncio.Queue[WorkflowJob] | None = None
_worker_tasks: list[asyncio.Task] = []   # 多个并发 worker，数量由 WORKER_CONCURRENCY 控制
_stop_event: asyncio.Event | None = None

# 正在执行中的 task_id（供前端显示"前面还有几个任务"）；worker 取走时加入、结束时移除
_inflight: set[str] = set()
# 已入队但还没被 worker 取走的 task_id，按入队顺序排列（用于算排队位次）
_pending_order: list[str] = []

# per-task 进度队列：task_id -> asyncio.Queue，SSE 端点从这里读取事件
_progress_queues: dict[str, asyncio.Queue] = {}
_PROGRESS_SENTINEL = None  # 放入 queue 表示流结束


def get_or_create_progress_queue(task_id: str) -> asyncio.Queue:
    """获取或创建 task 的进度队列，SSE 端点调用。"""
    if task_id not in _progress_queues:
        _progress_queues[task_id] = asyncio.Queue(maxsize=256)
    return _progress_queues[task_id]


def _drop_progress_queue(task_id: str) -> None:
    _progress_queues.pop(task_id, None)


def get_queue_info(task_id: str) -> dict[str, Any]:
    """给前端用的排队信息：这个任务前面还有几个在跑/在等。

    返回：
      running_count  —— 当前正在执行的任务数
      pending_count  —— 排在队列里还没开跑的任务数
      queue_position —— 本任务在等待队列中的位次(1 表示下一个就轮到它)；不在队列里则为 0
    """
    try:
        position = _pending_order.index(task_id) + 1
    except ValueError:
        position = 0
    return {
        "running_count": len(_inflight),
        "pending_count": len(_pending_order),
        "queue_position": position,
    }


def forget_task(task_id: str) -> bool:
    """把任务从队列登记与进度队列里摘掉（删除任务时调用）。

    返回该任务是否仍在执行中（无法中途打断，调用方据此提示用户）。
    说明：已经投进 asyncio.Queue 的 job 取不出来，但 `_run_job` 开头会
    `get_task()`，任务行已删则直接跳过，等价于取消。
    """
    if task_id in _pending_order:
        _pending_order.remove(task_id)
    _drop_progress_queue(task_id)
    return task_id in _inflight


async def start_worker(*, recover: bool = True) -> None:
    """启动后台 worker。按 WORKER_CONCURRENCY 起多个，实现任务并发执行。

    原来只起 1 个 worker，队列是严格串行的——一个耗时长的任务（比如 Engineer 的
    ReAct 循环）会把后面所有任务堵死。现在起 N 个，彼此独立取任务。
    """
    global _queue, _stop_event
    if _queue is None:
        _queue = asyncio.Queue()
    if _stop_event is None:
        _stop_event = asyncio.Event()

    # 清掉已结束的 worker，再补足到目标并发数
    _worker_tasks[:] = [t for t in _worker_tasks if not t.done()]
    target = get_settings().worker_concurrency
    if len(_worker_tasks) < target:
        _stop_event.clear()
        for i in range(len(_worker_tasks), target):
            _worker_tasks.append(
                asyncio.create_task(_worker_loop(), name=f"agent-task-worker-{i}")
            )
        logger.info(f"后台 worker 已启动，并发数={target}")

    if recover:
        await recover_pending_tasks()


async def stop_worker() -> None:
    """停止所有后台 worker。"""
    if _stop_event is not None:
        _stop_event.set()
    for task in _worker_tasks:
        task.cancel()
    for task in _worker_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    _worker_tasks.clear()


async def _put_job(job: WorkflowJob) -> None:
    """统一入队口：同时登记到 _pending_order，供前端展示排队位次。"""
    _pending_order.append(job.task_id)
    await _queue.put(job)  # type: ignore[union-attr]


async def enqueue_start(task_id: str, initial_state: dict[str, Any]) -> None:
    await _ensure_queue()
    await _put_job(WorkflowJob(task_id=task_id, payload=initial_state, kind="start"))


async def enqueue_resume(task_id: str, resume_data: Any) -> None:
    await _ensure_queue()
    await _put_job(
        WorkflowJob(
            task_id=task_id,
            payload=Command(resume=resume_data),
            kind="resume",
        )
    )


async def recover_pending_tasks() -> dict[str, int]:
    """崩溃恢复：进程重启后，把"内存队列已丢失、但磁盘上还没跑完"的任务捞回来。

    背景：_queue 是进程内存里的 asyncio.Queue，进程一崩就没了；但 tasks.db(任务清单)
    和检查点库(精确进度)都在磁盘上。这里扫 tasks.db 里未终结的任务，按状态分诊：
      - queued        → 还没开跑，直接重新入队(kind=start)
      - waiting_human → 在等人，不重跑，只刷新 checkpoint_recovered 指标
      - running       → 崩在半路：有检查点→重入队从断点续跑；无检查点→标 error
    输出：{"queued":n, "running":m, "waiting_human":k, "error":e} 恢复统计。
    """
    await _ensure_queue_started_only()
    recovered = {"queued": 0, "running": 0, "waiting_human": 0, "error": 0}
    tasks = await repo_task.list_tasks_by_status(["queued", "running", "waiting_human"], limit=500)
    for task in tasks:
        task_id = task["task_id"]
        snapshot = task.get("state_snapshot") or {}
        if task["status"] == "queued":
            payload = snapshot or {
                "user_input": task.get("user_input", ""),
                "task_id": task_id,
                "max_reflow_count": 2,
            }
            await _put_job(WorkflowJob(task_id=task_id, payload=payload, kind="start"))
            recovered["queued"] += 1
            continue

        if task["status"] == "waiting_human":
            snapshot = merge_task_metric(
                snapshot,
                checkpoint_recovered=has_checkpoint(task_id),
                state_persistence_coverage=1.0 if snapshot else 0.0,
            )
            await repo_task.update_task(task_id, state_snapshot=snapshot)
            recovered["waiting_human"] += 1
            continue

        # running 且有检查点：重入队。虽然 kind="start"、payload=snapshot，但因编译图挂了
        # checkpointer 且 thread_id 相同，graph.invoke 会优先从检查点已推进的进度续跑，
        # 而非从头重算；snapshot 只作无检查点时的兜底输入。
        if has_checkpoint(task_id):
            await repo_task.update_task(task_id, status="queued", current_node="queued")
            payload = snapshot or {
                "user_input": task.get("user_input", ""),
                "task_id": task_id,
                "max_reflow_count": 2,
            }
            await _put_job(WorkflowJob(task_id=task_id, payload=payload, kind="start"))
            recovered["running"] += 1
        else:
            # 崩得太早、一个检查点都没落 → 无从续起，诚实标 error

            snapshot = merge_task_metric(
                snapshot,
                background_task_success=False,
                checkpoint_recovered=False,
            )
            snapshot["error_message"] = "Task was running during restart and no checkpoint was found."
            await repo_task.update_task(
                task_id,
                status="error",
                state_snapshot=snapshot,
                current_node=snapshot.get("current_node", task.get("current_node", "")),
            )
            recovered["error"] += 1
    return recovered


def make_config(task_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": task_id}}


async def _ensure_queue() -> None:
    if _queue is None or not any(not t.done() for t in _worker_tasks):
        await _ensure_queue_started_only()


async def _ensure_queue_started_only() -> None:
    """确保队列存在且有活跃 worker（不触发崩溃恢复）。"""
    global _queue, _stop_event
    if _queue is None:
        _queue = asyncio.Queue()
    if _stop_event is None:
        _stop_event = asyncio.Event()
    _worker_tasks[:] = [t for t in _worker_tasks if not t.done()]
    target = get_settings().worker_concurrency
    if len(_worker_tasks) < target:
        _stop_event.clear()
        for i in range(len(_worker_tasks), target):
            _worker_tasks.append(
                asyncio.create_task(_worker_loop(), name=f"agent-task-worker-{i}")
            )


async def _run_graph_in_daemon_thread(fn, *args):
    """在【守护线程】里跑阻塞的图执行，并 await 其结果。

    为什么不用 `asyncio.to_thread`：它用的默认 ThreadPoolExecutor 是**非守护线程**，
    解释器退出时会 join 等它跑完。而图执行内部是阻塞的 LLM 请求（超时可达 300s，
    还会重试），于是 Ctrl+C 后进程要干等好几分钟才退出，看起来像"卡死"。
    守护线程不阻塞进程退出：Ctrl+C 时会被直接丢弃，未完成的那一步靠检查点续跑即可。
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def runner() -> None:
        try:
            result = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - 原样转交给调用方处理
            loop.call_soon_threadsafe(_set_if_pending, future, exc, True)
        else:
            loop.call_soon_threadsafe(_set_if_pending, future, result, False)

    threading.Thread(target=runner, name="graph-invoke", daemon=True).start()
    return await future


def _set_if_pending(future: asyncio.Future, value: Any, is_error: bool) -> None:
    """回填结果；future 可能已被取消（如进程正在关闭），此时忽略。"""
    if future.done():
        return
    if is_error:
        future.set_exception(value)
    else:
        future.set_result(value)


async def _worker_loop() -> None:
    assert _queue is not None
    while True:
        job = await _queue.get()
        # 出队即从"等待列表"移到"执行中"，供 get_queue_info 计算排队位次
        if job.task_id in _pending_order:
            _pending_order.remove(job.task_id)
        _inflight.add(job.task_id)
        try:
            await _run_job(job)
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.error(f"Background workflow job failed: {exc}", extra={"task_id": job.task_id})
        finally:
            _inflight.discard(job.task_id)
            _queue.task_done()


async def _run_job(job: WorkflowJob) -> None:
    queue_wait_seconds = round(time.perf_counter() - job.enqueued_at, 4)
    task = await repo_task.get_task(job.task_id)
    if task is None:
        logger.warning(f"Skipping missing task {job.task_id}")
        return

    state_snapshot = merge_task_metric(
        task.get("state_snapshot") or {},
        queue_wait_seconds=queue_wait_seconds,
        background_started_at=utc_now_iso(),
    )
    await repo_task.update_task(
        job.task_id,
        status="running",
        state_snapshot=state_snapshot,
    )

    graph = get_graph()
    config = make_config(job.task_id)   # {"configurable": {"thread_id": task_id}}，调度隔离键
    started = time.perf_counter()
    # 用 get_or_create 而非 .get()：任务往往在前端 SSE 连上之前就被 worker 取走执行，
    # 若此时队列还不存在，本轮所有进度事件都会被丢弃、SSE 端永远收不到消息。
    # 提前建好队列，SSE 稍后连上时拿到的是同一个实例，就能收到后续事件。
    # 无人订阅时事件堆到上限即丢弃（put_nowait 不阻塞），任务结束时统一清理。
    progress_q = get_or_create_progress_queue(job.task_id)

    try:
        # 调度总入口：把 payload + config 交给图，LangGraph 就按 builder 定义的拓扑
        # 一个节点一个节点地跑，直到遇到 interrupt(暂停) 或到 END(完成)。
        #   job.payload 是普通 dict  → 从 START 新跑；
        #   job.payload 是 Command(resume=...) → 按 thread_id 从检查点续跑并注回中断点。
        # 同步节点必须丢到线程里跑，否则会阻塞整个 asyncio 事件循环。
        # 用守护线程而非 asyncio.to_thread，这样 Ctrl+C 能立刻退出（详见函数注释）。
        result = await _run_graph_in_daemon_thread(graph.invoke, job.payload, config)
    except Exception as exc:
        state_snapshot = merge_task_metric(state_snapshot, background_task_success=False, node_error_count=1)
        state_snapshot["error_message"] = str(exc)
        await repo_task.update_task(
            job.task_id, status="error",
            current_node=state_snapshot.get("current_node", ""),
            state_snapshot=state_snapshot,
        )
        if progress_q is not None:
            await _push(progress_q, {"type": "error", "message": str(exc)})
            await progress_q.put(_PROGRESS_SENTINEL)
            _drop_progress_queue(job.task_id)
        logger.error(f"Workflow execution failed: {exc}", extra={"task_id": job.task_id})
        return

    duration = round(time.perf_counter() - started, 4)
    interrupt_info = extract_interrupt(result)
    status = "waiting_human" if interrupt_info else "completed"
    state_snapshot = safe_serialize(result)
    state_snapshot = merge_task_metric(
        state_snapshot,
        background_task_success=True,
        background_duration_seconds=duration,
        queue_wait_seconds=queue_wait_seconds,
        interrupted_task_recovered=job.kind == "resume",
        resume_success=True if job.kind == "resume" else None,
    )
    if interrupt_info:
        state_snapshot["interrupt_info"] = interrupt_info

    await repo_task.update_task(
        job.task_id,
        status=status,
        current_node=state_snapshot.get("current_node", ""),
        state_snapshot=state_snapshot,
        reflow_count=state_snapshot.get("reflow_count", 0),
    )
    if status == "completed":
        await repo_task.update_task(job.task_id, result=state_snapshot)

    # 推送节点完成事件和最终状态到进度队列
    if progress_q is not None:
        node = state_snapshot.get("current_node", "")
        if node:
            await _push(progress_q, {"type": "node_end", "node": node, "output": {}})
        if interrupt_info:
            await _push(progress_q, {"type": "interrupt", "node": node, "data": interrupt_info})
        await _push(progress_q, {"type": "done", "status": status})
        await progress_q.put(_PROGRESS_SENTINEL)
        _drop_progress_queue(job.task_id)


async def _push(q: asyncio.Queue, event: dict) -> None:
    """非阻塞推送，queue 满时丢弃（SSE 客户端未连接时不阻塞 worker）。"""
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        pass


def extract_interrupt(result: Any) -> Any | None:
    if isinstance(result, dict) and "__interrupt__" in result:
        return _jsonable(result["__interrupt__"])
    return None


def safe_serialize(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        clean = {}
        for key, value in data.items():
            if key.startswith("__"):
                continue
            clean[key] = _jsonable(value)
        return clean
    return {}


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        if isinstance(value, tuple):
            return [_jsonable(item) for item in value]
        if isinstance(value, list):
            return [_jsonable(item) for item in value]
        if isinstance(value, dict):
            return {str(key): _jsonable(item) for key, item in value.items()}
        return str(value)
