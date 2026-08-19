"""
百度网盘跨盘桥接器

功能:
  1. 解析百度网盘分享链接（提取 shareid、uk、提取码）
  2. 验证提取码并获取 randsk
  3. 获取分享文件列表
  4. 转存文件到用户网盘

依赖:
  - BDUSS Cookie（百度网盘登录凭证）

API 端点:
  - GET  /api/gettemplatevariable  → 获取 bdstoken
  - POST /share/verify             → 验证提取码
  - GET  /share/verify             → 获取分享信息
  - POST /share/transfer           → 转存文件

参考: https://github.com/hxz393/BaiduPanFilesTransfers
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse, parse_qs

import httpx
from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://pan.baidu.com"
_TIMEOUT = 15

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://pan.baidu.com/",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_share_info(url: str) -> dict:
    """
    从百度网盘分享链接中提取 shareid、uk、提取码。

    支持格式:
      - https://pan.baidu.com/s/1abcdefghijkl  (无提取码)
      - https://pan.baidu.com/s/1abcdefghijkl?pwd=xxxx  (有提取码)
      - https://pan.baidu.com/share/init?surl=abcdefghijkl  (旧格式)
    """
    result = {"shareid": "", "uk": "", "surl": "", "pass_code": ""}

    # 提取 surl (分享链接的关键部分)
    # 格式 1: /s/1xxxxxx
    match = re.search(r'/s/([a-zA-Z0-9_-]+)', url)
    if match:
        surl = match.group(1)
        # 去掉开头的 1 (如果有)
        if surl.startswith('1'):
            surl = surl[1:]
        result["surl"] = surl

    # 格式 2: surl=xxxx
    if not result["surl"]:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        if "surl" in params:
            result["surl"] = params["surl"][0].lstrip("1")

    # 提取提取码 (pwd 参数)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "pwd" in params:
        result["pass_code"] = params["pwd"][0]

    return result


def _parse_bduss_to_cookie(bduss: str, stoken: str = "") -> str:
    """将 BDUSS + STOKEN 值转换为完整的 Cookie 字符串。"""
    cookie = f"BDUSS={bduss}"
    if stoken:
        cookie += f"; STOKEN={stoken}"
    return cookie


# ---------------------------------------------------------------------------
# Bridge Class
# ---------------------------------------------------------------------------

class BaiduBridge:
    """
    百度网盘跨盘桥接器。

    使用 BDUSS Cookie 认证，支持:
      - 解析分享链接
      - 验证提取码
      - 转存文件到用户网盘
    """

    def __init__(self, bduss: str, stoken: str = ""):
        """
        初始化百度网盘桥接器。

        Args:
            bduss: 百度网盘 BDUSS Cookie 值
            stoken: 百度网盘 STOKEN Cookie 值（可选，用于 Web API 操作）
        """
        if not bduss:
            raise ValueError("BDUSS 不能为空")

        self._bduss = bduss
        self._stoken = stoken
        self._cookie = _parse_bduss_to_cookie(bduss, stoken)
        self._bdstoken = ""
        self._client = httpx.Client(
            headers={**_HEADERS, "Cookie": self._cookie},
            timeout=_TIMEOUT,
            follow_redirects=True,
        )

    def _get_bdstoken(self) -> str:
        """获取 bdstoken（所有操作的前提）。"""
        if self._bdstoken:
            return self._bdstoken

        resp = self._client.get(
            f"{_BASE_URL}/api/gettemplatevariable",
            params={
                "clienttype": 0,
                "app_id": 38824127,
                "web": 1,
                "fields": '["bdstoken","token","uk","isdocuser","servertime"]',
            },
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("errno", -1) != 0:
            raise RuntimeError(f"获取 bdstoken 失败: {data}")

        self._bdstoken = data.get("data", data.get("result", {})).get("bdstoken", "")
        if not self._bdstoken:
            raise RuntimeError("bdstoken 为空，请检查 BDUSS 是否有效")

        return self._bdstoken

    def verify_pass_code(self, surl: str, pass_code: str) -> str:
        """
        验证提取码是否正确，返回 randsk。

        Args:
            surl: 分享链接的 surl 部分
            pass_code: 提取码

        Returns:
            randsk 值（用于后续请求的 bdclnd cookie）
        """
        resp = self._client.post(
            f"{_BASE_URL}/share/verify",
            params={
                "surl": surl,
                "t": str(int(__import__("time").time() * 1000)),
                "channel": "chunlei",
                "web": 1,
                "clienttype": 0,
            },
            data={
                "pwd": pass_code,
                "vcode": "",
                "vcode_str": "",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        errno = data.get("errno", -1)
        if errno != 0:
            err_map = {
                -1: "链接不存在或已过期",
                -9: "提取码错误",
                105: "分享链接已过期或被取消",
                106: "分享链接已被和谐",
            }
            error_msg = err_map.get(errno, data.get("show_msg", data.get("errmsg", f"errno={errno}")))
            raise RuntimeError(f"提取码验证失败: {error_msg}")

        randsk = data.get("randsk", "")
        if not randsk:
            raise RuntimeError("randsk 为空，验证可能失败")

        return randsk

    def get_share_info(self, url: str) -> dict:
        """
        获取分享链接的文件列表信息。

        Args:
            url: 百度网盘分享链接

        Returns:
            包含 shareid、uk、file_list 的字典
        """
        share_info = _extract_share_info(url)
        surl = share_info["surl"]

        if not surl:
            raise ValueError(f"无法解析分享链接: {url}")

        # 构建请求 Cookie
        cookie = self._cookie
        if share_info["pass_code"]:
            randsk = self.verify_pass_code(surl, share_info["pass_code"])
            cookie += f"; bdclnd={randsk}"

        # 获取 shareid 和 uk
        # 方法1: 从 /share/init 页面提取 (用 curl_cffi 避免 gzip 问题)
        shareid = ""
        uk = ""
        try:
            resp = curl_requests.get(
                f"{_BASE_URL}/share/init",
                params={"surl": surl},
                headers={**_HEADERS, "Cookie": cookie},
                impersonate="chrome",
                allow_redirects=True,
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                shareid_match = re.search(r'"shareid"\s*:\s*(\d+)', resp.text)
                uk_match = re.search(r'"uk"\s*:\s*(\d+)', resp.text)
                if shareid_match:
                    shareid = shareid_match.group(1)
                if uk_match:
                    uk = uk_match.group(1)
        except Exception as e:
            logger.warning("share/init 页面访问失败: %s", e)

        # 方法2: 从 verify 响应 cookie 或 REST API 获取
        if not shareid or not uk:
            try:
                resp = self._client.get(
                    f"{_BASE_URL}/share/getshareinfo",
                    params={"surl": surl, "app_id": 250528, "clienttype": 0},
                )
                data = resp.json()
                if data.get("errno", -1) == 0:
                    shareid = str(data.get("shareid", ""))
                    uk = str(data.get("uk", ""))
            except Exception as e:
                logger.warning("getshareinfo 失败: %s", e)

        if not shareid or not uk:
            raise RuntimeError(
                f"无法获取分享信息 (shareid/uk)。链接可能已过期或无效: surl={surl}"
            )

        return {
            "shareid": shareid,
            "uk": uk,
            "surl": surl,
            "pass_code": share_info["pass_code"],
            "cookie": cookie,
        }

    def transfer_share(self, url: str, target_path: str = "/") -> dict:
        """
        转存分享链接中的文件到用户网盘。

        Args:
            url: 百度网盘分享链接
            target_path: 目标路径（默认根目录）

        Returns:
            转存结果字典
        """
        result = {
            "success": False,
            "message": "",
            "file_count": 0,
        }

        try:
            # 1. 获取 bdstoken
            bdstoken = self._get_bdstoken()

            # 2. 获取分享信息
            info = self.get_share_info(url)
            shareid = info["shareid"]
            uk = info["uk"]
            cookie = info["cookie"]

            # 3. 获取文件列表
            resp = self._client.get(
                f"{_BASE_URL}/share/list",
                params={
                    "uk": uk,
                    "shareid": shareid,
                    "order": "other",
                    "desc": 1,
                    "showempty": 0,
                    "web": 1,
                    "page": 1,
                    "num": 1000,
                    "dir": "/",
                },
                headers={**_HEADERS, "Cookie": cookie},
            )
            resp.raise_for_status()
            list_data = resp.json()

            if list_data.get("errno", -1) != 0:
                error_msg = list_data.get("show_msg", list_data.get("errmsg", "未知错误"))
                result["message"] = f"获取文件列表失败: {error_msg}"
                return result

            file_list = list_data.get("list", [])
            if not file_list:
                result["message"] = "分享链接中没有文件"
                return result

            # 4. 转存文件
            fsid_list = [str(f["fs_id"]) for f in file_list]
            fsid_str = ",".join(fsid_list)

            resp = self._client.post(
                f"{_BASE_URL}/share/transfer",
                params={
                    "shareid": shareid,
                    "from": uk,
                    "channel": "chunlei",
                    "web": 1,
                    "clienttype": 0,
                    "bdstoken": bdstoken,
                },
                data={
                    "fsidlist": f"[{fsid_str}]",
                    "path": target_path,
                },
                headers={**_HEADERS, "Cookie": cookie},
            )
            resp.raise_for_status()
            transfer_data = resp.json()

            if transfer_data.get("errno", -1) == 0:
                result["success"] = True
                result["file_count"] = len(file_list)
                result["message"] = f"成功转存 {len(file_list)} 个文件"
            else:
                error_msg = transfer_data.get("show_msg", transfer_data.get("errmsg", "未知错误"))
                result["message"] = f"转存失败: {error_msg}"

        except Exception as e:
            result["message"] = f"转存异常: {type(e).__name__}: {e}"

        return result

    def close(self):
        """关闭 HTTP 客户端。"""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# Convenience Function
# ---------------------------------------------------------------------------

def transfer_baidu_share(bduss: str, url: str, target_path: str = "/", stoken: str = "") -> dict:
    """
    便捷函数: 转存百度网盘分享链接。

    Args:
        bduss: 百度网盘 BDUSS Cookie
        url: 分享链接
        target_path: 目标路径
        stoken: 百度网盘 STOKEN Cookie（可选）

    Returns:
        转存结果字典
    """
    with BaiduBridge(bduss, stoken) as bridge:
        return bridge.transfer_share(url, target_path)
