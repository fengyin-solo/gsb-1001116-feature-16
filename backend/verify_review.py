"""阈值复核矩阵的端到端校验脚本（不依赖 fastapi，直接跑服务层）。

覆盖：矩阵判定/冲突裁定、批量分组事务、幂等、失败整组回滚、
规则切换迁移、历史支座保留、三处回写、总览风险同步。
"""
from __future__ import annotations

from datetime import date

from app.services.bearing_review import review_service
from app.store import store

TODAY = date(2026, 10, 1)
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}")
    if not condition:
        failures.append(name)


def bearing(no: str) -> dict:
    return next(r for r in store.rows("bearing") if r["支座编号"] == no)


def check_row(cid: int) -> dict:
    return store.find("bearing_check", cid)


# 1. 初始种子：N002/N003 已复核（专项结论更换），台账风险同步
check("种子-高风险支座数量", store.risk_summary()["高"] == 2, str(store.risk_summary()))
check("种子-N002台账回写", bearing("BEAR-N002")["复核结论"] == "更换" and bearing("BEAR-N002")["风险等级"] == "高")
check("种子-工程待办已挂账",
      any(r.get("待办键") == "bearing:更换:BEAR-N002:2026Q3" for r in store.rows("project")))
check("种子-定检清单回写",
      sum(1 for r in store.rows("bridge")
          if r.get("数据来源") == "支座阈值复核"
          and any(no in r.get("关联支座单号", "") for no in ("BEAR-N002", "BEAR-N003"))) == 2)

# 2. 历史支座保护
hist = bearing("BEAR-0001")
check("种子-历史支座标记", hist["历史支座"] is True and hist["复核结论"] == "历史保留")

# 3. 评估 N004：承载维修(85%)+锈蚀维修(16%) 一致 -> 维修，不构成冲突
rule = review_service.active_rule()
ev = review_service.evaluate(check_row(4), rule, today=TODAY)
check("N004-信号一致维修", ev["conclusion"] == "维修" and ev["conflict"] is False, ev["basis"])

# 4. 评估 N006：承载维修(88%)+锈蚀更换(29%) 冲突；承载力等级低(1500kN 属中)
#    赤水河桥专项结论=维修；(中,维修)->维修
ev6 = review_service.evaluate(check_row(6), rule, today=TODAY)
check("N006-阈值冲突共同裁定", ev6["conclusion"] == "维修" and ev6["conflict"] is True, ev6["basis"])

# 5. 构造一个 (低,更换) 场景：N007 承载利用率缺测但位移超限(42mm)->更换，锈蚀维修->冲突，
#    但其所属赤水河桥专项=维修、承载力低 -> (低,维修)=更换
ev7 = review_service.evaluate(check_row(7), rule, today=TODAY)
check("N007-低承载+专项维修裁定更换", ev7["conclusion"] == "更换" and ev7["conflict"] is True, ev7["basis"])

# 6. N001：无实测、周期 2026Q3 截止 10/10+容差，10/1 未到期 -> 信号不足=>巡检（非到期）
ev1 = review_service.evaluate(check_row(1), rule, today=TODAY)
check("N001-周期未到期判巡检", ev1["conclusion"] == "巡检", ev1["basis"])

# 7. 批量复核：两桥分组提交
before_project = len(store.rows("project"))
res = review_service.run_batch(cycle="2026Q3", batch_no="BATCH-TEST-001", today=TODAY)
check("批量-无失败", res["失败"] == 0, str({k: res[k] for k in ("更换", "维修", "巡检", "跳过", "失败")}))
check("批量-分组数=2", len(res["groups"]) == 2, f"groups={[g['桥梁'] for g in res['groups']]}")
check("批量-更换=1(N007)", res["更换"] == 1, str(res))
check("批量-维修=2(N004/N006)", res["维修"] == 2, str(res))
check("批量-巡检=2(N001/N005)", res["巡检"] == 2, str(res))
check("批量-跳过=2(N002/N003回放)", res["跳过"] == 2, str(res))
check("批量-每组独立提交", all(g["ok"] for g in res["groups"]))

# 8. 回写三处 + 风险同步
b7 = bearing("BEAR-N007")
check("回写-N007台账更换/高风险", b7["复核结论"] == "更换" and b7["风险等级"] == "高" and b7["status"] == "需更换")
check("回写-N006台账维修/中风险", bearing("BEAR-N006")["复核结论"] == "维修" and bearing("BEAR-N006")["风险等级"] == "中")
check("回写-N001巡检低风险不挂待办",
      bearing("BEAR-N001")["复核结论"] == "巡检" and bearing("BEAR-N001")["pending"] is False)
check("回写-新待办仅维修/更换",
      len(store.rows("project")) - before_project == 3,  # N004 N006 N007
      f"新增待办 {len(store.rows('project')) - before_project}")
check("回写-定检清单新增复核记录",
      sum(1 for r in store.rows("bridge") if r.get("数据来源") == "支座阈值复核") == 7)
ov = store.overview()
bearing_mod = next(m for m in ov["modules"] if m["name"] == "bearing")
check("总览-风险等级同步", bearing_mod["high_risk"] == 3 and bearing_mod["medium_risk"] == 2
      and any(c["label"] == "支座高风险" and c["value"] == 3 for c in ov["cards"]), str(bearing_mod))

# 9. 幂等：同批次号重跑 -> 回放，不新增任何数据
snapshot = store.snapshot(["bearing_check", "bearing", "bridge", "project"])
res2 = review_service.run_batch(cycle="2026Q3", batch_no="BATCH-TEST-001", today=TODAY)
check("幂等-标记回放", res2.get("幂等") is True)
check("幂等-数据零变化", store.snapshot(["bearing_check", "bearing", "bridge", "project"]) == snapshot)
# 无批次号重跑（自动生成同名批次，已 finished）同样回放
res3 = review_service.run_batch(cycle="2026Q3", today=TODAY)
check("幂等-自动批次也回放", res3.get("幂等") is True and res3["batch_no"] == "BATCH-TEST-001",
      f"回放批次 {res3.get('batch_no')}")

# 10. 结论降级 -> 旧待办核销（模拟 N006 重测后锈蚀回落到 6%，结论变巡检）
row6 = check_row(6)
row6["status"] = "待复核"
row6["承载利用率"] = "70%"
row6["实测锈蚀率"] = "6%"
res_d = review_service.run_batch(cycle="2026Q3", batch_no="BATCH-TEST-002", today=TODAY)
check("降级-复核结论转巡检", bearing("BEAR-N006")["复核结论"] == "巡检" and bearing("BEAR-N006")["风险等级"] == "低")
todo6 = next(r for r in store.rows("project") if r.get("待办键") == "bearing:维修:BEAR-N006:2026Q3")
check("降级-旧维修待办已核销", todo6["status"] == "已核销" and todo6["pending"] is False)

# 11. 规则切换迁移：启用 v2，未完成检查单迁移；历史单不动 & 已复核单不动
v2, message, migrated = review_service.activate_rule("v2")
check("切换-启用v2", v2["active"] is True and review_service.active_rule()["规则版本"] == "v2", message)
check("切换-迁移数量", len(migrated) == 0, "无未完成单时应为0（当前周期全部已复核）")
# 制造一张未完成单再切换
store.rows("bearing_check").append({
    "id": 99, "status": "待复核", "pending": True, "abnormal": False,
    "检查单号": "BCHK-N004-2026Q4", "支座编号": "BEAR-N004", "所属桥梁": "云溪大桥",
    "检查周期": "2026Q4", "规则版本": "v2", "历史支座": False,
    "承载利用率": "83%", "实测锈蚀率": "13%", "实测位移量": "10mm", "填报日期": "2026-10-01",
    "复核结论": "", "风险等级": "", "裁定依据": "", "数据来源": "现场实测",
    "复核批次": "", "复核时间": ""})
review_service.activate_rule("v1")
moved = check_row(99)
check("切换-v1后未完成单迁移", moved["规则版本"] == "v1" and "规则切换" in moved["裁定依据"])
check("切换-台账版本同步", bearing("BEAR-N004")["规则版本"] == "v1")
hist_check = check_row(8)
hist_check["status"] = "待复核"  # 异常地改掉也不应被迁移
_, _, migrated2 = review_service.activate_rule("v2")
check("切换-历史检查单不迁移", check_row(8)["规则版本"] == "v0" and check_row(8)["复核结论"] == "历史保留")
check("切换-非历史未完成单迁移", check_row(99)["规则版本"] == "v2")
hist_check["status"] = "已复核"

# 12. 失败分组整组回滚：云溪大桥构造一张台账缺失支座的坏单
store.rows("bearing_check").append({
    "id": 98, "status": "待复核", "pending": True, "abnormal": False,
    "检查单号": "BCHK-GHOST-2026Q4", "支座编号": "BEAR-GHOST", "所属桥梁": "幽灵桥",
    "检查周期": "2026Q4", "规则版本": "v2", "历史支座": False,
    "承载利用率": "99%", "实测锈蚀率": "40%", "实测位移量": "50mm", "填报日期": "2026-10-01",
    "复核结论": "", "风险等级": "", "裁定依据": "", "数据来源": "现场实测",
    "复核批次": "", "复核时间": ""})
# 给云溪/赤水河也加 Q4 待复核单，验证失败组不影响其他组提交
for cid, no, bridge, use, rust, disp in [
        (97, "BEAR-N004", "云溪大桥", "70%", "5%", "8mm"),
        (96, "BEAR-N005", "赤水河桥", "60%", "3%", "5mm")]:
    store.rows("bearing_check").append({
        "id": cid, "status": "待复核", "pending": True, "abnormal": False,
        "检查单号": f"BCHK-{no}-2026Q4", "支座编号": no, "所属桥梁": bridge,
        "检查周期": "2026Q4", "规则版本": "v2", "历史支座": False,
        "承载利用率": use, "实测锈蚀率": rust, "实测位移量": disp, "填报日期": "2026-10-01",
        "复核结论": "", "风险等级": "", "裁定依据": "", "数据来源": "现场实测",
        "复核批次": "", "复核时间": ""})
res4 = review_service.run_batch(cycle="2026Q4", batch_no="BATCH-TEST-Q4", today=TODAY)
groups = {g["桥梁"]: g for g in res4["groups"]}
check("失败-幽灵桥整组失败", groups["幽灵桥"]["ok"] is False and res4["失败"] == 1, groups["幽灵桥"].get("error", ""))
check("失败-坏单未被改写", check_row(98)["复核结论"] == "")
check("失败-其他桥正常提交", groups["云溪大桥"]["ok"] and groups["赤水河桥"]["ok"])
check("失败-批次台账记部分失败",
      next(r for r in store.rows("bearing_review_run") if r["批次号"] == "BATCH-TEST-Q4")["status"] == "部分失败")

# 12b. 失败批次同号重试：幽灵桥修复（补台账）后整组成功，批次行被更新而非追加
run_count_before = len(store.rows("bearing_review_run"))
store.rows("bearing").append({
    "id": 9901, "status": "锈蚀", "pending": True, "abnormal": True,
    "支座编号": "BEAR-GHOST", "所属桥梁": "幽灵桥", "支座类型": "板式橡胶支座",
    "设计承载力": "800kN", "位移量": "50mm", "锈蚀程度": "严重（40%）",
    "最近检查": "2026-07-01", "支座状态": "锈蚀",
    "历史支座": False, "复核结论": "", "风险等级": "", "规则版本": "v2", "处置建议": ""})
res4b = review_service.run_batch(cycle="2026Q4", batch_no="BATCH-TEST-Q4", today=TODAY)
same = [r for r in store.rows("bearing_review_run") if r["批次号"] == "BATCH-TEST-Q4"]
check("重试-批次行唯一", len(same) == 1 and len(store.rows("bearing_review_run")) == run_count_before)
check("重试-转已提交", same[0]["status"] == "已提交" and res4b["失败"] == 0 and res4b.get("幂等") is False)
# 再次同号执行：已成功 -> 幂等回放
res4c = review_service.run_batch(cycle="2026Q4", batch_no="BATCH-TEST-Q4", today=TODAY)
check("重试-成功后幂等回放", res4c.get("幂等") is True)

# 13. 历史支座动作拦截（服务层）
from app.services.bearing import BearingService
entry, msg = BearingService().run_action(1, "防锈处理")
check("历史支座-动作拦截", entry is None and "历史支座" in msg, msg)

print()
if failures:
    print(f"{len(failures)} 项失败：{failures}")
    raise SystemExit(1)
print("全部校验通过")
