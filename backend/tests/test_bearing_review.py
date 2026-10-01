"""承载力与锈蚀阈值复核矩阵测试。

只用标准库 unittest，运行：cd backend && python3 -m unittest discover -s tests
"""
from __future__ import annotations

import unittest
from datetime import datetime

from app.seed import SEED_ROWS
from app.services import bearing_review as review_mod
from app.services.bearing_review import (
    ADVICE_INSPECT,
    ADVICE_REPAIR,
    ADVICE_REPLACE,
    REVIEW_DONE,
    REVIEW_OPEN,
    GroupCommitError,
    ReviewRuleError,
    bearing_review_service as service,
    evaluate,
)
from app.store import store

TODAY = datetime(2026, 10, 1)


def reset_state() -> None:
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    service._version = "v1"


def bearing(code: str) -> dict:
    return next(row for row in store.rows("bearing") if row["支座编号"] == code)


class MatrixEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()

    def test_basic_advice_levels(self) -> None:
        self.assertEqual(evaluate(bearing("BEAR-0001"), version="v1", today=TODAY)["建议"], ADVICE_INSPECT)
        self.assertEqual(evaluate(bearing("BEAR-0005"), version="v1", today=TODAY)["建议"], ADVICE_REPAIR)
        self.assertEqual(evaluate(bearing("BEAR-0003"), version="v1", today=TODAY)["建议"], ADVICE_REPLACE)

    def test_hardline_overrides_special_conclusion(self) -> None:
        # BEAR-0006 承载力比 0.972，超过硬线，即使无专项结论也必须更换。
        result = evaluate(bearing("BEAR-0006"), version="v1", today=TODAY)
        self.assertEqual(result["建议"], ADVICE_REPLACE)
        self.assertIn("硬线", result["裁定依据"])

    def test_conflict_adjudicated_by_capacity_and_special(self) -> None:
        # 锈蚀 28% 达到维修档，但承载力富余（0.73）且专项「可继续使用」→ 共同裁定降一档为巡检。
        result = evaluate(bearing("BEAR-0002"), version="v1", today=TODAY)
        self.assertTrue(result["阈值冲突"])
        self.assertEqual(result["建议"], ADVICE_INSPECT)
        self.assertIn("专项结论", result["裁定依据"])

        # 锈蚀 55% 达更换档，专项「观察使用」+承载力富余 → 最多降一档为维修。
        result_8 = evaluate(bearing("BEAR-0008"), version="v1", today=TODAY)
        self.assertTrue(result_8["阈值冲突"])
        self.assertEqual(result_8["建议"], ADVICE_REPAIR)

    def test_overdue_cycle_forces_inspection(self) -> None:
        # BEAR-0004 最近检查 2026-03-02，v1 周期阈值 180 天，已逾期。
        result = evaluate(bearing("BEAR-0004"), version="v1", today=TODAY)
        self.assertIsNotNone(result["逾期天数"])
        self.assertGreater(result["逾期天数"], 180)
        self.assertIn("逾期", result["裁定依据"])

    def test_historical_bearing_flag(self) -> None:
        from app.services.bearing_review import is_historical

        self.assertTrue(is_historical(bearing("BEAR-0007")))
        self.assertFalse(is_historical(bearing("BEAR-0001")))


class BatchCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()

    def test_batch_writes_back_to_three_destinations(self) -> None:
        report = service.run_batch(today=TODAY)
        self.assertEqual(report["reviewed"], 7)
        self.assertEqual(report["skipped_history"], 1)

        # 回写一：支座台账
        b1 = bearing("BEAR-0001")
        self.assertEqual(b1["复核建议"], ADVICE_INSPECT)
        self.assertEqual(b1["复核风险"], "低")
        self.assertEqual(b1["复核版本"], "v1")
        self.assertFalse(b1["复核已闭环"])

        # 回写二：桥梁定检清单（每桥每版本一张聚合单）
        bridge_rows = [
            row for row in store.rows("bridge") if row.get("复核键") == "BEARING-REVIEW::振兴路立交桥::v1"
        ]
        self.assertEqual(len(bridge_rows), 1)
        self.assertEqual(bridge_rows[0]["检测类型"], "支座专项复核")
        self.assertIn("更换", bridge_rows[0]["主要病害"])

        # 回写三：工程待办（振兴路：0001 巡检、0002 冲突后降为巡检；河滨：更换）
        codes = {row.get("待办键") for row in store.rows("project")}
        self.assertIn("TODO-BEAR::振兴路立交桥::巡检", codes)
        self.assertIn("TODO-BEAR::河滨大桥::更换", codes)
        self.assertIn("TODO-BEAR::南站高架桥::维修", codes)
        self.assertNotIn("TODO-BEAR::振兴路立交桥::维修", codes)
        self.assertNotIn("TODO-BEAR::振兴路立交桥::更换", codes)

    def test_historical_bearing_keeps_original_record(self) -> None:
        service.run_batch(today=TODAY)
        legacy = [
            row for row in store.rows("bearing_review") if row.get("历史遗留")
        ]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0]["支座编号"], "BEAR-0007")
        self.assertEqual(legacy[0]["检查单状态"], REVIEW_DONE)

        b7 = bearing("BEAR-0007")
        self.assertNotIn("复核建议", b7)  # 历史台账不被矩阵改写

    def test_repeat_run_is_idempotent(self) -> None:
        first = service.run_batch(today=TODAY)
        checklists_after_first = len(store.rows("bearing_review"))
        snapshot_bridge = [dict(r) for r in store.rows("bridge")]
        snapshot_project = [dict(r) for r in store.rows("project")]

        second = service.run_batch(today=TODAY)
        self.assertEqual(first["advice_counts"], second["advice_counts"])
        self.assertEqual(len(store.rows("bearing_review")), checklists_after_first)
        self.assertEqual(
            [(r.get("复核键"), r.get("主要病害"), r.get("检测状态")) for r in store.rows("bridge")],
            [(r.get("复核键"), r.get("主要病害"), r.get("检测状态")) for r in snapshot_bridge],
        )
        self.assertEqual(
            [(r.get("待办键"), r.get("工程名称"), r.get("status")) for r in store.rows("project")],
            [(r.get("待办键"), r.get("工程名称"), r.get("status")) for r in snapshot_project],
        )

    def test_failed_group_rolls_back_without_partial_rewrite(self) -> None:
        # 让「河滨大桥」这一组在写定检清单时失败；该组此前已写的台账/检查单必须一起回滚。
        original = service._write_back_bridge

        def poisoned(bridge_name, checklists, version, today):  # type: ignore[no-untyped-def]
            if bridge_name == "河滨大桥":
                raise RuntimeError("模拟定检清单写库失败")
            return original(bridge_name, checklists, version, today)

        service._write_back_bridge = poisoned  # type: ignore[method-assign]
        try:
            with self.assertRaises(GroupCommitError):
                service.run_batch(today=TODAY)
        finally:
            service._write_back_bridge = original  # type: ignore[method-assign]

        # 失败组的支座台账未被改写
        for code in ("BEAR-0003", "BEAR-0004"):
            self.assertNotIn("复核建议", bearing(code))
        # 失败组没有残留检查单
        self.assertFalse(
            [c for c in store.rows("bearing_review") if c.get("所属桥梁") == "河滨大桥"]
        )
        # 失败组没有残留待办
        self.assertFalse(
            [p for p in store.rows("project") if p.get("施工路段") == "河滨大桥" and p.get("待办键")]
        )

    def test_groups_before_failed_group_remain_committed(self) -> None:
        # 分组事务：先成功的组保留，失败的组回滚。
        original = service._write_back_bridge

        def poisoned(bridge_name, checklists, version, today):  # type: ignore[no-untyped-def]
            if bridge_name == "河滨大桥":
                raise RuntimeError("boom")
            return original(bridge_name, checklists, version, today)

        service._write_back_bridge = poisoned  # type: ignore[method-assign]
        try:
            with self.assertRaises(GroupCommitError):
                service.run_batch(today=TODAY)
        finally:
            service._write_back_bridge = original  # type: ignore[method-assign]

        # 南站高架桥字母序最靠前，已成功提交。
        self.assertIn("复核建议", bearing("BEAR-0008"))

    def test_missing_and_duplicate_codes_reject_whole_batch(self) -> None:
        with self.assertRaises(ReviewRuleError):
            service.run_batch(bearing_codes=["BEAR-0001", "BEAR-9999"], today=TODAY)
        self.assertNotIn("复核建议", bearing("BEAR-0001"))

        bearing("BEAR-0002")["支座编号"] = "BEAR-0001"
        with self.assertRaises(ReviewRuleError):
            service.run_batch(today=TODAY)
        self.assertNotIn("复核建议", bearing("BEAR-0001"))

    def test_invalid_version_rejected(self) -> None:
        with self.assertRaises(ReviewRuleError):
            service.run_batch(version="v9", today=TODAY)


class RuleSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()

    def test_switch_migrates_open_checklists(self) -> None:
        service.run_batch(today=TODAY)
        checklist_1 = next(
            row for row in store.rows("bearing_review") if row["支座编号"] == "BEAR-0001"
        )
        self.assertEqual(checklist_1["建议"], ADVICE_INSPECT)
        self.assertEqual(checklist_1["规则版本"], "v1")

        report = service.switch_version("v2")
        self.assertEqual(report["from"], "v1")
        self.assertEqual(report["to"], "v2")
        self.assertEqual(service.active_version(), "v2")
        self.assertGreaterEqual(report["migrated"], 7)

        # 未完成检查单原地迁移：v2 加严后 0001 由巡检变维修，仍是同一条记录。
        open_1 = [
            row for row in store.rows("bearing_review")
            if row["支座编号"] == "BEAR-0001" and row["检查单状态"] == REVIEW_OPEN
        ]
        self.assertEqual(len(open_1), 1)
        self.assertEqual(open_1[0]["规则版本"], "v2")
        self.assertEqual(open_1[0]["建议"], ADVICE_REPAIR)
        self.assertEqual(open_1[0]["id"], checklist_1["id"])

        # 台账同步到 v2 结论
        self.assertEqual(bearing("BEAR-0001")["复核版本"], "v2")
        self.assertEqual(bearing("BEAR-0001")["复核建议"], ADVICE_REPAIR)

    def test_completed_and_historical_checklists_untouched(self) -> None:
        service.run_batch(today=TODAY)
        target = next(
            row for row in store.rows("bearing_review") if row["支座编号"] == "BEAR-0005"
        )
        service.complete_checklist(int(target["id"]))
        service.switch_version("v2")

        done = next(
            row for row in store.rows("bearing_review") if row["id"] == target["id"]
        )
        self.assertEqual(done["规则版本"], "v1")
        self.assertEqual(done["检查单状态"], REVIEW_DONE)

        legacy = next(row for row in store.rows("bearing_review") if row.get("历史遗留"))
        self.assertEqual(legacy["规则版本"], "v1")
        self.assertEqual(legacy["检查单状态"], REVIEW_DONE)

    def test_switch_is_guarded_by_version_lock_and_rollback(self) -> None:
        service.run_batch(today=TODAY)
        with self.assertRaises(ReviewRuleError):
            service.switch_version("v9")
        self.assertEqual(service.active_version(), "v1")

        # 同版本切换是幂等空操作，不迁移任何检查单
        same = service.switch_version("v1")
        self.assertEqual(same["migrated"], 0)
        self.assertEqual(service.active_version(), "v1")


class ChecklistCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()

    def test_complete_refreshes_aggregations_and_is_idempotent(self) -> None:
        service.run_batch(today=TODAY)
        target = next(
            row for row in store.rows("bearing_review") if row["支座编号"] == "BEAR-0001"
        )
        first = service.complete_checklist(int(target["id"]))
        self.assertEqual(first["检查单状态"], REVIEW_DONE)
        self.assertTrue(bearing("BEAR-0001")["复核已闭环"])

        finished_at = first["完成时间"]
        second = service.complete_checklist(int(target["id"]))
        self.assertEqual(second["完成时间"], finished_at)

        # 同周期重新批量复核不会把已完成检查单重开
        service.run_batch(today=TODAY)
        again = service.get_checklist(int(target["id"]))
        self.assertEqual(again["检查单状态"], REVIEW_DONE)
        self.assertTrue(bearing("BEAR-0001")["复核已闭环"])

    def test_last_open_item_closed_clears_bridge_and_project_aggregates(self) -> None:
        # 南站高架桥只有 0008 一个支座（v1 维修），闭环后定检清单与维修待办都应收口。
        service.run_batch(bridge="南站高架桥", today=TODAY)
        target = next(
            row for row in store.rows("bearing_review")
            if row["支座编号"] == "BEAR-0008"
        )
        service.complete_checklist(int(target["id"]))

        bridge_row = next(
            row for row in store.rows("bridge")
            if row.get("复核键") == "BEARING-REVIEW::南站高架桥::v1"
        )
        self.assertEqual(bridge_row["检测状态"], "已闭环")
        self.assertFalse(bridge_row["pending"])

        todo = next(
            row for row in store.rows("project")
            if row.get("待办键") == "TODO-BEAR::南站高架桥::维修"
        )
        self.assertEqual(todo["工程状态"], "已取消")
        self.assertFalse(todo["pending"])


    def test_advice_downgrade_cancels_open_project_todo(self) -> None:
        # BEAR-0002：锈蚀 28% 达维修档，删掉专项结论后阈值维持维修；
        # 先复核生成维修待办，再恢复专项结论重算降为巡检，维修待办应取消。
        bearing("BEAR-0002")["最近专项结论"] = ""
        service.run_batch(bridge="振兴路立交桥", today=TODAY)
        repair_todo = next(
            row for row in store.rows("project")
            if row.get("待办键") == "TODO-BEAR::振兴路立交桥::维修"
        )
        self.assertEqual(repair_todo["status"], "待开工")

        bearing("BEAR-0002")["最近专项结论"] = "可继续使用"
        service.run_batch(bridge="振兴路立交桥", today=TODAY)
        repair_todo = next(
            row for row in store.rows("project")
            if row.get("待办键") == "TODO-BEAR::振兴路立交桥::维修"
        )
        self.assertEqual(repair_todo["status"], "已取消")
        self.assertFalse(repair_todo["pending"])
        self.assertIsNotNone(
            next((row for row in store.rows("project")
                  if row.get("待办键") == "TODO-BEAR::振兴路立交桥::巡检"), None)
        )

    def test_batch_single_bridge_only_touches_that_group(self) -> None:
        service.run_batch(bridge="河滨大桥", today=TODAY)
        self.assertIn("复核建议", bearing("BEAR-0003"))
        self.assertNotIn("复核建议", bearing("BEAR-0001"))


class OverviewSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()

    def test_overview_risk_levels_track_active_version(self) -> None:
        from app.store import store

        overview = store.overview()
        self.assertEqual(overview["risk_levels"]["version"], "v1")
        self.assertEqual(overview["risk_levels"]["高"], 0)

        service.run_batch(today=TODAY)
        overview = store.overview()
        risks = overview["risk_levels"]
        self.assertEqual(risks["version"], "v1")
        self.assertEqual((risks["低"], risks["中"], risks["高"]), (2, 2, 3))
        self.assertIn("复核高风险", [card["label"] for card in overview["cards"]])

        # 闭环一张高风险检查单后，总览高风险数随之下降
        target = next(
            row for row in store.rows("bearing_review")
            if row["风险等级"] == "高" and row["检查单状态"] == REVIEW_OPEN
        )
        service.complete_checklist(int(target["id"]))
        overview = store.overview()
        self.assertEqual(overview["risk_levels"]["高"], 2)

    def test_switch_version_closes_old_bridge_aggregates(self) -> None:
        service.run_batch(today=TODAY)
        service.switch_version("v2")
        old_rows = [
            row for row in store.rows("bridge") if row.get("复核键", "").endswith("::v1")
        ]
        self.assertTrue(old_rows)
        for row in old_rows:
            self.assertFalse(row["pending"])
            self.assertIn(row["检测状态"], ("已随规则切换收口", "已闭环"))
        new_rows = [
            row for row in store.rows("bridge") if row.get("复核键", "").endswith("::v2")
        ]
        self.assertTrue(any(row["pending"] for row in new_rows))


if __name__ == "__main__":
    unittest.main()
