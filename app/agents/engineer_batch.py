"""Engineer 分批生成技术设计。

为什么要分批：原来 Engineer 一次调用就要吐出【架构 + 服务 + 全部 API + 全部数据表
+ 代码骨架】。功能模块一多，输出动辄上万 token，会撞到模型的单次输出上限
（如 deepseek-chat 硬顶 8192）——一旦被截断，function_calling 拿不到完整工具调用就
返回 None，只能重试或降级成规则兜底，既慢又差。

改成三步，每步的输出都很小、稳定可控：
  1. 架构骨架：架构概述/风格、服务划分、技术风险、代码骨架、数据库类型与表间关系思路
  2. 按模块分批：每批只处理 N 个功能模块，只产出这几个模块的 API 端点与数据表
  3. 合并成完整的 TechnicalDesign

附带好处：每批 prompt 都明确告知"本批要覆盖哪几个模块"，覆盖率天然更高，
不用再靠"生成完发现缺 → 反馈重来"的 ReAct 循环去补。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.core.llm import get_structured_llm
from app.core.logger import logger
from app.schemas.design import APIEndpoint, CodeScaffold, DBTable, ServiceComponent, TechRisk

# 并行批次上限：再高会同时打太多路 LLM 请求，容易触发服务商限流；
# 注意这是【单个任务内部】的并发，还要叠加 WORKER_CONCURRENCY 个任务同时跑。
_MAX_PARALLEL_BATCHES = 3


# ── 分步输出的 Schema（都是完整 TechnicalDesign 的子集）──────────────────

class ArchitectureSkeleton(BaseModel):
    """第一步产出：不含 API/DB 明细的架构骨架。"""

    architecture_overview: str = Field(..., description="架构概述")
    architecture_style: str = Field("", description="架构风格（微服务 / 单体 / Serverless 等）")
    services: list[ServiceComponent] = Field(default_factory=list, description="服务组件列表")
    tech_risks: list[TechRisk] = Field(default_factory=list, description="技术风险列表")
    code_scaffold: CodeScaffold = Field(default_factory=CodeScaffold, description="代码骨架建议")
    database_type: str = Field("PostgreSQL", description="数据库类型")
    db_relationships: list[str] = Field(
        default_factory=list, description="预期的表间关系描述（如 用户1:N订单）"
    )


class ModuleBatchDesign(BaseModel):
    """每一批产出：仅本批功能模块对应的 API 端点与数据表。"""

    api_endpoints: list[APIEndpoint] = Field(default_factory=list, description="本批模块的 API 端点")
    db_tables: list[DBTable] = Field(default_factory=list, description="本批模块的数据库表")


# ── Prompts ────────────────────────────────────────────────────────────

SKELETON_SYSTEM = """你是一名资深技术架构师。现在只需要输出**架构骨架**，不要输出任何 API 端点和数据库表明细
（那些会在后续分批细化，这一步输出它们只会浪费篇幅）。

请给出：
- architecture_overview：架构概述，2-4 句说清分层与关键取舍
- architecture_style：架构风格（单体 / 模块化单体 / 微服务 / Serverless 等），并与需求规模匹配
- services：服务/模块划分，每个含 name、responsibility、tech_stack；要能覆盖 PRD 的全部功能模块
- tech_risks：3-6 条真实风险，每条含 risk、impact、mitigation
- code_scaffold：目录结构（最多 8 条）、关键文件（最多 6 个，skeleton 只写类/函数签名，不写完整实现）、核心依赖
- database_type：选定的数据库类型
- db_relationships：预期的主要表间关系（如"用户 1:N 订单"），只写关系不写建表明细

注意：对支付、库存、审核、隐私等高风险领域，要在风险与缓解措施中体现幂等、一致性、
权限、审计、加密等横切关注点。"""

SKELETON_USER = """请基于以下 PRD 与需求澄清结果，输出技术架构骨架。

## PRD 文档
{prd_doc}

## 需求澄清结果
{clarified_requirement}

{extra_context}

{few_shot_examples}

{task_memory}

{memory_context}"""


BATCH_SYSTEM = """你是一名资深技术架构师，正在为一个已确定架构的系统细化**指定功能模块**的接口与数据表。

严格要求：
1. **只处理本次给定的功能模块**，不要涉及其他模块（其他模块由别的批次负责，重复只会冲突）。
2. 每个功能模块产出 **2-4 个 API 端点** 和 **1-2 张数据库表**。
3. API 路径必须使用**领域特定术语**，禁止只给 /items、/data、/list 这类通用路径。
   路径参数用单层花括号，例如 /courses/{course_id}/enroll、/assignments/{id}/grade。
   注意：本条说明不经过字符串格式化，请勿输出双花括号 {{ }}。
4. 每个 API 与每张表的 `related_features` 字段，必须填写它所覆盖的**功能模块原名**（照抄给定名称），
   这是后续覆盖率校验的依据。
5. 数据库表要包含业务关键字段：主键、业务字段、状态枚举、时间戳；需要幂等/审计的场景补上
   幂等键与审计字段；并给出必要索引。
6. request_body / response_body 各用一句话概括要点，不要罗列完整字段清单或示例 JSON。
7. 表和端点要与已确定的服务划分对应，不要另起一套架构。"""

BATCH_USER = """## 已确定的架构（请遵循，不要改动）
架构风格：{architecture_style}
服务划分：{services}
数据库类型：{database_type}

## 本批需要细化的功能模块（只处理这些）
{batch_modules}

## 已经设计过的表名与端点路径（避免重复）
{existing_summary}

{extra_context}

请输出本批模块对应的 API 端点与数据库表。"""


# ── 主流程 ─────────────────────────────────────────────────────────────

def generate_design_in_batches(
    prd_doc: dict,
    clarified_req: dict,
    *,
    extra_context: str = "",
    few_shot_text: str = "",
    task_memory_text: str = "",
    memory_context: str = "",
    batch_size: int = 4,
) -> tuple[dict, dict]:
    """分批生成完整技术设计。

    返回 (design_dict, stats)：
      design_dict —— 与 TechnicalDesign.model_dump() 结构一致，可直接写入状态
      stats       —— {"llm_calls", "batches", "failed_batches"} 供指标记录
    """
    modules = [m for m in (prd_doc.get("feature_modules") or []) if isinstance(m, dict)]

    # 第一步：架构骨架
    skeleton = _generate_skeleton(
        prd_doc, clarified_req, extra_context, few_shot_text, task_memory_text, memory_context
    )
    llm_calls = 1

    design: dict[str, Any] = {
        "architecture_overview": skeleton.architecture_overview,
        "architecture_style": skeleton.architecture_style,
        "services": [s.model_dump() for s in skeleton.services],
        "tech_risks": [r.model_dump() for r in skeleton.tech_risks],
        "code_scaffold": skeleton.code_scaffold.model_dump(),
        "api_endpoints": [],
        "db_schema": {
            "database_type": skeleton.database_type,
            "tables": [],
            "relationships": list(skeleton.db_relationships),
        },
    }

    # 第二步：按模块分批细化 API/DB —— 各批之间互不依赖，并行跑可把耗时压到约 1/N。
    # 代价是批次之间看不到对方的产出，可能撞表名/路径，所以合并后统一去重。
    batches = [modules[i : i + batch_size] for i in range(0, len(modules), batch_size)]
    results: list[ModuleBatchDesign | None] = [None] * len(batches)

    if len(batches) == 1:
        results[0] = _generate_batch(batches[0], design, skeleton, extra_context)
    else:
        # 线程池：底层是阻塞的 HTTP 调用，用线程并发即可（不受 GIL 影响）
        with ThreadPoolExecutor(max_workers=min(len(batches), _MAX_PARALLEL_BATCHES)) as pool:
            futures = {
                pool.submit(_generate_batch, batch, design, skeleton, extra_context): i
                for i, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                i = futures[future]
                names = ", ".join(str(m.get("name", "")) for m in batches[i])
                try:
                    results[i] = future.result()
                except Exception as exc:  # 单批失败不影响其他批
                    logger.warning(f"第 {i + 1} 批生成异常: {str(exc)[:160]}", extra={"node": "engineer"})
                    results[i] = None
                logger.info(
                    f"Engineer 分批生成 第 {i + 1}/{len(batches)} 批完成：{names}"
                    f"{'（失败）' if results[i] is None else ''}",
                    extra={"node": "engineer"},
                )

    failed = 0
    for i, part in enumerate(results):
        llm_calls += 1
        if part is None:
            failed += 1
            logger.warning(
                f"Engineer 第 {i + 1} 批无有效产出，该批模块将交给规则兜底补齐",
                extra={"node": "engineer"},
            )
            continue
        design["api_endpoints"].extend(e.model_dump() for e in part.api_endpoints)
        design["db_schema"]["tables"].extend(t.model_dump() for t in part.db_tables)

    _normalize_and_dedupe(design)
    return design, {"llm_calls": llm_calls, "batches": len(batches), "failed_batches": failed}


def _normalize_and_dedupe(design: dict) -> None:
    """就地清理合并结果：修正路径写法、按 (method,path) 与表名去重。

    并行批次之间互相看不见，可能产出重名表或同路径端点；另外模型偶尔会把路径参数
    写成双花括号 `{{id}}`（提示词里已明确要求单层，这里再兜一道）。
    """
    endpoints = design.get("api_endpoints") or []
    seen_ep: set[tuple[str, str]] = set()
    clean_eps = []
    for ep in endpoints:
        path = str(ep.get("path", "")).replace("{{", "{").replace("}}", "}")
        ep["path"] = path
        key = (str(ep.get("method", "")).upper(), path)
        if key in seen_ep:
            continue
        seen_ep.add(key)
        clean_eps.append(ep)
    design["api_endpoints"] = clean_eps

    tables = (design.get("db_schema") or {}).get("tables") or []
    seen_tbl: set[str] = set()
    clean_tbls = []
    for tbl in tables:
        name = str(tbl.get("table_name", "")).strip()
        if name and name in seen_tbl:
            continue
        seen_tbl.add(name)
        clean_tbls.append(tbl)
    design["db_schema"]["tables"] = clean_tbls

    dropped_ep = len(endpoints) - len(clean_eps)
    dropped_tbl = len(tables) - len(clean_tbls)
    if dropped_ep or dropped_tbl:
        logger.info(
            f"分批结果去重：移除 {dropped_ep} 个重复端点、{dropped_tbl} 张重复表",
            extra={"node": "engineer"},
        )


def _generate_skeleton(
    prd_doc: dict,
    clarified_req: dict,
    extra_context: str,
    few_shot_text: str,
    task_memory_text: str,
    memory_context: str,
) -> ArchitectureSkeleton:
    """生成架构骨架；失败时返回一个最小可用骨架，保证后续批次还能继续。"""
    llm = get_structured_llm(ArchitectureSkeleton)
    messages = [
        SystemMessage(content=SKELETON_SYSTEM),
        HumanMessage(
            content=SKELETON_USER.format(
                prd_doc=json.dumps(prd_doc, ensure_ascii=False, indent=2),
                clarified_requirement=json.dumps(clarified_req, ensure_ascii=False, indent=2),
                extra_context=extra_context,
                few_shot_examples=few_shot_text,
                task_memory=task_memory_text,
                memory_context=memory_context,
            )
        ),
    ]
    for attempt in range(1, 3):
        try:
            result = llm.invoke(messages)
        except Exception as exc:
            logger.warning(f"架构骨架生成异常（第 {attempt} 次）: {str(exc)[:160]}")
            result = None
        if result is not None:
            return result
    logger.error("架构骨架生成失败，使用最小骨架继续分批细化", extra={"node": "engineer"})
    return ArchitectureSkeleton(
        architecture_overview="（架构骨架生成失败，以下内容由分批细化与规则补齐组成）",
        architecture_style="模块化单体",
    )


def _generate_batch(
    batch: list[dict],
    design: dict,
    skeleton: ArchitectureSkeleton,
    extra_context: str,
) -> ModuleBatchDesign | None:
    """生成一批模块的 API/DB；失败返回 None（由调用方交给规则兜底）。"""
    llm = get_structured_llm(ModuleBatchDesign)
    module_lines = []
    for m in batch:
        subs = m.get("sub_features") or []
        sub_txt = f"；子功能：{', '.join(str(s) for s in subs[:6])}" if subs else ""
        module_lines.append(
            f"- {m.get('name', '')}：{str(m.get('description', ''))[:200]}{sub_txt}"
        )

    messages = [
        SystemMessage(content=BATCH_SYSTEM),
        HumanMessage(
            content=BATCH_USER.format(
                architecture_style=skeleton.architecture_style or "未指定",
                services=", ".join(s.name for s in skeleton.services) or "未划分",
                database_type=skeleton.database_type,
                batch_modules="\n".join(module_lines),
                existing_summary=_summarize_existing(design),
                extra_context=extra_context,
            )
        ),
    ]
    for attempt in range(1, 3):
        try:
            result = llm.invoke(messages)
        except Exception as exc:
            logger.warning(f"分批生成异常（第 {attempt} 次）: {str(exc)[:160]}")
            result = None
        if result is not None:
            return result
    return None


def _summarize_existing(design: dict) -> str:
    """把已产出的表名/路径列给下一批，避免重复设计。"""
    paths = [str(e.get("path", "")) for e in (design.get("api_endpoints") or [])]
    tables = [str(t.get("table_name", "")) for t in ((design.get("db_schema") or {}).get("tables") or [])]
    if not paths and not tables:
        return "（这是第一批，暂无已有设计）"
    return (
        f"已有端点：{', '.join(paths[-25:]) or '无'}\n"
        f"已有表名：{', '.join(tables[-25:]) or '无'}"
    )
