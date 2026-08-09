"""把技术设计确定性地生成为一个**可运行**的 FastAPI 骨架项目。

为什么不让 LLM 直接写整个项目：一个十几模块的系统意味着几百个文件、构建配置、
依赖版本对齐，LLM 逐个生成既慢又极易编译不过。而技术设计里已经有**结构化的
api_endpoints 与 db_schema**，从它们生成可运行代码本质上是模板套用——
确定性、秒级完成、不耗 token、不会语法错误。

产出的项目"能跑"的定义：
  docker compose up  →  服务启动、数据库建表完成
  打开 /docs         →  技术设计里的全部端点都在，可点开查看
  调用任一端点        →  返回 501 Not Implemented + 该接口的设计说明
开发者接手后逐个填业务逻辑即可，不必从零搭架子。

技术栈固定为 FastAPI + SQLAlchemy + PostgreSQL（与本项目一致，便于验证）。
若要支持其他栈，在 _render_* 层加一套 renderer 即可，数据提取逻辑可复用。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from app.core.logger import logger

# ── SQL 类型 → (SQLAlchemy 类型, 需要的 import) ───────────────────────────
# 只覆盖技术设计里实际出现的类型，未知类型统一退到 String(255) 并在注释里标注原类型。
_TYPE_MAP: list[tuple[str, str]] = [
    (r"^uuid$", "String(36)"),
    (r"^varchar\((\d+)\)$", "String({0})"),
    (r"^varchar\((\d+)\)\[\]$", "JSON"),          # 数组列用 JSON 承载，跨库更稳
    (r"^char\((\d+)\)$", "String({0})"),
    (r"^text$", "Text"),
    (r"^(bigint|bigserial)$", "BigInteger"),
    (r"^(int|integer|serial)$", "Integer"),
    (r"^smallint$", "Integer"),
    (r"^(decimal|numeric)\(([\d,\s]+)\)$", "Numeric({1})"),
    (r"^(decimal|numeric)$", "Numeric"),
    (r"^(float|double precision|real)$", "Float"),
    (r"^(bool|boolean)$", "Boolean"),
    (r"^(timestamp|timestamptz|datetime)$", "DateTime"),
    (r"^date$", "Date"),
    (r"^time$", "Time"),
    (r"^(json|jsonb)$", "JSON"),
]

_SA_IMPORTS = (
    "from sqlalchemy import (BigInteger, Boolean, Column, Date, DateTime, Float, "
    "Integer, JSON, Numeric, String, Text, Time)"
)

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def generate_project(
    technical_design: dict,
    prd_doc: dict | None = None,
    output_dir: Path | str = ".",
    *,
    make_zip: bool = True,
) -> dict[str, Any]:
    """生成骨架项目。

    Args:
        technical_design: Engineer 产出的技术设计（需含 api_endpoints / db_schema）
        prd_doc: PRD，仅用于取产品名写进 README
        output_dir: 项目根目录（会被创建；已存在同名目录时先清空重建）
        make_zip: 是否额外打一个 zip 便于下载

    Returns:
        {"project_dir", "zip_path", "file_count", "endpoint_count", "table_count", "routers"}
    """
    design = technical_design or {}
    tables = ((design.get("db_schema") or {}).get("tables")) or []
    endpoints = [e for e in (design.get("api_endpoints") or []) if isinstance(e, dict)]

    root = Path(output_dir)
    if root.exists():
        # 重新生成时清掉上一次的残留。ignore_errors 是必要的：Windows 上若项目里有
        # 正在被占用的文件（__pycache__、跑起来的服务持有的 data.db），rmtree 会抛
        # "Device or resource busy"。清不掉也没关系——下面会逐个覆盖写我们关心的文件。
        shutil.rmtree(root, ignore_errors=True)
    (root / "app" / "api" / "routers").mkdir(parents=True, exist_ok=True)
    (root / "app" / "core").mkdir(parents=True, exist_ok=True)
    (root / "app" / "db").mkdir(parents=True, exist_ok=True)
    (root / "migrations").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)

    product_name = str((prd_doc or {}).get("product_name") or "Generated Service").strip()
    groups = _group_endpoints(endpoints)

    files: dict[str, str] = {
        "README.md": _render_readme(product_name, design, groups, tables),
        "requirements.txt": _render_requirements(),
        "requirements-postgres.txt": _render_requirements_postgres(),
        "Dockerfile": _render_dockerfile(),
        "docker-compose.yml": _render_compose(),
        ".env.example": _render_env_example(),
        ".gitignore": "__pycache__/\n*.pyc\n.env\n.venv/\n",
        "app/__init__.py": "",
        "app/main.py": _render_main(product_name, design, groups),
        "app/core/__init__.py": "",
        "app/core/config.py": _render_config(product_name),
        "app/db/__init__.py": "",
        "app/db/base.py": _render_db_base(),
        "app/db/models.py": _render_models(tables),
        "app/api/__init__.py": "",
        "app/api/routers/__init__.py": "",
        "migrations/001_init.sql": _render_init_sql(tables),
        "tests/test_smoke.py": _render_smoke_test(groups),
        # 通用 CRUD：由 db_schema 直接驱动，**真能读写数据库**（不是 501 占位）。
        # 这样前端的管理页面是活的：能新增、能列表、能改、能删。
        "app/api/crud.py": _render_crud_router(tables),
    }
    for slug, eps in groups.items():
        files[f"app/api/routers/{slug}.py"] = _render_router(slug, eps)

    # ── 前端骨架 ──────────────────────────────────────────────
    # 只给后端的话，产品验收方看不到任何界面（/docs 是给开发者的）。
    # 这里按后端路由分组一一生成页面，每页列出该组接口并可"试调用"，
    # 从而能真正验证前后端链路是通的（当前会返回 501，但请求确实打到了后端）。
    files.update(_render_web(product_name, prd_doc or {}, groups, _table_meta(tables)))

    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    zip_path = ""
    if make_zip:
        # make_archive 的 base_name 不能带 .zip 后缀
        archive = shutil.make_archive(str(root), "zip", root_dir=str(root))
        zip_path = str(archive)

    result = {
        "project_dir": str(root),
        "zip_path": zip_path,
        "file_count": len(files),
        "endpoint_count": len(endpoints),
        "table_count": len(tables),
        "routers": sorted(groups.keys()),
    }
    logger.info(
        "骨架项目生成完成",
        extra={"file_count": len(files), "endpoints": len(endpoints), "tables": len(tables)},
    )
    return result


# ── 数据整理 ─────────────────────────────────────────────────────────────

def _group_endpoints(endpoints: list[dict]) -> dict[str, list[dict]]:
    """按 URL 路径首段把端点分组，每组一个 router 文件。

    用路径首段（如 /courses/{id}/enroll → courses）而不是 related_features，
    因为后者是中文模块名（"商品管理"），做不出合法的 ASCII 文件名/标识符。
    """
    groups: dict[str, list[dict]] = {}
    for ep in endpoints:
        raw = str(ep.get("path") or "").strip()
        if not raw.startswith("/"):
            raw = "/" + raw
        segs = [s for s in raw.strip("/").split("/") if s]
        first = next((s for s in segs if not s.startswith("{")), "")
        slug = _py_ident(first) or "misc"
        groups.setdefault(slug, []).append(ep)
    return groups


def _py_ident(text: str) -> str:
    """转成合法的 Python 标识符片段；无 ASCII 可用时返回空串由调用方兜底。"""
    cleaned = re.sub(r"[^0-9a-zA-Z_]+", "_", str(text)).strip("_").lower()
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"r_{cleaned}" if cleaned else ""
    return cleaned


def _path_params(path: str) -> list[str]:
    """提取路径参数名，如 /courses/{course_id}/lessons/{id} → [course_id, id]"""
    return [_py_ident(m) or "param" for m in re.findall(r"\{([^{}]+)\}", path)]


def _sa_type(sql_type: str) -> str:
    """SQL 类型 → SQLAlchemy 类型表达式；未知类型退到 String(255)。"""
    t = str(sql_type or "").strip().lower()
    for pattern, template in _TYPE_MAP:
        m = re.match(pattern, t)
        if m:
            groups = [g for g in m.groups() if g is not None]
            try:
                return template.format(*groups) if "{" in template else template
            except (IndexError, KeyError):
                return template.split("(")[0]
    return "String(255)"


# ── 各文件渲染 ───────────────────────────────────────────────────────────

def _render_router(slug: str, endpoints: list[dict]) -> str:
    lines = [
        '"""自动生成的路由骨架。',
        "",
        "每个端点都按技术设计声明了方法、路径与参数，当前统一返回 501，",
        "把 raise HTTPException(501 ...) 换成真实业务逻辑即可。",
        '"""',
        "",
        "from fastapi import APIRouter, HTTPException, status",
        "",
        f'router = APIRouter(prefix="/{slug}", tags=["{slug}"])',
        "",
    ]
    used: set[str] = set()
    for idx, ep in enumerate(endpoints, 1):
        method = str(ep.get("method") or "GET").upper()
        if method not in _HTTP_METHODS:
            method = "GET"
        path = str(ep.get("path") or "/").strip()
        if not path.startswith("/"):
            path = "/" + path
        # router 已带 prefix=/slug，这里去掉路径里重复的首段
        segs = [s for s in path.strip("/").split("/") if s]
        if segs and _py_ident(segs[0]) == slug:
            segs = segs[1:]
        sub_path = "/" + "/".join(segs) if segs else ""

        params = _path_params(path)
        # 函数名去重：同名会覆盖前一个定义
        base = _py_ident("_".join([method.lower()] + [s for s in segs if not s.startswith("{")])) or f"{method.lower()}_root"
        name = base
        n = 2
        while name in used:
            name = f"{base}_{n}"
            n += 1
        used.add(name)

        desc = str(ep.get("description") or "").replace('"""', "'''").strip()
        req = str(ep.get("request_body") or "").strip()
        resp = str(ep.get("response_body") or "").strip()
        auth = "需要认证" if ep.get("auth_required") else "无需认证"
        feats = ", ".join(str(f) for f in (ep.get("related_features") or []))

        args = ", ".join(f"{p}: str" for p in params)
        lines += [
            f'@router.{method.lower()}("{sub_path or "/"}", summary={json.dumps(desc[:80] or name, ensure_ascii=False)})',
            f"async def {name}({args}):",
            '    """' + (desc or "（技术设计未提供描述）"),
            "",
            f"    覆盖功能模块：{feats or '未标注'}",
            f"    认证要求：{auth}",
            f"    请求体：{req or '无'}",
            f"    响应体：{resp or '无'}",
            '    """',
            "    raise HTTPException(",
            "        status_code=status.HTTP_501_NOT_IMPLEMENTED,",
            f"        detail={json.dumps(f'尚未实现：{desc[:60] or name}', ensure_ascii=False)},",
            "    )",
            "",
            "",
        ]
    return "\n".join(lines)


def _table_meta(tables: list[dict]) -> list[dict]:
    """把 db_schema 整理成"资源"元数据，供 CRUD 后端与前端表单共用。

    每项：{resource(路径用的 slug), table, cls(模型类名), label(中文说明),
           pk(主键列名), columns:[{name,type,input,nullable,description,editable}]}
    editable=False 的列（主键、created_at 等）不出现在表单里，由数据库/后端生成。
    """
    metas: list[dict] = []
    used_cls: set[str] = set()
    for tbl in tables:
        raw = str(tbl.get("table_name") or "").strip()
        table = _py_ident(raw) or "unnamed_table"
        cls = "".join(p.capitalize() for p in table.split("_")) or "UnnamedTable"
        while cls in used_cls:
            cls += "X"
        used_cls.add(cls)

        cols: list[dict] = []
        seen: set[str] = set()
        has_id = False
        for col in (tbl.get("columns") or []):
            if not isinstance(col, dict):
                continue
            name = _py_ident(col.get("name")) or "col"
            if name in seen:
                continue
            seen.add(name)
            sql_type = str(col.get("type") or "").strip()
            if name == "id":
                has_id = True
            # 主键与审计时间戳交给数据库/后端，不放进表单
            editable = name not in {"id", "_pk", "created_at", "updated_at", "deleted_at"}
            cols.append({
                "name": name,
                "type": sql_type,
                "input": _input_kind(sql_type),
                "nullable": col.get("nullable") is not False,
                "description": str(col.get("description") or "").replace("\n", " ").strip(),
                "editable": editable,
            })
        metas.append({
            "resource": table,
            "table": table,
            "cls": cls,
            "label": str(tbl.get("description") or raw).strip()[:60] or raw,
            "pk": "id" if has_id else "_pk",
            "columns": cols,
        })
    return metas


def _input_kind(sql_type: str) -> str:
    """SQL 类型 → 前端表单控件类型。"""
    t = str(sql_type or "").lower()
    if re.match(r"^(bool|boolean)", t):
        return "checkbox"
    if re.match(r"^(int|integer|bigint|smallint|serial|bigserial|decimal|numeric|float|double|real)", t):
        return "number"
    if re.match(r"^(timestamp|timestamptz|datetime)", t):
        return "datetime-local"
    if re.match(r"^date$", t):
        return "date"
    if re.match(r"^(text|json|jsonb)", t) or "[]" in t:
        return "textarea"
    return "text"


def _render_crud_router(tables: list[dict]) -> str:
    """生成【真能用】的通用 CRUD 路由：一个文件覆盖所有表。

    设计取舍：不为每张表写一个文件（几十张表会爆），而是用 `{resource}` 作为路径段、
    在 CRUD_MODELS 里查表。前端因此也只需要一个通用页面组件。
    """
    metas = _table_meta(tables)
    registry = ",\n".join(
        f'    "{m["resource"]}": (models.{m["cls"]}, "{m["pk"]}")' for m in metas
    )
    return f'''"""通用 CRUD 接口 —— 由技术设计的 db_schema 自动生成，**可直接读写数据库**。

与 api/routers/ 下那些"业务端点"不同：那些是按技术设计声明的领域接口（当前返回 501，
等待实现业务逻辑）；这里提供的是每张表的基础增删改查，让前端管理页面立刻可用。

约定路径：/api/v1/crud/{{resource}}
  GET    /crud/{{resource}}              列表（?limit=&offset=）
  POST   /crud/{{resource}}              新增
  GET    /crud/{{resource}}/{{item_id}}    单条
  PUT    /crud/{{resource}}/{{item_id}}    更新
  DELETE /crud/{{resource}}/{{item_id}}    删除
  GET    /crud/_schema                 所有资源的字段结构（前端表单据此渲染）
"""

import json
from datetime import date, datetime, time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.db import models
from app.db.base import get_db

router = APIRouter(prefix="/crud", tags=["crud"])

# resource 名 -> (模型类, 主键列名)
CRUD_MODELS: dict[str, tuple[type, str]] = {{
{registry}
}}


def _model_for(resource: str) -> tuple[type, str]:
    if resource not in CRUD_MODELS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未知资源：{{resource}}；可用资源：{{', '.join(sorted(CRUD_MODELS))}}",
        )
    return CRUD_MODELS[resource]


def _to_dict(obj: Any) -> dict:
    """ORM 对象 → 可 JSON 化的 dict（日期等统一转字符串）。"""
    out = {{}}
    for col in sa_inspect(type(obj)).columns:
        value = getattr(obj, col.key, None)
        out[col.key] = value if isinstance(value, (int, float, bool, type(None), str)) else str(value)
    return out


def _coerce(column: Any, value: Any) -> Any:
    """把前端传来的字符串转成列类型要求的 Python 值。

    HTML 表单里所有输入都是字符串（datetime-local 给 "2026-08-10T12:00"、
    number 给 "42"、checkbox 给 true/false），而 SQLAlchemy 的 DateTime/Integer
    列不接受字符串，会直接抛 "SQLite DateTime type only accepts Python datetime"。
    """
    if value is None or value == "":
        return None
    type_name = type(column.type).__name__
    try:
        if type_name == "DateTime":
            if isinstance(value, datetime):
                return value
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if type_name == "Date":
            return date.fromisoformat(str(value)[:10])
        if type_name == "Time":
            return time.fromisoformat(str(value))
        if type_name in ("Integer", "BigInteger", "SmallInteger"):
            return int(value)
        if type_name in ("Numeric", "Float"):
            return float(value)
        if type_name == "Boolean":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if type_name == "JSON":
            # 表单里 JSON 列是文本框，尝试解析；解析不了就原样存字符串
            if isinstance(value, (dict, list)):
                return value
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return value
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"字段 {{column.key}} 的值 {{value!r}} 不符合类型 {{column.type}}：{{exc}}",
        ) from exc
    return value


def _clean_payload(model: type, payload: dict) -> dict:
    """只保留模型上真实存在的列，忽略前端多传的字段，并按列类型做转换。"""
    columns = {{c.key: c for c in sa_inspect(model).columns}}
    cleaned = {{}}
    for k, v in (payload or {{}}).items():
        if k in columns:
            cleaned[k] = _coerce(columns[k], v)
    return cleaned


@router.get("/_schema")
async def crud_schema():
    """返回所有资源及其列信息，前端用它渲染表格列与表单控件。"""
    out = {{}}
    for resource, (model, pk) in CRUD_MODELS.items():
        out[resource] = {{
            "table": model.__tablename__,
            "pk": pk,
            "columns": [
                {{"name": c.key, "type": str(c.type), "nullable": c.nullable}}
                for c in sa_inspect(model).columns
            ],
        }}
    return out


@router.get("/{{resource}}")
async def list_items(resource: str, limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    model, _ = _model_for(resource)
    rows = db.query(model).offset(max(0, offset)).limit(min(max(1, limit), 200)).all()
    return {{"total": db.query(model).count(), "items": [_to_dict(r) for r in rows]}}


@router.post("/{{resource}}", status_code=status.HTTP_201_CREATED)
async def create_item(resource: str, payload: dict, db: Session = Depends(get_db)):
    model, _ = _model_for(resource)
    try:
        obj = model(**_clean_payload(model, payload))
        db.add(obj)
        db.commit()
        db.refresh(obj)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"新增失败：{{exc}}") from exc
    return _to_dict(obj)


@router.get("/{{resource}}/{{item_id}}")
async def get_item(resource: str, item_id: str, db: Session = Depends(get_db)):
    model, pk = _model_for(resource)
    obj = db.query(model).filter(getattr(model, pk) == item_id).first()
    if obj is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return _to_dict(obj)


@router.put("/{{resource}}/{{item_id}}")
async def update_item(resource: str, item_id: str, payload: dict, db: Session = Depends(get_db)):
    model, pk = _model_for(resource)
    obj = db.query(model).filter(getattr(model, pk) == item_id).first()
    if obj is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    try:
        for k, v in _clean_payload(model, payload).items():
            if k != pk:                      # 主键不允许改
                setattr(obj, k, v)
        db.commit()
        db.refresh(obj)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"更新失败：{{exc}}") from exc
    return _to_dict(obj)


@router.delete("/{{resource}}/{{item_id}}")
async def delete_item(resource: str, item_id: str, db: Session = Depends(get_db)):
    model, pk = _model_for(resource)
    obj = db.query(model).filter(getattr(model, pk) == item_id).first()
    if obj is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    db.delete(obj)
    db.commit()
    return {{"deleted": True, "id": item_id}}
'''


def _render_models(tables: list[dict]) -> str:
    lines = [
        '"""自动生成的 SQLAlchemy 模型（由技术设计的 db_schema 推导）。',
        "",
        "两处必要的默认值，否则插入会直接违反 NOT NULL：",
        "  - 字符串/UUID 型主键：技术设计只写了类型没写生成方式，这里用 uuid4 兜底",
        "    （整型主键交给数据库自增，不需要默认值）",
        "  - created_at / updated_at：统一用当前 UTC 时间",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import uuid",
        "from datetime import datetime, timezone",
        "",
        _SA_IMPORTS,
        "",
        "from app.db.base import Base",
        "",
        "",
        "def _uuid() -> str:",
        '    """字符串型主键的默认值生成器。"""',
        "    return str(uuid.uuid4())",
        "",
        "",
        "def _now() -> datetime:",
        "    return datetime.now(timezone.utc)",
        "",
        "",
    ]
    used: set[str] = set()
    for tbl in tables:
        raw_name = str(tbl.get("table_name") or "").strip()
        table_name = _py_ident(raw_name) or "unnamed_table"
        cls = "".join(p.capitalize() for p in table_name.split("_")) or "UnnamedTable"
        while cls in used:
            cls += "X"
        used.add(cls)

        desc = str(tbl.get("description") or "").strip()
        cols = [c for c in (tbl.get("columns") or []) if isinstance(c, dict)]
        lines += [f"class {cls}(Base):", f'    """{desc or raw_name}"""', "", f'    __tablename__ = "{table_name}"', ""]

        if not cols:
            lines += ["    id = Column(Integer, primary_key=True)  # 技术设计未给出列定义", "", ""]
            continue

        seen_cols: set[str] = set()
        has_pk = False
        for col in cols:
            cname = _py_ident(col.get("name")) or "col"
            if cname in seen_cols:
                continue
            seen_cols.add(cname)
            raw_type = str(col.get("type") or "")
            sa = _sa_type(raw_type)
            # 约定：名为 id 的列视为主键；至少保证有一个主键
            is_pk = cname == "id" and not has_pk
            has_pk = has_pk or is_pk
            opts = ["primary_key=True"] if is_pk else []
            if is_pk:
                if sa.startswith(("Integer", "BigInteger")):
                    # SQLite 只对 INTEGER PRIMARY KEY 自增，BIGINT 不会 —— 而技术设计
                    # 常写 BIGSERIAL。用 with_variant 让 PostgreSQL 保持 BigInteger、
                    # SQLite 降级为 Integer，两边都能自增。
                    sa = "BigInteger().with_variant(Integer, 'sqlite')"
                    opts.append("autoincrement=True")
                else:
                    # 字符串/UUID 主键必须给默认值，否则 INSERT 会违反 NOT NULL
                    opts.append("default=_uuid")
            if not is_pk and cname in ("created_at", "updated_at"):
                opts.append("default=_now")
                if cname == "updated_at":
                    opts.append("onupdate=_now")
            if not is_pk and col.get("nullable") is False:
                opts.append("nullable=False")
            opt_txt = (", " + ", ".join(opts)) if opts else ""
            cdesc = str(col.get("description") or "").replace("\n", " ").strip()
            comment = f"  # {col.get('type')} {cdesc}".rstrip()
            lines.append(f"    {cname} = Column({sa}{opt_txt}){comment}")
        if not has_pk:
            lines.insert(len(lines) - len(seen_cols), "    _pk = Column(Integer, primary_key=True)  # 技术设计未标主键，自动补充")
        lines += ["", ""]
    return "\n".join(lines)


def _render_init_sql(tables: list[dict]) -> str:
    lines = ["-- 自动生成的建表脚本（由技术设计的 db_schema 推导）", "-- 目标数据库：PostgreSQL", ""]
    for tbl in tables:
        name = _py_ident(tbl.get("table_name")) or "unnamed_table"
        desc = str(tbl.get("description") or "").replace("\n", " ")
        cols = [c for c in (tbl.get("columns") or []) if isinstance(c, dict)]
        lines.append(f"-- {desc}")
        lines.append(f"CREATE TABLE IF NOT EXISTS {name} (")
        if not cols:
            lines += ["    id BIGSERIAL PRIMARY KEY", ");", ""]
            continue
        parts = []
        seen: set[str] = set()
        has_pk = False
        for col in cols:
            cname = _py_ident(col.get("name")) or "col"
            if cname in seen:
                continue
            seen.add(cname)
            ctype = str(col.get("type") or "VARCHAR(255)").strip() or "VARCHAR(255)"
            frag = f"    {cname} {ctype}"
            if cname == "id" and not has_pk:
                frag += " PRIMARY KEY"
                has_pk = True
            elif col.get("nullable") is False:
                frag += " NOT NULL"
            parts.append(frag)
        if not has_pk:
            parts.insert(0, "    _pk BIGSERIAL PRIMARY KEY")
        lines.append(",\n".join(parts))
        lines.append(");")
        # 索引
        for idx in (tbl.get("indexes") or []):
            idx_txt = str(idx).strip()
            if idx_txt:
                lines.append(f"-- 建议索引：{idx_txt}")
        lines.append("")
    return "\n".join(lines)


def _render_main(product_name: str, design: dict, groups: dict[str, list[dict]]) -> str:
    imports = "\n".join(f"from app.api.routers import {slug}" for slug in sorted(groups))
    includes = "\n".join(f"app.include_router({slug}.router, prefix=settings.api_prefix)" for slug in sorted(groups))
    overview = str(design.get("architecture_overview") or "").replace('"""', "'''")[:500]
    return f'''"""{product_name} —— 由技术设计自动生成的服务入口。

架构概述（来自技术设计）：
{overview}
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import crud
from app.core.config import settings
from app.db.base import init_db
{imports}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 开发期便利：启动时按模型建表。生产环境请改用 migrations/001_init.sql 或 Alembic。
    init_db()
    yield


app = FastAPI(
    title="{product_name}",
    description="骨架项目：端点与数据表由技术设计生成，业务逻辑待实现。",
    version="0.1.0",
    lifespan=lifespan,
)

{includes}

# 通用 CRUD（可直接读写数据库，支撑前端管理页面）
app.include_router(crud.router, prefix=settings.api_prefix)


@app.get("/health", tags=["meta"])
async def health():
    return {{"status": "ok"}}
'''


def _render_config(product_name: str) -> str:
    return f'''"""配置：环境变量优先，缺省值保证开箱即跑。"""

import os


class Settings:
    app_name: str = {json.dumps(product_name, ensure_ascii=False)}
    api_prefix: str = os.getenv("API_PREFIX", "/api/v1")
    # 默认用本地 SQLite，无需装数据库即可启动；docker-compose 里会注入 PostgreSQL 地址
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data.db")


settings = Settings()
'''


def _render_db_base() -> str:
    return '''"""数据库会话与建表。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings

# SQLite 需要额外参数才能在多线程下使用
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """按模型建表（开发期便利，生产请用迁移脚本）。"""
    from app.db import models  # noqa: F401  —— 导入以注册所有模型

    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI 依赖：请求级数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
'''


def _render_requirements() -> str:
    # 只放本地跑起来必需的依赖。PostgreSQL 驱动挪到 requirements-postgres.txt：
    # 本地默认用 SQLite 根本不需要它，而 psycopg2 在部分环境装不上会直接卡住演示。
    return "\n".join([
        "fastapi>=0.115,<1.0",
        "uvicorn[standard]>=0.30,<1.0",
        "SQLAlchemy>=2.0,<2.1",
        "pydantic>=2.7,<3.0",
        "httpx>=0.27,<1.0",
        "pytest>=8.2,<9.0",
        "",
    ])


def _render_requirements_postgres() -> str:
    return "\n".join([
        "# 仅在连 PostgreSQL 时需要（Docker 方式会自动安装）。",
        "# 本地用默认的 SQLite 不必安装。",
        "-r requirements.txt",
        "psycopg2-binary>=2.9,<3.0",
        "",
    ])


def _render_dockerfile() -> str:
    return '''FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt requirements-postgres.txt ./
# Docker 里连 PostgreSQL，所以装带驱动的那份
RUN pip install --no-cache-dir -r requirements-postgres.txt

COPY . .

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
'''


def _render_compose() -> str:
    return '''services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: app
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app"]
      interval: 5s
      retries: 10

  api:
    build: .
    environment:
      DATABASE_URL: postgresql+psycopg2://app:app@db:5432/app
    depends_on:
      db:
        condition: service_healthy
    ports: ["8000:8000"]

  web:
    build: ./web
    depends_on: [api]
    ports: ["5174:80"]      # 打开 http://127.0.0.1:5174 查看前端
'''


def _render_env_example() -> str:
    return "API_PREFIX=/api/v1\n# 留空则使用本地 SQLite，无需安装数据库\nDATABASE_URL=\n"


def _render_smoke_test(groups: dict[str, list[dict]]) -> str:
    total = sum(len(v) for v in groups.values())
    return f'''"""冒烟测试：确认服务能起、端点已注册、未实现的端点返回 501。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {{"status": "ok"}}


def test_all_endpoints_registered():
    """技术设计里的 {total} 个端点都应出现在 OpenAPI 文档中。"""
    paths = client.get("/openapi.json").json()["paths"]
    # 除 /health 外都是生成的业务端点
    assert len([p for p in paths if p != "/health"]) > 0
'''


def _render_web(
    product_name: str,
    prd_doc: dict,
    groups: dict[str, list[dict]],
    metas: list[dict],
) -> dict[str, str]:
    """生成 Vue 3 + Vite 前端。

    两类页面：
    1. **数据管理页（应用真正的功能界面）**——按 db_schema 为每张表生成"列表 + 新增/
       编辑表单 + 删除"，走后端通用 CRUD，**真能读写数据库**。这是产品验收方要看的界面。
       实现上只用一个通用组件 + `/data/:resource` 路由，由 schema 数据驱动，
       避免为几十张表各生成一个几乎相同的页面文件。
    2. **接口清单页**——把技术设计声明的业务端点列出来并可"试调用"（当前返回 501），
       是给开发者对照设计用的参考视图，收在单独一页里，不占主导。
    """
    modules = [
        str(m.get("name", "")) for m in (prd_doc.get("feature_modules") or []) if isinstance(m, dict)
    ]

    # 每个分组的端点数据以 JSON 内联进页面，避免额外请求，也让页面自解释
    def endpoints_json(eps: list[dict]) -> str:
        rows = [
            {
                "method": str(e.get("method") or "GET").upper(),
                "path": str(e.get("path") or "/"),
                "description": str(e.get("description") or ""),
                "auth": bool(e.get("auth_required")),
                "features": [str(f) for f in (e.get("related_features") or [])],
            }
            for e in eps
        ]
        return json.dumps(rows, ensure_ascii=False, indent=2)

    files: dict[str, str] = {
        "web/package.json": json.dumps(
            {
                "name": "generated-web",
                "private": True,
                "type": "module",
                "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
                "dependencies": {"axios": "^1.7.0", "vue": "^3.5.0", "vue-router": "^4.4.0"},
                "devDependencies": {"@vitejs/plugin-vue": "^5.1.0", "vite": "^5.4.0"},
            },
            indent=2,
        )
        + "\n",
        "web/vite.config.js": '''import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5174,
    // 把 /api 代理到后端，避免跨域；Docker 里由 nginx 承担同样的角色
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } },
  },
})
''',
        "web/index.html": f'''<!doctype html>
<html lang="zh-CN">
  <head><meta charset="UTF-8" /><title>{product_name}</title></head>
  <body><div id="app"></div><script type="module" src="/src/main.js"></script></body>
</html>
''',
        "web/src/main.js": '''import { createApp } from 'vue'
import App from './App.vue'
import router from './router'

createApp(App).use(router).mount('#app')
''',
        "web/src/api/client.js": '''import axios from 'axios'

// baseURL 留空：请求走 /api/...，由 vite 代理（开发）或 nginx（Docker）转发到后端
export const api = axios.create({ timeout: 30000 })

/** 试调用一个端点：把路径参数替换成占位值，返回 {status, body} */
export async function tryCall(method, path) {
  const filled = path.replace(/\\{[^{}]+\\}/g, '1')   // 路径参数用 1 占位
  const url = `/api/v1${filled}`
  try {
    const res = await api.request({ method, url, data: method === 'GET' ? undefined : {} })
    return { status: res.status, body: res.data }
  } catch (err) {
    return {
      status: err.response?.status ?? 0,
      body: err.response?.data ?? { detail: String(err.message || err) },
    }
  }
}
''',
        "web/src/App.vue": _render_web_app(product_name, metas),
        "web/src/router/index.js": _render_web_router(),
        "web/src/views/Overview.vue": _render_web_overview(product_name, modules, groups, metas),
        # 资源结构（表格列 + 表单控件由它驱动）
        "web/src/schema.js": "export const RESOURCES = "
        + json.dumps(
            [
                {
                    "resource": m["resource"],
                    "label": m["label"],
                    "pk": m["pk"],
                    "columns": m["columns"],
                }
                for m in metas
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        # 应用功能界面：列表 + 新增/编辑 + 删除，真连数据库
        "web/src/views/DataResource.vue": _render_web_data_page(),
        # 开发者参考：技术设计声明的业务端点清单
        "web/src/views/ApiCatalog.vue": _render_web_api_catalog(groups),
        "web/Dockerfile": '''FROM node:20-alpine AS build
WORKDIR /web
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
''',
        "web/nginx.conf": '''server {
    listen 80;
    root /usr/share/nginx/html;
    index index.html;

    # 单页应用：未匹配到静态文件时回落到 index.html
    location / { try_files $uri $uri/ /index.html; }

    # 转发接口请求到后端服务
    location /api/ {
        proxy_pass http://api:8000/api/;
        proxy_set_header Host $host;
    }
}
''',
    }

    return files


def _render_web_app(product_name: str, metas: list[dict]) -> str:
    """侧边栏：数据管理页（应用功能）在前，接口清单（开发者参考）在后。"""
    return f'''<script setup>
import {{ RouterLink, RouterView }} from 'vue-router'
import {{ RESOURCES }} from './schema'

// 后端 Swagger 地址：本地开发时后端独立跑在 8000；Docker 下 compose 也把 api 映射到 8000，
// 所以两种方式都可用。若后端换端口，改这里即可。
const docsUrl = `${{location.protocol}}//${{location.hostname}}:8000/docs`
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <h1 class="brand">{product_name}</h1>
      <p class="sub">自动生成的骨架应用</p>

      <nav>
        <RouterLink class="nav-item" to="/">总览</RouterLink>

        <div class="nav-group">数据管理</div>
        <RouterLink
          v-for="r in RESOURCES" :key="r.resource"
          class="nav-item" :to="`/data/${{r.resource}}`"
          :title="r.label"
        >{{{{ r.label || r.resource }}}}</RouterLink>

        <div class="nav-group">开发者</div>
        <RouterLink class="nav-item" to="/api">接口清单</RouterLink>
        <!-- 直连后端：FastAPI 的文档在 /docs，而前端的 /api 代理会把路径原样转发过去，
             写成 /api/docs 会变成后端的 /api/docs（不存在）而 404 -->
        <a class="nav-item" :href="docsUrl" target="_blank">Swagger 文档 ↗</a>
      </nav>
    </aside>
    <main class="content"><RouterView /></main>
  </div>
</template>

<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color: #1f2328; background: #f6f8fa; }}
.layout {{ display: flex; min-height: 100vh; }}
.sidebar {{ width: 236px; flex-shrink: 0; padding: 20px 14px; background: #fff; border-right: 1px solid #e5e7eb; overflow-y: auto; max-height: 100vh; }}
.brand {{ font-size: 16px; margin: 0 0 2px; }}
.sub {{ font-size: 11px; color: #8b949e; margin: 0 0 14px; }}
.nav-group {{ font-size: 10.5px; font-weight: 700; color: #9ca3af; text-transform: uppercase; letter-spacing: .06em; margin: 14px 0 5px 10px; }}
.nav-item {{ display: block; padding: 7px 10px; margin-bottom: 2px; border-radius: 6px; color: #374151; text-decoration: none; font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.nav-item:hover {{ background: #f3f4f6; }}
.nav-item.router-link-exact-active {{ background: #eff6ff; color: #1d4ed8; font-weight: 600; }}
.content {{ flex: 1; padding: 24px 28px; overflow-x: hidden; }}
h2 {{ margin: 0 0 4px; font-size: 20px; }}
.hint {{ color: #6b7280; font-size: 13px; margin: 0 0 16px; }}
.card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 16px; margin-bottom: 12px; }}
.row {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
button {{ font: inherit; font-size: 12.5px; padding: 5px 12px; border: 1px solid #d1d5db; background: #fff; border-radius: 6px; cursor: pointer; }}
button:hover:not(:disabled) {{ border-color: #1d4ed8; color: #1d4ed8; }}
button:disabled {{ opacity: .5; cursor: not-allowed; }}
button.primary {{ background: #1d4ed8; border-color: #1d4ed8; color: #fff; }}
button.primary:hover:not(:disabled) {{ background: #1e40af; color: #fff; }}
button.danger:hover {{ border-color: #dc2626; color: #dc2626; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
th, td {{ text-align: left; padding: 7px 9px; border-bottom: 1px solid #eef0f2; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
th {{ color: #6b7280; font-weight: 600; font-size: 11.5px; background: #fafbfc; }}
label {{ display: block; font-size: 12px; color: #374151; margin-bottom: 3px; }}
input, textarea, select {{ width: 100%; font: inherit; font-size: 12.5px; padding: 6px 9px; border: 1px solid #d1d5db; border-radius: 6px; }}
input[type=checkbox] {{ width: auto; }}
textarea {{ min-height: 62px; resize: vertical; }}
.form-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 11px; margin-bottom: 12px; }}
.field-desc {{ font-size: 10.5px; color: #9ca3af; margin-top: 2px; }}
.msg {{ padding: 8px 11px; border-radius: 6px; font-size: 12.5px; margin-bottom: 10px; }}
.msg.err {{ background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }}
.msg.ok {{ background: #f0fdf4; color: #15803d; border: 1px solid #bbf7d0; }}
.method {{ font-family: ui-monospace, monospace; font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 5px; background: #eff6ff; color: #1d4ed8; }}
.path {{ font-family: ui-monospace, monospace; font-size: 12.5px; }}
.badge {{ font-size: 10.5px; padding: 1px 6px; border-radius: 4px; background: #fef3c7; color: #92400e; }}
pre {{ background: #0d1117; color: #c9d1d9; padding: 10px 12px; border-radius: 7px; font-size: 11.5px; overflow-x: auto; margin: 9px 0 0; }}
</style>
'''


def _render_web_router() -> str:
    """只有 3 条路由：总览、数据管理（动态 resource）、接口清单。"""
    return '''import { createRouter, createWebHistory } from 'vue-router'
import Overview from '../views/Overview.vue'
import DataResource from '../views/DataResource.vue'
import ApiCatalog from '../views/ApiCatalog.vue'

// 数据管理页用同一个组件 + :resource 参数，由 schema.js 驱动，
// 不必为每张表各生成一个几乎相同的页面文件。
export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/data/:resource', name: 'data', component: DataResource, props: true },
    { path: '/api', name: 'api', component: ApiCatalog },
  ],
})
'''


def _render_web_data_page() -> str:
    """应用真正的功能界面：列表 + 新增/编辑表单 + 删除，走后端通用 CRUD。"""
    return '''<script setup>
import { ref, computed, watch } from 'vue'
import { api } from '../api/client'
import { RESOURCES } from '../schema'

const props = defineProps({ resource: String })

const meta = computed(() => RESOURCES.find((r) => r.resource === props.resource) || null)
const editable = computed(() => (meta.value?.columns || []).filter((c) => c.editable))

const items = ref([])
const total = ref(0)
const loading = ref(false)
const saving = ref(false)
const err = ref('')
const ok = ref('')
const form = ref({})
const editingId = ref(null)

function blankForm() {
  const f = {}
  for (const c of editable.value) f[c.name] = c.input === 'checkbox' ? false : ''
  return f
}

async function load() {
  if (!meta.value) return
  loading.value = true; err.value = ''
  try {
    const { data } = await api.get(`/api/v1/crud/${props.resource}`, { params: { limit: 50 } })
    items.value = data.items || []
    total.value = data.total || 0
  } catch (e) {
    err.value = detail(e)
  } finally { loading.value = false }
}

function detail(e) {
  return e.response?.data?.detail || e.message || '请求失败'
}

async function save() {
  saving.value = true; err.value = ''; ok.value = ''
  try {
    if (editingId.value === null) {
      await api.post(`/api/v1/crud/${props.resource}`, form.value)
      ok.value = '新增成功'
    } else {
      await api.put(`/api/v1/crud/${props.resource}/${editingId.value}`, form.value)
      ok.value = '更新成功'
    }
    form.value = blankForm(); editingId.value = null
    await load()
  } catch (e) { err.value = detail(e) } finally { saving.value = false }
}

function edit(row) {
  editingId.value = row[meta.value.pk]
  const f = {}
  for (const c of editable.value) f[c.name] = row[c.name] ?? (c.input === 'checkbox' ? false : '')
  form.value = f
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function cancelEdit() { editingId.value = null; form.value = blankForm() }

async function remove(row) {
  if (!confirm('确定删除这条记录？')) return
  err.value = ''; ok.value = ''
  try {
    await api.delete(`/api/v1/crud/${props.resource}/${row[meta.value.pk]}`)
    ok.value = '已删除'
    await load()
  } catch (e) { err.value = detail(e) }
}

watch(() => props.resource, () => {
  items.value = []; editingId.value = null; ok.value = ''; err.value = ''
  form.value = blankForm(); load()
}, { immediate: true })
</script>

<template>
  <div v-if="!meta"><h2>未知资源</h2><p class="hint">{{ resource }}</p></div>
  <div v-else>
    <h2>{{ meta.label || meta.resource }}</h2>
    <p class="hint">
      数据表 <code>{{ meta.table }}</code> ·
      共 {{ total }} 条记录。此页直连后端通用 CRUD 接口，增删改查真实写入数据库。
    </p>

    <div v-if="err" class="msg err">{{ err }}</div>
    <div v-if="ok" class="msg ok">{{ ok }}</div>

    <div class="card">
      <div class="row" style="margin-bottom:10px">
        <strong style="font-size:13px">{{ editingId === null ? '新增记录' : '编辑记录 #' + editingId }}</strong>
        <button v-if="editingId !== null" style="margin-left:auto" @click="cancelEdit">取消编辑</button>
      </div>
      <div class="form-grid">
        <div v-for="c in editable" :key="c.name">
          <label>{{ c.name }}<span v-if="!c.nullable" style="color:#dc2626"> *</span></label>
          <textarea v-if="c.input === 'textarea'" v-model="form[c.name]" :placeholder="c.type" />
          <input v-else-if="c.input === 'checkbox'" type="checkbox" v-model="form[c.name]" />
          <input v-else :type="c.input" v-model="form[c.name]" :placeholder="c.type" />
          <div v-if="c.description" class="field-desc">{{ c.description }}</div>
        </div>
      </div>
      <button class="primary" :disabled="saving" @click="save">
        {{ saving ? '提交中…' : (editingId === null ? '新增' : '保存修改') }}
      </button>
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:8px">
        <strong style="font-size:13px">记录列表</strong>
        <button style="margin-left:auto" :disabled="loading" @click="load">
          {{ loading ? '加载中…' : '刷新' }}
        </button>
      </div>
      <div style="overflow-x:auto">
        <table>
          <thead>
            <tr>
              <th v-for="c in meta.columns" :key="c.name">{{ c.name }}</th>
              <th style="width:110px">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in items" :key="i">
              <td v-for="c in meta.columns" :key="c.name" :title="String(row[c.name] ?? '')">
                {{ row[c.name] ?? '—' }}
              </td>
              <td>
                <button @click="edit(row)">编辑</button>
                <button class="danger" style="margin-left:4px" @click="remove(row)">删除</button>
              </td>
            </tr>
            <tr v-if="!items.length && !loading">
              <td :colspan="meta.columns.length + 1" style="color:#9ca3af;text-align:center;padding:20px">
                暂无数据，用上方表单新增一条试试
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
'''


def _render_web_api_catalog(groups: dict[str, list[dict]]) -> str:
    """开发者参考页：技术设计声明的业务端点清单，可试调用（当前返回 501）。"""
    rows = []
    for slug, eps in sorted(groups.items()):
        for e in eps:
            rows.append({
                "group": slug,
                "method": str(e.get("method") or "GET").upper(),
                "path": str(e.get("path") or "/"),
                "description": str(e.get("description") or ""),
                "auth": bool(e.get("auth_required")),
                "features": [str(f) for f in (e.get("related_features") or [])],
            })
    data = json.dumps(rows, ensure_ascii=False, indent=2)
    return f'''<script setup>
import {{ ref, computed }} from 'vue'
import {{ tryCall }} from '../api/client'

// 端点数据来自技术设计，构建时内联
const endpoints = {data}

const keyword = ref('')
const results = ref({{}})
const busy = ref('')

const filtered = computed(() => {{
  const k = keyword.value.trim().toLowerCase()
  if (!k) return endpoints
  return endpoints.filter((e) =>
    e.path.toLowerCase().includes(k) ||
    e.description.toLowerCase().includes(k) ||
    e.group.includes(k))
}})

async function run(ep, idx) {{
  busy.value = idx
  results.value = {{ ...results.value, [idx]: null }}
  results.value = {{ ...results.value, [idx]: await tryCall(ep.method, ep.path) }}
  busy.value = ''
}}
</script>

<template>
  <h2>接口清单</h2>
  <p class="hint">
    技术设计声明的 {{{{ endpoints.length }}}} 个业务端点（开发者参考）。
    这些是**待实现**的领域接口，点「试调用」会返回 501 及设计说明。
    应用的实际功能界面请看左侧「数据管理」。
  </p>

  <div class="card">
    <input v-model="keyword" placeholder="搜索路径、说明或模块…" />
  </div>

  <div v-for="(ep, idx) in filtered" :key="idx" class="card">
    <div class="row">
      <span class="method">{{{{ ep.method }}}}</span>
      <span class="path">{{{{ ep.path }}}}</span>
      <span v-if="ep.auth" class="badge">需认证</span>
      <span v-for="f in ep.features" :key="f" class="badge">{{{{ f }}}}</span>
      <button style="margin-left:auto" :disabled="busy === idx" @click="run(ep, idx)">
        {{{{ busy === idx ? '请求中…' : '试调用' }}}}
      </button>
    </div>
    <p class="hint" style="margin:7px 0 0">{{{{ ep.description || '（技术设计未提供描述）' }}}}</p>
    <pre v-if="results[idx]">HTTP {{{{ results[idx].status }}}}
{{{{ JSON.stringify(results[idx].body, null, 2) }}}}</pre>
  </div>
</template>
'''


def _render_web_overview(
    product_name: str, modules: list[str], groups: dict[str, list[dict]], metas: list[dict]
) -> str:
    res = json.dumps(
        [{"resource": m["resource"], "label": m["label"], "cols": len(m["columns"])} for m in metas],
        ensure_ascii=False,
    )
    mods = json.dumps(modules, ensure_ascii=False)
    total = sum(len(v) for v in groups.values())
    return f'''<script setup>
const resources = {res}
const modules = {mods}
const endpointTotal = {total}
</script>

<template>
  <h2>{product_name}</h2>
  <p class="hint">
    由技术设计自动生成的骨架应用：{{{{ resources.length }}}} 个数据管理页（可直接增删改查）、
    {{{{ endpointTotal }}}} 个待实现的业务接口。
  </p>

  <div class="card">
    <h3 style="margin:0 0 8px;font-size:14px">数据管理（应用功能界面）</h3>
    <p class="hint" style="margin:0 0 10px">
      每个资源对应一张数据表，进入后可新增、编辑、删除记录——真实写入数据库。
    </p>
    <table>
      <thead><tr><th>资源</th><th>字段数</th><th></th></tr></thead>
      <tbody>
        <tr v-for="r in resources" :key="r.resource">
          <td>{{{{ r.label || r.resource }}}}</td>
          <td>{{{{ r.cols }}}}</td>
          <td><RouterLink :to="`/data/${{r.resource}}`">进入管理 →</RouterLink></td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class="card" v-if="modules.length">
    <h3 style="margin:0 0 8px;font-size:14px">PRD 功能模块</h3>
    <div class="row">
      <span v-for="m in modules" :key="m" class="badge">{{{{ m }}}}</span>
    </div>
    <p class="hint" style="margin:10px 0 0">
      这些模块的业务接口已在技术设计中声明，见
      <RouterLink to="/api">接口清单</RouterLink>，待填入业务逻辑。
    </p>
  </div>
</template>
'''


def _render_readme(product_name: str, design: dict, groups: dict[str, list[dict]], tables: list[dict]) -> str:
    ep_total = sum(len(v) for v in groups.values())
    router_rows = "\n".join(
        f"| `app/api/routers/{slug}.py` | {len(eps)} |" for slug, eps in sorted(groups.items())
    )
    table_rows = "\n".join(
        f"- `{t.get('table_name')}` —— {str(t.get('description') or '').strip()[:60]}" for t in tables
    )
    services = ", ".join(str(s.get("name", "")) for s in (design.get("services") or []) if isinstance(s, dict))
    return f'''# {product_name}

> 本项目由「多智能体需求交付系统」根据技术设计**自动生成**。
> 端点与数据表结构均已就位，**业务逻辑待实现**——每个端点当前返回 `501 Not Implemented`。

## 快速开始（本地运行，推荐）

**后端默认使用 SQLite，不需要安装任何数据库。** 需要两个终端。

> Windows PowerShell 注意：**不支持 `&&`** 连接命令（会报解析错误），
> 下面按行分开写；`cd` 与后续命令请分两行执行，或用 `;` 连接。

**终端 1 —— 后端（端口 8000）**

```powershell
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

**终端 2 —— 前端（端口 5174）**

```powershell
cd web
npm install
npm run dev
```

打开 **http://127.0.0.1:5174** 即可。前端的 `/api` 请求由 Vite 代理到后端 8000，无需配跨域。

### 另一种方式：Docker（需要装 Docker Desktop，连 PostgreSQL）

```powershell
docker compose up --build
```

前端 http://127.0.0.1:5174 ｜ 后端文档 http://127.0.0.1:8000/docs

## 怎么验证它真的跑通了

1. 打开 http://127.0.0.1:5174 ，左侧「**数据管理**」下有 {len(tables)} 个资源
2. 进任一资源页 → 填写上方「新增记录」表单 → 点「新增」
3. 下方列表里立刻出现这条记录 —— **数据真的写进了数据库**
4. 点该行「编辑」可回填修改、「删除」可移除
5. 左侧「开发者 → 接口清单」是技术设计声明的 {ep_total} 个业务端点，
   点「试调用」会返回 `501` 及设计说明（等待填入业务逻辑）

> 如果新增时报字段校验错误，看表单里标了红星的必填项是否都填了。

## 项目结构

```
app/                    # 后端（FastAPI）
  main.py               # 服务入口，注册所有路由
  core/config.py        # 配置（环境变量优先）
  db/base.py            # 数据库会话；启动时自动建表
  db/models.py          # {len(tables)} 个 SQLAlchemy 模型
  api/routers/          # 按 URL 首段分组的路由
web/                    # 前端（Vue 3 + Vite）
  src/views/Overview.vue    # 总览：模块与接口统计
  src/views/*.vue           # 每个后端路由分组一个页面，可试调用
  src/api/client.js         # axios 封装 + tryCall
migrations/001_init.sql # PostgreSQL 建表脚本（生产用）
tests/test_smoke.py     # 冒烟测试
```

## 路由分布（共 {ep_total} 个端点）

| 文件 | 端点数 |
|---|---|
{router_rows}

## 数据表（共 {len(tables)} 张）

{table_rows}

## 服务划分（来自技术设计）

{services or "（技术设计未给出服务划分）"}

## 下一步

1. 打开任一 `app/api/routers/*.py`，函数 docstring 里写着该接口的设计说明
   （覆盖的功能模块、认证要求、请求/响应要点）。
2. 把 `raise HTTPException(501 ...)` 替换成真实实现，用 `Depends(get_db)` 注入数据库会话。
3. 前端 `web/src/views/*.vue` 目前是"接口清单 + 试调用"的开发者视图，
   请按 PRD 的用户流程替换成真正的业务页面（表单、列表、详情等）。
4. 生产环境请改用 `migrations/001_init.sql` 或引入 Alembic 管理迁移，
   而不是依赖启动时的 `create_all`。

> 注意：模型与建表脚本由技术设计推导，外键与索引未自动生成（仅在 SQL 注释里给出建议），
> 落地前请结合实际业务复核。
'''
