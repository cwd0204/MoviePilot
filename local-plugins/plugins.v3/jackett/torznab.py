"""Jackett Torznab 接口的纯函数：地址与参数构造、XML 解析和字段映射，不依赖插件运行状态。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Optional

from lxml import etree

from app.schemas.types import MediaSource, MediaType

ALL_INDEXER_ID = "all"
TORZNAB_NS = "http://torznab.com/schemas/2015/feed"

_IMDB_ID_PATTERN = re.compile(r"^tt\d{7,8}$")
# 禁用实体与网络访问：Jackett 响应来自用户自建服务，但仍按不可信输入解析
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)

# Torznab 标准大类区间 → MoviePilot 媒体类型；精确匹配优先级按列表顺序
_CATEGORY_RANGES: tuple[tuple[int, int, MediaType], ...] = (
    (2000, 2999, MediaType.MOVIE),
    (5000, 5999, MediaType.TV),
    (3000, 3999, MediaType.MUSIC),
)
_CATEGORY_BY_MEDIA_TYPE = {
    MediaType.MOVIE: "2000",
    MediaType.TV: "5000",
    MediaType.MUSIC: "3000",
}


class TorznabError(Exception):
    """Jackett 返回 <error> 文档，或响应无法按 Torznab XML 解析。"""


@dataclass(frozen=True)
class JackettIndexer:
    """Jackett 中一个已配置索引器的摘要。"""

    id: str
    name: str
    type: str = ""
    language: str = ""


def normalize_host(host: Optional[str]) -> str:
    """规范化 Jackett 地址：去空白、补 http 协议、去尾部斜杠；空值返回空串。"""
    value = (host or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    return value.rstrip("/")


def torznab_url(host: str, indexer_id: str) -> str:
    """拼出指定索引器（或 all 聚合）的 Torznab 接口地址。"""
    return f"{host}/api/v2.0/indexers/{indexer_id}/results/torznab/api"


def build_search_params(
    api_key: str,
    keyword: Optional[str] = None,
    mtype: Optional[MediaType] = None,
) -> dict[str, str]:
    """按关键字与媒体类型构造 Torznab 查询参数；IMDb ID 走 movie/tvsearch，其余走 search。"""
    params: dict[str, str] = {"apikey": api_key}
    keyword = (keyword or "").strip()
    if keyword and _IMDB_ID_PATTERN.match(keyword):
        params["t"] = "tvsearch" if mtype == MediaType.TV else "movie"
        params["imdbid"] = keyword
    else:
        params["t"] = "search"
        if keyword:
            params["q"] = keyword
    category = _CATEGORY_BY_MEDIA_TYPE.get(mtype) if mtype else None
    if category:
        params["cat"] = category
    return params


def build_indexers_params(api_key: str) -> dict[str, str]:
    """构造列出已配置索引器的 Torznab 参数。"""
    return {"apikey": api_key, "t": "indexers", "configured": "true"}


def map_category(values: Iterable[Any]) -> Optional[str]:
    """把 Torznab 分类编号映射为电影、电视剧或音乐文本，无法识别时返回 None。"""
    for value in values:
        number = _to_int(value)
        if number is None:
            continue
        for lower, upper, media_type in _CATEGORY_RANGES:
            if lower <= number <= upper:
                return media_type.value
    return None


def parse_indexers(content: bytes) -> list[JackettIndexer]:
    """解析 t=indexers 响应，只保留 configured 不为 false 的索引器。"""
    root = _parse_root(content)
    indexers: list[JackettIndexer] = []
    for node in root.iter("indexer"):
        indexer_id = (node.get("id") or "").strip()
        if not indexer_id or (node.get("configured") or "true").lower() == "false":
            continue
        indexers.append(JackettIndexer(
            id=indexer_id,
            name=_text(node, "title") or indexer_id,
            type=_text(node, "type"),
            language=_text(node, "language"),
        ))
    return indexers


def parse_results(content: bytes, *, default_indexer_name: str) -> list[dict[str, Any]]:
    """解析 Torznab 搜索响应为 TorrentInfo 字段字典列表；无下载链接的条目被丢弃。"""
    root = _parse_root(content)
    channel = root.find("channel")
    if channel is None:
        return []
    channel_name = _text(channel, "title") or default_indexer_name
    releases: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        release = _parse_item(item, channel_name)
        if release is not None:
            releases.append(release)
    return releases


def format_pubdate(value: Optional[str]) -> Optional[str]:
    """把 RFC 2822 发布时间转为本地时区的 MoviePilot 时间文本，解析失败返回 None。"""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _parse_root(content: bytes) -> Any:
    """解析 XML 根节点；空响应、语法错误或 <error> 文档统一抛出 TorznabError。"""
    if not content:
        raise TorznabError("响应为空")
    try:
        root = etree.fromstring(content, parser=_XML_PARSER)
    except (etree.XMLSyntaxError, ValueError) as err:
        raise TorznabError(f"响应不是合法的 Torznab XML：{err}") from err
    if root is None:
        raise TorznabError("响应不是合法的 Torznab XML")
    if root.tag == "error":
        raise TorznabError(
            f"Jackett 返回错误 {root.get('code') or '-'}：{root.get('description') or '未知原因'}"
        )
    return root


def _parse_item(item: Any, default_indexer_name: str) -> Optional[dict[str, Any]]:
    """把单个 <item> 投影为 TorrentInfo 字段字典。"""
    title = _text(item, "title")
    attrs, categories = _torznab_attrs(item)
    enclosure_node = item.find("enclosure")
    enclosure = (
        (enclosure_node.get("url") if enclosure_node is not None else "")
        or _text(item, "link")
        or attrs.get("magneturl")
        or ""
    ).strip()
    if not title or not enclosure:
        return None
    seeders = _to_int(attrs.get("seeders")) or 0
    peers_total = _to_int(attrs.get("peers")) or 0
    description = _text(item, "description")
    page_url = _text(item, "comments") or _text(item, "guid")
    indexer_node = item.find("jackettindexer")
    indexer_name = (
        (indexer_node.text or "").strip() if indexer_node is not None else ""
    ) or default_indexer_name
    media_source, media_id = _media_identity(attrs)
    categories.extend(node.text for node in item.findall("category"))
    return {
        "title": title,
        "description": "" if description == title else description,
        "enclosure": enclosure,
        "page_url": page_url if page_url.startswith(("http://", "https://")) else None,
        "size": _to_int(_text(item, "size"))
        or _to_int(enclosure_node.get("length") if enclosure_node is not None else None)
        or 0,
        "seeders": seeders,
        "peers": max(peers_total - seeders, 0),
        "grabs": _to_int(_text(item, "grabs")) or _to_int(attrs.get("grabs")) or 0,
        "pubdate": format_pubdate(_text(item, "pubDate")),
        "uploadvolumefactor": _to_float(attrs.get("uploadvolumefactor")),
        "downloadvolumefactor": _to_float(attrs.get("downloadvolumefactor")),
        "media_source": media_source,
        "media_id": media_id,
        "category": map_category(categories),
        "site_name": f"[Jackett] {indexer_name}",
    }


def _torznab_attrs(item: Any) -> tuple[dict[str, str], list[Any]]:
    """读取 torznab:attr：普通属性取第一个值，category 单独累积为列表。"""
    attrs: dict[str, str] = {}
    categories: list[Any] = []
    for node in item.findall(f"{{{TORZNAB_NS}}}attr"):
        name = (node.get("name") or "").strip().lower()
        value = node.get("value")
        if not name or value is None:
            continue
        if name == "category":
            categories.append(value)
        else:
            attrs.setdefault(name, value)
    return attrs, categories


def _media_identity(attrs: dict[str, str]) -> tuple[Optional[MediaSource], Optional[str]]:
    """按 IMDb、TMDB、TVDB 的优先级从属性中取出媒体身份。"""
    imdb_id = (attrs.get("imdbid") or "").strip()
    if _IMDB_ID_PATTERN.match(imdb_id):
        return MediaSource.IMDb, imdb_id
    imdb_number = _to_int(attrs.get("imdb"))
    if imdb_number:
        return MediaSource.IMDb, f"tt{imdb_number:07d}"
    tmdb_id = _to_int(attrs.get("tmdbid"))
    if tmdb_id:
        return MediaSource.TMDB, str(tmdb_id)
    tvdb_id = _to_int(attrs.get("tvdbid"))
    if tvdb_id:
        return MediaSource.TVDB, str(tvdb_id)
    return None, None


def _text(element: Any, tag: str) -> str:
    """读取子节点文本并去空白，缺失时返回空串。"""
    node = element.find(tag)
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def _to_int(value: Any) -> Optional[int]:
    """宽松地把文本转为非负整数，失败返回 None。"""
    if value is None:
        return None
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _to_float(value: Any) -> Optional[float]:
    """把文本转为浮点数，失败返回 None。"""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None
