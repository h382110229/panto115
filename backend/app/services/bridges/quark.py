"""
夸克网盘中转桥接器

流程:
  1. 解析夸克分享链接 → 获取分享文件列表
  2. 转存分享内容到临时目录 /Panto115_Temp
  3. 获取文件直链 (download_url)
  4. 可选: 清理临时文件
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_QUARK_API = "https://drive-pc.quark.cn/1/clouddrive"
_QUARK_SHARE_API = "https://drive-pc.quark.cn/1/clouddrive/share"
_QUARK_TIMEOUT = 15.0
_TEMP_DIR_NAME = "Panto115_Temp"


class QuarkBridge:
    """夸克网盘中转桥接器。"""

    def __init__(self, cookie: str):
        self._cookie = cookie
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": cookie,
            "Referer": "https://pan.quark.cn/",
        }
        self._temp_fid: Optional[str] = None  # 临时目录 fid

    @staticmethod
    def is_configured() -> bool:
        return bool(settings.quark_cookie)

    @staticmethod
    def parse_share_code(url: str) -> Optional[str]:
        """从夸克分享链接提取 share code。"""
        import re
        m = re.search(r"pan\.quark\.cn/s/([a-zA-Z0-9]+)", url)
        return m.group(1) if m else None

    async def _api_get(self, path: str, params: dict = None) -> dict:
        async with httpx.AsyncClient(headers=self._headers, follow_redirects=True) as c:
            r = await c.get(f"{_QUARK_API}{path}", params=params, timeout=_QUARK_TIMEOUT)
            r.raise_for_status()
            return r.json()

    async def _api_post(self, path: str, data: dict = None) -> dict:
        async with httpx.AsyncClient(headers=self._headers, follow_redirects=True) as c:
            r = await c.post(f"{_QUARK_API}{path}", json=data or {}, timeout=_QUARK_TIMEOUT)
            r.raise_for_status()
            return r.json()

    async def _get_or_create_temp_dir(self) -> str:
        """获取或创建临时目录，返回 fid。"""
        if self._temp_fid:
            return self._temp_fid

        # 列出根目录
        resp = await self._api_get("/file/sort", params={
            "pdir_fid": "0", "_page": "1", "_size": "100", "_fetch_total": "1",
            "_sort": "file_type:asc,updated_at:desc",
        })
        data = resp.get("data", {})
        for item in data.get("list", []):
            if item.get("file_name") == _TEMP_DIR_NAME and item.get("dir"):
                self._temp_fid = item["fid"]
                return self._temp_fid

        # 创建临时目录
        resp = await self._api_post("/file", {
            "pdir_fid": "0",
            "file_name": _TEMP_DIR_NAME,
            "dir_path": "",
            "dir_init_lock": False,
        })
        self._temp_fid = resp.get("data", {}).get("fid", "0")
        return self._temp_fid

    async def transfer_share(self, share_url: str, extract_code: str = "") -> dict:
        """
        转存夸克分享到临时目录并获取直链。

        Returns:
            {"success": bool, "message": str, "download_urls": list[str], "file_count": int}
        """
        result = {"success": False, "message": "", "download_urls": [], "file_count": 0}

        try:
            share_code = self.parse_share_code(share_url)
            if not share_code:
                result["message"] = f"无法解析夸克分享码: {share_url}"
                return result

            # 1. 获取分享详情
            async with httpx.AsyncClient(headers=self._headers, follow_redirects=True) as c:
                r = await c.get(
                    f"{_QUARK_SHARE_API}/share/sharepage/token",
                    params={"pwd_id": share_code, "passcode": extract_code or ""},
                    timeout=_QUARK_TIMEOUT,
                )
                resp = r.json()
                if resp.get("code") != 0 and resp.get("status") != 200:
                    result["message"] = f"获取分享信息失败: {resp.get('message', '未知')}"
                    return result

                stoken = resp.get("data", {}).get("stoken", "")
                share_token = resp.get("data", {}).get("share_token", stoken)

            # 2. 获取分享文件列表
            async with httpx.AsyncClient(headers=self._headers, follow_redirects=True) as c:
                r = await c.get(
                    f"{_QUARK_SHARE_API}/share/sharepage/detail",
                    params={"pwd_id": share_code, "stoken": share_token, "pdir_fid": "0",
                            "_page": "1", "_size": "50", "_fetch_banner": "0",
                            "_fetch_share": "1", "_fetch_total": "1", "_sort": ""},
                    timeout=_QUARK_TIMEOUT,
                )
                resp = r.json()
                file_list = resp.get("data", {}).list if hasattr(resp.get("data", {}), "list") else resp.get("data", {}).get("list", [])

            if not file_list:
                result["message"] = "分享链接无文件"
                return result

            # 3. 获取临时目录
            temp_fid = await self._get_or_create_temp_dir()

            # 4. 转存到临时目录
            file_fids = [f["fid"] for f in file_list]
            async with httpx.AsyncClient(headers=self._headers, follow_redirects=True) as c:
                r = await c.post(
                    f"{_QUARK_SHARE_API}/share/sharepage/save",
                    json={
                        "fid_list": file_fids,
                        "fid_token_list": [f.get("share_fid_token", "") for f in file_list],
                        "to_pdir_fid": temp_fid,
                        "pwd_id": share_code,
                        "stoken": share_token,
                        "pdir_fid": "0",
                        "scene": "link",
                    },
                    timeout=_QUARK_TIMEOUT,
                )
                resp = r.json()
                if resp.get("code") != 0 and resp.get("status") != 200:
                    result["message"] = f"转存失败: {resp.get('message', '未知')}"
                    return result

            # 5. 获取直链
            saved_fids = resp.get("data", {}).get("save_as", {}).get("save_as_top_fids", file_fids)
            download_urls = []
            for fid in saved_fids:
                try:
                    async with httpx.AsyncClient(headers=self._headers, follow_redirects=True) as c:
                        r = await c.post(
                            f"{_QUARK_API}/file/download",
                            json={"fids": [fid]},
                            timeout=_QUARK_TIMEOUT,
                        )
                        dl_resp = r.json()
                        for item in dl_resp.get("data", []):
                            url = item.get("download_url", "")
                            if url:
                                download_urls.append(url)
                except Exception as e:
                    logger.warning("获取夸克直链失败 fid=%s: %s", fid, e)

            result["download_urls"] = download_urls
            result["file_count"] = len(file_list)
            result["success"] = bool(download_urls)
            result["message"] = (
                f"已转存 {len(file_list)} 个文件并获取 {len(download_urls)} 个直链"
                if download_urls else "转存成功但未获取到直链"
            )

            # 6. 可选: 清理临时文件
            if settings.auto_clean_temp and saved_fids:
                try:
                    async with httpx.AsyncClient(headers=self._headers, follow_redirects=True) as c:
                        await c.post(
                            f"{_QUARK_API}/file/delete",
                            json={"action_type": 2, "filelist": saved_fids, "exclude_fids": []},
                            timeout=_QUARK_TIMEOUT,
                        )
                    logger.info("已清理夸克临时文件 %d 个", len(saved_fids))
                except Exception as e:
                    logger.warning("清理夸克临时文件失败: %s", e)

        except Exception as e:
            result["message"] = f"夸克桥接异常: {type(e).__name__}: {e}"

        return result
