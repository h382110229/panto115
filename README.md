# Panto115

多网盘资源聚合搜索 · 一键转存到 115 网盘

## 功能

- 🔍 多源聚合搜索（UP云搜 + PanSearch，覆盖阿里云盘/夸克/百度/迅雷等）
- 🔄 115 分享链接转存
- 🧲 磁力/HTTP 离线下载到 115
- 🌐 响应式深色 Web 界面，零前端依赖

## 快速部署 (Docker Compose)

### 前置依赖

- Docker 20.10+
- Docker Compose v2
- 115 网盘 Cookie

### 部署步骤

```bash
# 1. 克隆仓库
git clone https://github.com/h382110229/panto115.git
cd panto115

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 115 Cookie（见下方抓取方式）

# 3. 启动服务
docker compose up -d --build

# 4. 验证
curl http://localhost:8000/health
# 浏览器访问 http://localhost:8000
```

### 自定义端口

```bash
# .env 中设置
APP_PORT=9000
```

## 115 Cookie 抓取方式

1. 浏览器登录 [115.com](https://115.com)
2. F12 → Application → Cookies → `https://115.com`
3. 复制完整 Cookie 字符串（需包含 `UID`, `CID`, `SEID`, `KID` 等字段）
4. 粘贴到 `.env` 的 `COOKIE_115` 字段

> ⚠️ Cookie 有效期有限，过期后需重新抓取更新。

## Cloudflare Tunnel 接入

如需通过域名公网访问：

```bash
# 安装 cloudflared
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared

# 登录
cloudflared tunnel login

# 创建隧道
cloudflared tunnel create panto115
cloudflared tunnel route dns panto115 panto.yourdomain.com

# 运行（或配置为 systemd 服务）
cloudflared tunnel run --url http://localhost:8000 panto115
```

## API

| 端点 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 健康检查 |
| `/api/status` | GET | 115 账号状态 |
| `/api/search?q=关键词&pan=all` | GET | 多源聚合搜索 |
| `/api/save` | POST | 一键转存 `{"url":"...","extract_code":""}` |

## 技术栈

- **后端**: FastAPI + httpx + pydantic + pycryptodome
- **前端**: 原生 HTML/CSS/JS（零依赖）
- **加密**: m115 协议（AES-CBC + RSA + XOR）
- **容器**: Python 3.11-slim + Docker Compose
