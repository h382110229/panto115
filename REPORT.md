# Panto115 项目部署报告

## 项目概述

Panto115 — 115 网盘聚合搜索服务，支持多网盘资源搜索、115 分享链接转存、磁力/HTTP 离线下载。

- **技术栈**: FastAPI + 静态前端 (HTML/CSS/JS)
- **公网地址**: https://panto115.hawkren.online
- **本地目录**: `/home/hawk/panto115/`
- **Docker**: 单容器，镜像 `panto115-panto115`

---

## 已完成的修复（2026-08-19）

### 1. 前端静态文件 404 — main.py 路径错误

**问题**: 访问根路径 `/` 返回 `{"detail":"Not Found"}`

**原因**: `backend/app/main.py` 中 `Path(__file__).resolve().parent.parent.parent` 多了一层 parent，解析到 `/` 而非 `/app`

**修复**: 改为 `parent.parent` + fallback 逻辑

```python
BASE_DIR = Path(__file__).resolve().parent.parent  # /app
FRONTEND_DIR = BASE_DIR / "frontend"
if not FRONTEND_DIR.exists():
    FRONTEND_DIR = Path("/app/frontend")
```

### 2. Docker 网络隔离 — 无法被 Traefik/CF Tunnel 访问

**问题**: panto115 在独立的 `panto115_default` 网络，Traefik 和 cloudflared 都在 `homelab` 网络，无法解析 `panto115` 主机名

**修复**:
- `docker-compose.yml`: 移除 `ports` 映射，加入 `homelab` external 网络
- 容器不再直接暴露宿主机端口

### 3. CF Tunnel 路由未经过 Traefik

**问题**: Tunnel ingress 直接指向 `http://panto115:8000`，与其他服务（统一走 Traefik）不一致

**修复**: 通过 CF API 将 ingress 改为 `https://traefik:443`

```
panto115.hawkren.online -> https://traefik:443
```

### 4. CF Tunnel 缺少 noTLSVerify

**问题**: cloudflared 连接 Traefik HTTPS 时做 TLS 证书验证失败（Traefik 用 Let's Encrypt 证书）

**修复**: 在 CF Tunnel ingress 的 panto115 规则中添加 `originRequest.noTLSVerify: true`

### 5. 115 API TLS 指纹检测 — httpx 被拦截

**问题**: 115.com 做 JA3 TLS 指纹检测，httpx 的指纹（`python-httpx/0.28.1`）被识别为非浏览器，返回 HTML 登录页而非 JSON

**症状**: `/api/status` 返回 `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`

**修复**:
- `requirements.txt`: 新增 `curl_cffi>=0.7`
- `saver_115.py`: 所有 115 API 调用从 `httpx.AsyncClient` 改为 `curl_cffi.requests.Session(impersonate="chrome")`
- 添加 `Referer: https://115.com/` 和 `Accept-Language` 请求头
- User-Agent 从 `115Browser/27.0.5.7` 改为标准 Chrome UA

---

## 当前状态

| 接口 | 状态 | 响应 |
|------|------|------|
| `GET /` | ✅ | 前端 HTML 正常渲染 |
| `GET /health` | ✅ | `{"status":"ok"}` |
| `GET /api/status` | ✅ | `logged_in: true, user_id: 103060531` |
| `GET /api/search?q=流浪地球` | ✅ | 4 条结果, 1250ms |

**登录状态**: ✅ 已登录 (Cookie 有效)

---

## 架构图

```
用户浏览器
  ↓ HTTPS
Cloudflare (DNS + CDN + SSL)
  ↓
CF Tunnel (cloudflared, homelab 网络)
  ↓ https://traefik:443
Traefik (homelab 网络, 动态路由 panto115.yml)
  ↓ http://panto115:8000
Panto115 容器 (homelab 网络, curl_cffi → 115.com API)
```

---

## Homelab 集成情况

- ✅ Docker 网络: `homelab` external
- ✅ Traefik 动态路由: `/home/hawk/homelab/traefik/dynamic/panto115.yml`
- ✅ CF Tunnel ingress: 14 条规则，panto115 已统一
- ✅ AGENTS.md: 服务清单和 Cloudflare 配置表已更新

---

## 已知限制 / 待改进

1. **space_used / space_total 为 null**: `/api/status` 返回的用户空间信息缺失，可能是 115 nav API 返回结构变化
2. **搜索依赖外部服务**: `/api/search` 通过 upyunso 等第三方搜索，非 115 官方 API
3. **Cookie 过期需手动更新**: 115 Cookie 有有效期，过期后需从浏览器重新获取并更新 `.env`
4. **无自动健康监控**: 未接入 Prometheus / Uptime Kuma 监控

---

## 文件变更清单

| 文件 | 变更 |
|------|------|
| `backend/app/main.py` | 修复 frontend 路径 + fallback |
| `backend/app/services/saver_115.py` | httpx → curl_cffi, UA/Referer 修复 |
| `backend/requirements.txt` | 新增 curl_cffi |
| `docker-compose.yml` | 移除 ports, 加入 homelab 网络 |
| `homelab/traefik/dynamic/panto115.yml` | 已有 (之前创建) |
| `homelab/AGENTS.md` | 新增 Panto115 条目 |
