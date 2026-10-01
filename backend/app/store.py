"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

复核矩阵需要「按桥梁分组事务提交、失败整体回滚」，这里用表级快照模拟事务：
- snapshot/restore 只作用于调用方点名的表，组间互不污染；
- batch_lock 是一把串行锁，保证批量复核与规则切换不会交错改写。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self.batch_lock = threading.RLock()

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

    # -- 事务支持：深拷贝整表作为快照，异常时按快照恢复（含被删除的行） --
    def snapshot(self, *modules: str) -> dict[str, list[dict[str, Any]]]:
        return {name: [dict(row) for row in self.rows(name)] for name in modules}

    def restore(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        for name, rows in snapshot.items():
            self._tables[name] = [dict(row) for row in rows]

    def overview(self) -> dict[str, object]:
        # 总览风险等级只统计当前生效版本、尚未闭环的复核建议。
        active_version = ""
        active_risks = {"低": 0, "中": 0, "高": 0}
        try:
            from app.services.bearing_review import bearing_review_service

            active_version = bearing_review_service.active_version()
            for row in self.rows("bearing"):
                risk = row.get("复核风险")
                if (
                    risk in active_risks
                    and row.get("复核版本") == active_version
                    and not row.get("复核已闭环")
                ):
                    active_risks[str(risk)] += 1
        except Exception:
            pass

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
                item["risks"] = dict(active_risks)
            modules.append(item)
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
            {"label": "复核高风险", "value": active_risks["高"]},
        ]
        return {
            "cards": cards,
            "modules": modules,
            "risk_levels": {"version": active_version, **active_risks},
        }


store = Store()
