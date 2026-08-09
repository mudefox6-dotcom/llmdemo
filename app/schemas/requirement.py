"""需求相关的 Pydantic Schema 定义。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class UserRequirement(BaseModel):
    """用户原始需求输入。"""

    raw_input: str = Field(..., description="用户原始自然语言需求")
    domain: Optional[str] = Field(None, description="业务领域提示（可选）")
    constraints: Optional[str] = Field(None, description="用户明确给出的约束条件")


class ClarifiedRequirement(BaseModel):
    """Planner 输出的需求澄清结果。"""

    goal: str = Field(..., description="项目目标")
    constraints: list[str] = Field(default_factory=list, description="约束条件")
    target_audience: str = Field("", description="目标受众")
    core_features: list[str] = Field(default_factory=list, description="核心功能列表")
    open_questions: list[str] = Field(default_factory=list, description="待澄清的问题")
    assumptions: list[str] = Field(default_factory=list, description="假设条件")
    priority: str = Field("medium", description="需求优先级: high / medium / low")
    alternatives: list[dict] = Field(
        default_factory=list,
        description="1-2个备选策略简述，每个含 goal/architecture_hint/core_features/tradeoff_note",
    )

    @property
    def needs_clarification(self) -> bool:
        """判断是否需要人工澄清：阻塞性问题达到阈值，或连核心功能都没识别出来。

        阈值由 CLARIFY_QUESTION_THRESHOLD 配置（默认 2）。配套约束在 Planner 提示词里：
        只问"不问就无法确定功能范围/流程/数据实体"的阻塞性问题、最多 3 条，
        技术选型与规模指标一律不问、能定的写进 assumptions。
        这样"打断用户"这件事才稀缺且有价值。
        """
        from app.core.config import get_settings

        threshold = get_settings().clarify_question_threshold
        return len(self.open_questions) >= threshold or len(self.core_features) == 0
