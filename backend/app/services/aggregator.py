"""
多网盘资源异步聚合搜索服务

搜索源:
  1. upyunso.com  — 加密 API (AES-CBC + HMAC-SHA256 签名)
  2. pansearch.me — 公开 JSON API (baidu/quark/aliyundrive/xunlei)
  3. nyaa.si      — 公开磁力搜索 (anime/resources)

去重: 基于 share_url 或 (title + pan_type)
容错: asyncio.gather(return_exceptions=True)，单源超时不阻塞其他源
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac as hmac_mod
import json
import logging
import random
import re
import string
from typing import Optional

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class SearchResult(BaseModel):
    """单条搜索结果"""
    title: str
    pan_type: str  # 115, quark, aliyun, baidu, xunlei, magnet, other
    share_url: str
    extract_code: Optional[str] = None
    datetime: Optional[str] = None
    source: str  # 搜索源渠道名称


class SearchResponse(BaseModel):
    """聚合搜索响应"""
    keyword: str
    total: int
    results: list[SearchResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    elapsed_ms: int = 0


# ---------------------------------------------------------------------------
# Pan type normalization — 域名正则强制校准
# ---------------------------------------------------------------------------

def _normalize_pan_type(raw: str, url: str = "") -> str:
    """
    根据原始标签和 URL 域名正则强制校准网盘类型。
    优先使用 URL 域名判断，避免源标签误判。
    """
    url_lower = url.lower()

    # URL 域名优先判断
    if "115.com" in url_lower:
        return "115"
    if "pan.quark.cn" in url_lower or "pan.quark" in url_lower:
        return "quark"
    if "alipan.com" in url_lower or "aliyundrive.com" in url_lower or "aliyun" in raw.lower():
        return "aliyun"
    if "pan.baidu.com" in url_lower or "baidu" in raw.lower():
        return "baidu"
    if "xunlei" in url_lower or "xunlei" in raw.lower():
        return "xunlei"

    # fallback 到原始标签
    raw_map = {
        "ali": "aliyun", "aliyundrive": "aliyun",
        "quark": "quark", "baidu": "baidu",
        "xunlei": "xunlei", "115": "115",
    }
    return raw_map.get(raw.lower().strip(), raw.lower().strip() or "other")


# ---------------------------------------------------------------------------
# UP云搜 (upyunso.com) — 加密 API
# ---------------------------------------------------------------------------

_UPYUNSO_AES_KEY = b"qq1920520460qqxx"
_UPYUNSO_HMAC_KEY = "upyunso_hmac_s3cr3t_2026"
_UPYUNSO_BASE = "https://www.upyunso.com"
_UPYUNSO_TIMEOUT = 8.0


def _upyunso_encrypt_params(params: dict) -> dict:
    """Encrypt search params for upyunso.com API."""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    sign_input = {**params, "_nonce": nonce}
    sign_str = "&".join(f"{k}={sign_input[k]}" for k in sorted(sign_input))
    sign = hmac_mod.new(
        _UPYUNSO_HMAC_KEY.encode(), sign_str.encode(), hashlib.sha256
    ).hexdigest()

    payload_json = json.dumps(params, separators=(",", ":"))
    cipher = AES.new(_UPYUNSO_AES_KEY, AES.MODE_CBC, _UPYUNSO_AES_KEY)
    ct = cipher.encrypt(pad(payload_json.encode(), AES.block_size))
    payload_b64 = base64.b64encode(ct).decode()

    return {"__payload": payload_b64, "_nonce": nonce, "_sign": sign}


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


async def _search_upyunso(
    client: httpx.AsyncClient, keyword: str, page: int = 1
) -> list[SearchResult]:
    """Search upyunso.com with encrypted API."""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    params = {
        "keyword": keyword, "pan_type": "all",
        "page": str(page), "page_size": "20",
        "file_type": "all", "time_range": "all",
    }
    encrypted = _upyunso_encrypt_params(params)

    resp = await client.get(
        f"{_UPYUNSO_BASE}/api/search", params=encrypted, timeout=_UPYUNSO_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()

    if not body.get("__encrypted"):
        raise ValueError("upyunso: unexpected non-encrypted response")

    ct = base64.b64decode(body["data"])
    cipher = AES.new(_UPYUNSO_AES_KEY, AES.MODE_CBC, _UPYUNSO_AES_KEY)
    pt = unpad(cipher.decrypt(ct), AES.block_size)
    data = json.loads(pt.decode())

    if data.get("status") != "success":
        raise ValueError(f"upyunso: {data.get('msg', 'unknown error')}")

    results: list[SearchResult] = []
    for item in data.get("result", {}).get("list", []):
        rid = item.get("rid", "")
        title = _strip_html(item.get("title", ""))
        pan_raw = item.get("pan_type", "other")
        share_url = f"{_UPYUNSO_BASE}/resource/{rid}"
        results.append(SearchResult(
            title=title,
            pan_type=_normalize_pan_type(pan_raw, share_url),
            share_url=share_url,
            extract_code=None,
            datetime=item.get("insert_time"),
            source="upyunso",
        ))
    return results


# ---------------------------------------------------------------------------
# PanSearch.me — 公开 JSON API
# ---------------------------------------------------------------------------

_PANSEARCH_BASE = "https://www.pansearch.me"
_PANSEARCH_TIMEOUT = 8.0
_PANSEARCH_PAN_TYPES = ["baidu", "quark", "aliyundrive", "xunlei"]

_SHARE_URL_RE = re.compile(
    r'href="(https?://pan\.(?:baidu|quark)\.cn/[^"]+)"'
)
_EXTRACT_CODE_RE = re.compile(r"(?:pwd|提取码)[=:]\s*([a-zA-Z0-9]{4})")


async def _search_pansearch(
    client: httpx.AsyncClient, keyword: str, pan: str, page: int = 1
) -> list[SearchResult]:
    """Search pansearch.me for a specific pan type."""
    resp = await client.get(
        f"{_PANSEARCH_BASE}/api/search",
        params={"keyword": keyword, "page": page, "pan": pan},
        timeout=_PANSEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise ValueError(f"pansearch: {data['error']}")

    results: list[SearchResult] = []
    for item in data.get("data", []):
        content = item.get("content", "")
        pan_label = item.get("pan", pan)
        time_str = item.get("time", "")

        urls = _SHARE_URL_RE.findall(content)
        if not urls:
            continue

        title_match = re.search(r"名称[：:]\s*(.+?)(?:\n|$)", content)
        title = _strip_html(title_match.group(1).strip()) if title_match else keyword

        code_match = _EXTRACT_CODE_RE.search(content)
        extract_code = code_match.group(1) if code_match else None

        for url in urls:
            pwd_match = re.search(r"[?&]pwd=([a-zA-Z0-9]+)", url)
            if pwd_match:
                extract_code = extract_code or pwd_match.group(1)

            results.append(SearchResult(
                title=title,
                pan_type=_normalize_pan_type(pan_label, url),
                share_url=url,
                extract_code=extract_code,
                datetime=time_str,
                source="pansearch",
            ))
    return results


async def _search_pansearch_all(
    client: httpx.AsyncClient, keyword: str, page: int = 1
) -> list[SearchResult]:
    """Search pansearch.me across all supported pan types concurrently."""
    tasks = [
        _search_pansearch(client, keyword, pan, page)
        for pan in _PANSEARCH_PAN_TYPES
    ]
    all_results: list[SearchResult] = []
    for coro in asyncio.as_completed(tasks):
        try:
            all_results.extend(await coro)
        except Exception as exc:
            logger.warning("pansearch sub-query failed: %s", exc)
    return all_results


# ---------------------------------------------------------------------------
# Nyaa.si — 公开磁力搜索
# ---------------------------------------------------------------------------

_NYAA_BASE = "https://nyaa.si"
_NYAA_TIMEOUT = 10.0


async def _search_nyaa(
    client: httpx.AsyncClient, keyword: str, page: int = 1
) -> list[SearchResult]:
    """Search nyaa.si for magnet links."""
    resp = await client.get(
        _NYAA_BASE,
        params={"f": "0", "c": "0_0", "q": keyword, "p": str(page)},
        timeout=_NYAA_TIMEOUT,
    )
    resp.raise_for_status()
    html = resp.text

    # Parse title + magnet pairs
    titles = re.findall(r'<a href="/view/(\d+)"[^>]*title="([^"]+)"', html)
    magnets = re.findall(r'magnet:\?xt=urn:btih:([a-fA-F0-9]+)', html)

    results: list[SearchResult] = []
    for i, (tid, title) in enumerate(titles):
        if i >= len(magnets):
            break
        magnet_url = f"magnet:?xt=urn:btih:{magnets[i]}"
        results.append(SearchResult(
            title=title.strip(),
            pan_type="magnet",
            share_url=magnet_url,
            extract_code=None,
            datetime=None,
            source="nyaa",
        ))
    return results


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _dedupe(results: list[SearchResult]) -> list[SearchResult]:
    """Deduplicate results by share_url or (title + pan_type)."""
    seen_urls: set[str] = set()
    seen_title_pan: set[tuple[str, str]] = set()
    unique: list[SearchResult] = []

    for r in results:
        url_key = r.share_url.rstrip("/").lower()
        title_key = (r.title.lower().strip(), r.pan_type)

        if url_key in seen_urls or title_key in seen_title_pan:
            continue

        seen_urls.add(url_key)
        seen_title_pan.add(title_key)
        unique.append(r)

    return unique


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

class SearchAggregator:
    """
    多网盘资源异步聚合搜索器。

    并发调用多个搜索源，单个源超时或异常不影响其他源。
    结果基于 share_url / (title + pan_type) 去重。
    """

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    async def search(self, keyword: str, page: int = 1) -> SearchResponse:
        """执行聚合搜索。"""
        t0 = asyncio.get_event_loop().time()
        errors: list[str] = []

        # 如果关键词是磁力链接，直接返回 nyaa 搜索
        if keyword.strip().startswith("magnet:"):
            return SearchResponse(
                keyword=keyword, total=0, results=[], errors=[], elapsed_ms=0,
            )

        async with httpx.AsyncClient(
            headers=self._headers, follow_redirects=True
        ) as client:
            sources = [
                ("upyunso", _search_upyunso(client, keyword, page)),
                ("pansearch", _search_pansearch_all(client, keyword, page)),
                ("nyaa", _search_nyaa(client, keyword, page)),
            ]

            results_list = await asyncio.gather(
                *[task for _, task in sources],
                return_exceptions=True,
            )

        all_results: list[SearchResult] = []
        for (name, _), result in zip(sources, results_list):
            if isinstance(result, Exception):
                err_msg = f"{name}: {type(result).__name__}: {result}"
                logger.warning("Source failed: %s", err_msg)
                errors.append(err_msg)
            else:
                all_results.extend(result)

        unique = _dedupe(all_results)
        elapsed = int((asyncio.get_event_loop().time() - t0) * 1000)
        return SearchResponse(
            keyword=keyword,
            total=len(unique),
            results=unique,
            errors=errors,
            elapsed_ms=elapsed,
        )


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

async def _main():
    import sys

    keyword = sys.argv[1] if len(sys.argv) > 1 else "流浪地球"
    print(f"搜索: {keyword}")
    print("=" * 60)

    agg = SearchAggregator()
    resp = await agg.search(keyword)

    print(f"共 {resp.total} 条结果 (耗时 {resp.elapsed_ms}ms)")
    if resp.errors:
        print(f"错误: {resp.errors}")
    print("-" * 60)

    for i, r in enumerate(resp.results[:20], 1):
        print(f"{i:>2}. [{r.pan_type:^7}] {r.title[:45]}")
        print(f"    链接: {r.share_url[:70]}")
        if r.extract_code:
            print(f"    提取码: {r.extract_code}")
        if r.datetime:
            print(f"    时间: {r.datetime}")
        print(f"    来源: {r.source}")
        print()


if __name__ == "__main__":
    asyncio.run(_main())
