"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

批量复核要求「按桥梁分组事务提交、失败不能部分改写」，内存实现用深拷贝快照模拟
事务：组内任意一步校验失败就回滚该组快照，已提交的其他组不受影响。
"""
from __future__ import annotations

import copy
from typing import Any

from app.seed import SEED_ROWS

# 复核结论到风险等级的统一口径，看板与各处写回都从这里取
RISK_BY_CONCLUSION = {"更换": "高", "维修": "中", "巡检": "低", "历史保留": "低", "": ""}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def snapshot(self, modules: list[str]) -> dict[str, list[dict[str, Any]]]:
        """对若干表做深拷贝快照，供事务回滚使用。"""
        return {module: copy.deepcopy(self.rows(module)) for module in modules}

    def restore(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        """把表恢复到快照状态：组内中途失败时调用，保证不留下半截建议。"""
        for module, rows in snapshot.items():
            self._tables[module] = copy.deepcopy(rows)

    def risk_summary(self) -> dict[str, int]:
        """支座台账按风险等级汇总，供总览看板与复核接口共用同一份口径。"""
        summary = {"高": 0, "中": 0, "低": 0}
        for row in self.rows("bearing"):
            level = str(row.get("风险等级") or "")
            if level in summary:
                summary[level] += 1
        return summary

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            item: dict[str, object] = {
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            }
            if name == "bearing":
                risk = self.risk_summary()
                item["high_risk"] = risk["高"]
                item["medium_risk"] = risk["中"]
                item["low_risk"] = risk["低"]
            modules.append(item)
        risk = self.risk_summary()
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
            {"label": "支座高风险", "value": risk["高"]},
            {"label": "支座中风险", "value": risk["中"]},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
