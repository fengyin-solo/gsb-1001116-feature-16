"""承载力与锈蚀阈值复核矩阵的全部业务规则。

口径概览
========
1. 复核对象是「支座检查单」（bearing_check），按 支座编号 × 检查周期 唯一，按桥梁分组。
2. 每张检查单从三个维度取信号：承载利用率、锈蚀率、检查周期（是否到期），各自对照
   当前启用矩阵（bearing_rule 中 active 的一条）得到 巡检/维修/更换 建议。
3. 三个信号一致时直接采纳；出现分歧即「阈值冲突」，由设计承载力等级
   （高 ≥3000kN / 中 ≥1500kN / 低 <1500kN）与该桥最近一次专项检测结论
   （更换 / 维修 / 正常）共同按裁定表裁定，专项结论超过有效期不再采信。
4. 复核结论回写三处：支座台账（bearing）、桥梁定检清单（bridge）、工程待办（project），
   并同步风险等级（更换=高 / 维修=中 / 巡检=低）。
5. 历史支座（历史支座=True）继续按原检查记录保留：不参与规则迁移，复核批次直接跳过。
6. 批量复核按桥梁分组事务提交：组内任一步失败整组回滚，不允许部分改写处置建议；
   重复批次号 / 重复检查单幂等，已复核检查单只回放结论、不重复写回。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from app.store import RISK_BY_CONCLUSION, store

CHECK_MODULE = "bearing_check"
BEARING_MODULE = "bearing"
RULE_MODULE = "bearing_rule"
RUN_MODULE = "bearing_review_run"
BRIDGE_MODULE = "bridge"
PROJECT_MODULE = "project"

CONCLUSIONS = ("巡检", "维修", "更换")
WRITE_TABLES = [CHECK_MODULE, BEARING_MODULE, BRIDGE_MODULE, PROJECT_MODULE]

# 设计承载力等级（kN）
CAPACITY_GRADE_HIGH = 3000
CAPACITY_GRADE_MID = 1500

# 专项结论在有效期内时的共同裁定表：(设计承载力等级, 专项结论) -> 最终建议
# 承载力越弱、专项结论越重，处置越重；取两者共同支撑的最严档，避免单一阈值误判。
ARBITRATION: dict[tuple[str, str], str] = {
    ("高", "更换"): "维修",
    ("高", "维修"): "维修",
    ("高", "正常"): "巡检",
    ("中", "更换"): "更换",
    ("中", "维修"): "维修",
    ("中", "正常"): "维修",
    ("低", "更换"): "更换",
    ("低", "维修"): "更换",
    ("低", "正常"): "维修",
}

# 中文锈蚀程度档位到百分比的兜底映射（字段没写百分号时使用）
RUST_LEVEL = {"无": 0.0, "轻微": 0.08, "中度": 0.16, "严重": 0.3, "较重": 0.22}


def _first_number(text: Any) -> float | None:
    """从 '97%'、'1800kN'、'42mm' 这类文本里取第一个数字。"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    match = re.search(r"-?\d+(?:\.\d+)?", str(text))
    return float(match.group()) if match else None


def parse_ratio(text: Any) -> float | None:
    """把承载利用率/锈蚀率解析成 0~1 的小数；纯数字按百分数处理。"""
    value = _first_number(text)
    if value is None:
        return None
    if value > 1:
        value /= 100.0
    return value


def parse_capacity_kN(text: Any) -> float | None:
    """解析设计承载力，统一成 kN；识别以 MN / 吨 标注的写法。"""
    raw = str(text or "")
    value = _first_number(raw)
    if value is None:
        return None
    if re.search(r"MN|兆牛", raw, re.IGNORECASE):
        return value * 1000
    if "吨" in raw or re.search(r"\bt\b", raw, re.IGNORECASE):
        return value * 9.80665
    return value


def parse_rust_ratio(text: Any) -> float | None:
    """锈蚀率：优先取百分数字面量；没有数字时按 轻微/中度/严重 档位兜底。"""
    ratio = parse_ratio(text)
    if ratio is not None:
        return ratio
    for word, level in RUST_LEVEL.items():
        if word in str(text or ""):
            return level
    return None


def parse_date(text: Any) -> date | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    return None


def current_cycle(today: date) -> str:
    quarter = (today.month - 1) // 3 + 1
    return f"{today.year}Q{quarter}"


def cycle_deadline(cycle: str, tolerance_days: int) -> date | None:
    """季度周期的截止时间取季末 + 容差天数；无法解析周期则不判到期。"""
    match = re.fullmatch(r"(\d{4})Q([1-4])", str(cycle or ""))
    if not match:
        return None
    year, quarter = int(match.group(1)), int(match.group(2))
    end_month, end_day = [(3, 31), (6, 30), (9, 30), (12, 31)][quarter - 1]
    return date(year, end_month, end_day) + timedelta(days=tolerance_days)


class BearingReviewService:
    # ---- 矩阵规则 ----------------------------------------------------------
    def list_rules(self) -> list[dict[str, Any]]:
        return store.rows(RULE_MODULE)

    def active_rule(self) -> dict[str, Any] | None:
        rules = [row for row in store.rows(RULE_MODULE) if row.get("active")]
        return rules[0] if rules else None

    def create_rule(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        version = str(values.get("规则版本") or "").strip()
        if not version:
            return None, "规则版本不能为空"
        if any(row.get("规则版本") == version for row in store.rows(RULE_MODULE)):
            return None, f"规则版本 {version} 已存在"
        rule = self._rule_from_values(store.next_id(RULE_MODULE), values, version, active=False)
        store.rows(RULE_MODULE).append(rule)
        return rule, ""

    def activate_rule(self, version: str, *, today: date | None = None) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
        """切换启用规则；切换时把未完成检查单迁移到新版本。

        历史支座的检查单（历史支座=True、含历史结论）不迁移，继续按原检查记录保留。
        迁移本身也是事务：任一单迁移失败则整次切换回滚。
        """
        today = today or date.today()
        target = next((row for row in store.rows(RULE_MODULE) if row.get("规则版本") == version), None)
        if target is None:
            return None, f"规则版本 {version} 不存在", []
        current = self.active_rule()
        if current is not None and current.get("规则版本") == version:
            return target, "该版本已处于启用状态", []

        snapshot = store.snapshot([RULE_MODULE, CHECK_MODULE])
        try:
            migrated: list[dict[str, Any]] = []
            for check in store.rows(CHECK_MODULE):
                if check.get("历史支座"):
                    continue  # 历史支座继续按原检查记录保留
                if check.get("status") in ("已复核",) and check.get("复核结论"):
                    continue  # 已完成的检查单不重算
                old_version = check.get("规则版本")
                check["规则版本"] = version
                note = f"规则切换：{old_version or '未挂版本'} → {version}，阈值待重新复核"
                check["裁定依据"] = note
                migrated.append(check)
            for row in store.rows(RULE_MODULE):
                row["active"] = row.get("规则版本") == version
            # 在役支座台账上的规则版本标记同步迁移，历史支座不动
            for bearing in store.rows(BEARING_MODULE):
                if not bearing.get("历史支座"):
                    bearing["规则版本"] = version
        except Exception as exc:  # pragma: no cover - 防御性回滚
            store.restore(snapshot)
            return None, f"规则切换迁移失败，已回滚：{exc}", []
        return target, f"已切换到 {version}，迁移未完成检查单 {len(migrated)} 张", migrated

    def _rule_from_values(self, rule_id: int, values: dict[str, Any], version: str, *, active: bool) -> dict[str, Any]:
        return {
            "id": rule_id,
            "规则版本": version,
            "规则名称": str(values.get("规则名称") or f"阈值矩阵 {version}"),
            "承载维修阈值": float(values.get("承载维修阈值", 0.85)),
            "承载更换阈值": float(values.get("承载更换阈值", 0.95)),
            "锈蚀维修阈值": float(values.get("锈蚀维修阈值", 0.15)),
            "锈蚀更换阈值": float(values.get("锈蚀更换阈值", 0.25)),
            "位移限值mm": float(values.get("位移限值mm", 30)),
            "检查周期": str(values.get("检查周期", "季度")),
            "周期容差天": int(values.get("周期容差天", 10)),
            "active": active,
            "生效日期": str(values.get("生效日期") or ""),
            "裁定说明": str(values.get("裁定说明") or "阈值冲突以设计承载力与最近专项结论共同裁定"),
        }

    # ---- 检查单 ------------------------------------------------------------
    def list_checks(
        self,
        *,
        bridge: str | None = None,
        bearing_no: str | None = None,
        cycle: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(CHECK_MODULE)
        if bridge:
            rows = [row for row in rows if bridge in str(row.get("所属桥梁", ""))]
        if bearing_no:
            rows = [row for row in rows if bearing_no in str(row.get("支座编号", ""))]
        if cycle:
            rows = [row for row in rows if row.get("检查周期") == cycle]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def submit_measurement(self, check_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """现场填报实测承载利用率/锈蚀率/位移，填完进入待复核。"""
        check = store.find(CHECK_MODULE, check_id)
        if check is None:
            return None, f"检查单 {check_id} 不存在"
        if check.get("历史支座"):
            return None, "历史支座检查单按原记录保留，不接收新填报"
        for field in ("承载利用率", "实测锈蚀率", "实测位移量"):
            if field in values and str(values[field]).strip():
                check[field] = str(values[field]).strip()
        if values.get("填报日期"):
            check["填报日期"] = str(values["填报日期"]).strip()
        check["数据来源"] = "现场实测"
        check["status"] = "待复核"
        check["pending"] = True
        return check, "实测数据已填报，等待阈值复核"

    # ---- 矩阵判定 ----------------------------------------------------------
    def _bearing_for(self, check: dict[str, Any]) -> dict[str, Any] | None:
        for bearing in store.rows(BEARING_MODULE):
            if bearing.get("支座编号") == check.get("支座编号"):
                return bearing
        return None

    def _latest_special(self, bridge_name: str, *, today: date) -> dict[str, Any] | None:
        """取该桥最近一次专项检测结论；超过结论有效期的不再作为裁定依据。"""
        specials = [
            row for row in store.rows(BRIDGE_MODULE)
            if row.get("桥梁名称") == bridge_name
            and "专项" in str(row.get("检测类型", ""))
            and row.get("专项结论")
        ]
        if not specials:
            return None
        specials.sort(key=lambda row: parse_date(row.get("检测日期")) or date.min, reverse=True)
        latest = specials[0]
        expire = parse_date(latest.get("结论有效期至"))
        if expire is not None and expire < today:
            return None
        return latest

    def evaluate(
        self,
        check: dict[str, Any],
        rule: dict[str, Any],
        *,
        today: date,
    ) -> dict[str, Any]:
        """对一张检查单跑矩阵，返回信号、是否冲突、最终结论与裁定依据。"""
        bearing = self._bearing_for(check)
        signals: dict[str, str] = {}
        details: list[str] = []

        # 维度一：承载利用率。无实测数据时退回台账位移/设计承载力做保守判断。
        usage = parse_ratio(check.get("承载利用率"))
        if usage is None and bearing is not None:
            displacement = _first_number(bearing.get("位移量"))
            capacity = parse_capacity_kN(bearing.get("设计承载力"))
            if displacement is not None and capacity:
                usage = min(1.0, displacement / float(rule["位移限值mm"]))
                details.append("承载利用率缺测，按位移/限值保守估算")
        if usage is not None:
            if usage >= float(rule["承载更换阈值"]):
                signals["承载"] = "更换"
            elif usage >= float(rule["承载维修阈值"]):
                signals["承载"] = "维修"
            else:
                signals["承载"] = "巡检"
            details.append(f"承载利用率 {usage:.0%}")

        # 维度二：锈蚀率
        rust = parse_rust_ratio(check.get("实测锈蚀率"))
        if rust is None and bearing is not None:
            rust = parse_rust_ratio(bearing.get("锈蚀程度"))
        if rust is not None:
            if rust >= float(rule["锈蚀更换阈值"]):
                signals["锈蚀"] = "更换"
            elif rust >= float(rule["锈蚀维修阈值"]):
                signals["锈蚀"] = "维修"
            else:
                signals["锈蚀"] = "巡检"
            details.append(f"锈蚀率 {rust:.0%}")

        # 位移超限按最严的更换信号参与判定
        displacement = _first_number(check.get("实测位移量"))
        if displacement is None and bearing is not None:
            displacement = _first_number(bearing.get("位移量"))
        if displacement is not None and displacement > float(rule["位移限值mm"]):
            signals["位移"] = "更换"
            details.append(f"位移 {displacement:g}mm 超限值 {float(rule['位移限值mm']):g}mm")

        # 维度三：检查周期是否到期（到期未检，至少安排巡检）
        deadline = cycle_deadline(str(check.get("检查周期", "")), int(rule["周期容差天"]))
        measured = bool(str(check.get("承载利用率") or "").strip() or str(check.get("实测锈蚀率") or "").strip())
        overdue = deadline is not None and today > deadline and not measured
        if overdue:
            signals["周期"] = "巡检"
            details.append(f"{check.get('检查周期')} 周期已到期（容差至 {deadline}）且无实测数据")
        else:
            details.append(f"检查周期 {check.get('检查周期')} 内")

        if not signals:
            signals["周期"] = "巡检"

        votes = Counter(signals.values())
        distinct = set(signals.values())
        severity = {"巡检": 0, "维修": 1, "更换": 2}
        initial = max(distinct, key=lambda item: (severity[item], votes[item]))

        if len(distinct) == 1:
            return {
                "conclusion": initial,
                "conflict": False,
                "signals": signals,
                "basis": "、".join(details) + f"；三维信号一致→{initial}",
            }

        # 阈值冲突：设计承载力等级 × 最近专项结论 共同裁定
        grade = None
        if bearing is not None:
            capacity = parse_capacity_kN(bearing.get("设计承载力"))
            if capacity is not None:
                grade = "高" if capacity >= CAPACITY_GRADE_HIGH else "中" if capacity >= CAPACITY_GRADE_MID else "低"
                details.append(f"设计承载力 {capacity:g}kN→等级「{grade}」")
        special = self._latest_special(str(check.get("所属桥梁", "")), today=today)
        special_conclusion = None
        if special is not None:
            raw = str(special.get("专项结论", ""))
            special_conclusion = "更换" if "更换" in raw else "维修" if ("维修" in raw or "观察" in raw) else "正常" if "正常" in raw else None
            details.append(f"最近专项结论「{special_conclusion}」（{special.get('检测日期')}）")
        else:
            details.append("最近专项结论缺失或已过有效期")

        if grade is not None and special_conclusion is not None:
            conclusion = ARBITRATION[(grade, special_conclusion)]
            arbiter = f"阈值冲突：设计承载力等级「{grade}」+专项结论「{special_conclusion}」→共同裁定{conclusion}"
        else:
            # 裁定依据不全时不放宽，按矩阵最重信号裁定并注明依据缺失
            conclusion = max(distinct, key=lambda item: severity[item])
            missing = "设计承载力无法解析" if grade is None else "专项结论缺失"
            arbiter = f"阈值冲突但{missing}，按最严信号裁定{conclusion}"
        return {
            "conclusion": conclusion,
            "conflict": True,
            "signals": signals,
            "basis": "、".join(details) + "；" + arbiter,
        }

    # ---- 批量复核（分组事务） ----------------------------------------------
    def review_one(
        self,
        check_id: int,
        *,
        batch_no: str,
        today: date,
        rule: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """复核单张检查单。调用方负责事务边界；本函数只做校验+计算+写回。"""
        check = store.find(CHECK_MODULE, check_id)
        if check is None:
            raise ValueError(f"检查单 {check_id} 不存在")
        rule = rule or self.active_rule()
        if rule is None:
            raise ValueError("没有启用的阈值矩阵规则")

        if check.get("历史支座"):
            check["status"] = "已复核"
            return {"检查单号": check.get("检查单号"), "支座编号": check.get("支座编号"),
                    "结论": "历史保留", "跳过": True, "原因": "历史支座继续按原检查记录保留"}

        # 幂等：已复核检查单只回放既有结论，不重复回写台账/清单/待办
        if check.get("status") == "已复核" and check.get("复核结论") in CONCLUSIONS:
            return {"检查单号": check.get("检查单号"), "支座编号": check.get("支座编号"),
                    "结论": check.get("复核结论"), "跳过": True, "原因": "已复核，回放既有结论"}

        result = self.evaluate(check, rule, today=today)
        conclusion = result["conclusion"]
        risk = RISK_BY_CONCLUSION[conclusion]
        bearing = self._bearing_for(check)
        if bearing is None:
            raise ValueError(f"支座 {check.get('支座编号')} 在台账中不存在，拒绝复核")

        self._write_back(check, bearing, conclusion, risk, result, rule, batch_no, today)
        return {"检查单号": check.get("检查单号"), "支座编号": check.get("支座编号"),
                "结论": conclusion, "跳过": False, "冲突": result["conflict"], "裁定依据": result["basis"]}

    def _write_back(
        self,
        check: dict[str, Any],
        bearing: dict[str, Any],
        conclusion: str,
        risk: str,
        result: dict[str, Any],
        rule: dict[str, Any],
        batch_no: str,
        today: date,
    ) -> None:
        cycle = str(check.get("检查周期", ""))
        bearing_no = str(check.get("支座编号", ""))
        bridge_name = str(check.get("所属桥梁", ""))

        # 1) 检查单本身
        check["复核结论"] = conclusion
        check["风险等级"] = risk
        check["裁定依据"] = result["basis"]
        check["规则版本"] = rule["规则版本"]
        check["复核批次"] = batch_no
        check["复核时间"] = today.isoformat()
        check["status"] = "已复核"
        check["pending"] = False
        check["abnormal"] = conclusion == "更换"

        # 2) 支座台账：历史字段（最近检查原始记录）不动，只追加复核结论与风险
        bearing["复核结论"] = conclusion
        bearing["风险等级"] = risk
        bearing["规则版本"] = rule["规则版本"]
        bearing["最近复核周期"] = cycle
        bearing["最近复核批次"] = batch_no
        bearing["处置建议"] = result["basis"]
        bearing["支座状态"] = {"巡检": "正常", "维修": "锈蚀", "更换": "需更换"}[conclusion]
        bearing["status"] = bearing["支座状态"]
        bearing["abnormal"] = conclusion in ("维修", "更换")
        bearing["pending"] = conclusion in ("维修", "更换")

        # 3) 桥梁定检清单：同桥同周期幂等 upsert 一条支座复核记录
        self._upsert_bridge_entry(bridge_name, bearing_no, cycle, conclusion, risk, batch_no, today)

        # 4) 工程待办：维修/更换 幂等挂账；结论降为巡检时核销旧待办
        self._sync_project_todo(bearing, bridge_name, conclusion, cycle, batch_no)

    def _upsert_bridge_entry(
        self, bridge_name: str, bearing_no: str, cycle: str,
        conclusion: str, risk: str, batch_no: str, today: date,
    ) -> None:
        link_key = f"bearing:{bearing_no}:{cycle}"
        existing = next(
            (row for row in store.rows(BRIDGE_MODULE)
             if row.get("数据来源") == "支座阈值复核" and row.get("关联支座单号") == link_key),
            None,
        )
        label = {"更换": "安排更换", "维修": "安排维修", "巡检": "安排巡检"}[conclusion]
        disease = f"{bearing_no} 阈值复核结论：{conclusion}（风险{risk}，批次 {batch_no}）"
        if existing is not None:
            existing["主要病害"] = disease
            existing["检测状态"] = label
            existing["status"] = "已评定"
            existing["pending"] = False
            existing["abnormal"] = conclusion == "更换"
            existing["复核批次"] = batch_no
            return
        store.rows(BRIDGE_MODULE).append({
            "id": store.next_id(BRIDGE_MODULE),
            "status": "已评定",
            "pending": False,
            "abnormal": conclusion == "更换",
            "检测编号": f"BRID-BEAR-{bearing_no}-{cycle}",
            "桥梁名称": bridge_name,
            "检测类型": "支座专项复核",
            "检测日期": today.isoformat(),
            "技术状况评分": "",
            "主要病害": disease,
            "检测单位": "阈值复核矩阵",
            "检测状态": label,
            "专项结论": "",
            "结论有效期至": "",
            "关联支座单号": link_key,
            "数据来源": "支座阈值复核",
            "复核批次": batch_no,
        })

    def _sync_project_todo(
        self, bearing: dict[str, Any], bridge_name: str,
        conclusion: str, cycle: str, batch_no: str,
    ) -> None:
        bearing_no = str(bearing.get("支座编号", ""))
        todo_type = {"维修": "支座维修", "更换": "支座更换"}.get(conclusion)
        todo_key = f"bearing:{conclusion}:{bearing_no}:{cycle}"
        # 先把该支座旧周期/旧结论挂着的待办核销，避免同一支座挂多张互相矛盾的单
        for row in store.rows(PROJECT_MODULE):
            if row.get("关联支座编号") == bearing_no and row.get("待办来源") == "支座阈值复核":
                if row.get("待办键") != todo_key and row.get("status") not in ("已竣工", "已验收", "已核销"):
                    row["status"] = "已核销"
                    row["工程状态"] = "已核销"
                    row["pending"] = False
                    row["abnormal"] = False
        existing = next((row for row in store.rows(PROJECT_MODULE) if row.get("待办键") == todo_key), None)
        if todo_type is None:
            if existing is not None and existing.get("status") not in ("已竣工", "已验收", "已核销"):
                existing["status"] = "已核销"
                existing["工程状态"] = "已核销"
                existing["pending"] = False
                existing["abnormal"] = False
            return
        if existing is not None:
            if existing.get("status") == "已核销":
                existing["status"] = "待开工"
                existing["工程状态"] = "待开工"
                existing["pending"] = True
                existing["abnormal"] = True
            existing["复核批次"] = batch_no
            return
        store.rows(PROJECT_MODULE).append({
            "id": store.next_id(PROJECT_MODULE),
            "status": "待开工",
            "pending": True,
            "abnormal": True,
            "工程编号": f"PROJ-{bearing_no}-{cycle}",
            "工程名称": f"{bridge_name} {bearing_no} 支座{conclusion}工程",
            "工程类型": todo_type,
            "施工路段": bridge_name,
            "承建单位": "待派单",
            "开工日期": "",
            "竣工日期": "",
            "工程状态": "待开工",
            "待办来源": "支座阈值复核",
            "关联支座编号": bearing_no,
            "关联检查单号": f"BCHK-{bearing_no[5:]}-{cycle}" if bearing_no.startswith("BEAR-") else "",
            "待办键": todo_key,
            "复核批次": batch_no,
        })

    def run_batch(
        self,
        *,
        batch_no: str | None = None,
        bridge: str | None = None,
        cycle: str | None = None,
        today: date | None = None,
    ) -> dict[str, Any]:
        """批量复核：按桥梁支座分组，一组一个事务；重复批次号幂等回放。"""
        today = today or date.today()
        rule = self.active_rule()
        if rule is None:
            raise ValueError("没有启用的阈值矩阵规则")
        cycle = cycle or current_cycle(today)

        # 幂等：批次号已成功提交过则原样回放，不再写任何数据；
        # 上次部分失败的批次允许带同号重试，结果更新回原批次行，不追加重复台账。
        previous = next((row for row in store.rows(RUN_MODULE) if row.get("批次号") == batch_no), None)
        if previous is not None and batch_no is not None and previous.get("finished"):
            return {"batch_no": batch_no, "幂等": True, "groups": [], **{
                key: previous[key] for key in ("更换", "维修", "巡检", "跳过", "失败")
            }, "message": "批次已提交，回放既有结果"}

        targets = [
            row for row in store.rows(CHECK_MODULE)
            if row.get("检查周期") == cycle
            and (not bridge or row.get("所属桥梁") == bridge)
        ]
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in targets:
            groups.setdefault(str(row.get("所属桥梁", "未分组桥梁")), []).append(row)

        if not batch_no:
            batch_no = f"BATCH-{today.strftime('%Y%m%d')}-{cycle}"
            run_row = next((row for row in store.rows(RUN_MODULE) if row.get("批次号") == batch_no), None)
            if run_row is not None and run_row.get("finished"):
                return {"batch_no": batch_no, "幂等": True, "groups": [], **{
                    key: run_row[key] for key in ("更换", "维修", "巡检", "跳过", "失败")
                }, "message": "批次已提交，回放既有结果"}

        # 没有显式批次号时，重复执行必须幂等：目标单都已复核完就回放该周期最近一批，
        # 不新建批次台账、不再触发任何写回。
        if targets and not any(
            row.get("status") != "已复核" or row.get("复核结论") not in CONCLUSIONS
            for row in targets
        ):
            latest = next(
                (row for row in reversed(store.rows(RUN_MODULE))
                 if row.get("检查周期") == cycle and row.get("finished")),
                None,
            )
            if latest is not None:
                return {"batch_no": str(latest["批次号"]), "幂等": True, "groups": [], **{
                    key: latest[key] for key in ("更换", "维修", "巡检", "跳过", "失败")
                }, "message": "该周期检查单均已复核，回放最近批次结果"}

        group_results: list[dict[str, Any]] = []
        totals = Counter()
        for bridge_name, checks in groups.items():
            snapshot = store.snapshot(WRITE_TABLES)
            try:
                items = [
                    self.review_one(int(check["id"]), batch_no=batch_no, today=today, rule=rule)
                    for check in checks
                ]
            except Exception as exc:
                # 整组回滚：该桥任何一张单失败，都不允许留下半截处置建议
                store.restore(snapshot)
                totals["失败"] += len(checks)
                group_results.append({
                    "桥梁": bridge_name, "status": "失败", "ok": False,
                    "检查单数": len(checks), "items": [], "error": str(exc),
                })
                continue
            counted = Counter(item["结论"] for item in items if not item.get("跳过"))
            skipped = sum(1 for item in items if item.get("跳过"))
            totals.update(counted)
            totals["跳过"] += skipped
            group_results.append({
                "桥梁": bridge_name, "status": "已提交", "ok": True,
                "检查单数": len(checks),
                "更换": counted["更换"], "维修": counted["维修"], "巡检": counted["巡检"],
                "跳过": skipped, "items": items,
            })

        failed = any(not group["ok"] for group in group_results)
        if previous is not None:
            run_record = previous
            run_record.update({
                "桥梁分组": "、".join(groups.keys()),
                "检查单数": len(targets),
                "更换": totals["更换"], "维修": totals["维修"], "巡检": totals["巡检"],
                "跳过": totals["跳过"], "失败": totals["失败"],
                "status": "部分失败" if failed else "已提交",
                "finished": not failed,
                "复核时间": today.isoformat(),
                "规则版本": rule["规则版本"],
                "检查周期": cycle,
            })
        else:
            run_record = {
                "id": store.next_id(RUN_MODULE),
                "批次号": batch_no,
                "桥梁分组": "、".join(groups.keys()),
                "检查单数": len(targets),
                "更换": totals["更换"], "维修": totals["维修"], "巡检": totals["巡检"],
                "跳过": totals["跳过"], "失败": totals["失败"],
                "status": "部分失败" if failed else "已提交",
                "finished": not failed,
                "复核时间": today.isoformat(),
                "规则版本": rule["规则版本"],
                "检查周期": cycle,
            }
            store.rows(RUN_MODULE).append(run_record)
        return {
            "batch_no": batch_no, "幂等": False, "groups": group_results,
            "更换": totals["更换"], "维修": totals["维修"], "巡检": totals["巡检"],
            "跳过": totals["跳过"], "失败": totals["失败"],
            "risk": store.risk_summary(),
        }


review_service = BearingReviewService()
