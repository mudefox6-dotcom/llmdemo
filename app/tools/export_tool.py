"""Export Tool —— 将最终结果导出为 Markdown 文件。

V1 仅支持 Markdown 导出，后续可扩展 PDF / DOCX / Notion。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.core.config import get_settings
from app.core.logger import logger
from app.tools.scaffold_generator import generate_project
from app.tools.template_tool import render_template


def export_deliverable(state_data: dict, task_id: str = "") -> dict[str, str]:
    """将交付产物导出为 Markdown 文件。

    Args:
        state_data: 包含所有产物的状态字典
        task_id: 任务 ID

    Returns:
        文件名到文件路径的映射。
    """
    settings = get_settings()
    output_dir = settings.output_dir / (task_id or "default")
    output_dir.mkdir(parents=True, exist_ok=True)

    exported = {}
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # 1. 导出 PRD
    prd_doc = state_data.get("prd_doc")
    if prd_doc:
        try:
            content = render_template("prd", prd_doc)
            path = output_dir / f"prd_{timestamp}.md"
            path.write_text(content, encoding="utf-8")
            exported["prd"] = str(path)
        except Exception as e:
            logger.error(f"导出 PRD 失败: {e}")
            # 回退为 JSON 导出
            path = output_dir / f"prd_{timestamp}.json"
            path.write_text(json.dumps(prd_doc, ensure_ascii=False, indent=2), encoding="utf-8")
            exported["prd"] = str(path)

    # 2. 导出技术设计
    tech_design = state_data.get("technical_design")
    if tech_design:
        try:
            content = render_template("technical_design", tech_design)
            path = output_dir / f"technical_design_{timestamp}.md"
            path.write_text(content, encoding="utf-8")
            exported["technical_design"] = str(path)
        except Exception as e:
            logger.error(f"导出技术设计失败: {e}")
            path = output_dir / f"technical_design_{timestamp}.json"
            path.write_text(json.dumps(tech_design, ensure_ascii=False, indent=2), encoding="utf-8")
            exported["technical_design"] = str(path)

    # 3. 导出评审报告
    review_result = state_data.get("review_result")
    if review_result:
        try:
            content = render_template("review_report", review_result)
            path = output_dir / f"review_report_{timestamp}.md"
            path.write_text(content, encoding="utf-8")
            exported["review_report"] = str(path)
        except Exception as e:
            logger.error(f"导出评审报告失败: {e}")
            path = output_dir / f"review_report_{timestamp}.json"
            path.write_text(json.dumps(review_result, ensure_ascii=False, indent=2), encoding="utf-8")
            exported["review_report"] = str(path)

    # 4. 导出需求澄清结果
    clarified = state_data.get("clarified_requirement")
    if clarified:
        path = output_dir / f"requirement_{timestamp}.json"
        path.write_text(json.dumps(clarified, ensure_ascii=False, indent=2), encoding="utf-8")
        exported["clarified_requirement"] = str(path)

    # 5. 生成可运行的骨架项目（确定性模板，不调 LLM）
    # 交付物不该只有文档：技术设计里已有结构化的 API 端点与数据表，
    # 据此生成一个能 `docker compose up` 启动、/docs 可见全部端点的 FastAPI 项目，
    # 开发者接手后填业务逻辑即可，不必从零搭架子。
    if tech_design:
        try:
            info = generate_project(
                tech_design,
                prd_doc or {},
                output_dir / "project",
                make_zip=True,
            )
            exported["project_dir"] = info["project_dir"]
            if info.get("zip_path"):
                exported["project_zip"] = info["zip_path"]
            logger.info(
                f"骨架项目已生成：{info['file_count']} 个文件、"
                f"{info['endpoint_count']} 个端点、{info['table_count']} 张表",
                extra={"task_id": task_id},
            )
        except Exception as e:
            # 骨架生成失败不能影响文档交付
            logger.error(f"生成骨架项目失败: {e}", extra={"task_id": task_id})

    # 6. 导出完整交付包 JSON
    path = output_dir / f"full_deliverable_{timestamp}.json"
    path.write_text(json.dumps(state_data, ensure_ascii=False, indent=2), encoding="utf-8")
    exported["full_deliverable"] = str(path)

    logger.info(f"导出完成，共 {len(exported)} 个文件", extra={"task_id": task_id})
    return exported
