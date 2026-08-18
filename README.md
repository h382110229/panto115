# Panto115

115 网盘聚合管理服务。

## 快速开始

```bash
cp .env.example .env
# 编辑 .env 填入 115 Cookie

# Docker 部署
docker-compose up -d

# 本地开发
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## API

- `GET /health` — 健康检查
