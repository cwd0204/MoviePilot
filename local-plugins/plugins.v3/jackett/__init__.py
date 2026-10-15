"""Jackett 资源源插件：把 Jackett 已配置的索引器接入 MoviePilot 的搜索与订阅刷新。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

from app.schemas.types import MediaType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.media import TorrentInfo
from app.sdk.network import AsyncRequestUtils, RequestUtils
from app.sdk.plugin import _PluginBase  # pylint: disable=no-name-in-module

from .torznab import (
    ALL_INDEXER_ID,
    JackettIndexer,
    TorznabError,
    build_indexers_params,
    build_search_params,
    normalize_host,
    parse_indexers,
    parse_results,
    torznab_url,
)


class Jackett(_PluginBase):
    """以插件资源源身份接入 Jackett，参与资源搜索与订阅刷新。"""

    plugin_name = "Jackett"
    plugin_desc = "接入 Jackett 已配置的索引器，参与资源搜索与订阅刷新。"
    plugin_icon = "https://raw.githubusercontent.com/Jackett/Jackett/master/src/Jackett.Common/Content/jacket_medium.png"
    plugin_version = "1.0.0"
    plugin_author = "neiltsai"
    author_url = "https://github.com/neiltsai"
    plugin_config_prefix = "jackett_"
    plugin_order = 10
    auth_level = 1

    _DEFAULT_TIMEOUT = 30
    _MIN_TIMEOUT = 5
    _MAX_TIMEOUT = 300
    _DISCOVERY_TIMEOUT = 10
    _MAX_WORKERS = 8
    _INDEXERS_DATA_KEY = "indexers"

    _enabled = False
    _host = ""
    _api_key = ""
    _indexers: List[str] = []
    _timeout = _DEFAULT_TIMEOUT
    _proxy = False
    _priority = 0

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """读取配置并规范化地址、超时、优先级与索引器列表。"""
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._host = normalize_host(str(config.get("host") or ""))
        self._api_key = str(config.get("api_key") or "").strip()
        self._indexers = [
            str(item).strip() for item in (config.get("indexers") or []) if str(item).strip()
        ]
        self._timeout = self._normalize_timeout(config.get("timeout"))
        self._proxy = bool(config.get("proxy"))
        self._priority = self._normalize_int(config.get("priority"), default=0)

    def get_state(self) -> bool:
        """启用且填写了地址与 API Key 时视为运行中。"""
        return bool(self._enabled and self._host and self._api_key)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """本插件不注册 API。"""
        return []

    def get_module(self) -> Dict[str, Any]:
        """声明插件资源源方法，宿主以 site={} 每轮调用一次。"""
        return {
            "search_torrents": self.search_torrents,
            "async_search_torrents": self.async_search_torrents,
            "refresh_torrents": self.refresh_torrents,
            "async_refresh_torrents": self.async_refresh_torrents,
        }

    def stop_service(self) -> None:
        """插件没有后台服务，无需释放资源。"""
        return None

    # ---------- 资源源方法 ----------

    def search_torrents(
        self,
        site: dict,
        keyword: Optional[str] = None,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """按关键字搜索 Jackett，只提供第一页。"""
        if not keyword or not self._should_query(site, page):
            return []
        params = build_search_params(self._api_key, keyword=keyword, mtype=mtype)
        return self._fetch_all(params, label=f"搜索「{keyword}」")

    async def async_search_torrents(
        self,
        site: dict,
        keyword: Optional[str] = None,
        mtype: Optional[MediaType] = None,
        page: Optional[int] = 0,
    ) -> List[TorrentInfo]:
        """异步按关键字搜索 Jackett，只提供第一页。"""
        if not keyword or not self._should_query(site, page):
            return []
        params = build_search_params(self._api_key, keyword=keyword, mtype=mtype)
        return await self._async_fetch_all(params, label=f"搜索「{keyword}」")

    def refresh_torrents(
        self,
        site: dict,
        keyword: Optional[str] = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
        mtype: Optional[MediaType] = None,
    ) -> List[TorrentInfo]:
        """拉取 Jackett 最新种子，供订阅刷新匹配；cat 为站点分类语义，这里不适用。"""
        del cat
        if not self._should_query(site, page):
            return []
        params = build_search_params(self._api_key, keyword=keyword, mtype=mtype)
        return self._fetch_all(params, label="刷新最新种子")

    async def async_refresh_torrents(
        self,
        site: dict,
        keyword: Optional[str] = None,
        cat: Optional[str] = None,
        page: Optional[int] = 0,
        mtype: Optional[MediaType] = None,
    ) -> List[TorrentInfo]:
        """异步拉取 Jackett 最新种子。"""
        del cat
        if not self._should_query(site, page):
            return []
        params = build_search_params(self._api_key, keyword=keyword, mtype=mtype)
        return await self._async_fetch_all(params, label="刷新最新种子")

    # ---------- 请求编排 ----------

    def _should_query(self, site: Optional[dict], page: Optional[int]) -> bool:
        """插件资源只有一页，且只响应宿主以空 site 发起的插件源调用。"""
        if not self.get_state():
            return False
        if page:
            return False
        if site and (site.get("id") is not None or site.get("domain")):
            # 宿主按站点逐个调用的路径不属于插件资源源，避免结果混入站点缓存
            return False
        return True

    def _target_indexers(self) -> List[str]:
        """返回本次要请求的索引器 ID；未选择时用 all 聚合端点一次完成。"""
        return list(self._indexers) if self._indexers else [ALL_INDEXER_ID]

    def _fetch_all(self, params: Dict[str, str], *, label: str) -> List[TorrentInfo]:
        """同步请求全部目标索引器并合并结果，多索引器时并发执行。"""
        targets = self._target_indexers()
        if len(targets) == 1:
            return self._fetch_one(targets[0], params, label=label)
        results: List[TorrentInfo] = []
        with ThreadPoolExecutor(max_workers=min(len(targets), self._MAX_WORKERS)) as pool:
            for items in pool.map(lambda target: self._fetch_one(target, params, label=label), targets):
                results.extend(items)
        return results

    async def _async_fetch_all(self, params: Dict[str, str], *, label: str) -> List[TorrentInfo]:
        """异步并发请求全部目标索引器并合并结果。"""
        batches = await asyncio.gather(
            *(self._async_fetch_one(target, params, label=label) for target in self._target_indexers())
        )
        return [item for batch in batches for item in batch]

    def _fetch_one(self, indexer_id: str, params: Dict[str, str], *, label: str) -> List[TorrentInfo]:
        """请求单个索引器；任何异常只记日志并返回空列表。"""
        url = torznab_url(self._host, indexer_id)
        try:
            response = RequestUtils(
                ua=settings.USER_AGENT,
                proxies=self._proxies(),
                timeout=self._timeout,
            ).get_res(url, params=params)
        except Exception as err:  # noqa: BLE001 - 单个索引器失败不影响其它来源
            logger.warning(f"Jackett 索引器 {indexer_id} {label}出错：{err}")
            return []
        return self._handle_response(response, indexer_id, label=label)

    async def _async_fetch_one(self, indexer_id: str, params: Dict[str, str], *, label: str) -> List[TorrentInfo]:
        """异步请求单个索引器；任何异常只记日志并返回空列表。"""
        url = torznab_url(self._host, indexer_id)
        try:
            response = await AsyncRequestUtils(
                ua=settings.USER_AGENT,
                proxies=self._proxies(),
                timeout=self._timeout,
            ).get_res(url, params=params)
        except Exception as err:  # noqa: BLE001 - 单个索引器失败不影响其它来源
            logger.warning(f"Jackett 索引器 {indexer_id} {label}出错：{err}")
            return []
        return self._handle_response(response, indexer_id, label=label)

    def _handle_response(self, response: Any, indexer_id: str, *, label: str) -> List[TorrentInfo]:
        """校验响应并把 Torznab 条目投影为 TorrentInfo。"""
        if response is None:
            logger.warning(f"Jackett 索引器 {indexer_id} {label}失败：无响应（网络错误或超时）")
            return []
        if response.status_code != 200:
            logger.warning(f"Jackett 索引器 {indexer_id} {label}失败：HTTP {response.status_code}")
            return []
        try:
            releases = parse_results(response.content, default_indexer_name=indexer_id)
        except TorznabError as err:
            logger.warning(f"Jackett 索引器 {indexer_id} {label}失败：{err}")
            return []
        torrents = [self._build_torrent(release) for release in releases]
        logger.info(f"Jackett 索引器 {indexer_id} {label}返回 {len(torrents)} 条资源")
        return torrents

    def _build_torrent(self, release: Dict[str, Any]) -> TorrentInfo:
        """把解析结果补上插件来源信息后构造 TorrentInfo。"""
        return TorrentInfo(
            site=None,
            site_name=release["site_name"],
            site_cookie=None,
            site_ua=settings.USER_AGENT,
            site_proxy=self._proxy,
            site_order=self._priority,
            site_downloader=None,
            title=release["title"],
            description=release["description"],
            media_source=release["media_source"],
            media_id=release["media_id"],
            enclosure=release["enclosure"],
            page_url=release["page_url"],
            size=release["size"],
            seeders=release["seeders"],
            peers=release["peers"],
            grabs=release["grabs"],
            pubdate=release["pubdate"],
            uploadvolumefactor=release["uploadvolumefactor"],
            downloadvolumefactor=release["downloadvolumefactor"],
            category=release["category"],
        )

    def _proxies(self) -> Optional[Dict[str, str]]:
        """按配置决定是否使用系统代理访问 Jackett。"""
        return settings.PROXY if self._proxy else None

    # ---------- 索引器发现 ----------

    def _discover_indexers(self) -> Tuple[List[JackettIndexer], Optional[str]]:
        """向 Jackett 请求已配置索引器；失败时返回上次缓存并附带原因。"""
        if not self._host or not self._api_key:
            return self._cached_indexers(), "未配置 Jackett 地址或 API Key"
        url = torznab_url(self._host, ALL_INDEXER_ID)
        try:
            response = RequestUtils(
                ua=settings.USER_AGENT,
                proxies=self._proxies(),
                timeout=self._DISCOVERY_TIMEOUT,
            ).get_res(url, params=build_indexers_params(self._api_key))
        except Exception as err:  # noqa: BLE001 - 表单展示只需要原因文本
            return self._cached_indexers(), f"连接 Jackett 失败：{err}"
        if response is None:
            return self._cached_indexers(), "连接 Jackett 失败：无响应（网络错误或超时）"
        if response.status_code != 200:
            return self._cached_indexers(), f"Jackett 返回 HTTP {response.status_code}"
        try:
            indexers = parse_indexers(response.content)
        except TorznabError as err:
            return self._cached_indexers(), str(err)
        self.save_data(self._INDEXERS_DATA_KEY, [asdict(item) for item in indexers])
        return indexers, None

    def _cached_indexers(self) -> List[JackettIndexer]:
        """读取上次成功发现的索引器列表。"""
        cached = self.get_data(self._INDEXERS_DATA_KEY) or []
        indexers: List[JackettIndexer] = []
        for item in cached:
            if isinstance(item, dict) and item.get("id"):
                indexers.append(JackettIndexer(
                    id=str(item.get("id")),
                    name=str(item.get("name") or item.get("id")),
                    type=str(item.get("type") or ""),
                    language=str(item.get("language") or ""),
                ))
        return indexers

    # ---------- 配置规范化 ----------

    @classmethod
    def _normalize_timeout(cls, value: Any) -> int:
        """把超时夹在允许范围内，非法值回退默认。"""
        timeout = cls._normalize_int(value, default=cls._DEFAULT_TIMEOUT)
        return max(cls._MIN_TIMEOUT, min(cls._MAX_TIMEOUT, timeout))

    @staticmethod
    def _normalize_int(value: Any, *, default: int) -> int:
        """宽松地把配置值转为整数。"""
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    # ---------- UI ----------

    def get_form(self) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, Any]]:
        """返回 Vuetify JSON 配置表单；索引器选项来自 Jackett 实时发现或上次缓存。"""
        indexers, error = self._discover_indexers()
        items = [
            {"title": f"{item.name}（{item.type or '未知类型'}）", "value": item.id}
            for item in indexers
        ]
        known_ids = {item.id for item in indexers}
        # 已选但本次未发现的索引器保留在选项里，避免保存时被静默丢弃
        items.extend(
            {"title": f"{indexer_id}（未在 Jackett 中发现）", "value": indexer_id}
            for indexer_id in self._indexers
            if indexer_id not in known_ids
        )
        if error:
            alert = {"type": "warning", "text": f"无法读取 Jackett 索引器：{error}"}
        else:
            alert = {"type": "success", "text": f"已连接 Jackett，发现 {len(indexers)} 个已配置索引器。"}
        return [
            {
                "component": "VForm",
                "content": [
                    self._row([
                        self._col({"component": "VAlert", "props": {**alert, "variant": "tonal"}}, md=12),
                    ]),
                    self._row([
                        self._col({"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}),
                        self._col({"component": "VSwitch", "props": {"model": "proxy", "label": "使用系统代理访问 Jackett"}}),
                    ]),
                    self._row([
                        self._col({
                            "component": "VTextField",
                            "props": {"model": "host", "label": "Jackett 地址", "placeholder": "http://127.0.0.1:9117"},
                        }, md=8),
                        self._col({
                            "component": "VTextField",
                            "props": {"model": "api_key", "label": "API Key", "type": "password"},
                        }, md=4),
                    ]),
                    self._row([
                        self._col({
                            "component": "VSelect",
                            "props": {
                                "model": "indexers",
                                "label": "索引器（留空表示全部，走 all 聚合端点）",
                                "multiple": True,
                                "chips": True,
                                "clearable": True,
                                "items": items,
                            },
                        }, md=12),
                    ]),
                    self._row([
                        self._col({
                            "component": "VTextField",
                            "props": {"model": "timeout", "label": "请求超时（秒，5-300）", "type": "number", "placeholder": "30"},
                        }),
                        self._col({
                            "component": "VTextField",
                            "props": {"model": "priority", "label": "资源优先级（site_order）", "type": "number", "placeholder": "0"},
                        }),
                    ]),
                    self._row([
                        self._col({
                            "component": "VAlert",
                            "props": {
                                "type": "info",
                                "variant": "tonal",
                                "text": "插件结果会与 PT 站点结果一起进入搜索、订阅搜索和订阅刷新；"
                                        "种子通过 Jackett 代理链接下载，无需站点 Cookie。",
                            },
                        }, md=12),
                    ]),
                ],
            }
        ], self._default_config()

    def get_page(self) -> Optional[List[Dict[str, Any]]]:
        """详情页：列出 Jackett 已配置索引器及其是否纳入本插件搜索。"""
        if not self._host or not self._api_key:
            return [{
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": "请先在配置中填写 Jackett 地址和 API Key。"},
            }]
        indexers, error = self._discover_indexers()
        content: List[Dict[str, Any]] = []
        if error:
            content.append({
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal", "text": f"无法读取 Jackett 索引器：{error}，以下为上次缓存。"},
            })
        selected = set(self._indexers)
        header = {
            "component": "tr",
            "content": [{"component": "th", "text": title} for title in ("名称", "ID", "类型", "语言", "纳入搜索")],
        }
        rows = [
            {
                "component": "tr",
                "content": [
                    {"component": "td", "text": item.name},
                    {"component": "td", "text": item.id},
                    {"component": "td", "text": item.type or "-"},
                    {"component": "td", "text": item.language or "-"},
                    {"component": "td", "text": "是" if not selected or item.id in selected else "否"},
                ],
            }
            for item in indexers
        ]
        content.append({
            "component": "VTable",
            "props": {"hover": True},
            "content": [
                {"component": "thead", "content": [header]},
                {"component": "tbody", "content": rows},
            ],
        })
        return content

    @staticmethod
    def _row(cols: List[Dict[str, Any]]) -> Dict[str, Any]:
        """构造 VRow。"""
        return {"component": "VRow", "content": cols}

    @staticmethod
    def _col(component: Dict[str, Any], md: int = 6) -> Dict[str, Any]:
        """构造带响应式栅格的 VCol。"""
        return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": [component]}

    @classmethod
    def _default_config(cls) -> Dict[str, Any]:
        """表单默认值。"""
        return {
            "enabled": False,
            "host": "",
            "api_key": "",
            "indexers": [],
            "timeout": cls._DEFAULT_TIMEOUT,
            "proxy": False,
            "priority": 0,
        }
