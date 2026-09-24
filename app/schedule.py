"""节目单延误分摊核心逻辑（纯标准库实现，不依赖 Web 框架）。

规则概述
--------
* 节目段按播出顺序排列，每段给出计划时长 ``planned_duration_s`` 与不可击穿的
  最短时长 ``min_duration_s``；部分段携带不可移动的固定开场时刻
  ``fixed_start_s``（固定点）。
* 已结束段通过 ``actual_ends`` 提交实际结束时刻（秒），必须构成节目的前缀；
  最后一个实际结束时刻即为当前调度光标，后续可移动段从该光标连续排开。
* 每个固定点把节目单切成彼此独立的分摊窗口：光标与固定开场时刻之间的可
  移动段必须在固定点前播完。延误秒数 = 光标 + 窗口计划总时长 - 固定开场时刻。
* 延误为正时，按各段可压缩余量（计划时长 - 最短时长）比例摊派压缩秒数；
  整除不尽的余数采用最大余数法分配——小数份额从大到小优先，同分按节目顺序
  （靠前的段优先）。任何段调整后不得低于最短时长。
* 窗口可压缩余量总和不足以吸收延误时抛出 ``INSUFFICIENT_SLACK``，``details``
  中逐固定点给出缺口，并合计无法追回秒数。
* 延误为负（播出提前）时不压缩也不拉长，各段保持计划时长，固定点前剩余
  秒数（空档）如实回传。
"""

from __future__ import annotations

from typing import Any

from .compiler import CompileError


def rebalance_schedule(body: dict[str, Any]) -> dict[str, Any]:
    """根据固定点与已结束段实际结束时刻，分摊延误并返回调整后的时长。

    :raises CompileError: 节目段/实际结束时刻非法，或固定点前余量不足时抛出，
        ``code`` 可用于程序化区分错误类型。
    """
    items: list[dict[str, Any]] = body["segments"]
    actual_items: list[dict[str, Any]] = body.get("actual_ends") or []

    # 1) 建立 id 索引并检测重复 id
    by_id: dict[str, dict[str, Any]] = {}
    ids: list[str] = []
    duplicates: list[str] = []
    for item in items:
        sid = item["id"]
        if sid in by_id:
            if sid not in duplicates:
                duplicates.append(sid)
            continue
        by_id[sid] = item
        ids.append(sid)

    if duplicates:
        raise CompileError(
            "DUPLICATE_ID",
            f"存在重复的节目段 id: {', '.join(sorted(duplicates))}",
            {"duplicates": sorted(duplicates)},
        )

    invalid_ids = [sid for sid in ids if sid == ""]
    if invalid_ids:
        raise CompileError(
            "INVALID_ID",
            "节目段 id 不能为空",
            {"ids": invalid_ids},
        )

    # 2) 时长合法性：计划时长为正；最短时长为正且不超过计划时长
    bad_planned = [
        sid for sid in ids if by_id[sid]["planned_duration_s"] <= 0
    ]
    if bad_planned:
        raise CompileError(
            "INVALID_DURATION",
            "planned_duration_s 必须为正数",
            {"ids": bad_planned},
        )

    bad_min = [
        sid
        for sid in ids
        if by_id[sid]["min_duration_s"] <= 0
        or by_id[sid]["min_duration_s"] > by_id[sid]["planned_duration_s"]
    ]
    if bad_min:
        raise CompileError(
            "INVALID_MIN_DURATION",
            "min_duration_s 必须为正数，且不得大于 planned_duration_s",
            {"ids": bad_min},
        )

    # 3) 解析已结束段的实际结束时刻
    ends: dict[str, int] = {}
    duplicate_ends: list[str] = []
    for entry in actual_items:
        sid = entry["id"]
        if sid in ends or sid in duplicate_ends:
            duplicate_ends.append(sid)
            continue
        if sid not in by_id:
            raise CompileError(
                "ACTUAL_END_UNKNOWN_ID",
                f"实际结束时刻引用了不存在的节目段 id: {sid}",
                {"id": sid},
            )
        ends[sid] = entry["end_s"]

    if duplicate_ends:
        raise CompileError(
            "INVALID_ACTUAL_END",
            "同一个节目段只能提交一次实际结束时刻",
            {"ids": sorted(set(duplicate_ends))},
        )

    bad_end = [sid for sid in ids if sid in ends and ends[sid] < 0]
    if bad_end:
        raise CompileError(
            "INVALID_ACTUAL_END",
            "实际结束时刻不能为负",
            {"ids": bad_end},
        )

    # 4) 已结束段必须构成节目前缀（直播事实：不可能跳过尚未结束的段）
    finished = [sid in ends for sid in ids]
    if any(finished):
        last_finished = max(i for i, done in enumerate(finished) if done)
        missing = [ids[i] for i in range(last_finished + 1) if not finished[i]]
        if missing:
            raise CompileError(
                "INVALID_ACTUAL_END",
                "已结束段必须构成节目的连续前缀，中间不能夹杂未结束段",
                {"ids": missing},
            )

    # 5) 顺序扫掠：固定点切出分摊窗口，逐个窗口摊派
    cursor = 0
    window: list[str] = []
    adjusted: dict[str, int] = {}
    cuts: dict[str, int] = {}
    fixed_points: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []

    def close_window(target: int, fp_id: str) -> None:
        """把当前窗口对 ``target`` 固定点结算，结果写入外层映射。"""
        planned_total = sum(by_id[s]["planned_duration_s"] for s in window)
        delay = cursor + planned_total - target

        if delay <= 0:
            # 准点或提前：保持计划时长，提前量成为固定点前的空档
            for sid in window:
                adjusted[sid] = by_id[sid]["planned_duration_s"]
                cuts[sid] = 0
            fixed_points.append(
                {
                    "id": fp_id,
                    "fixed_start_s": target,
                    "segment_ids": list(window),
                    "delay_s": 0,
                    "recovered_s": 0,
                    "remaining_s": -delay,
                }
            )
            return

        slacks = [
            by_id[s]["planned_duration_s"] - by_id[s]["min_duration_s"]
            for s in window
        ]
        capacity = sum(slacks)
        if delay > capacity:
            # 余量不足：记录缺口（best-effort 压到最短，供后续窗口独立结算）
            violations.append(
                {
                    "id": fp_id,
                    "fixed_start_s": target,
                    "segment_ids": list(window),
                    "required_recovery_s": delay,
                    "recoverable_s": capacity,
                    "shortage_s": delay - capacity,
                }
            )
            for sid in window:
                adjusted[sid] = by_id[sid]["min_duration_s"]
                cuts[sid] = (
                    by_id[sid]["planned_duration_s"]
                    - by_id[sid]["min_duration_s"]
                )
            return

        allocated = _apportion_cuts(delay, slacks)
        for sid, cut in zip(window, allocated):
            adjusted[sid] = by_id[sid]["planned_duration_s"] - cut
            cuts[sid] = cut
        fixed_points.append(
            {
                "id": fp_id,
                "fixed_start_s": target,
                "segment_ids": list(window),
                "delay_s": delay,
                "recovered_s": delay,
                "remaining_s": 0,
            }
        )

    for sid in ids:
        item = by_id[sid]
        planned = item["planned_duration_s"]
        fixed_start = item.get("fixed_start_s")

        if sid in ends:
            end = ends[sid]
            # 已结束的固定段：实际开场时刻必须与固定开场时刻一致
            if fixed_start is not None and cursor != fixed_start:
                raise CompileError(
                    "FIXED_START_CONFLICT",
                    (
                        f"已结束的固定段 {sid} 实际开场时刻为 {cursor} s，"
                        f"与其固定开场时刻 {fixed_start} s 不一致"
                    ),
                    {
                        "id": sid,
                        "fixed_start_s": fixed_start,
                        "actual_start_s": cursor,
                    },
                )
            if end <= cursor:
                raise CompileError(
                    "INVALID_ACTUAL_END",
                    (
                        f"节目段 {sid} 的实际结束时刻 {end} s 不得晚于/等于其"
                        f"实际开场时刻 {cursor} s"
                    ),
                    {"id": sid, "start_s": cursor, "end_s": end},
                )
            # 前缀校验保证窗口此时必为空；实际时长（可能超时）照实记录
            window = []
            adjusted[sid] = end - cursor
            cuts[sid] = planned - (end - cursor)
            cursor = end
        elif fixed_start is not None:
            close_window(fixed_start, sid)
            window = []
            # 固定段本身不参与压缩，按计划时长继续向后排
            adjusted[sid] = planned
            cuts[sid] = 0
            cursor = fixed_start + planned
        else:
            window.append(sid)

    # 节目单尾部没有后续固定点的可移动段不受约束，保持计划时长
    for sid in window:
        adjusted[sid] = by_id[sid]["planned_duration_s"]
        cuts[sid] = 0

    if violations:
        unrecoverable = sum(v["shortage_s"] for v in violations)
        if len(violations) == 1:
            v = violations[0]
            message = (
                f"固定点 {v['id']} 前可压缩余量不足：需追回 "
                f"{v['required_recovery_s']} s，最多追回 {v['recoverable_s']} s，"
                f"无法追回 {v['shortage_s']} s"
            )
        else:
            message = (
                f"共有 {len(violations)} 个固定点前可压缩余量不足，"
                f"合计无法追回 {unrecoverable} s"
            )
        raise CompileError(
            "INSUFFICIENT_SLACK",
            message,
            {"unrecoverable_s": unrecoverable, "fixed_points": violations},
        )

    return {
        "segments": [
            {
                "id": sid,
                "planned_duration_s": by_id[sid]["planned_duration_s"],
                "adjusted_duration_s": adjusted[sid],
                "cut_s": cuts[sid],
                "fixed_start_s": by_id[sid].get("fixed_start_s"),
            }
            for sid in ids
        ],
        "fixed_points": fixed_points,
    }


def _apportion_cuts(amount: int, slacks: list[int]) -> list[int]:
    """把 ``amount`` 秒按 ``slacks`` 权重比例摊派为整数秒。

    先取每份的整数部分，再把余数秒按小数份额（``amount*w mod 总余量``，
    分母相同，可直接比较余数）从大到小补 1 秒；余数相同则节目顺序靠前的
    优先。调用方须保证 ``0 <= amount <= sum(slacks)``。
    """
    total_slack = sum(slacks)
    numerators = [amount * slack for slack in slacks]
    cuts = [num // total_slack for num in numerators]
    remainders = [num % total_slack for num in numerators]

    left = amount - sum(cuts)
    order = sorted(range(len(slacks)), key=lambda i: (-remainders[i], i))
    for i in order[:left]:
        cuts[i] += 1
    return cuts
