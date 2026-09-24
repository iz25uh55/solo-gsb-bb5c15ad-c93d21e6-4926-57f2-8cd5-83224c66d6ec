"""请求 / 响应的 Pydantic 模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CommandItem(BaseModel):
    """单条导播口令。

    ``anchor`` 为 ``START`` 时表示以时间原点（0 ms）为锚；也可以填另一条
    口令的 id，此时 ``offset_ms`` 相对该锚点口令的 **结束时刻** 计算。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="口令唯一标识；不能为空，也不能是保留字 START")
    channel: str = Field(..., description="通道标识；只有同一通道内的口令才会产生冲突")
    anchor: str = Field(
        default="START",
        description="锚点：START（时间原点）或另一条口令的 id",
    )
    offset_ms: int = Field(
        default=0,
        description="相对锚点结束时刻的偏移（毫秒），允许为负",
    )
    duration_ms: int = Field(..., description="持续时长（毫秒），必须为正数")


class TimelineEntry(BaseModel):
    id: str
    channel: str
    anchor: str
    offset_ms: int
    duration_ms: int
    start_ms: int = Field(..., description="解析后的起点（毫秒，>= 0）")
    end_ms: int = Field(..., description="解析后的结束时刻（毫秒）")


class ConflictPair(BaseModel):
    channel: str = Field(..., description="冲突所在通道")
    a: str = Field(..., description="冲突对中 id 较小者")
    b: str = Field(..., description="冲突对中 id 较大者")
    a_interval: list[int] = Field(..., description="[a 的起点, a 的结束时刻)")
    b_interval: list[int] = Field(..., description="[b 的起点, b 的结束时刻)")


class CompileResponse(BaseModel):
    timeline: list[TimelineEntry] = Field(
        ..., description="按 (start_ms, id) 升序排列的时间线"
    )
    conflicts: list[ConflictPair] = Field(
        ..., description="同通道半开区间重叠的冲突 id 对，按 (a, b, channel) 排序"
    )


class CommandChange(BaseModel):
    """对一条现有口令的改动：指定 id，并提供 offset_ms / duration_ms 新值。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="要改动的现有口令 id（须存在于口令数组中）")
    offset_ms: int | None = Field(
        default=None,
        description="新的相对锚点结束时刻偏移（毫秒），允许为负；不填则保留原值",
    )
    duration_ms: int | None = Field(
        default=None,
        description="新的持续时长（毫秒），必须为正数；不填则保留原值",
    )

    @model_validator(mode="after")
    def require_at_least_one_new_value(self) -> "CommandChange":
        if self.offset_ms is None and self.duration_ms is None:
            raise ValueError(
                "offset_ms 与 duration_ms 至少需要提供一个新值"
            )
        return self


class PreviewRequest(BaseModel):
    """POST /preview 请求体：/compile 同格式口令数组 + 一项改动。"""

    model_config = ConfigDict(extra="forbid")

    commands: list[CommandItem] = Field(
        ..., description="口令数组，格式与 POST /compile 的请求体一致"
    )
    change: CommandChange = Field(..., description="对现有口令的一项改动")


class PreviewResponse(BaseModel):
    changed_ids: list[str] = Field(
        ...,
        description=(
            "时间起止发生变化的口令 id（含锚点依赖传导），按 id 升序"
        ),
    )
    added_conflicts: list[ConflictPair] = Field(
        ...,
        description=(
            "改动后新增的同通道冲突对，携带改动后版本的区间，"
            "按 (a, b, channel) 排序"
        ),
    )
    removed_conflicts: list[ConflictPair] = Field(
        ...,
        description=(
            "改动后消失的同通道冲突对，携带原版本的区间，"
            "按 (a, b, channel) 排序"
        ),
    )


class SegmentItem(BaseModel):
    """一个按播出顺序排列的节目段。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="节目段唯一标识；不能为空")
    planned_duration_s: int = Field(
        ..., description="计划时长（秒），必须为正数"
    )
    min_duration_s: int = Field(
        ...,
        description="最短时长（秒），必须为正数，且不得大于计划时长",
    )
    fixed_start_s: int | None = Field(
        default=None,
        description=(
            "不可移动的固定开场时刻（距节目开播原点的秒数）；"
            "不填表示该段开场时刻可随前面段落移动"
        ),
    )


class ActualEndItem(BaseModel):
    """已结束段的实际结束时刻。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="已结束的节目段 id")
    end_s: int = Field(
        ...,
        description="实际结束时刻（距节目开播原点的秒数，不能为负）",
    )


class RebalanceRequest(BaseModel):
    """POST /schedule/rebalance 请求体。"""

    model_config = ConfigDict(extra="forbid")

    segments: list[SegmentItem] = Field(
        ..., description="按播出顺序排列的节目段"
    )
    actual_ends: list[ActualEndItem] = Field(
        default_factory=list,
        description=(
            "已结束段的实际结束时刻；必须构成节目的连续前缀，"
            "缺省表示节目尚未开始，按完整计划单分摊"
        ),
    )


class AdjustedSegment(BaseModel):
    id: str
    planned_duration_s: int = Field(..., description="原计划时长（秒）")
    adjusted_duration_s: int = Field(
        ...,
        description=(
            "调整后的时长（秒）；已结束段为实际时长，其余为分摊压缩后的时长"
        ),
    )
    cut_s: int = Field(
        ...,
        description="被压缩的秒数（>=0；已结束段为相对计划的超时/提前净值）"
    )
    fixed_start_s: int | None = Field(
        ..., description="该段的固定开场时刻；无固定点时为 null"
    )


class FixedPointResult(BaseModel):
    id: str = Field(..., description="固定段（分摊窗口后边界）的节目段 id")
    fixed_start_s: int = Field(..., description="固定开场时刻（秒）")
    segment_ids: list[str] = Field(
        ..., description="该固定点前参与分摊的可移动段 id，按节目顺序"
    )
    delay_s: int = Field(
        ..., description="固定点前的延误秒数（提前时为 0）"
    )
    recovered_s: int = Field(
        ..., description="已通过压缩追回的秒数"
    )
    remaining_s: int = Field(
        ...,
        description=(
            "固定点前剩余秒数：准点追回后为 0；提前播出时为可保留的空档秒数"
        ),
    )


class RebalanceResponse(BaseModel):
    segments: list[AdjustedSegment] = Field(
        ..., description="按节目顺序排列的调整后时长"
    )
    fixed_points: list[FixedPointResult] = Field(
        ...,
        description="各固定点的延误分摊结果，按固定开场时刻（节目顺序）排列",
    )


class ErrorBody(BaseModel):
    code: Literal[
        "INVALID_REQUEST",
        "DUPLICATE_ID",
        "INVALID_ID",
        "INVALID_CHANNEL",
        "INVALID_DURATION",
        "INVALID_MIN_DURATION",
        "INVALID_ACTUAL_END",
        "ACTUAL_END_UNKNOWN_ID",
        "FIXED_START_CONFLICT",
        "INSUFFICIENT_SLACK",
        "MISSING_ANCHOR",
        "DEPENDENCY_CYCLE",
        "NEGATIVE_START",
        "CHANGE_TARGET_NOT_FOUND",
    ]
    message: str
    details: dict | None = None
