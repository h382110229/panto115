"""
115 网盘转存与离线下载服务

基于 ChenyangGao/p115client SDK，自动处理:
  - m115 加密协议 (RSA + XOR)
  - 客户端 AppVersion 请求头
  - Cookie 认证与登录状态管理

支持:
  - Cookie 登录状态检查
  - 用户空间信息查询
  - 115 分享链接转存
  - 磁力/HTTP 离线下载
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from p115client import P115Client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Link parsing
# ---------------------------------------------------------------------------

_115_SHARE_RE = re.compile(r"115\.com/s/([a-zA-Z0-9]+)")
_MAGNET_RE = re.compile(r"^magnet:\?xt=urn:btih:", re.IGNORECASE)
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def parse_115_share_code(url: str) -> Optional[str]:
    """从 URL 中提取 115 分享码。支持完整链接或纯 code。"""
    if re.match(r"^[a-zA-Z0-9]{8,20}$", url.strip()):
        return url.strip()
    m = _115_SHARE_RE.search(url)
    return m.group(1) if m else None


def classify_url(url: str) -> str:
    """
    判断链接类型:
      '115_share' | 'magnet' | 'http' | 'quark' | 'aliyun' | 'baidu' | 'unknown'
    """
    if parse_115_share_code(url):
        return "115_share"
    if _MAGNET_RE.match(url):
        return "magnet"
    if "pan.quark.cn" in url:
        return "quark"
    if "aliyundrive.com" in url or "alipan.com" in url:
        return "aliyun"
    if "pan.baidu.com" in url:
        return "baidu"
    if _HTTP_URL_RE.match(url):
        return "http"
    return "unknown"


def _format_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读大小。"""
    if size_bytes <= 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


# ---------------------------------------------------------------------------
# 115 Saver Service (基于 p115client)
# ---------------------------------------------------------------------------

class Pan115Saver:
    """115 网盘转存与离线下载服务。"""

    def __init__(self, cookie: str = ""):
        self._cookie = cookie or os.environ.get("COOKIE_115", "")
        self._client: Optional[P115Client] = None

    def _ensure_client(self) -> P115Client:
        if not self._cookie:
            raise ValueError(
                "115 Cookie 未配置。请设置环境变量 COOKIE_115 或初始化时传入 cookie。"
            )
        if self._client is None:
            self._client = P115Client(cookie=self._cookie)
        return self._client

    # -----------------------------------------------------------------------
    # Login status & space info
    # -----------------------------------------------------------------------

    async def check_login_status(self) -> dict:
        """
        验证 Cookie 有效性，返回用户信息与空间使用情况。

        Returns:
            {
                "logged_in": bool,
                "user_id": int | None,
                "user_name": str | None,
                "space_used": str | None,
                "space_total": str | None,
                "error": str | None,
            }
        """
        result: dict = {
            "logged_in": False, "user_id": None, "user_name": None,
            "space_used": None, "space_total": None, "error": None,
        }

        try:
            client = self._ensure_client()

            # 检查登录状态
            logged_in = await client.login_status(async_=True)
            if not logged_in:
                result["error"] = "Cookie 无效或已过期"
                return result

            result["logged_in"] = True

            # 获取用户信息
            try:
                user_info = await client.user_info(async_=True)
                result["user_id"] = user_info.get("user_id")
                result["user_name"] = user_info.get("user_name")
            except Exception as e:
                logger.warning("获取用户信息失败: %s", e)

            # 获取空间信息
            try:
                space = await client.fs_space_summury(async_=True)
                # p115client 返回格式: {"total": int, "used": int, ...}
                total = space.get("total", 0)
                used = space.get("used", 0) or space.get("use", 0)
                if total:
                    result["space_total"] = _format_size(total)
                if used:
                    result["space_used"] = _format_size(used)
                # Fallback: 尝试 user_space_info
                if not result["space_total"]:
                    space2 = await client.user_space_info(async_=True)
                    total2 = space2.get("total", 0) or space2.get("all_total", 0)
                    used2 = space2.get("used", 0) or space2.get("all_use", 0) or space2.get("use", 0)
                    if total2:
                        result["space_total"] = _format_size(total2)
                    if used2:
                        result["space_used"] = _format_size(used2)
            except Exception as e:
                logger.warning("获取空间信息失败: %s", e)

        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"

        return result

    # -----------------------------------------------------------------------
    # Share link operations
    # -----------------------------------------------------------------------

    async def get_share_snap(self, share_code: str, receive_code: str = "") -> dict:
        """获取 115 分享链接的文件列表快照。"""
        client = self._ensure_client()
        return await client.share_snap(
            {"share_code": share_code, "receive_code": receive_code},
            async_=True,
        )

    async def save_share_link(
        self, share_url: str, receive_code: str = "", target_cid: str = "0"
    ) -> dict:
        """
        转存 115 分享链接到自己的网盘。
        """
        client = self._ensure_client()
        share_code = parse_115_share_code(share_url)
        if not share_code:
            return {
                "success": False, "message": f"无法解析 115 分享码: {share_url}",
                "file_count": 0, "share_code": None, "snap": None,
            }

        result: dict = {
            "success": False, "message": "", "file_count": 0,
            "share_code": share_code, "snap": None,
        }

        try:
            # 获取分享快照
            snap = await self.get_share_snap(share_code, receive_code)
            result["snap"] = snap
            if not snap.get("state"):
                result["message"] = f"获取分享快照失败: {snap.get('error', '未知')}"
                return result

            file_list = snap.get("data", {}).get("list", [])
            result["file_count"] = len(file_list)
            if not file_list:
                result["message"] = "分享链接无文件"
                return result

            # 转存
            resp = await client.share_receive(
                {
                    "share_code": share_code,
                    "receive_code": receive_code,
                    "cid": target_cid,
                },
                async_=True,
            )
            if resp.get("state") or resp.get("errno") == 0:
                result["success"] = True
                result["message"] = f"成功转存 {result['file_count']} 个文件"
            else:
                result["message"] = (
                    f"转存失败: {resp.get('error', resp.get('msg', '未知'))}"
                )

        except Exception as e:
            result["message"] = f"转存异常: {type(e).__name__}: {e}"

        return result

    # -----------------------------------------------------------------------
    # Offline download
    # -----------------------------------------------------------------------

    async def add_offline_task(self, url: str, save_dir_id: str = "0") -> dict:
        """
        添加离线下载任务 (磁力链接或 HTTP 直链)。

        使用 p115client SDK 的 clouddownload_task_add_urls 接口，
        自动处理 m115 加密协议和客户端 AppVersion 请求头。
        """
        client = self._ensure_client()
        result: dict = {
            "success": False, "message": "", "task_count": 0, "info_hashes": [],
        }

        try:
            resp = await client.clouddownload_task_add_urls(
                {"url[0]": url, "wp_path_id": save_dir_id},
                async_=True,
            )

            if resp.get("state") or resp.get("errno") == 0:
                result["success"] = True
                # 解析任务信息
                task_list = resp.get("result", resp.get("task_list", []))
                if isinstance(task_list, list):
                    result["task_count"] = len(task_list)
                    result["info_hashes"] = [
                        t.get("info_hash", "") for t in task_list if t.get("info_hash")
                    ]
                else:
                    result["task_count"] = 1
                result["message"] = f"已添加 {result['task_count']} 个离线任务"
            else:
                error_msg = resp.get("error", resp.get("msg", "未知错误"))
                result["message"] = f"离线任务添加失败: {error_msg}"

        except Exception as e:
            result["message"] = f"离线下载异常: {type(e).__name__}: {e}"

        return result

    # -----------------------------------------------------------------------
    # Auto save (unified entry)
    # -----------------------------------------------------------------------

    async def auto_save(self, url: str, extract_code: str = "") -> dict:
        """
        自动判断链接类型并执行相应操作。

        - 115 分享 → save_share_link
        - magnet / http → add_offline_task
        - 其他网盘 → 友好提示
        """
        link_type = classify_url(url)
        logger.info("auto_save: type=%s, url=%s", link_type, url[:60])

        if link_type == "115_share":
            return await self.save_share_link(url, extract_code)

        if link_type in ("magnet", "http"):
            return await self.add_offline_task(url)

        names = {"quark": "夸克网盘", "aliyun": "阿里云盘", "baidu": "百度网盘"}
        name = names.get(link_type, "该类型")
        return {
            "success": False,
            "message": f"暂不支持直接跨盘转存{name}链接，建议通过离线或手动转存。",
            "link_type": link_type,
            "suggestion": "可尝试将资源离线下载后手动上传到 115。",
        }


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio

    async def _test():
        saver = Pan115Saver()

        print("=" * 60)
        print("115 Saver 模块测试 (p115client)")
        print("=" * 60)

        # 1. 链接分类
        print("\n[1] 链接类型识别:")
        for u in [
            "https://115.com/s/abc123def456",
            "abc123def456",
            "magnet:?xt=urn:btih:abcdef1234567890",
            "https://pan.quark.cn/s/abcdef",
            "https://www.aliyundrive.com/s/abc",
            "https://pan.baidu.com/s/1abc",
            "https://example.com/file.zip",
        ]:
            print(f"  {classify_url(u):^12} <- {u[:50]}")

        # 2. 分享码解析
        print("\n[2] 115 分享码解析:")
        for u in [
            "https://115.com/s/abc123def456",
            "abc123def456",
            "https://pan.quark.cn/s/abcdef",
        ]:
            print(f"  {str(parse_115_share_code(u)):>20} <- {u[:50]}")

        # 3. 登录检查 + 空间信息
        print("\n[3] 登录状态 + 空间信息:")
        cookie = os.environ.get("COOKIE_115", "")
        if cookie:
            try:
                info = await saver.check_login_status()
                print(f"  登录: {info['logged_in']}")
                print(f"  用户: {info.get('user_name', 'N/A')}")
                print(f"  空间: {info.get('space_used', '?')} / {info.get('space_total', '?')}")
                if info.get("error"):
                    print(f"  错误: {info['error']}")
            except Exception as e:
                print(f"  异常: {e}")
        else:
            print("  [SKIP] 未设置 COOKIE_115")

        # 4. auto_save 路由
        print("\n[4] auto_save 路由:")
        for u in [
            "https://115.com/s/abc123def456",
            "magnet:?xt=urn:btih:abcdef",
            "https://pan.quark.cn/s/abcdef",
        ]:
            try:
                r = await saver.auto_save(u)
                status = "OK" if r.get("success") else "SKIP"
                print(f"  [{classify_url(u):^10}] {status} - {r['message'][:50]}")
            except ValueError as e:
                print(f"  [{classify_url(u):^10}] SKIP - {e}")
            except Exception as e:
                print(f"  [{classify_url(u):^10}] ERR - {type(e).__name__}: {e}")

        print("\n" + "=" * 60)
        print("测试完成")

    asyncio.run(_test())
