"""
Panto115 API 路由

GET  /api/status  — 115 账号状态
GET  /api/search  — 多源聚合搜索
POST /api/save    — 一键转存
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services.saver_115 import Pan115Saver, classify_url
from app.services.aggregator import SearchAggregator, SearchResponse

router = APIRouter(prefix="/api")

_saver = Pan115Saver()
_aggregator = SearchAggregator()


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------

@router.get("/status")
async def get_status():
    """返回 115 账号登录状态与空间信息。"""
    try:
        info = await _saver.check_login_status()
        return {"success": True, "data": info}
    except ValueError as e:
        return {"success": False, "data": {"logged_in": False, "error": str(e)}}
    except Exception as e:
        return {"success": False, "data": {"logged_in": False, "error": f"{type(e).__name__}: {e}"}}


# ---------------------------------------------------------------------------
# GET /api/search?q=关键词&pan=all
# ---------------------------------------------------------------------------

@router.get("/search")
async def search(
    q: str = Query(..., min_length=1, description="搜索关键词"),
    pan: str = Query("all", description="网盘类型过滤: all|115|quark|aliyun|baidu"),
    page: int = Query(1, ge=1, description="页码"),
):
    """多源聚合搜索，支持网盘类型过滤。"""
    try:
        resp: SearchResponse = await _aggregator.search(q, page=page)

        # 按 pan 类型过滤
        if pan != "all":
            resp.results = [r for r in resp.results if r.pan_type == pan]
            resp.total = len(resp.results)

        return {
            "success": True,
            "data": resp.model_dump(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜索失败: {e}")


# ---------------------------------------------------------------------------
# POST /api/save
# ---------------------------------------------------------------------------

class SaveRequest(BaseModel):
    url: str
    extract_code: str = ""
    target_cid: str = "0"


@router.post("/save")
async def save(req: SaveRequest):
    """一键转存：自动判断链接类型并执行。"""
    try:
        result = await _saver.auto_save(req.url, req.extract_code)
        return {
            "success": result.get("success", False),
            "message": result.get("message", ""),
            "data": result,
        }
    except ValueError as e:
        return {"success": False, "message": str(e), "data": {}}
    except Exception as e:
        return {"success": False, "message": f"{type(e).__name__}: {e}", "data": {}}
