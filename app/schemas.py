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


class RebalanceSegment(BaseModel):
    """单条节目段。

    ``fixed_start_at_sec`` 非空表示该段是固定点：开场时刻不可移动（时长仍可被
    压缩）；``actual_end_at_sec`` 非空表示该段已结束，取值为实际结束时刻。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="节目段唯一标识；不能为空")
    planned_duration_sec: int = Field(
        ..., description="计划时长（秒），必须为正数且不短于最短时长"
    )
    min_duration_sec: int = Field(
        ..., description="最短时长（秒），必须为正数；延误分摊时的压缩下限"
    )
    fixed_start_at_sec: int | None = Field(
        default=None,
        description="固定开场时刻（秒，相对节目单原点）；不可移动，缺省表示非固定点",
    )
    actual_end_at_sec: int | None = Field(
        default=None,
        description="已结束段的实际结束时刻（秒）；已结束段必须构成节目单的连续前缀",
    )


class RebalanceRequest(BaseModel):
    """POST /schedule/rebalance 请求体。"""

    model_config = ConfigDict(extra="forbid")

    segments: list[RebalanceSegment] = Field(
        ..., description="按播出顺序排列的节目段"
    )


class RebalancedSegment(BaseModel):
    id: str
    planned_duration_sec: int = Field(..., description="原计划时长（秒）")
    adjusted_duration_sec: int = Field(
        ..., description="分摊后的时长（秒）；已结束段保持计划时长"
    )
    compressed_sec: int = Field(
        ..., description="被压缩吸收的秒数（planned - adjusted），已结束段为 0"
    )
    finished: bool = Field(..., description="该段是否已结束（已结束段不参与压缩）")


class FixedPointSlack(BaseModel):
    segment_id: str = Field(..., description="固定点所在段的 id")
    fixed_start_at_sec: int = Field(..., description="固定开场时刻（秒）")
    absorbed_sec: int = Field(
        ..., description="该固定点前的窗口内被压缩吸收的延误秒数"
    )
    remaining_sec: int = Field(
        ..., description="该固定点前的剩余秒数（发生压缩时为 0）"
    )


class RebalanceResponse(BaseModel):
    segments: list[RebalancedSegment] = Field(
        ..., description="按节目顺序排列的分摊结果"
    )
    fixed_points: list[FixedPointSlack] = Field(
        ..., description="各固定点的延误吸收与剩余秒数，按固定时刻升序"
    )


class ErrorBody(BaseModel):
    code: Literal[
        "INVALID_REQUEST",
        "DUPLICATE_ID",
        "INVALID_ID",
        "INVALID_CHANNEL",
        "INVALID_DURATION",
        "MISSING_ANCHOR",
        "DEPENDENCY_CYCLE",
        "NEGATIVE_START",
        "CHANGE_TARGET_NOT_FOUND",
        "INVALID_FIXED_START",
        "INVALID_ACTUAL_END",
        "INSUFFICIENT_MARGIN",
    ]
    message: str
    details: dict | None = None
