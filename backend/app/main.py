from fastapi import FastAPI

from app.config import settings

app = FastAPI(
    title="Panto115",
    description="115 网盘聚合管理服务",
    debug=settings.debug,
)


@app.get("/health")
async def health_check():
    return {"status": "ok"}
