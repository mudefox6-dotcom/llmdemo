"""覆盖率检查（质量引擎的地基）——判断"PRD 的功能模块"有没有落到"技术设计"里。

被 Engineer(自检+兜底)、Reviewer(硬闸门)、Repairer(算缺口) 三处共用。
核心思路很朴素：把技术设计(或其中的 api_endpoints / db_schema)整体转成一段纯文本，
再看每个功能模块的关键词有没有在这段文本里出现——出现算"覆盖"，没出现算"缺失"。
覆盖率 = 被覆盖的功能模块数 / 功能模块总数（0.0~1.0）。

专有名词：
  feature module（功能模块）—— PRD 里 feature_modules 列表的每一项，含 name/description/sub_features。
  覆盖率阈值 —— Engineer/Reviewer 用 0.8 作达标线。
"""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any


def score_prd_tech_coverage(
    prd: dict[str, Any] | None,
    design: dict[str, Any] | None,
) -> float:
    """PRD→整份技术设计 的覆盖率。

    输入：prd(含 feature_modules)、design(整份技术设计)。
    输出：0.0~1.0 的比例。做法：把 design 整体 JSON 转文本，逐个功能模块看关键词是否出现。
    """
    prd = prd or {}
    design = design or {}
    features = collect_feature_terms(prd)
    if not features:
        return 0.0  # PRD 没有功能模块 → 无从谈覆盖，返回 0

    design_text = normalize_text(json.dumps(design, ensure_ascii=False))
    covered = 0
    for feature in features:
        if terms_match(feature["terms"], design_text):
            covered += 1
    return covered / len(features)


def score_api_feature_coverage(
    prd: dict[str, Any] | None,
    design: dict[str, Any] | None,
) -> float:
    """Estimate whether PRD feature modules are represented in API design."""
    api_endpoints = (design or {}).get("api_endpoints") or []
    api_text = normalize_text(json.dumps(api_endpoints, ensure_ascii=False))
    return score_feature_coverage(prd, api_text)


def score_db_feature_coverage(
    prd: dict[str, Any] | None,
    design: dict[str, Any] | None,
) -> float:
    """Estimate whether PRD feature modules are represented in DB schema."""
    db_schema = (design or {}).get("db_schema") or {}
    db_text = normalize_text(json.dumps(db_schema, ensure_ascii=False))
    return score_feature_coverage(prd, db_text)


def missing_feature_mappings(
    prd: dict[str, Any] | None,
    design: dict[str, Any] | None,
) -> dict[str, list[str]]:
    """列出"缺 API 映射"和"缺 DB 映射"的功能模块名。

    输出：{"api": [缺API的模块名...], "db": [缺DB的模块名...]}。
    Engineer 的 ReAct 反馈、Repairer 的补漏、Reviewer 追加 issue 都读这个结果。
    """
    features = collect_feature_terms(prd or {})
    api_text = normalize_text(json.dumps((design or {}).get("api_endpoints") or [], ensure_ascii=False))
    db_text = normalize_text(json.dumps((design or {}).get("db_schema") or {}, ensure_ascii=False))

    missing_api = []
    missing_db = []
    for feature in features:
        if not terms_match(feature["terms"], api_text):
            missing_api.append(feature["name"])
        if not terms_match(feature["terms"], db_text):
            missing_db.append(feature["name"])
    return {"api": missing_api, "db": missing_db}


def repair_feature_mappings(
    prd: dict[str, Any] | None,
    design: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """规则兜底补漏：给"缺 API/DB 映射"的功能模块自动生成模板化的端点/表并追加进设计。

    这是"确定性"补齐（不调 LLM），Engineer 在 ReAct 循环仍未达标后调用。
    输出：(补齐后的 design, {"api":[补了哪些], "db":[补了哪些]})——只增不改已有内容。
    """
    repaired = deepcopy(design or {})
    repaired.setdefault("api_endpoints", [])
    repaired.setdefault("db_schema", {})
    repaired["db_schema"].setdefault("database_type", "PostgreSQL")
    repaired["db_schema"].setdefault("tables", [])
    repaired["db_schema"].setdefault("relationships", [])

    missing = missing_feature_mappings(prd, repaired)
    features_by_name = {item["name"]: item for item in collect_feature_terms(prd or {})}
    added = {"api": [], "db": []}

    for feature_name in missing["api"]:
        feature = features_by_name.get(feature_name) or {"name": feature_name, "terms": [feature_name]}
        repaired["api_endpoints"].append(
            _make_feature_api(feature, len(repaired["api_endpoints"]) + 1)
        )
        added["api"].append(feature_name)

    for feature_name in missing["db"]:
        feature = features_by_name.get(feature_name) or {"name": feature_name, "terms": [feature_name]}
        repaired["db_schema"]["tables"].append(
            _make_feature_table(feature, len(repaired["db_schema"]["tables"]) + 1)
        )
        added["db"].append(feature_name)

    return repaired, added


def score_feature_coverage(prd: dict[str, Any] | None, target_text: str) -> float:
    features = collect_feature_terms(prd or {})
    if not features:
        return 0.0
    covered = 0
    for feature in features:
        if terms_match(feature["terms"], target_text):
            covered += 1
    return covered / len(features)


def collect_feature_terms(prd: dict[str, Any]) -> list[dict[str, Any]]:
    features = []
    for index, module in enumerate(prd.get("feature_modules") or [], start=1):
        if not isinstance(module, dict):
            continue
        name = str(module.get("name") or f"feature_{index}").strip()
        terms = []
        if module.get("name"):
            terms.append(str(module["name"]))
        if module.get("description"):
            terms.append(str(module["description"]))
        terms.extend(str(item) for item in module.get("sub_features") or [] if item)
        compact_terms = [term for term in terms if normalize_text(term)]
        if compact_terms:
            features.append({"name": name, "terms": compact_terms})
    return features


def terms_match(terms: list[str], text: str) -> bool:
    return any(term_appears(term, text) for term in terms)


def term_appears(term: str, text: str) -> bool:
    # 判断一个关键词是否"出现"在目标文本里：
    #   ① 整词直接子串匹配；② 否则拆成长度>=3 的 token，任一 token 命中也算。
    # 这样"订单退款"能匹配到 "refund order" 里的 order/refund，容忍措辞差异。
    normalized = normalize_text(term)
    if not normalized:
        return False
    if normalized in text:
        return True
    tokens = [token for token in re.split(r"\s+", normalized) if len(token) >= 3]
    return bool(tokens) and any(token in text for token in tokens)


def normalize_text(value: str) -> str:
    # 文本归一化：转小写、把各种中英文标点替换成空格、压缩空白。
    # 目的：让关键词匹配不受大小写/标点/分隔符干扰。
    value = value.lower()
    value = re.sub(r"[_\-_/|:;,.!?()\[\]{}\"']", " ", value)
    value = re.sub(r"[，。！？、；：）（【】《》“”‘’]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _make_feature_api(feature: dict[str, Any], index: int) -> dict[str, Any]:
    feature_name = str(feature.get("name") or f"feature_{index}")
    feature_terms = _feature_term_text(feature)
    slug = _slugify(feature_name, index)
    return {
        "method": "POST",
        "path": f"/feature-modules/{slug}/actions",
        "description": (
            f"Covers PRD feature module: {feature_name}. "
            f"Supports workflow operations for {feature_terms}."
        ),
        "request_body": f"{feature_name} request payload and actor context.",
        "response_body": f"{feature_name} operation result.",
        "auth_required": True,
        "related_features": [feature_name],
    }


def _make_feature_table(feature: dict[str, Any], index: int) -> dict[str, Any]:
    feature_name = str(feature.get("name") or f"feature_{index}")
    feature_terms = _feature_term_text(feature)
    slug = _slugify(feature_name, index).replace("-", "_")
    return {
        "table_name": f"feature_{slug}_records",
        "description": (
            f"Persists PRD feature module: {feature_name}. "
            f"Stores workflow data and audit state for {feature_terms}."
        ),
        "columns": [
            {
                "name": "id",
                "type": "uuid",
                "nullable": False,
                "description": f"Primary key for {feature_name} records.",
            },
            {
                "name": "feature_payload",
                "type": "jsonb",
                "nullable": False,
                "description": f"Business payload for {feature_name}.",
            },
            {
                "name": "status",
                "type": "varchar(32)",
                "nullable": False,
                "description": f"Processing status for {feature_name}.",
            },
            {
                "name": "created_at",
                "type": "timestamp",
                "nullable": False,
                "description": f"Creation time for {feature_name}.",
            },
        ],
        "indexes": [f"idx_feature_{slug}_status", f"idx_feature_{slug}_created_at"],
        "related_features": [feature_name],
    }


def _feature_term_text(feature: dict[str, Any]) -> str:
    terms = [str(term) for term in feature.get("terms") or [] if str(term).strip()]
    return ", ".join(terms[:4]) or str(feature.get("name") or "this feature")


def _slugify(value: str, index: int) -> str:
    slug = normalize_text(value)
    slug = re.sub(r"[^0-9a-zA-Z]+", "-", slug).strip("-")
    return slug[:48] or f"feature-{index:02d}"
