"""覆盖报告路由（P1-A）。

独立成 router 的理由与 baseline / hypotheses 一致：一个能力一处落点，
`projects.py` 已经承载了项目 CRUD 与验收盘点，再塞进来会更难维护。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sharp.server.coverage import build_coverage, coverage_markdown
from sharp.server.db import get_conn

router = APIRouter(tags=["coverage"])


@router.get("/projects/{project_id}/coverage")
def project_coverage(project_id: str):
    """项目覆盖报告：打到了什么 / 已验证不通 / 盲区 / 成本。

    返回结构化字段 + 渲染好的 `markdown`（报告上下文与前端共用同一份渲染，
    避免两处各写一套口径）。
    """
    with get_conn() as conn:
        try:
            report = build_coverage(conn, project_id)
        except HTTPException:
            raise
        except Exception as exc:  # 项目不存在时 get_project_or_404 会抛 404，其余按 500 暴露
            raise HTTPException(500, f"覆盖报告生成失败：{exc}") from exc
    payload = report.to_dict()
    payload["markdown"] = coverage_markdown(report)
    return payload
