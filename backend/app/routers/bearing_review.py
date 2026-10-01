"""承载力与锈蚀阈值复核矩阵接口。

- GET  /matrix        查看当前阈值矩阵与规则版本
- POST /batch         按桥梁分组批量复核（事务、幂等）
- GET  /checklists    复核检查单列表
- POST /checklists/{id}/complete  检查单闭环
- POST /switch-version 规则切换并迁移未完成检查单
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.schemas import ActionResult, PageResult
from app.services.bearing_review import (
    GroupCommitError,
    ReviewRuleError,
    bearing_review_service,
)

router = APIRouter(prefix="/api/bearing/review", tags=["支座复核矩阵"])


class BatchPayload(BaseModel):
    bridge: str | None = None
    bearing_codes: list[str] | None = None
    version: str | None = None


class SwitchPayload(BaseModel):
    version: str


@router.get("/matrix")
def get_matrix() -> dict[str, Any]:
    """读取当前生效的复核阈值矩阵与全部规则版本。"""
    return bearing_review_service.rule_matrix()


@router.post("/batch", response_model=ActionResult)
def run_batch(payload: BatchPayload) -> ActionResult:
    """按桥梁支座分组批量复核；任一组写回失败都会整组回滚，不会部分改写建议。"""
    try:
        report = bearing_review_service.run_batch(
            bridge=payload.bridge,
            bearing_codes=payload.bearing_codes,
            version=payload.version,
        )
    except ReviewRuleError as exc:
        return ActionResult(ok=False, message=str(exc))
    except GroupCommitError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    message = (
        f"复核完成：{report['group_count']} 座桥、{report['reviewed']} 个支座"
        f"（更换 {report['advice_counts']['更换']}、"
        f"维修 {report['advice_counts']['维修']}、"
        f"巡检 {report['advice_counts']['巡检']}），"
        f"历史支座保留原记录 {report['skipped_history']} 个"
    )
    return ActionResult(ok=True, message=message, entry=report)


@router.get("/checklists", response_model=PageResult[dict])
def list_checklists(
    bridge: str | None = Query(default=None, description="按所属桥梁过滤"),
    advice: str | None = Query(default=None, description="巡检、维修、更换"),
    status: str | None = Query(default=None, description="待处置、已完成"),
    include_history: bool = True,
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """复核检查单分页查询；历史支座的遗留记录默认保留可见。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = bearing_review_service.list_checklists(
        bridge=bridge,
        advice=advice,
        status=status,
        include_history=include_history,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/checklists/{checklist_id}/complete", response_model=ActionResult)
def complete_checklist(checklist_id: int) -> ActionResult:
    """闭环一张复核检查单，并同步刷新定检清单与工程待办；重复提交幂等。"""
    try:
        checklist = bearing_review_service.complete_checklist(checklist_id)
    except ReviewRuleError as exc:
        return ActionResult(ok=False, message=str(exc))
    except GroupCommitError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ActionResult(ok=True, message=f"检查单 {checklist_id} 已闭环", entry=checklist)


@router.post("/switch-version", response_model=ActionResult)
def switch_version(payload: SwitchPayload) -> ActionResult:
    """切换阈值规则版本，未完成检查单迁移到新版本，已完成与历史记录保留。"""
    try:
        report = bearing_review_service.switch_version(payload.version)
    except ReviewRuleError as exc:
        return ActionResult(ok=False, message=str(exc))
    except GroupCommitError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ActionResult(
        ok=True,
        message=(
            f"规则已切换 {report['from']} → {report['to']}，"
            f"迁移未完成检查单 {report['migrated']} 张，"
            f"已完成/历史记录保留 {report['unchanged_done']} 张"
        ),
        entry=report,
    )
