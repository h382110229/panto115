"""
阿里云盘中转桥接器

流程:
  1. 用 refresh_token 获取 access_token
  2. 解析阿里分享链接 → 获取分享文件列表
  3. 转存分享内容到临时目录 /Panto115_Temp
  4. 获取文件直链
  5. 可选: 清理临时文件
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_ALI_AUTH_API = "https://auth.aliyundrive.com"
_ALI_API = "https://api.aliyundrive.com"
_ALI_OPEN_API = "https://open.aliyundrive.com"
_ALI_TIMEOUT = 15.0
_TEMP_DIR_NAME = "Panto115_Temp"


class AliyunBridge:
    """阿里云盘中转桥接器。"""

    def __init__(self, refresh_token: str):
        self._refresh_token = refresh_token
        self._access_token: Optional[str] = None
        self._headers: dict = {}
        self._temp_file_id: Optional[str] = None

    @staticmethod
    def is_configured() -> bool:
        return bool(settings.aliyun_refresh_token)

    @staticmethod
    def parse_share_code(url: str) -> Optional[str]:
        """从阿里分享链接提取 share_id。"""
        m = re.search(r"(?:aliyundrive\.com|alipan\.com)/s/([a-zA-Z0-9]+)", url)
        return m.group(1) if m else None

    async def _refresh_access_token(self) -> str:
        """用 refresh_token 换取 access_token。"""
        if self._access_token:
            return self._access_token

        async with httpx.AsyncClient(follow_redirects=True) as c:
            r = await c.post(
                f"{_ALI_AUTH_API}/v2/account/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
                timeout=_ALI_TIMEOUT,
            )
            resp = r.json()
            self._access_token = resp.get("access_token", "")
            if not self._access_token:
                raise ValueError(f"刷新阿里 token 失败: {resp.get('message', '未知')}")
            self._headers = {
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            }
            return self._access_token

    async def _api_post(self, path: str, data: dict = None, base: str = _ALI_API) -> dict:
        await self._refresh_access_token()
        async with httpx.AsyncClient(headers=self._headers, follow_redirects=True) as c:
            r = await c.post(f"{base}{path}", json=data or {}, timeout=_ALI_TIMEOUT)
            r.raise_for_status()
            return r.json()

    async def _get_or_create_temp_dir(self) -> str:
        """获取或创建临时目录，返回 file_id。"""
        if self._temp_file_id:
            return self._temp_file_id

        # 列出根目录
        resp = await self._api_post("/adrive/v1.0/openFile/list", {
            "drive_id": await self._get_drive_id(),
            "parent_file_id": "root",
            "limit": 100,
        })
        for item in resp.get("items", []):
            if item.get("name") == _TEMP_DIR_NAME and item.get("type") == "folder":
                self._temp_file_id = item["file_id"]
                return self._temp_file_id

        # 创建临时目录
        resp = await self._api_post("/adrive/v1.0/openFile/create", {
            "drive_id": await self._get_drive_id(),
            "parent_file_id": "root",
            "name": _TEMP_DIR_NAME,
            "type": "folder",
            "check_name_mode": "auto_rename",
        })
        self._temp_file_id = resp.get("file_id", "root")
        return self._temp_file_id

    async def _get_drive_id(self) -> str:
        """获取默认 drive_id。"""
        resp = await self._api_post("/adrive/v1.0/user/getDriveInfo", {})
        return resp.get("default_drive_id", "")

    async def transfer_share(self, share_url: str, extract_code: str = "") -> dict:
        """
        转存阿里分享到临时目录并获取直链。

        Returns:
            {"success": bool, "message": str, "download_urls": list[str], "file_count": int}
        """
        result = {"success": False, "message": "", "download_urls": [], "file_count": 0}

        try:
            share_id = self.parse_share_code(share_url)
            if not share_id:
                result["message"] = f"无法解析阿里分享码: {share_url}"
                return result

            await self._refresh_access_token()

            # 1. 获取分享 token
            async with httpx.AsyncClient(headers=self._headers, follow_redirects=True) as c:
                r = await c.post(
                    f"{_ALI_API}/v2/share_link/get_share_token",
                    json={"share_id": share_id, "share_pwd": extract_code or ""},
                    timeout=_ALI_TIMEOUT,
                )
                resp = r.json()
                share_token = resp.get("share_token", "")
                if not share_token:
                    result["message"] = f"获取分享 token 失败: {resp.get('message', '未知')}"
                    return result

            # 2. 获取分享文件列表
            share_headers = {**self._headers, "x-share-token": share_token}
            async with httpx.AsyncClient(headers=share_headers, follow_redirects=True) as c:
                r = await c.post(
                    f"{_ALI_API}/adrive/v2/share_link/list",
                    json={"share_id": share_id, "parent_file_id": "root",
                          "limit": 100, "order_by": "name", "order_direction": "ASC"},
                    timeout=_ALI_TIMEOUT,
                )
                resp = r.json()
                file_list = resp.get("items", [])

            if not file_list:
                result["message"] = "分享链接无文件"
                return result

            # 3. 转存到临时目录
            temp_fid = await self._get_or_create_temp_dir()
            drive_id = await self._get_drive_id()

            async with httpx.AsyncClient(headers=share_headers, follow_redirects=True) as c:
                r = await c.post(
                    f"{_ALI_API}/adrive/v2/share_link/save",
                    json={
                        "share_id": share_id,
                        "file_ids": [f["file_id"] for f in file_list],
                        "to_drive_id": drive_id,
                        "to_parent_file_id": temp_fid,
                        "auto_rename": True,
                    },
                    timeout=_ALI_TIMEOUT,
                )
                resp = r.json()
                saved_files = resp.get("saved_files", [])

            if not saved_files:
                result["message"] = "转存失败"
                return result

            # 4. 获取直链
            download_urls = []
            for f in saved_files:
                try:
                    async with httpx.AsyncClient(headers=self._headers, follow_redirects=True) as c:
                        r = await c.post(
                            f"{_ALI_API}/adrive/v1.0/openFile/getDownloadUrl",
                            json={"drive_id": drive_id, "file_id": f["file_id"]},
                            timeout=_ALI_TIMEOUT,
                        )
                        dl_resp = r.json()
                        url = dl_resp.get("url", "")
                        if url:
                            download_urls.append(url)
                except Exception as e:
                    logger.warning("获取阿里直链失败: %s", e)

            result["download_urls"] = download_urls
            result["file_count"] = len(file_list)
            result["success"] = bool(download_urls)
            result["message"] = (
                f"已转存 {len(file_list)} 个文件并获取 {len(download_urls)} 个直链"
                if download_urls else "转存成功但未获取到直链"
            )

            # 5. 可选: 清理临时文件
            if settings.auto_clean_temp and saved_files:
                try:
                    async with httpx.AsyncClient(headers=self._headers, follow_redirects=True) as c:
                        for f in saved_files:
                            await c.post(
                                f"{_ALI_API}/adrive/v1.0/openFile/delete",
                                json={"drive_id": drive_id, "file_id": f["file_id"]},
                                timeout=_ALI_TIMEOUT,
                            )
                    logger.info("已清理阿里临时文件 %d 个", len(saved_files))
                except Exception as e:
                    logger.warning("清理阿里临时文件失败: %s", e)

        except Exception as e:
            result["message"] = f"阿里桥接异常: {type(e).__name__}: {e}"

        return result
