"""
Panto115 — 115 网盘聚合管理服务
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routers.api import router as api_router

app = FastAPI(
    title="Panto115",
    description="115 网盘聚合管理服务",
    debug=settings.debug,
)

# CORS — 允许全源访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 API 路由
app.include_router(api_router)

# 健康检查
@app.get("/health")
async def health_check():
    return {"status": "ok"}


# 挂载前端静态文件 (映射到 Web 根路径)
# main.py 在 /app/app/main.py，parent.parent = /app
BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
if not FRONTEND_DIR.exists():
    FRONTEND_DIR = Path("/app/frontend")
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:
    print(f"[Warning] Frontend directory not found! Checked: {BASE_DIR / 'frontend'}, /app/frontend")
