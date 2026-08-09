"""全局配置模块，基于 pydantic-settings 管理环境变量。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 加载 .env 文件
from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env")


class Settings(BaseModel):
    """全局配置：所有开关与参数集中在此，均可用同名环境变量(.env)覆盖。

    读取方式：全项目通过 get_settings()（带 lru_cache 单例）拿它。
    注意：改 .env 后因缓存需重启进程/调试会话才生效。
    分组：LLM 接入、LangSmith、数据库、App 基础、记忆/RAG、短期记忆、
         Few-shot、记忆巩固、Repairer、Self-Refine、多轮对话、上下文压缩、
         Plan-Verify、各类路径。
    """

    # LLM
    openai_api_key: str = Field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_api_base: str = Field(
        default_factory=lambda: os.getenv("OPENAI_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    )
    openai_model_name: str = Field(
        default_factory=lambda: os.getenv("OPENAI_MODEL_NAME", "qwen-max")
    )
    openai_timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("OPENAI_TIMEOUT_SECONDS", "180"))
    )
    # 单次调用的最大输出 token 数。0 = 不显式指定，用服务商默认值。
    # 为什么需要它：Engineer 生成完整技术设计时输出很长，部分服务商的默认上限
    # 低于模型实际能力，不显式要求就拿不到；显式调大可减少"输出被截断→结构化
    # 解析失败返回 None"的情况。注意不能超过所选模型的硬上限（超了通常会报错）。
    openai_max_tokens: int = Field(
        default_factory=lambda: int(os.getenv("OPENAI_MAX_TOKENS", "0"))
    )
    structured_output_method: str = Field(
        default_factory=lambda: os.getenv("STRUCTURED_OUTPUT_METHOD", "function_calling")
    )

    # LangSmith
    langchain_tracing_v2: bool = Field(
        default_factory=lambda: os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"
    )
    langchain_api_key: str = Field(default_factory=lambda: os.getenv("LANGCHAIN_API_KEY", ""))
    langchain_project: str = Field(
        default_factory=lambda: os.getenv("LANGCHAIN_PROJECT", "multi-agent-v1")
    )

    # Database
    database_url: str = Field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'tasks.db'}"
        )
    )

    # App
    app_env: str = Field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    max_reflow_count: int = Field(
        default_factory=lambda: int(os.getenv("MAX_REFLOW_COUNT", "2"))
    )
    # 后台并发执行的任务数。原来是单 worker 顺序执行，一个大任务会把后面的全堵住。
    # 提高可让多个任务同时跑，但会同时打多路 LLM 请求——注意服务商的并发/限流与成本。
    # 每个任务的图仍在独立线程里跑（asyncio.to_thread），彼此按 thread_id 隔离互不干扰。
    worker_concurrency: int = Field(
        default_factory=lambda: max(1, int(os.getenv("WORKER_CONCURRENCY", "3")))
    )
    prompt_version: str = Field(
        default_factory=lambda: os.getenv("PROMPT_VERSION", "v1")
    )
    # 触发"人工澄清"所需的待澄清问题数下限。
    # Planner 的提示词已要求"只问阻塞性问题、最多 3 条"，所以这里用 2：
    # 出现 2 条以上真正阻塞的问题才值得打断用户；0~1 条按假设继续跑。
    # 设成很大的数（如 99）可基本关闭澄清环节；设成 1 则任何疑问都会询问。
    clarify_question_threshold: int = Field(
        default_factory=lambda: max(1, int(os.getenv("CLARIFY_QUESTION_THRESHOLD", "2")))
    )

    # Memory / RAG
    memory_enabled: bool = Field(
        default_factory=lambda: os.getenv("MEMORY_ENABLED", "false").lower() == "true"
    )
    memory_top_k: int = Field(
        default_factory=lambda: int(os.getenv("MEMORY_TOP_K", "3"))
    )
    memory_context_max_chars: int = Field(
        default_factory=lambda: int(os.getenv("MEMORY_CONTEXT_MAX_CHARS", "1800"))
    )
    memory_item_max_chars: int = Field(
        default_factory=lambda: int(os.getenv("MEMORY_ITEM_MAX_CHARS", "900"))
    )
    memory_embedding_model: str = Field(
        default_factory=lambda: os.getenv("MEMORY_EMBEDDING_MODEL", "text-embedding-v3")
    )
    memory_embedding_dimensions: int = Field(
        default_factory=lambda: int(os.getenv("MEMORY_EMBEDDING_DIMENSIONS", "1024"))
    )

    # Short-term memory
    short_term_memory_max_chars: int = Field(
        default_factory=lambda: int(os.getenv("SHORT_TERM_MEMORY_MAX_CHARS", "1200"))
    )
    short_term_memory_max_entries: int = Field(
        default_factory=lambda: int(os.getenv("SHORT_TERM_MEMORY_MAX_ENTRIES", "15"))
    )

    # Dynamic Few-shot
    few_shot_top_k: int = Field(
        default_factory=lambda: int(os.getenv("FEW_SHOT_TOP_K", "2"))
    )
    few_shot_max_chars: int = Field(
        default_factory=lambda: int(os.getenv("FEW_SHOT_MAX_CHARS", "1500"))
    )
    few_shot_min_score: float = Field(
        default_factory=lambda: float(os.getenv("FEW_SHOT_MIN_SCORE", "7.0"))
    )

    # Memory Consolidation & Conflict Detection
    memory_consolidation_enabled: bool = Field(
        default_factory=lambda: os.getenv("MEMORY_CONSOLIDATION_ENABLED", "true").lower() == "true"
    )
    memory_consolidation_trigger_count: int = Field(
        default_factory=lambda: int(os.getenv("MEMORY_CONSOLIDATION_TRIGGER_COUNT", "20"))
    )
    memory_conflict_similarity_threshold: float = Field(
        default_factory=lambda: float(os.getenv("MEMORY_CONFLICT_SIMILARITY_THRESHOLD", "0.65"))
    )
    memory_consolidation_max_chars: int = Field(
        default_factory=lambda: int(os.getenv("MEMORY_CONSOLIDATION_MAX_CHARS", "4000"))
    )

    # ── 登录认证 ────────────────────────────────────────────
    # 演示部署到公网时必须开启，否则任何人都能创建任务、消耗你的 LLM 额度。
    # 账号密码走环境变量，不写进代码；auth_secret 用于签发 token，务必改掉默认值。
    auth_enabled: bool = Field(
        default_factory=lambda: os.getenv("AUTH_ENABLED", "true").lower() == "true"
    )
    auth_username: str = Field(
        default_factory=lambda: os.getenv("AUTH_USERNAME", "admin")
    )
    auth_password: str = Field(
        default_factory=lambda: os.getenv("AUTH_PASSWORD", "admin123")
    )
    # 签名密钥。默认值仅供本地开发；部署时一定要在 .env 里换成随机长串，
    # 否则别人拿默认值就能自己伪造出合法 token。
    auth_secret: str = Field(
        default_factory=lambda: os.getenv("AUTH_SECRET", "change-me-in-production")
    )
    auth_token_ttl_hours: int = Field(
        default_factory=lambda: max(1, int(os.getenv("AUTH_TOKEN_TTL_HOURS", "12")))
    )

    # 演示模式：压低单次流程耗时，但【保留全部智能体环节】（澄清/方案/工程/评审/审批
    # 都照走），因为那些正是要展示的东西。省时间靠三处：
    #   1. Solution 最多产出 demo_max_feature_modules 个功能模块
    #      —— 实测这是耗时的支配因素：模块数一多，Engineer 从单次生成转入分批，
    #         同一需求 Engineer 耗时从 110s 涨到 263s。
    #   2. input_normalize 不再调 LLM（模板需求本来就是规整文本，省一次整轮调用）
    #   3. 关闭 self-refine 的额外自我批评调用
    demo_mode: bool = Field(
        default_factory=lambda: os.getenv("DEMO_MODE", "false").lower() == "true"
    )
    demo_max_feature_modules: int = Field(
        default_factory=lambda: max(1, int(os.getenv("DEMO_MAX_FEATURE_MODULES", "3")))
    )

    # Engineer 分批生成技术设计
    # 一次性生成整份技术设计（架构+服务+全部API+全部表+代码骨架）在模块多时会撞
    # 模型单次输出上限、被截断后只能重试或降级。开启后改为"先出架构骨架，再按模块
    # 分批出 API/DB，最后合并"，每次调用都小而稳。
    engineer_batch_enabled: bool = Field(
        default_factory=lambda: os.getenv("ENGINEER_BATCH_ENABLED", "true").lower() == "true"
    )
    # 每批处理几个功能模块。模块总数 <= 该值时不分批（单次生成更省一次调用）。
    engineer_batch_size: int = Field(
        default_factory=lambda: max(1, int(os.getenv("ENGINEER_BATCH_SIZE", "4")))
    )

    # Repairer (轻量修复)
    repairer_enabled: bool = Field(
        default_factory=lambda: os.getenv("REPAIRER_ENABLED", "true").lower() == "true"
    )

    # Self-Refine
    # 演示模式下强制关闭：自我批评是额外一次 LLM 调用，只在覆盖率不达标时触发，
    # 对演示观感贡献不大却可能多花几十秒。
    # 注意这里必须【无条件覆盖】——之前写成"读 SELF_REFINE_ENABLED 的值"，
    # 而 .env 里显式设了 true，导致演示模式下它依然生效（实测踩过）。
    self_refine_enabled: bool = Field(
        default_factory=lambda: (
            False
            if os.getenv("DEMO_MODE", "false").lower() == "true"
            else os.getenv("SELF_REFINE_ENABLED", "true").lower() == "true"
        )
    )
    self_refine_max_chars: int = Field(
        default_factory=lambda: int(os.getenv("SELF_REFINE_MAX_CHARS", "800"))
    )

    # Multi-Agent Dialogue
    dialogue_enabled: bool = Field(
        default_factory=lambda: os.getenv("DIALOGUE_ENABLED", "false").lower() == "true"
    )
    dialogue_max_rounds: int = Field(
        default_factory=lambda: int(os.getenv("DIALOGUE_MAX_ROUNDS", "3"))
    )

    # Context Compression
    dialogue_compression_enabled: bool = Field(
        default_factory=lambda: os.getenv("DIALOGUE_COMPRESSION_ENABLED", "true").lower() == "true"
    )
    dialogue_compression_threshold: int = Field(
        default_factory=lambda: int(os.getenv("DIALOGUE_COMPRESSION_THRESHOLD", "2"))
    )
    context_budget_total_tokens: int = Field(
        default_factory=lambda: int(os.getenv("CONTEXT_BUDGET_TOTAL_TOKENS", "6000"))
    )

    # Plan-Verify (轻量并行规划)
    plan_variants_enabled: bool = Field(
        default_factory=lambda: os.getenv("PLAN_VARIANTS_ENABLED", "false").lower() == "true"
    )
    plan_variant_count: int = Field(
        default_factory=lambda: int(os.getenv("PLAN_VARIANT_COUNT", "2"))
    )

    # Paths
    data_dir: Path = Field(default=BASE_DIR / "data")
    templates_dir: Path = Field(default=BASE_DIR / "app" / "tools" / "templates")
    output_dir: Path = Field(default=BASE_DIR / "output")
    memory_persist_dir: Path = Field(
        default_factory=lambda: Path(os.getenv("MEMORY_PERSIST_DIR", BASE_DIR / "data" / "chroma"))
    )
    checkpoint_dir: Path = Field(
        default_factory=lambda: Path(os.getenv("CHECKPOINT_DIR", BASE_DIR / "data" / "checkpoints"))
    )


@lru_cache()
def get_settings() -> Settings:
    """获取全局配置单例。"""
    return Settings()
