"""承载力与锈蚀阈值复核矩阵接口。

路由注册在 bearing 主路由之前：/matrix、/checks、/reviews 若后注册会被
bearing 的 /{entry_id}（int）截获报 422。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.bearing_review import review_service

router = APIRouter(prefix="/api/bearing", tags=["支座阈值复核"])


@router.get("/matrix", response_model=dict)
def list_matrix() -> dict:
    """列出全部版本的阈值矩阵，并标明当前启用版本。"""
    rules = review_service.list_rules()
    active = next((row.get("规则版本") for row in rules if row.get("active")), None)
    return {"active": active, "items": rules}


@router.post("/matrix", response_model=ActionResult)
def create_matrix(payload: EntryPayload) -> ActionResult:
    """登记新版本阈值矩阵（默认不启用，需显式切换）。"""
    rule, message = review_service.create_rule(payload.values)
    if rule is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=f"矩阵 {rule['规则版本']} 已登记（未启用）", entry=rule)


@router.post("/matrix/{version}/activate", response_model=ActionResult)
def activate_matrix(version: str) -> ActionResult:
    """切换启用版本：同步迁移未完成检查单，历史支座检查单保持原记录。"""
    rule, message, migrated = review_service.activate_rule(version)
    if rule is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"rule": rule, "migrated": migrated})


@router.get("/checks", response_model=PageResult[dict])
def list_checks(
    bridge: str | None = Query(default=None, description="按所属桥梁过滤"),
    bearing_no: str | None = Query(default=None, description="按支座编号过滤"),
    cycle: str | None = Query(default=None, description="检查周期，如 2026Q3"),
    status: str | None = Query(default=None, description="待巡检 / 待复核 / 已复核"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """支座检查单分页列表，支持按桥梁支座与检查周期过滤。"""
    items, total = review_service.list_checks(
        bridge=bridge, bearing_no=bearing_no, cycle=cycle, status=status, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/checks/{check_id}/measurement", response_model=ActionResult)
def submit_measurement(check_id: int, payload: EntryPayload) -> ActionResult:
    """现场填报实测承载利用率、锈蚀率、位移量；历史支座检查单拒收。"""
    check, message = review_service.submit_measurement(check_id, payload.values)
    if check is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=check)


@router.post("/reviews/batch", response_model=ActionResult)
def run_batch(payload: EntryPayload) -> ActionResult:
    """批量复核：按桥梁分组事务提交；重复批次幂等；失败组回滚、不部分改写建议。"""
    values = payload.values or {}
    raw_date = values.get("today")
    today = date.fromisoformat(raw_date) if raw_date else date.today()
    try:
        result = review_service.run_batch(
            batch_no=(str(values["batch_no"]).strip() if values.get("batch_no") else None),
            bridge=(str(values["bridge"]).strip() if values.get("bridge") else None),
            cycle=(str(values["cycle"]).strip() if values.get("cycle") else None),
            today=today,
        )
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    ok = not result.get("失败")
    message = (
        f"批次 {result['batch_no']} 提交完成："
        f"更换 {result['更换']} / 维修 {result['维修']} / 巡检 {result['巡检']}"
        f" / 跳过 {result['跳过']} / 失败 {result['失败']}"
        + ("（重复执行，幂等回放）" if result.get("幂等") else "")
    )
    return ActionResult(ok=ok, message=message, entry=result)


@router.get("/reviews", response_model=dict)
def list_reviews() -> dict:
    """复核批次台账与当前风险等级汇总。"""
    from app.store import store

    return {
        "items": list(reversed(store.rows("bearing_review_run"))),
        "risk": store.risk_summary(),
    }
