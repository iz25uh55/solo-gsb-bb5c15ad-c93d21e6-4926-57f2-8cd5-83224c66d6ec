"""app.schedule 的单元测试（仅依赖标准库，可直接 `python -m unittest discover`）。"""

from __future__ import annotations

import unittest

from app.compiler import CompileError
from app.schedule import rebalance_schedule


def seg(sid, planned, minimum, fixed=None):
    item = {
        "id": sid,
        "planned_duration_s": planned,
        "min_duration_s": minimum,
    }
    if fixed is not None:
        item["fixed_start_s"] = fixed
    return item


def body(segments, ends=()):
    return {
        "segments": segments,
        "actual_ends": [{"id": sid, "end_s": end} for sid, end in ends],
    }


def by_id(result):
    return {e["id"]: e for e in result["segments"]}


class BasicRebalanceTests(unittest.TestCase):
    def test_no_actual_ends_no_fixed_points_keeps_plan(self):
        result = rebalance_schedule(
            body([seg("a", 100, 60), seg("b", 200, 100)])
        )
        self.assertEqual(by_id(result)["a"]["adjusted_duration_s"], 100)
        self.assertEqual(by_id(result)["b"]["adjusted_duration_s"], 200)
        self.assertEqual(result["fixed_points"], [])

    def test_empty_segments(self):
        result = rebalance_schedule(body([]))
        self.assertEqual(result, {"segments": [], "fixed_points": []})

    def test_single_fixed_segment_not_compressed(self):
        # 固定开场段即使在固定点后，也不参与压缩
        result = rebalance_schedule(
            body([seg("a", 100, 60), seg("b", 50, 50, fixed=100)])
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["adjusted_duration_s"], 100)
        self.assertEqual(entries["b"]["adjusted_duration_s"], 50)
        fp = result["fixed_points"][0]
        self.assertEqual((fp["id"], fp["delay_s"], fp["remaining_s"]), ("b", 0, 0))

    def test_exact_fit_no_cut(self):
        # 窗口计划总长 100，固定点在 100：无需压缩
        result = rebalance_schedule(
            body([seg("a", 60, 40), seg("b", 40, 30), seg("f", 10, 10, fixed=100)])
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["cut_s"], 0)
        self.assertEqual(entries["b"]["cut_s"], 0)
        self.assertEqual(result["fixed_points"][0]["delay_s"], 0)

    def test_delay_absorbed_single_segment(self):
        # a 实际在 70 结束（计划 60，延误 10）；f 固定 100 -> 窗口 [b] 需追回 10
        result = rebalance_schedule(
            body(
                [seg("a", 60, 40), seg("b", 40, 30), seg("f", 10, 10, fixed=100)],
                ends=[("a", 70)],
            )
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["adjusted_duration_s"], 70)  # 已结束段=实际时长
        self.assertEqual(entries["b"]["adjusted_duration_s"], 30)  # 40-10
        fp = result["fixed_points"][0]
        self.assertEqual((fp["delay_s"], fp["recovered_s"], fp["remaining_s"]), (10, 10, 0))

    def test_delay_split_proportional_to_slack(self):
        # a 余量 50、b 余量 100（总余量 150），窗口计划总 300，f 固定 270 -> 延误 30
        # 30 * 50/150 = 10，30 * 100/150 = 20，整除
        result = rebalance_schedule(
            body(
                [seg("a", 100, 50), seg("b", 200, 100), seg("f", 10, 10, fixed=270)],
            )
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["cut_s"], 10)
        self.assertEqual(entries["b"]["cut_s"], 20)
        self.assertEqual(entries["a"]["adjusted_duration_s"], 90)
        self.assertEqual(entries["b"]["adjusted_duration_s"], 180)

    def test_remainder_largest_fraction_first(self):
        # 窗口余量 a=10、b=30、c=30（总 70），延误 20：
        # 份额 20*10/70 ≈ 2.857（余 60/70），20*30/70 ≈ 8.571（余 40/70）x2
        # 底数 2/8/8=18，余 2 秒：a 的小数份额最大先拿 1 秒，
        # b/c 小数相同，按节目顺序由 b 再拿 1 秒
        result = rebalance_schedule(
            body(
                [
                    seg("a", 100, 90),
                    seg("b", 100, 70),
                    seg("c", 100, 70),
                    seg("f", 1, 1, fixed=280),
                ]
            )
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["cut_s"], 3)
        self.assertEqual(entries["b"]["cut_s"], 9)
        self.assertEqual(entries["c"]["cut_s"], 8)

    def test_ties_broken_by_program_order(self):
        # 三段余量相同，延误 1：唯一余秒给最靠前的段
        result = rebalance_schedule(
            body(
                [
                    seg("a", 100, 90),
                    seg("b", 100, 90),
                    seg("c", 100, 90),
                    seg("f", 1, 1, fixed=299),
                ]
            )
        )
        entries = by_id(result)
        self.assertEqual(
            [entries[s]["cut_s"] for s in "abc"], [1, 0, 0]
        )

    def test_no_segment_below_minimum(self):
        # a 余量 10、b 余量 30，延误 20：a 至多切 10，其余落在 b
        result = rebalance_schedule(
            body(
                [seg("a", 50, 40), seg("b", 100, 70), seg("f", 1, 1, fixed=130)]
            )
        )
        entries = by_id(result)
        self.assertGreaterEqual(entries["a"]["adjusted_duration_s"], 40)
        self.assertGreaterEqual(entries["b"]["adjusted_duration_s"], 70)
        self.assertEqual(
            [entries[s]["cut_s"] for s in "ab"], [5, 15]  # 20*10/40=5, 20*30/40=15
        )

    def test_segment_with_zero_slack_never_cut(self):
        # b 已在最短时长（余量 0），全部分摊到 a
        result = rebalance_schedule(
            body(
                [seg("a", 100, 50), seg("b", 40, 40), seg("f", 1, 1, fixed=120)]
            )
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["cut_s"], 20)
        self.assertEqual(entries["b"]["cut_s"], 0)


class AheadOfScheduleTests(unittest.TestCase):
    def test_early_finish_leaves_gap_before_fixed_point(self):
        # a 计划 60，实际 45 结束（提前 15）；f 固定 100
        result = rebalance_schedule(
            body(
                [seg("a", 60, 40), seg("b", 40, 30), seg("f", 10, 10, fixed=100)],
                ends=[("a", 45)],
            )
        )
        entries = by_id(result)
        self.assertEqual(entries["b"]["adjusted_duration_s"], 40)  # 不压缩不拉长
        fp = result["fixed_points"][0]
        self.assertEqual((fp["delay_s"], fp["recovered_s"], fp["remaining_s"]), (0, 0, 15))


class MultipleFixedPointsTests(unittest.TestCase):
    def test_windows_are_independent(self):
        # 窗口1：a 计划 100，f1 固定 90 -> 延误 10，a 压缩 10（90~110）
        # 窗口2：f1 结束 110 + c 计划 60 = 170，f2 固定 170 -> 准点
        result = rebalance_schedule(
            body(
                [
                    seg("a", 100, 70),
                    seg("f1", 20, 20, fixed=90),
                    seg("c", 60, 40),
                    seg("f2", 5, 5, fixed=170),
                ]
            )
        )
        entries = by_id(result)
        self.assertEqual(entries["a"]["cut_s"], 10)
        self.assertEqual(entries["f1"]["adjusted_duration_s"], 20)
        self.assertEqual(entries["c"]["cut_s"], 0)
        fps = {fp["id"]: fp for fp in result["fixed_points"]}
        self.assertEqual(fps["f1"]["delay_s"], 10)
        self.assertEqual(fps["f2"]["delay_s"], 0)
        self.assertEqual(fps["f2"]["remaining_s"], 0)

    def test_actual_end_anchor_resets_cursor(self):
        # a 计划 30 实际 60 结束（延误 30）；b 固定 200（前面已无可移动段，提前 140）；
        # b 200~300；c 计划 100 -> 400，f 固定 400 准点
        result = rebalance_schedule(
            body(
                [
                    seg("a", 30, 20),
                    seg("b", 100, 80, fixed=200),
                    seg("c", 100, 80),
                    seg("f", 10, 10, fixed=400),
                ],
                ends=[("a", 60)],
            )
        )
        fps = {fp["id"]: fp for fp in result["fixed_points"]}
        # 窗口1 为空（b 紧接已结束段，固定点 200 远在光标之后）
        self.assertEqual(fps["b"]["segment_ids"], [])
        self.assertEqual(fps["b"]["remaining_s"], 140)
        # b 结束于 300；c 计划 100 -> 400，f 固定 400 准点
        self.assertEqual(fps["f"]["delay_s"], 0)


class InsufficientSlackTests(unittest.TestCase):
    def test_shortage_returns_structured_422(self):
        # 窗口可压缩余量共 20，延误 50
        with self.assertRaises(CompileError) as ctx:
            rebalance_schedule(
                body(
                    [seg("a", 100, 90), seg("b", 100, 90), seg("f", 1, 1, fixed=150)]
                )
            )
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_SLACK")
        details = ctx.exception.details
        self.assertEqual(details["unrecoverable_s"], 30)
        fp = details["fixed_points"][0]
        self.assertEqual(fp["id"], "f")
        self.assertEqual(fp["required_recovery_s"], 50)
        self.assertEqual(fp["recoverable_s"], 20)
        self.assertEqual(fp["shortage_s"], 30)
        self.assertEqual(fp["segment_ids"], ["a", "b"])

    def test_shortage_with_actual_end(self):
        # a 已结束延误 40（计划 60 实际 100）；b 余量 20；f 固定 120
        # 光标 100 + b 计划 40 = 140 > 120 -> 需追回 20，刚好等于余量
        with self.assertRaises(CompileError) as ctx:
            rebalance_schedule(
                body(
                    [
                        seg("a", 60, 50),
                        seg("b", 40, 30),
                        seg("f", 1, 1, fixed=110),
                    ],
                    ends=[("a", 100)],
                )
            )
        # 100+40-110 = 30 > 余量 10 -> 缺口 20
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_SLACK")
        self.assertEqual(ctx.exception.details["unrecoverable_s"], 20)

    def test_multiple_violations_aggregated(self):
        # 窗口1：a 计划 100、余量 10，f1 固定 80 -> 需追回 20，缺口 10
        # f1 80~90；窗口2：c 计划 100、余量 5，f2 固定 160 -> 需追回 30，缺口 25
        with self.assertRaises(CompileError) as ctx:
            rebalance_schedule(
                body(
                    [
                        seg("a", 100, 90),
                        seg("f1", 10, 10, fixed=80),
                        seg("c", 100, 95),
                        seg("f2", 10, 10, fixed=160),
                    ]
                )
            )
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_SLACK")
        self.assertEqual(
            sorted(fp["id"] for fp in ctx.exception.details["fixed_points"]),
            ["f1", "f2"],
        )
        self.assertEqual(ctx.exception.details["unrecoverable_s"], 35)


class ValidationTests(unittest.TestCase):
    def test_duplicate_segment_id(self):
        with self.assertRaises(CompileError) as ctx:
            rebalance_schedule(body([seg("a", 10, 5), seg("a", 10, 5)]))
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")

    def test_empty_id(self):
        with self.assertRaises(CompileError) as ctx:
            rebalance_schedule(body([seg("", 10, 5)]))
        self.assertEqual(ctx.exception.code, "INVALID_ID")

    def test_non_positive_planned_duration(self):
        for bad in (0, -5):
            with self.assertRaises(CompileError) as ctx:
                rebalance_schedule(body([seg("a", bad, 5)]))
            self.assertEqual(ctx.exception.code, "INVALID_DURATION")

    def test_invalid_min_duration(self):
        for planned, minimum in ((10, 0), (10, -1), (10, 11)):
            with self.assertRaises(CompileError) as ctx:
                rebalance_schedule(body([seg("a", planned, minimum)]))
            self.assertEqual(ctx.exception.code, "INVALID_MIN_DURATION")

    def test_actual_end_unknown_id(self):
        with self.assertRaises(CompileError) as ctx:
            rebalance_schedule(
                body([seg("a", 10, 5)], ends=[("ghost", 5)])
            )
        self.assertEqual(ctx.exception.code, "ACTUAL_END_UNKNOWN_ID")

    def test_duplicate_actual_end(self):
        with self.assertRaises(CompileError) as ctx:
            rebalance_schedule(
                body(
                    [seg("a", 10, 5), seg("b", 10, 5)],
                    ends=[("a", 5), ("a", 6)],
                )
            )
        self.assertEqual(ctx.exception.code, "INVALID_ACTUAL_END")

    def test_non_prefix_actual_ends(self):
        with self.assertRaises(CompileError) as ctx:
            rebalance_schedule(
                body(
                    [seg("a", 10, 5), seg("b", 10, 5)],
                    ends=[("b", 20)],
                )
            )
        self.assertEqual(ctx.exception.code, "INVALID_ACTUAL_END")
        self.assertEqual(ctx.exception.details["ids"], ["a"])

    def test_negative_actual_end(self):
        with self.assertRaises(CompileError) as ctx:
            rebalance_schedule(body([seg("a", 10, 5)], ends=[("a", -1)]))
        self.assertEqual(ctx.exception.code, "INVALID_ACTUAL_END")

    def test_actual_end_must_be_after_actual_start(self):
        # 第一个段（光标 0）实际结束时刻为 0：实际时长非正，非法
        with self.assertRaises(CompileError) as ctx:
            rebalance_schedule(
                body([seg("a", 10, 5)], ends=[("a", 0)])
            )
        self.assertEqual(ctx.exception.code, "INVALID_ACTUAL_END")

    def test_finished_fixed_segment_start_conflict(self):
        # 节目已开播，a 是固定段且固定开场 10，但光标为 0 时不一致
        with self.assertRaises(CompileError) as ctx:
            rebalance_schedule(
                body([seg("a", 10, 5, fixed=10)], ends=[("a", 15)])
            )
        self.assertEqual(ctx.exception.code, "FIXED_START_CONFLICT")
        self.assertEqual(ctx.exception.details["actual_start_s"], 0)

    def test_finished_fixed_segment_at_origin_ok(self):
        # 首个段固定开场 0 且已结束：合法
        result = rebalance_schedule(
            body([seg("a", 10, 5, fixed=0), seg("b", 20, 10)], ends=[("a", 12)])
        )
        self.assertEqual(by_id(result)["a"]["adjusted_duration_s"], 12)


class TrailingWindowTests(unittest.TestCase):
    def test_trailing_segments_after_last_fixed_keep_plan(self):
        # f 固定 100，其后 x/y 无后续固定点约束，保持计划时长
        result = rebalance_schedule(
            body(
                [
                    seg("a", 100, 80),
                    seg("f", 10, 10, fixed=100),
                    seg("x", 50, 30),
                    seg("y", 70, 40),
                ]
            )
        )
        entries = by_id(result)
        self.assertEqual(entries["x"]["adjusted_duration_s"], 50)
        self.assertEqual(entries["y"]["adjusted_duration_s"], 70)
        self.assertEqual(len(result["fixed_points"]), 1)

    def test_trailing_segments_after_actual_end_keep_plan(self):
        result = rebalance_schedule(
            body(
                [seg("a", 10, 5), seg("b", 20, 10), seg("c", 30, 15)],
                ends=[("a", 12)],
            )
        )
        entries = by_id(result)
        self.assertEqual(entries["b"]["adjusted_duration_s"], 20)
        self.assertEqual(entries["c"]["adjusted_duration_s"], 30)
        self.assertEqual(result["fixed_points"], [])


if __name__ == "__main__":
    unittest.main()
