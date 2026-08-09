"""LangGraph 检查点持久化：在官方 MemorySaver 之上做 SQLite 增量落盘。

为什么要自己写：本项目安装的 langgraph 版本没有随包提供 sqlite checkpointer，
而纯内存的 MemorySaver 进程一关状态就没了，无法支撑断点续跑与崩溃恢复。

实现要点（相对早期"全量快照"版本的改进）：
  旧版每次写入都把【所有任务的全部检查点】重新 pickle 一遍覆盖写三行，
  写放大随任务数和步数线性增长，任务一多就越来越慢。
  现版改为**逐条增量写**：一次 put 只写「1 条 checkpoint 行 + 本步变化通道的 blob 行」，
  一次 put_writes 只写「该 checkpoint 的 1 条 writes 行」，与历史数据量无关。

MemorySaver 的三个内存结构（键结构决定了怎么拆表）：
  storage[thread_id][checkpoint_ns][checkpoint_id] = (checkpoint, metadata, parent_id)
  blobs[(thread_id, checkpoint_ns, channel, version)] = 通道值        ← 真正的大对象
  writes[(thread_id, checkpoint_ns, checkpoint_id)][(task_id, idx)] = 中间写入
三者都以 thread_id 打头，因此可以按 thread/checkpoint 精确定位到行。
"""

from __future__ import annotations

import pickle
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from app.core.config import get_settings
from app.core.logger import logger

# 旧版单表（三行全量快照）表名，仅用于一次性迁移
_LEGACY_TABLE = "langgraph_checkpoint_store"


def _make_default_serde():
    """构造检查点序列化器，并（在支持的版本上）把自定义类型加入 msgpack 白名单。

    兼容性说明：`with_msgpack_allowlist` 只存在于部分 langgraph-checkpoint 版本
    （如 3.0.1 已移除）。旧代码直接调用会抛 AttributeError，导致 checkpointer
    构造失败、进而整个工作流第一步就崩溃。这里改为"有则用、无则回退默认 serde"。
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    allowlist = getattr(serde, "with_msgpack_allowlist", None)
    if allowlist is None:
        return serde
    try:
        return allowlist([("app.schemas.review", "ReviewTargetType")])
    except Exception as exc:  # pragma: no cover - 不同版本签名差异时的兜底
        logger.warning(f"msgpack allowlist unavailable, using default serde: {exc}")
        return serde


class PersistentMemorySaver(MemorySaver):
    """兼容 MemorySaver 的检查点存档器，把内存状态**增量**持久化到 SQLite。"""

    def __init__(self, path: Path, *, serde: Any | None = None) -> None:
        if serde is None:
            serde = _make_default_serde()
        super().__init__(serde=serde)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 图节点跑在线程池里（并发 worker 时有多个线程），所有落盘都由这把锁串行化；
        # 因此可以安全复用同一个连接（check_same_thread=False），省掉每次写的建连开销。
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._init_db()
        self._load()

    # ── 写入路径（增量）────────────────────────────────────────

    def put(self, config, checkpoint, metadata, new_versions):  # type: ignore[override]
        """LangGraph 每跑完一个节点回调这里落检查点。

        入参：config(定位 thread/ns/父checkpoint)、checkpoint(本步状态快照)、
             metadata(step/来源/哪个节点写的)、new_versions(本步变化的通道及新版本号)。
        只写本次新增的那一条 checkpoint 和 new_versions 涉及的 blob，不动历史数据。
        """
        with self._lock:
            result = super().put(config, checkpoint, metadata, new_versions)
            try:
                cfg = result["configurable"]
                self._save_checkpoint(
                    cfg["thread_id"], cfg["checkpoint_ns"], cfg["checkpoint_id"], new_versions
                )
            except Exception as exc:  # 落盘失败不能影响图执行，只告警
                logger.warning(f"检查点落盘失败（内存状态仍正常）: {exc}")
            return result

    def put_writes(self, config, writes, task_id, task_path: str = "") -> None:  # type: ignore[override]
        """暂存中间写入：并行节点各自的产出、或 interrupt 挂起时尚未并入 checkpoint 的写入。

        只重写该 checkpoint 对应的那一条 writes 行（内含若干 task 的写入），量很小。
        """
        with self._lock:
            super().put_writes(config, writes, task_id, task_path)
            try:
                cfg = config["configurable"]
                self._save_writes(
                    cfg["thread_id"], cfg.get("checkpoint_ns", ""), cfg["checkpoint_id"]
                )
            except Exception as exc:
                logger.warning(f"中间写入落盘失败（内存状态仍正常）: {exc}")

    def delete_thread(self, thread_id: str) -> None:
        with self._lock:
            super().delete_thread(thread_id)
            try:
                for table in ("cp_checkpoints", "cp_blobs", "cp_writes"):
                    self._conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
                self._conn.commit()
            except Exception as exc:
                logger.warning(f"删除线程检查点失败: {exc}")

    def has_thread(self, thread_id: str) -> bool:
        return bool(self.storage.get(thread_id))

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ── 内部：建表 / 单条落盘 / 加载 ──────────────────────────

    def _init_db(self) -> None:
        with self._lock:
            # WAL 模式：读写不互相阻塞，多 worker 并发落盘更顺
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(
                """
                -- 一步一行：storage[thread][ns][ckpt] 的值
                CREATE TABLE IF NOT EXISTS cp_checkpoints (
                    thread_id     TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    value         BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                );
                -- 一个通道版本一行：blobs[(thread,ns,channel,version)]，写一次不再改
                -- key_blob 保存原始 key 元组，因为 version 可能是 int/str，光存文本无法还原类型
                CREATE TABLE IF NOT EXISTS cp_blobs (
                    thread_id     TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    channel       TEXT NOT NULL,
                    version_txt   TEXT NOT NULL,
                    key_blob      BLOB NOT NULL,
                    value         BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, channel, version_txt)
                );
                -- 一个 checkpoint 一行：writes[(thread,ns,ckpt)] 整个内层 dict
                CREATE TABLE IF NOT EXISTS cp_writes (
                    thread_id     TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    value         BLOB NOT NULL,
                    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                );
                """
            )
            self._conn.commit()

    def _save_checkpoint(self, thread_id: str, ns: str, ckpt_id: str, new_versions) -> None:
        """写 1 条 checkpoint 行 + 本步变化通道的 blob 行。"""
        entry = self.storage.get(thread_id, {}).get(ns, {}).get(ckpt_id)
        if entry is None:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO cp_checkpoints"
            " (thread_id, checkpoint_ns, checkpoint_id, value) VALUES (?,?,?,?)",
            (thread_id, ns, ckpt_id, pickle.dumps(entry)),
        )
        blob_rows = []
        for channel, version in (new_versions or {}).items():
            key = (thread_id, ns, channel, version)
            if key in self.blobs:
                blob_rows.append(
                    (thread_id, ns, str(channel), str(version),
                     pickle.dumps(key), pickle.dumps(self.blobs[key]))
                )
        if blob_rows:
            self._conn.executemany(
                "INSERT OR REPLACE INTO cp_blobs"
                " (thread_id, checkpoint_ns, channel, version_txt, key_blob, value)"
                " VALUES (?,?,?,?,?,?)",
                blob_rows,
            )
        self._conn.commit()

    def _save_writes(self, thread_id: str, ns: str, ckpt_id: str) -> None:
        """重写该 checkpoint 的 writes 行（内层 dict 很小）。"""
        inner = self.writes.get((thread_id, ns, ckpt_id))
        if not inner:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO cp_writes"
            " (thread_id, checkpoint_ns, checkpoint_id, value) VALUES (?,?,?,?)",
            (thread_id, ns, ckpt_id, pickle.dumps(dict(inner))),
        )
        self._conn.commit()

    def _load(self) -> None:
        """进程启动时把磁盘上的检查点全部读回内存（只在启动时做一次）。"""
        with self._lock:
            if self._migrate_legacy():
                return  # 迁移过程已重建内存结构

            storage: Any = defaultdict(lambda: defaultdict(dict))
            blobs: dict = {}
            writes: Any = defaultdict(dict)
            try:
                for tid, ns, cid, value in self._conn.execute(
                    "SELECT thread_id, checkpoint_ns, checkpoint_id, value FROM cp_checkpoints"
                ):
                    storage[tid][ns][cid] = pickle.loads(value)
                for key_blob, value in self._conn.execute(
                    "SELECT key_blob, value FROM cp_blobs"
                ):
                    blobs[pickle.loads(key_blob)] = pickle.loads(value)
                for tid, ns, cid, value in self._conn.execute(
                    "SELECT thread_id, checkpoint_ns, checkpoint_id, value FROM cp_writes"
                ):
                    writes[(tid, ns, cid)] = pickle.loads(value)
            except Exception as exc:
                logger.warning(f"读取持久化检查点失败，按空状态启动: {exc}")
                return

            self.storage = storage
            self.blobs = blobs
            self.writes = writes
            if storage:
                logger.info(f"已从磁盘恢复 {len(storage)} 个任务的检查点")

    def _migrate_legacy(self) -> bool:
        """把旧的"三行全量快照"格式迁移到新表；无旧数据则返回 False。"""
        try:
            exists = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (_LEGACY_TABLE,)
            ).fetchone()
            if not exists:
                return False
            rows = dict(self._conn.execute(f"SELECT key, value FROM {_LEGACY_TABLE}"))
            if not rows:
                return False
            self.storage = _restore_storage(pickle.loads(rows.get("storage", b"")))
            self.writes = defaultdict(dict, pickle.loads(rows.get("writes", b"")) or {})
            self.blobs = dict(pickle.loads(rows.get("blobs", b"")) or {})
        except Exception as exc:
            logger.warning(f"旧格式检查点迁移失败，忽略旧数据: {exc}")
            return False

        # 全量写一次新表，然后丢弃旧表
        try:
            for tid, namespaces in self.storage.items():
                for ns, ckpts in namespaces.items():
                    for cid, entry in ckpts.items():
                        self._conn.execute(
                            "INSERT OR REPLACE INTO cp_checkpoints"
                            " (thread_id, checkpoint_ns, checkpoint_id, value) VALUES (?,?,?,?)",
                            (tid, ns, cid, pickle.dumps(entry)),
                        )
            for key, value in self.blobs.items():
                tid, ns, channel, version = key
                self._conn.execute(
                    "INSERT OR REPLACE INTO cp_blobs"
                    " (thread_id, checkpoint_ns, channel, version_txt, key_blob, value)"
                    " VALUES (?,?,?,?,?,?)",
                    (tid, ns, str(channel), str(version), pickle.dumps(key), pickle.dumps(value)),
                )
            for (tid, ns, cid), inner in self.writes.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO cp_writes"
                    " (thread_id, checkpoint_ns, checkpoint_id, value) VALUES (?,?,?,?)",
                    (tid, ns, cid, pickle.dumps(dict(inner))),
                )
            self._conn.execute(f"DROP TABLE {_LEGACY_TABLE}")
            self._conn.commit()
            logger.info("旧格式检查点已迁移为增量表结构")
        except Exception as exc:
            logger.warning(f"迁移写入新表失败（内存状态已恢复，不影响运行）: {exc}")
        return True


_checkpointer: PersistentMemorySaver | MemorySaver | None = None


def get_checkpoint_path() -> Path:
    settings = get_settings()
    return settings.checkpoint_dir / "langgraph_checkpoints.sqlite"


def get_checkpointer() -> PersistentMemorySaver | MemorySaver:
    """Return the process-wide checkpointer."""
    global _checkpointer
    if _checkpointer is None:
        try:
            _checkpointer = PersistentMemorySaver(get_checkpoint_path())
        except Exception as exc:  # pragma: no cover - last-resort fallback
            logger.warning(f"Persistent checkpointer unavailable, using MemorySaver: {exc}")
            _checkpointer = MemorySaver(serde=_make_default_serde())
    return _checkpointer


def reset_checkpointer_for_tests() -> None:
    """Drop the cached checkpointer so tests can simulate a process restart."""
    global _checkpointer
    if isinstance(_checkpointer, PersistentMemorySaver):
        _checkpointer.close()
    _checkpointer = None


def has_checkpoint(thread_id: str) -> bool:
    saver = get_checkpointer()
    if hasattr(saver, "has_thread"):
        return bool(saver.has_thread(thread_id))  # type: ignore[attr-defined]
    try:
        return saver.get_tuple({"configurable": {"thread_id": thread_id}}) is not None
    except Exception:
        return False


def _restore_storage(value: dict | None):
    """把普通嵌套 dict 还原成 MemorySaver 期望的 defaultdict 结构。"""
    restored: Any = defaultdict(lambda: defaultdict(dict))
    for thread_id, namespaces in (value or {}).items():
        restored[thread_id] = defaultdict(dict, namespaces)
    return restored
