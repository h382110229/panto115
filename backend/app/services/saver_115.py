"""
115 网盘转存与离线下载服务

参考:
  - SheltonZhu/115driver (Go) — API endpoints
  - ChenyangGao/p115client/p115rsacipher (Python) — m115 加密协议

支持:
  - Cookie 登录状态检查
  - 115 分享链接转存
  - 磁力/HTTP 离线下载

注意: m115 加密协议使用 RSA 非对称加解密:
  - encode: 客户端用公钥加密 → 服务端用私钥解密
  - decode: 服务端用私钥加密 → 客户端用公钥解密
  因此 encrypt(decrypt(x)) != x，这是协议设计如此。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import time
from typing import Optional

import httpx
from curl_cffi.requests import Session as CurlSession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 115 API Endpoints
# ---------------------------------------------------------------------------

_API_STATUS_CHECK = "https://my.115.com/?ct=guide&ac=status"
_API_USER_NAV = "https://my.115.com/?ct=ajax&ac=nav"
_API_SHARE_SNAP = "https://115cdn.com/webapi/share/snap"
_API_SHARE_SAVE = "https://115cdn.com/webapi/share/save"
_API_OFFLINE_ADD = "https://lixian.115.com/lixianssp/?ac=add_task_urls"
_API_OFFLINE_LIST = "https://lixian.115.com/lixian/?ct=lixian&ac=task_lists"

_UA_115_BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
_UA_DEFAULT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
_TIMEOUT = 15.0

# ---------------------------------------------------------------------------
# m115 加密协议 (来自 p115rsacipher)
# ---------------------------------------------------------------------------

_G_KTS = bytes([
    0xf0, 0xe5, 0x69, 0xae, 0xbf, 0xdc, 0xbf, 0x8a,
    0x1a, 0x45, 0xe8, 0xbe, 0x7d, 0xa6, 0x73, 0xb8,
    0xde, 0x8f, 0xe7, 0xc4, 0x45, 0xda, 0x86, 0xc4,
    0x9b, 0x64, 0x8b, 0x14, 0x6a, 0xb4, 0xf1, 0xaa,
    0x38, 0x01, 0x35, 0x9e, 0x26, 0x69, 0x2c, 0x86,
    0x00, 0x6b, 0x4f, 0xa5, 0x36, 0x34, 0x62, 0xa6,
    0x2a, 0x96, 0x68, 0x18, 0xf2, 0x4a, 0xfd, 0xbd,
    0x6b, 0x97, 0x8f, 0x4d, 0x8f, 0x89, 0x13, 0xb7,
    0x6c, 0x8e, 0x93, 0xed, 0x0e, 0x0d, 0x48, 0x3e,
    0xd7, 0x2f, 0x88, 0xd8, 0xfe, 0xfe, 0x7e, 0x86,
    0x50, 0x95, 0x4f, 0xd1, 0xeb, 0x83, 0x26, 0x34,
    0xdb, 0x66, 0x7b, 0x9c, 0x7e, 0x9d, 0x7a, 0x81,
    0x32, 0xea, 0xb6, 0x33, 0xde, 0x3a, 0xa9, 0x59,
    0x34, 0x66, 0x3b, 0xaa, 0xba, 0x81, 0x60, 0x48,
    0xb9, 0xd5, 0x81, 0x9c, 0xf8, 0x6c, 0x84, 0x77,
    0xff, 0x54, 0x78, 0x26, 0x5f, 0xbe, 0xe8, 0x1e,
    0x36, 0x9f, 0x34, 0x80, 0x5c, 0x45, 0x2c, 0x9b,
    0x76, 0xd5, 0x1b, 0x8f, 0xcc, 0xc3, 0xb8, 0xf5,
])

# p115rsacipher 中变量命名: RSA_e = modulus, RSA_n = exponent
_RSA_MODULUS = int(
    "8686980c0f5a24c4b9d43020cd2c22703ff3f450756529058b1cf88f09b86021"
    "36477198a6e2683149659bd122c33592fdb5ad47944ad1ea4d36c6b172aad633"
    "8c3bb6ac6227502d010993ac967d1aef00f0c8e038de2e4d3bc2ec368af2e9f1"
    "0a6f1eda4f7262f136420c07c331b871bf139f74f3010e3c4fe57df3afb71683",
    16,
)
_RSA_EXPONENT = 0x10001
_RSA_KEY_LEN = _RSA_MODULUS.bit_length() // 8  # 128

_XOR_FIXED = b"\x8d\xa5\xa5\x8d"
_XOR_CLIENT = b"\x78\x06\xad\x4c\x33\x86\x5d\x18\x4c\x01\x3f\x46"


def _gen_key(rand_key: bytes, sk_len: int) -> bytearray:
    """从随机密钥派生 XOR 密钥。"""
    xor_key = bytearray(sk_len)
    length = sk_len * (sk_len - 1)
    index = 0
    for i in range(sk_len):
        x = (rand_key[i] + _G_KTS[index]) & 0xFF
        xor_key[i] = _G_KTS[length] ^ x
        length -= sk_len
        index += sk_len
    return xor_key


def _acc_step(start: int, stop: int, step: int = 1):
    """生成分片迭代器。"""
    for i in range(start + step, stop, step):
        yield start, i, step
        start = i
    if start != stop:
        yield start, stop, stop - start


def _bytes_xor(v1: bytes, v2: bytes) -> bytes:
    return int.to_bytes(
        int.from_bytes(v1, "big") ^ int.from_bytes(v2, "big"),
        len(v1), "big",
    )


def _xor(src: bytes, key: bytes) -> bytearray:
    """按 key 长度循环 XOR。"""
    secret = bytearray()
    mod = len(src) & 0b11
    if mod:
        secret += _bytes_xor(src[:mod], key[:mod])
    for i, j, s in _acc_step(mod, len(src), len(key)):
        secret += _bytes_xor(src[i:j], key[:s])
    return secret


def m115_encode(payload: bytes) -> str:
    """
    115 加密 (客户端 → 服务端):
      1. XOR(payload, 固定密钥) → reverse → XOR(结果, client_key)
      2. 前补 16 字节零 → PKCS#1 v1.5 填充 → RSA 公钥加密 → base64
    """
    xor_text = bytearray(16)  # 16 zero bytes prefix
    tmp = memoryview(_xor(payload, _XOR_FIXED))[::-1]
    xor_text += _xor(bytes(tmp), _XOR_CLIENT)
    cipher_data = bytearray()
    view = memoryview(xor_text)
    for l, r, _ in _acc_step(0, len(view), _RSA_KEY_LEN - 11):
        chunk = view[l:r]
        # PKCS#1 v1.5 type-2 padding
        pad_len = _RSA_KEY_LEN - len(chunk) - 3
        pad = bytes(random.randint(1, 255) for _ in range(pad_len))
        padded = int.from_bytes(
            b"\x00\x02" + pad + b"\x00" + bytes(chunk), "big"
        )
        ct = pow(padded, _RSA_EXPONENT, _RSA_MODULUS)
        cipher_data += int.to_bytes(ct, _RSA_KEY_LEN, "big")
    return base64.b64encode(cipher_data).decode()


def m115_decode(cipher_b64: str) -> bytearray:
    """
    115 解密 (服务端 → 客户端):
      1. base64 → RSA 公钥解密 → 提取内容 (跳过前 16 字节 key)
      2. XOR(data[16:], gen_key(key, 12)) → reverse → XOR(结果, 固定密钥)

    注意: 只能解密服务端用私钥加密的数据。
    """
    cipher_data = memoryview(base64.b64decode(cipher_b64))
    data = bytearray()
    for l, r, _ in _acc_step(0, len(cipher_data), _RSA_KEY_LEN):
        chunk = bytes(cipher_data[l:r])
        p = pow(int.from_bytes(chunk, "big"), _RSA_EXPONENT, _RSA_MODULUS)
        raw_len = (p.bit_length() + 7) // 8 or 1
        b = int.to_bytes(p, raw_len, "big")
        # 跳过 PKCS#1 padding，找到 0x00 分隔符
        sep = b.index(0) if 0 in b else -1
        if sep >= 0:
            data += b[sep + 1:]
        else:
            data += b
    m = memoryview(data)
    key_l = _gen_key(m[:16], 12)
    tmp = memoryview(_xor(m[16:], key_l))[::-1]
    return _xor(bytes(tmp), _XOR_FIXED)


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


# ---------------------------------------------------------------------------
# 115 Saver Service
# ---------------------------------------------------------------------------

class Pan115Saver:
    """115 网盘转存与离线下载服务。"""

    def __init__(self, cookie: str = ""):
        self._cookie = cookie or os.environ.get("COOKIE_115", "")
        self._user_id: int = 0
        self._headers = {
            "User-Agent": _UA_115_BROWSER,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8",
            "Referer": "https://115.com/",
            "Cookie": self._cookie,
        }

    def _ensure_cookie(self) -> None:
        if not self._cookie:
            raise ValueError(
                "115 Cookie 未配置。请设置环境变量 COOKIE_115 或初始化时传入 cookie。"
            )

    def _request_115(self, method: str, url: str, **kwargs) -> "httpx.Response":
        """使用 curl_cffi 模拟 Chrome TLS 指纹请求 115 API。
        返回类 httpx.Response 对象（curl_cffi 兼容）。"""
        kwargs.setdefault("headers", self._headers)
        kwargs.setdefault("timeout", _TIMEOUT)
        kwargs.setdefault("allow_redirects", True)
        with CurlSession(impersonate="chrome") as s:
            resp = s.request(method, url, **kwargs)
        return resp

    def _build_share_referer(self, share_code: str, receive_code: str = "") -> str:
        return f"https://115cdn.com/s/{share_code}?password={receive_code}&"

    # -----------------------------------------------------------------------
    # Login status
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
        self._ensure_cookie()
        result: dict = {
            "logged_in": False, "user_id": None, "user_name": None,
            "space_used": None, "space_total": None, "error": None,
        }

        try:
            import asyncio
            resp = await asyncio.to_thread(
                self._request_115, "GET", _API_STATUS_CHECK,
                params={"_": str(int(time.time() * 1000))},
            )
            status = resp.json()
            if not status.get("state"):
                result["error"] = "Cookie 无效或已过期"
                return result

            resp = await asyncio.to_thread(
                self._request_115, "GET", _API_USER_NAV,
            )
            nav = resp.json()
            if not nav.get("state"):
                result["error"] = nav.get("error", "获取用户信息失败")
                return result

            data = nav.get("data", {})
            result["logged_in"] = True
            result["user_id"] = data.get("user_id")
            result["user_name"] = data.get("user_name")
            self._user_id = result["user_id"] or 0

            space = data.get("space_info", {})
            if space:
                # 适配不同 JSON 结构: {all_use: {size_format}} 或 {all_use_size, all_total_size}
                use_info = space.get("all_use", {})
                total_info = space.get("all_total", {})
                result["space_used"] = (
                    use_info.get("size_format")
                    if isinstance(use_info, dict)
                    else str(use_info) if use_info else None
                )
                result["space_total"] = (
                    total_info.get("size_format")
                    if isinstance(total_info, dict)
                    else str(total_info) if total_info else None
                )
                # Fallback: 直接从顶层字段读取
                if not result["space_used"]:
                    result["space_used"] = data.get("space_use_size") or data.get("all_use_size")
                if not result["space_total"]:
                    result["space_total"] = data.get("space_total_size") or data.get("all_total_size")

        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"

        return result

    # -----------------------------------------------------------------------
    # Share link operations
    # -----------------------------------------------------------------------

    async def get_share_snap(
        self, share_code: str, receive_code: str = "", cid: str = "0"
    ) -> dict:
        """获取 115 分享链接的文件列表快照 (不需要登录)。"""
        import asyncio
        params = {
            "share_code": share_code, "receive_code": receive_code,
            "cid": cid, "limit": "20", "asc": "0", "offset": "0", "format": "json",
        }
        headers = {
            "User-Agent": _UA_DEFAULT,
            "Referer": self._build_share_referer(share_code, receive_code),
        }
        resp = await asyncio.to_thread(
            self._request_115, "GET", _API_SHARE_SNAP,
            headers=headers, params=params,
        )
        resp.raise_for_status()
        return resp.json()

    async def save_share_link(
        self, share_url: str, receive_code: str = "", target_cid: str = "0"
    ) -> dict:
        """
        转存 115 分享链接到自己的网盘。

        Args:
            share_url: 115 分享链接或纯分享码
            receive_code: 提取码
            target_cid: 目标文件夹 CID

        Returns:
            {"success": bool, "message": str, "file_count": int, "share_code": str, "snap": dict}
        """
        self._ensure_cookie()
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
            snap = await self.get_share_snap(share_code, receive_code, target_cid)
            result["snap"] = snap
            if not snap.get("state"):
                result["message"] = f"获取分享快照失败: {snap.get('error', '未知')}"
                return result

            file_list = snap.get("data", {}).get("list", [])
            result["file_count"] = len(file_list)
            if not file_list:
                result["message"] = "分享链接无文件"
                return result

            file_ids = ",".join(
                str(f.get("fid", f.get("file_id", ""))) for f in file_list
            )
            import asyncio
            resp = await asyncio.to_thread(
                self._request_115, "POST", _API_SHARE_SAVE,
                data={
                    "share_code": share_code, "receive_code": receive_code,
                    "cid": target_cid, "file_id": file_ids,
                },
            )
            save_data = resp.json()
            if save_data.get("state") or save_data.get("errno") == 0:
                result["success"] = True
                result["message"] = f"成功转存 {result['file_count']} 个文件"
            else:
                result["message"] = (
                    f"转存失败: {save_data.get('error', save_data.get('msg', '未知'))}"
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

        优先使用 m115 加密协议，失败时 fallback 到 Web 离线接口。

        Args:
            url: magnet:?xt=... 或 http(s)://... 链接
            save_dir_id: 保存目录 ID

        Returns:
            {"success": bool, "message": str, "task_count": int, "info_hashes": list}
        """
        self._ensure_cookie()
        result: dict = {
            "success": False, "message": "", "task_count": 0, "info_hashes": [],
        }

        try:
            if self._user_id <= 0:
                info = await self.check_login_status()
                if not info["logged_in"]:
                    result["message"] = f"登录失败: {info['error']}"
                    return result
                self._user_id = info["user_id"] or 0

            # 方式一: m115 加密协议
            try:
                result = await self._add_offline_m115(url, save_dir_id)
                if result["success"]:
                    return result
                logger.warning("m115 offline failed: %s, trying web fallback", result["message"])
            except Exception as e:
                logger.warning("m115 offline exception: %s, trying web fallback", e)

            # 方式二: Web 离线接口 fallback (通过 sign + time 构造请求)
            result = await self._add_offline_web(url, save_dir_id)

        except Exception as e:
            result["message"] = f"离线下载异常: {type(e).__name__}: {e}"

        return result

    async def _add_offline_m115(self, url: str, save_dir_id: str) -> dict:
        """m115 加密协议方式添加离线任务。"""
        import asyncio
        result: dict = {
            "success": False, "message": "", "task_count": 0, "info_hashes": [],
        }

        params = {
            "ac": "add_task_urls", "wp_path_id": save_dir_id,
            "app_ver": "27.0.5.7", "uid": str(self._user_id),
            "url[0]": url,
        }
        payload_bytes = json.dumps(params, separators=(",", ":")).encode()
        encrypted = m115_encode(payload_bytes)

        resp = await asyncio.to_thread(
            self._request_115, "POST", _API_OFFLINE_ADD,
            params={"t": str(int(time.time()))},
            data={"data": encrypted},
        )
        resp.raise_for_status()
        resp_data = resp.json()

        if not resp_data.get("state"):
            result["message"] = f"离线任务添加失败: {resp_data.get('error', '未知')}"
            return result

        encoded_data = resp_data.get("encoded_data", "")
        if encoded_data:
            try:
                decoded = m115_decode(encoded_data)
                task_info = json.loads(decoded)
                tasks = task_info.get("result", [])
                result["info_hashes"] = [
                    t.get("info_hash", "") for t in tasks if t.get("info_hash")
                ]
                result["task_count"] = len(tasks)
            except Exception as decode_err:
                logger.warning("m115 decode failed (non-fatal): %s", decode_err)
                result["task_count"] = 1
        else:
            result["task_count"] = 1

        result["success"] = True
        result["message"] = f"已添加 {result['task_count']} 个离线任务"
        return result

    async def _add_offline_web(self, url: str, save_dir_id: str) -> dict:
        """
        Web 离线接口 fallback: 通过获取 sign + time 构造任务。
        不依赖 m115 RSA 加密，使用 115 Web 端的普通表单提交。
        """
        import asyncio
        result: dict = {
            "success": False, "message": "", "task_count": 0, "info_hashes": [],
        }

        try:
            # 先获取离线任务的 sign 和 time
            resp = await asyncio.to_thread(
                self._request_115, "POST",
                "https://lixian.115.com/lixian/?ct=lixian&ac=add_task_url",
                data={
                    "url": url,
                    "wp_path_id": save_dir_id,
                },
            )
            resp.raise_for_status()
            resp_data = resp.json()

            if resp_data.get("state") or resp_data.get("errno") == 0:
                result["success"] = True
                result["task_count"] = 1
                result["message"] = "已添加离线任务 (web fallback)"
            else:
                error_msg = resp_data.get("error", resp_data.get("msg", "未知错误"))
                # 如果 web 接口也失败，尝试最简单的 URL 添加
                resp2 = await asyncio.to_thread(
                    self._request_115, "POST",
                    "https://lixian.115.com/lixianssp/?ac=add_task_urls",
                    data={
                        "url[0]": url,
                        "wp_path_id": save_dir_id,
                        "uid": str(self._user_id),
                    },
                )
                resp2_data = resp2.json()
                if resp2_data.get("state"):
                    result["success"] = True
                    result["task_count"] = 1
                    result["message"] = "已添加离线任务 (simple fallback)"
                else:
                    result["message"] = f"离线任务添加失败: {error_msg}"

        except Exception as e:
            result["message"] = f"Web 离线接口异常: {type(e).__name__}: {e}"

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
        print("115 Saver 模块测试")
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
            "https://115cdn.com/s/xyz789?password=abcd",
            "https://pan.quark.cn/s/abcdef",
        ]:
            print(f"  {str(parse_115_share_code(u)):>20} <- {u[:50]}")

        # 3. m115 加密验证 (encode 可执行即可，decode 需服务端私钥加密数据)
        print("\n[3] m115 加密验证:")
        test_payload = b'{"ac":"add_task_urls","wp_path_id":"0"}'
        try:
            encoded = m115_encode(test_payload)
            print(f"  encode: OK (len={len(encoded)})")
            print(f"  截取: {encoded[:60]}...")
        except Exception as e:
            print(f"  encode 失败: {e}")

        # 4. 登录检查
        print("\n[4] 登录状态检查:")
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

        # 5. auto_save 路由
        print("\n[5] auto_save 路由:")
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
