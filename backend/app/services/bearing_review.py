"""支座「承载力与锈蚀阈值复核矩阵」业务规则。

判定输入（按支座编号取台账值）：
- 承载力比 = 实际荷载 / 设计承载力（锈蚀折减后）；
- 锈蚀率（锈蚀程度字段里的百分数或等级描述）；
- 位移量（mm）；
- 检查周期：由「最近检查」推导所属季度，作为检查单幂等键的一部分。

矩阵输出：巡检 / 维修 / 更换，三档建议对应低 / 中 / 高风险。

口径约定：
- 承载力阈值与锈蚀阈值给出的建议不一致时视为「阈值冲突」，由设计承载力比
  与最近专项结论共同裁定（见 arbitrate）；
- 历史支座（状态为已拆除/已更换，或打了「历史支座」标记）不重新计算，
  继续按原检查记录保留；
- 规则切换（v1/v2）时，未完成检查单原地迁移到新版本并重算建议，
  已完成的检查单保持原版本、原结论不动；
- 批量复核按「所属桥梁」分组，每组一次事务：组内任一支座写回失败，
  整组回滚，不会出现部分建议已改写的情况；
- 重复执行幂等：同（支座编号、检查周期、规则版本）只保留一张检查单，
  定检清单与工程待办用稳定的业务键做 upsert。
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from datetime import datetime
from typing import Any

from app.store import store

BEARING_MODULE = "bearing"
REVIEW_MODULE = "bearing_review"
BRIDGE_MODULE = "bridge"
PROJECT_MODULE = "project"

ADVICE_INSPECT = "巡检"
ADVICE_REPAIR = "维修"
ADVICE_REPLACE = "更换"
RISK_BY_ADVICE = {ADVICE_INSPECT: "低", ADVICE_REPAIR: "中", ADVICE_REPLACE: "高"}
ADVICE_RANK = {ADVICE_INSPECT: 1, ADVICE_REPAIR: 2, ADVICE_REPLACE: 3}

REVIEW_OPEN = "待处置"
REVIEW_DONE = "已完成"

HISTORY_STATUSES = {"已拆除", "已更换", "历史"}

# 专项结论到建议档的映射：只有明确结论才参与裁定。
SPECIAL_ADVICE = {
    "可继续使用": ADVICE_INSPECT,
    "观察使用": ADVICE_INSPECT,
    "加强观察": ADVICE_INSPECT,
    "建议维修": ADVICE_REPAIR,
    "维修后观察": ADVICE_REPAIR,
    "建议更换": ADVICE_REPLACE,
    "必须更换": ADVICE_REPLACE,
}

# 锈蚀等级描述（无百分数时）的保守映射。
RUST_LEVEL_RATE = {
    "无": 0.0,
    "轻微": 0.10,
    "一般": 0.22,
    "明显": 0.35,
    "较重": 0.45,
    "严重": 0.60,
}

PROJECT_OPEN_STATUSES = {"待开工", "施工中"}
PROJECT_DONE_STATUSES = {"已竣工", "已验收"}

GROUP_TABLES = (BEARING_MODULE, REVIEW_MODULE, BRIDGE_MODULE, PROJECT_MODULE)


class ReviewRuleError(ValueError):
    """复核前置条件不满足（缺编号、规则版本不存在等），调用方整批放弃。"""


class GroupCommitError(RuntimeError):
    """某一组事务提交阶段失败，触发该组回滚。"""


# ---------------------------------------------------------------------------
# 阈值规则：不同版本对应不同阈值，切换版本即「规则切换」。
# ---------------------------------------------------------------------------
RULE_SETS: dict[str, dict[str, Any]] = {
    "v1": {
        "label": "2023 版支座复核阈值",
        # 承载力比：>=replace 更换，>=repair 维修，否则该维度不触发处置
        "capacity_replace": 0.95,
        "capacity_repair": 0.80,
        # 锈蚀率
        "rust_replace": 0.50,
        "rust_repair": 0.25,
        # 位移（mm）
        "displace_replace": 30.0,
        "displace_repair": 20.0,
        # 检查周期逾期（天）：超过即安排巡检
        "overdue_days": 180,
        # 冲突裁定：承载力比超过硬线时，即使专项结论偏乐观也必须更换
        "capacity_hardline": 0.90,
    },
    "v2": {
        "label": "2026 版支座复核阈值（加严）",
        "capacity_replace": 0.90,
        "capacity_repair": 0.75,
        "rust_replace": 0.45,
        "rust_repair": 0.20,
        "displace_replace": 25.0,
        "displace_repair": 15.0,
        "overdue_days": 120,
        "capacity_hardline": 0.85,
    },
}

DEFAULT_VERSION = "v1"


def _first_number(text: Any) -> float | None:
    """从「2000kN」「35mm」「52%」这类带单位字符串里取第一个数。"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    match = re.search(r"-?\d+(?:\.\d+)?", str(text).replace(",", ""))
    return float(match.group()) if match else None


def parse_capacity(text: Any) -> float | None:
    return _first_number(text)


def parse_rust_rate(text: Any) -> float | None:
    """锈蚀程度支持百分数（52%）与等级描述（严重）两种口径。"""
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    number = _first_number(raw)
    if number is not None and ("%" in raw or number <= 100):
        return number / 100.0 if number > 1 else number
    for word, rate in RUST_LEVEL_RATE.items():
        if word in raw:
            return rate
    return None


def parse_displacement(text: Any) -> float | None:
    return _first_number(text)


def parse_date(text: Any) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def inspection_period(date_text: Any, *, today: datetime | None = None) -> str:
    """检查周期按季度归一，如 2026-08-20 → 2026Q3；无法解析时落到未知周期。"""
    date = parse_date(date_text)
    if date is None:
        return "未知周期"
    quarter = (date.month - 1) // 3 + 1
    return f"{date.year}Q{quarter}"


def is_historical(bearing: dict[str, Any]) -> bool:
    if bearing.get("历史支座"):
        return True
    return str(bearing.get("支座状态") or bearing.get("status") or "") in HISTORY_STATUSES


def _capacity_advice(ratio: float | None, rule: dict[str, Any]) -> str | None:
    if ratio is None:
        return None
    if ratio >= rule["capacity_replace"]:
        return ADVICE_REPLACE
    if ratio >= rule["capacity_repair"]:
        return ADVICE_REPAIR
    return None


def _rust_advice(rate: float | None, rule: dict[str, Any]) -> str | None:
    if rate is None:
        return None
    if rate >= rule["rust_replace"]:
        return ADVICE_REPLACE
    if rate >= rule["rust_repair"]:
        return ADVICE_REPAIR
    return None


def _displacement_advice(value: float | None, rule: dict[str, Any]) -> str | None:
    if value is None:
        return None
    if value >= rule["displace_replace"]:
        return ADVICE_REPLACE
    if value >= rule["displace_repair"]:
        return ADVICE_REPAIR
    return None


def arbitrate(
    capacity_advice: str | None,
    rust_advice: str | None,
    displacement_advice: str | None,
    capacity_ratio: float | None,
    special_text: str,
    rule: dict[str, Any],
) -> tuple[str, bool, str]:
    """承载力阈值与锈蚀阈值冲突时，由设计承载力与最近专项结论共同裁定。

    返回 (建议, 是否发生阈值冲突, 裁定依据)。

    裁定原则（安全优先，专项结论只能「共同裁定」不能无条件放行）：
    1. 设计承载力比超过硬线 → 直接更换，专项结论不能放宽；
    2. 阈值档与专项结论不一致即视为冲突：专项更严则加严；专项更松时，
       凭设计承载力富余度共同裁定，最多下调一档；
    3. 无专项结论且承载力/锈蚀维度互不一致 → 就高不就低。
    """
    threshold_dimensions = [a for a in (capacity_advice, rust_advice, displacement_advice) if a]
    threshold_advice = max(
        threshold_dimensions,
        key=lambda a: ADVICE_RANK[a],
        default=ADVICE_INSPECT,
    )
    dimensional_conflict = (
        len({a for a in (capacity_advice, rust_advice) if a}) >= 2
        and capacity_advice != rust_advice
    )

    special_text = str(special_text or "").strip()
    special_advice = next(
        (adv for word, adv in SPECIAL_ADVICE.items() if word in special_text),
        None,
    )

    # 设计承载力硬线：实荷/设计超过该比值，任何结论都不能放行更换要求。
    if capacity_ratio is not None and capacity_ratio >= rule["capacity_hardline"]:
        reason = f"设计承载力比 {capacity_ratio:.2f} 达到硬线 {rule['capacity_hardline']:.2f}"
        if special_advice:
            reason += f"，与专项结论「{special_text}」共同裁定仍为更换"
        return ADVICE_REPLACE, dimensional_conflict, reason

    conflict = dimensional_conflict or (
        special_advice is not None and special_advice != threshold_advice
    )

    if special_advice is None:
        if dimensional_conflict:
            return (
                threshold_advice,
                True,
                "承载力与锈蚀阈值结论冲突且无专项结论，按安全优先就高裁定为"
                + threshold_advice,
            )
        if threshold_dimensions:
            return threshold_advice, False, "按承载力/锈蚀/位移阈值判定"
        return ADVICE_INSPECT, False, "各项阈值均在允许范围"

    if ADVICE_RANK[special_advice] >= ADVICE_RANK[threshold_advice]:
        basis = f"按最近专项结论「{special_text}」加严裁定为{special_advice}"
        if dimensional_conflict:
            basis = "阈值冲突，" + basis
        return special_advice, conflict, basis

    # 专项结论更宽松：设计承载力富余度共同裁定，最多下调一档。
    capacity_healthy = capacity_ratio is not None and capacity_ratio < rule["capacity_repair"]
    if capacity_healthy and ADVICE_RANK[threshold_advice] > ADVICE_RANK[ADVICE_INSPECT]:
        softened_rank = max(ADVICE_RANK[threshold_advice] - 1, ADVICE_RANK[special_advice])
        softened = next(name for name, rank in ADVICE_RANK.items() if rank == softened_rank)
        return (
            softened,
            conflict,
            f"阈值判定为{threshold_advice}，设计承载力比 {capacity_ratio:.2f} 富余、"
            f"最近专项结论「{special_text}」，共同裁定下调为{softened}",
        )
    return (
        threshold_advice,
        conflict,
        f"专项结论「{special_text}」偏松，但设计承载力比 "
        f"{capacity_ratio if capacity_ratio is None else round(capacity_ratio, 2)}"
        f"不富余，维持{threshold_advice}",
    )


def evaluate(
    bearing: dict[str, Any],
    *,
    version: str = DEFAULT_VERSION,
    today: datetime | None = None,
) -> dict[str, Any]:
    """对单条支座执行矩阵复核，产出可落库的结论结构（本函数不改任何数据）。"""
    rule = RULE_SETS[version]
    today = today or datetime.now()

    design = parse_capacity(bearing.get("设计承载力"))
    actual = parse_capacity(bearing.get("实际荷载"))
    rust_rate = parse_rust_rate(bearing.get("锈蚀程度"))
    displacement = parse_displacement(bearing.get("位移量"))

    # 承载力维度取实荷/设计的原始比值；锈蚀另走锈蚀阈值，避免重复计罚。
    capacity_ratio: float | None = None
    if design and actual is not None and design > 0:
        capacity_ratio = actual / design

    cap_advice = _capacity_advice(capacity_ratio, rule)
    rust_advice = _rust_advice(rust_rate, rule)
    disp_advice = _displacement_advice(displacement, rule)

    last_date = parse_date(bearing.get("最近检查"))
    overdue_days: int | None = None
    if last_date is not None:
        overdue_days = (today - last_date).days

    advice, conflict, basis = arbitrate(
        cap_advice,
        rust_advice,
        disp_advice,
        capacity_ratio,
        bearing.get("最近专项结论", ""),
        rule,
    )

    notes: list[str] = []
    if displacement is not None and disp_advice and ADVICE_RANK[disp_advice] > ADVICE_RANK[advice]:
        advice = disp_advice
        notes.append(f"位移量 {displacement:g}mm 达到{disp_advice}阈值，加严为{disp_advice}")

    # 检查周期逾期：至少安排巡检。
    if overdue_days is not None and overdue_days > rule["overdue_days"]:
        notes.append(f"最近检查已逾期 {overdue_days} 天（周期阈值 {rule['overdue_days']} 天），安排巡检")
        if ADVICE_RANK[advice] < ADVICE_RANK[ADVICE_INSPECT]:
            advice = ADVICE_INSPECT
    elif advice == ADVICE_INSPECT and not conflict:
        notes.append("按检查周期安排常规巡检")

    if notes:
        basis = f"{basis}；{'；'.join(notes)}"

    return {
        "支座编号": bearing.get("支座编号"),
        "所属桥梁": bearing.get("所属桥梁"),
        "检查周期": inspection_period(bearing.get("最近检查"), today=today),
        "规则版本": version,
        "建议": advice,
        "风险等级": RISK_BY_ADVICE[advice],
        "承载力比": round(capacity_ratio, 3) if capacity_ratio is not None else None,
        "锈蚀率": rust_rate,
        "位移mm": displacement,
        "逾期天数": overdue_days,
        "阈值冲突": conflict,
        "裁定依据": basis,
    }


class BearingReviewService:
    """矩阵复核 + 三处回写 + 规则迁移的应用服务。"""

    def __init__(self) -> None:
        self._version = DEFAULT_VERSION
        self._version_lock = threading.Lock()

    # -- 规则版本 ----------------------------------------------------------
    def active_version(self) -> str:
        return self._version

    def rule_matrix(self) -> dict[str, Any]:
        version = self._version
        rule = RULE_SETS[version]
        return {
            "active_version": version,
            "versions": [
                {"version": name, "label": item["label"], "active": name == version}
                for name, item in RULE_SETS.items()
            ],
            "thresholds": rule,
            "advice_levels": [
                {"advice": ADVICE_INSPECT, "risk": "低"},
                {"advice": ADVICE_REPAIR, "risk": "中"},
                {"advice": ADVICE_REPLACE, "risk": "高"},
            ],
        }

    # -- 检查单查询 --------------------------------------------------------
    def list_checklists(
        self,
        *,
        bridge: str | None = None,
        advice: str | None = None,
        status: str | None = None,
        include_history: bool = True,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(REVIEW_MODULE)
        if not include_history:
            rows = [row for row in rows if not row.get("历史遗留")]
        if bridge:
            rows = [row for row in rows if bridge in str(row.get("所属桥梁", ""))]
        if advice:
            rows = [row for row in rows if row.get("建议") == advice]
        if status:
            rows = [row for row in rows if row.get("检查单状态") == status]
        rows.sort(key=lambda row: (str(row.get("复核时间", "")), int(row.get("id", 0))), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_checklist(self, checklist_id: int) -> dict[str, Any] | None:
        return store.find(REVIEW_MODULE, checklist_id)

    # -- 批量复核（按桥梁分组事务） ----------------------------------------
    def run_batch(
        self,
        *,
        bridge: str | None = None,
        version: str | None = None,
        bearing_codes: list[str] | None = None,
        today: datetime | None = None,
    ) -> dict[str, Any]:
        version = version or self._version
        if version not in RULE_SETS:
            raise ReviewRuleError(f"规则版本「{version}」不存在，可选：{'、'.join(RULE_SETS)}")
        today = today or datetime.now()

        bearings = [
            row for row in store.rows(BEARING_MODULE)
            if not bridge or str(row.get("所属桥梁") or "") == bridge
        ]
        if bearing_codes:
            wanted = set(bearing_codes)
            bearings = [row for row in bearings if str(row.get("支座编号") or "") in wanted]
            found = {str(row.get("支座编号")) for row in bearings}
            missing = sorted(wanted - found)
            if missing:
                raise ReviewRuleError(f"支座编号不存在或不在指定桥梁下：{'、'.join(missing)}")

        # 前置校验：支座编号缺失/重复会让幂等键失真，整批直接拒绝、不动任何数据。
        codes = [str(row.get("支座编号") or "").strip() for row in bearings]
        empty = [str(row.get("id")) for row, code in zip(bearings, codes) if not code]
        if empty:
            raise ReviewRuleError(f"以下台账记录缺少支座编号，无法复核：{'、'.join(empty)}")
        dup = sorted({code for code in codes if codes.count(code) > 1})
        if dup:
            raise ReviewRuleError(f"支座编号重复，复核结果无法唯一定位：{'、'.join(dup)}")

        groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        for row in sorted(bearings, key=lambda r: (str(r.get("所属桥梁") or ""), str(r.get("支座编号") or ""))):
            groups.setdefault(str(row.get("所属桥梁") or "未归类桥梁"), []).append(row)

        results: list[dict[str, Any]] = []
        with store.batch_lock:
            for bridge_name, group_rows in groups.items():
                outcome = self._commit_group(bridge_name, group_rows, version, today)
                results.append(outcome)

        reviewed = sum(item["reviewed"] for item in results)
        skipped = sum(item["skipped_history"] for item in results)
        return {
            "version": version,
            "groups": results,
            "group_count": len(results),
            "reviewed": reviewed,
            "skipped_history": skipped,
            "advice_counts": {
                ADVICE_INSPECT: sum(item["advice_counts"].get(ADVICE_INSPECT, 0) for item in results),
                ADVICE_REPAIR: sum(item["advice_counts"].get(ADVICE_REPAIR, 0) for item in results),
                ADVICE_REPLACE: sum(item["advice_counts"].get(ADVICE_REPLACE, 0) for item in results),
            },
        }

    def _commit_group(
        self,
        bridge_name: str,
        bearings: list[dict[str, Any]],
        version: str,
        today: datetime,
    ) -> dict[str, Any]:
        """一组桥梁一次事务：先快照四张表，写回过程出错则整组恢复快照。"""
        snapshot = store.snapshot(*GROUP_TABLES)
        advice_counts = {ADVICE_INSPECT: 0, ADVICE_REPAIR: 0, ADVICE_REPLACE: 0}
        reviewed = 0
        skipped_history = 0
        checklists: list[dict[str, Any]] = []
        try:
            for bearing in bearings:
                if is_historical(bearing):
                    skipped_history += 1
                    continue
                result = evaluate(bearing, version=version, today=today)
                checklist = self._upsert_checklist(bearing, result, today)
                self._write_back_bearing(bearing, result, checklist)
                checklists.append(checklist)
                advice_counts[result["建议"]] += 1
                reviewed += 1
            self._write_back_bridge(bridge_name, checklists, version, today)
            self._write_back_project(bridge_name, checklists, today)
        except Exception as exc:  # 整组回滚，杜绝部分改写
            store.restore(snapshot)
            raise GroupCommitError(f"桥梁「{bridge_name}」复核写回失败，整组未提交：{exc}") from exc
        return {
            "bridge": bridge_name,
            "reviewed": reviewed,
            "skipped_history": skipped_history,
            "advice_counts": advice_counts,
            "checklist_ids": [int(item["id"]) for item in checklists],
        }

    # -- 检查单 upsert（幂等核心） -----------------------------------------
    def _upsert_checklist(
        self,
        bearing: dict[str, Any],
        result: dict[str, Any],
        today: datetime,
    ) -> dict[str, Any]:
        rows = store.rows(REVIEW_MODULE)
        code = result["支座编号"]
        period = result["检查周期"]
        version = result["规则版本"]

        existing_done = next(
            (row for row in rows
             if row.get("支座编号") == code
             and row.get("检查周期") == period
             and row.get("检查单状态") == REVIEW_DONE
             and not row.get("历史遗留")),
            None,
        )
        if existing_done is not None:
            # 周期内已完成的检查单不重开，重复执行保持原结论。
            return existing_done

        existing_open = next(
            (row for row in rows
             if row.get("支座编号") == code and row.get("检查单状态") == REVIEW_OPEN),
            None,
        )
        now_text = today.strftime("%Y-%m-%d %H:%M:%S")
        if existing_open is not None:
            existing_open.update({k: v for k, v in result.items() if k != "支座编号"})
            existing_open["检查单状态"] = REVIEW_OPEN
            existing_open["完成时间"] = None
            # 复核时间只在首次建单或建议发生变化时更新，保证重复执行字节级幂等。
            if existing_open.get("建议") != result["建议"]:
                existing_open["复核时间"] = now_text
            return existing_open

        entry = {
            "id": store.next_id(REVIEW_MODULE),
            **result,
            "检查单状态": REVIEW_OPEN,
            "复核时间": now_text,
            "完成时间": None,
            "历史遗留": False,
        }
        rows.append(entry)
        return entry

    # -- 回写一：支座台账 ---------------------------------------------------
    def _write_back_bearing(
        self,
        bearing: dict[str, Any],
        result: dict[str, Any],
        checklist: dict[str, Any],
    ) -> None:
        advice = result["建议"]
        bearing.update({
            "复核建议": advice,
            "复核风险": result["风险等级"],
            "复核依据": result["裁定依据"],
            "复核周期": result["检查周期"],
            "复核版本": result["规则版本"],
            "复核检查单": checklist["id"],
            "复核已闭环": checklist["检查单状态"] == REVIEW_DONE,
            "复核时间": checklist["复核时间"],
        })
        bearing["abnormal"] = advice != ADVICE_INSPECT
        bearing["pending"] = checklist["检查单状态"] != REVIEW_DONE

    # -- 回写二：桥梁定检清单 -----------------------------------------------
    def _bridge_key(self, bridge_name: str, version: str) -> str:
        return f"BEARING-REVIEW::{bridge_name}::{version}"

    def _write_back_bridge(
        self,
        bridge_name: str,
        checklists: list[dict[str, Any]],
        version: str,
        today: datetime,
    ) -> None:
        """每座桥每个规则版本维护一张「支座专项复核」定检清单（幂等 upsert）。"""
        rows = store.rows(BRIDGE_MODULE)
        key = self._bridge_key(bridge_name, version)
        entry = next((row for row in rows if row.get("复核键") == key), None)

        open_items = [c for c in checklists if c["检查单状态"] == REVIEW_OPEN]
        replace_n = sum(1 for c in open_items if c["建议"] == ADVICE_REPLACE)
        repair_n = sum(1 for c in open_items if c["建议"] == ADVICE_REPAIR)
        inspect_n = sum(1 for c in open_items if c["建议"] == ADVICE_INSPECT)
        worst = ADVICE_INSPECT
        for item in open_items:
            if ADVICE_RANK[item["建议"]] > ADVICE_RANK[worst]:
                worst = item["建议"]
        findings = (
            f"支座复核（{version}）：更换 {replace_n}、维修 {repair_n}、巡检 {inspect_n}；"
            f"最高处置档：{worst if open_items else '无待处置'}"
        )

        if entry is None:
            new_id = store.next_id(BRIDGE_MODULE)
            entry = {
                "id": new_id,
                "检测编号": f"BREV-{new_id:04d}",
                "复核键": key,
                "桥梁名称": bridge_name,
                "检测类型": "支座专项复核",
            }
            rows.append(entry)

        entry.update({
            "检测日期": today.strftime("%Y-%m-%d"),
            "技术状况评分": {"低": "二类", "中": "三类", "高": "四类"}[RISK_BY_ADVICE[worst]]
            if open_items else "一类",
            "主要病害": findings,
            "检测单位": "承载力与锈蚀阈值复核矩阵",
            "检测状态": "待跟踪" if open_items else "已闭环",
        })
        entry["status"] = "检测中" if open_items else "已评定"
        entry["pending"] = bool(open_items)
        entry["abnormal"] = replace_n > 0

    # -- 回写三：工程待办 ---------------------------------------------------
    def _project_code(self, bridge_name: str, advice: str) -> str:
        return f"TODO-BEAR::{bridge_name}::{advice}"

    def _write_back_project(
        self,
        bridge_name: str,
        checklists: list[dict[str, Any]],
        today: datetime,
    ) -> None:
        """按（桥、处置档）维护工程待办：仍需要则 upsert，降档则取消旧待办。"""
        rows = store.rows(PROJECT_MODULE)
        wanted = {c["建议"] for c in checklists if c["检查单状态"] == REVIEW_OPEN}

        for advice in (ADVICE_INSPECT, ADVICE_REPAIR, ADVICE_REPLACE):
            code = self._project_code(bridge_name, advice)
            entry = next((row for row in rows if row.get("待办键") == code), None)
            if advice in wanted:
                counts = sum(
                    1 for c in checklists
                    if c["检查单状态"] == REVIEW_OPEN and c["建议"] == advice
                )
                if entry is None:
                    new_id = store.next_id(PROJECT_MODULE)
                    entry = {
                        "id": new_id,
                        "工程编号": f"TB-{new_id:04d}",
                        "待办键": code,
                        "工程名称": f"{bridge_name}支座{advice}待办",
                        "工程类型": f"支座{advice}",
                        "施工路段": bridge_name,
                        "承建单位": "待派单",
                        "开工日期": "",
                        "竣工日期": "",
                        "工程状态": "待开工",
                    }
                    rows.append(entry)
                if entry.get("status") not in PROJECT_DONE_STATUSES:
                    prefix = {
                        ADVICE_INSPECT: "安排支座巡检",
                        ADVICE_REPAIR: "安排支座维修",
                        ADVICE_REPLACE: "安排支座更换",
                    }[advice]
                    entry["工程名称"] = f"{bridge_name}支座{advice}待办（{counts}个）"
                    entry["主要内容"] = f"{prefix}，涉及支座 {counts} 个"
                    if str(entry.get("工程状态") or "") == "已取消":
                        entry["工程状态"] = "待开工"
                    entry["status"] = entry.get("status") if entry.get("status") in PROJECT_OPEN_STATUSES else "待开工"
                    entry["pending"] = True
                    entry["abnormal"] = advice == ADVICE_REPLACE
            elif entry is not None and entry.get("status") in PROJECT_OPEN_STATUSES | {"待开工", "施工中"}:
                # 建议降档/消失：尚未实施的待办取消，已实施的留作工程记录。
                entry["工程状态"] = "已取消"
                entry["status"] = "已取消"
                entry["pending"] = False
                entry["abnormal"] = False

    # -- 检查单闭环 --------------------------------------------------------
    def complete_checklist(self, checklist_id: int) -> dict[str, Any]:
        with store.batch_lock:
            checklist = store.find(REVIEW_MODULE, checklist_id)
            if checklist is None:
                raise ReviewRuleError(f"检查单 {checklist_id} 不存在")
            if checklist.get("检查单状态") == REVIEW_DONE:
                return checklist  # 幂等
            snapshot = store.snapshot(*GROUP_TABLES)
            try:
                now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                checklist["检查单状态"] = REVIEW_DONE
                checklist["完成时间"] = now_text

                code = checklist.get("支座编号")
                bearing = next(
                    (row for row in store.rows(BEARING_MODULE) if row.get("支座编号") == code),
                    None,
                )
                if bearing is not None:
                    bearing["复核已闭环"] = True
                    bearing["复核时间"] = now_text
                    bearing["pending"] = False

                # 重建该桥该版本的定检清单与待办聚合。
                bridge_name = str(checklist.get("所属桥梁") or "")
                version = str(checklist.get("规则版本") or self._version)
                siblings = [
                    row for row in store.rows(REVIEW_MODULE)
                    if row.get("所属桥梁") == bridge_name
                    and row.get("规则版本") == version
                    and not row.get("历史遗留")
                ]
                self._write_back_bridge(bridge_name, siblings, version, datetime.now())
                self._write_back_project(bridge_name, siblings, datetime.now())
            except Exception as exc:
                store.restore(snapshot)
                raise GroupCommitError(f"检查单 {checklist_id} 闭环失败，已回滚：{exc}") from exc
            return checklist

    # -- 规则切换：迁移未完成检查单 ----------------------------------------
    def switch_version(self, new_version: str) -> dict[str, Any]:
        if new_version not in RULE_SETS:
            raise ReviewRuleError(f"规则版本「{new_version}」不存在，可选：{'、'.join(RULE_SETS)}")
        with self._version_lock, store.batch_lock:
            old_version = self._version
            if new_version == old_version:
                return {"from": old_version, "to": new_version, "migrated": 0, "unchanged_done": 0}
            snapshot = store.snapshot(*GROUP_TABLES)
            try:
                migrated: list[dict[str, Any]] = []
                today = datetime.now()
                open_checklists = [
                    row for row in store.rows(REVIEW_MODULE)
                    if row.get("规则版本") == old_version
                    and row.get("检查单状态") == REVIEW_OPEN
                    and not row.get("历史遗留")
                ]
                done_checklists = [
                    row for row in store.rows(REVIEW_MODULE)
                    if row.get("规则版本") == old_version
                    and (row.get("检查单状态") == REVIEW_DONE or row.get("历史遗留"))
                ]

                # 先把旧版本打开的检查单按桥分组，用新版本重算并原地迁移。
                by_bridge: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
                for checklist in open_checklists:
                    by_bridge.setdefault(str(checklist.get("所属桥梁") or ""), []).append(checklist)

                migrated_bridges: list[str] = []
                for bridge_name, checklists in by_bridge.items():
                    for checklist in checklists:
                        bearing = next(
                            (row for row in store.rows(BEARING_MODULE)
                             if row.get("支座编号") == checklist.get("支座编号")),
                            None,
                        )
                        if bearing is None or is_historical(bearing):
                            continue
                        result = evaluate(bearing, version=new_version, today=today)
                        checklist.update({k: v for k, v in result.items() if k != "支座编号"})
                        # 迁移本身不改变「待处置」状态，也不重置周期。
                        checklist["检查单状态"] = REVIEW_OPEN
                        self._write_back_bearing(bearing, result, checklist)
                        migrated.append(checklist)
                    migrated_bridges.append(bridge_name)
                    group_all = [
                        row for row in store.rows(REVIEW_MODULE)
                        if row.get("所属桥梁") == bridge_name
                        and row.get("规则版本") == new_version
                        and not row.get("历史遗留")
                    ]
                    self._write_back_bridge(bridge_name, group_all, new_version, today)
                    self._write_back_project(bridge_name, group_all, today)

                # 旧版本定检聚合单整体收口留痕（工程待办不按版本区分，已在上面按新结论刷新）。
                for bridge_row in store.rows(BRIDGE_MODULE):
                    key = str(bridge_row.get("复核键") or "")
                    if key.startswith("BEARING-REVIEW::") and key.endswith(f"::{old_version}"):
                        if bridge_row.get("检测状态") != "已闭环":
                            bridge_row["检测状态"] = "已随规则切换收口"
                            bridge_row["status"] = "已评定"
                            bridge_row["pending"] = False

                # 旧版本的定检聚合清单：新周期没有待处置项则标记闭环，保留可追溯。
                self._version = new_version
            except Exception as exc:
                store.restore(snapshot)
                self._version = old_version
                raise GroupCommitError(f"规则切换失败，已回滚到 {old_version}：{exc}") from exc

            return {
                "from": old_version,
                "to": new_version,
                "migrated": len(migrated),
                "unchanged_done": len(done_checklists),
                "label": RULE_SETS[new_version]["label"],
            }


bearing_review_service = BearingReviewService()
