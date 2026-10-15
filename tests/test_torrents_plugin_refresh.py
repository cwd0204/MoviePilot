"""订阅刷新链接入插件资源源的离线回归：调度拆分、独立缓存桶与站点浏览入口。"""

import asyncio
import copy
from unittest.mock import AsyncMock, Mock, patch

from app.chain.torrents import PLUGIN_TORRENT_SOURCE_DOMAIN, PLUGIN_TORRENT_SOURCE_NAME, TorrentsChain
from app.domain.context import TorrentInfo
from app.schemas.types import MediaType


def _plugin_torrent(title: str = "Plugin.Movie.2026.1080p", category: str = MediaType.MOVIE.value) -> TorrentInfo:
    """构造一条插件资源源返回的种子。"""
    return TorrentInfo(
        site=None,
        site_name="[Jackett] Demo",
        title=title,
        enclosure=f"http://jackett:9117/dl/demo/?file={title}",
        category=category,
        pubdate="2026-10-15 00:00:00",
    )


def test_refresh_plugin_torrents_dispatches_only_plugin_providers():
    """插件刷新入口只调度插件模块，site 固定为空字典。"""
    chain = TorrentsChain()
    dispatcher = Mock()
    dispatcher.execute_plugin_modules.return_value = [_plugin_torrent()]
    chain._module_dispatcher = dispatcher

    result = chain.refresh_plugin_torrents(mtype=MediaType.TV)

    assert [item.title for item in result] == ["Plugin.Movie.2026.1080p"]
    dispatcher.execute_plugin_modules.assert_called_once_with(
        "refresh_torrents", None, site={}, keyword=None, cat=None, page=0, mtype=MediaType.TV
    )
    dispatcher.execute_system_modules.assert_not_called()


def test_refresh_site_torrents_dispatches_only_system_modules():
    """站点刷新入口只调度宿主模块，不再把插件按站点逐个调用。"""
    chain = TorrentsChain()
    dispatcher = Mock()
    dispatcher.execute_system_modules.return_value = None
    chain._module_dispatcher = dispatcher
    site = {"id": 1, "domain": "example.com"}

    result = chain.refresh_site_torrents(site=site, page=1)

    assert result == []
    dispatcher.execute_system_modules.assert_called_once_with(
        "refresh_torrents", None, site=site, keyword=None, cat=None, page=1, mtype=None
    )
    dispatcher.execute_plugin_modules.assert_not_called()


def test_async_refresh_entries_use_async_method_names():
    """异步入口使用 async_refresh_torrents 契约并分别只调度插件或宿主模块。"""
    chain = TorrentsChain()
    dispatcher = Mock()
    dispatcher.async_execute_plugin_modules = AsyncMock(return_value=[_plugin_torrent()])
    dispatcher.async_execute_system_modules = AsyncMock(return_value=None)
    chain._module_dispatcher = dispatcher
    site = {"id": 1, "domain": "example.com"}

    plugin_result = asyncio.run(chain.async_refresh_plugin_torrents(mtype=MediaType.MOVIE))
    site_result = asyncio.run(chain.async_refresh_site_torrents(site=site, mtype=MediaType.MUSIC))

    assert [item.title for item in plugin_result] == ["Plugin.Movie.2026.1080p"]
    assert site_result == []
    dispatcher.async_execute_plugin_modules.assert_awaited_once_with(
        "async_refresh_torrents", None, site={}, keyword=None, cat=None, page=0, mtype=MediaType.MOVIE
    )
    dispatcher.async_execute_system_modules.assert_awaited_once_with(
        "async_refresh_torrents", None, site=site, keyword=None, cat=None, page=0, mtype=MediaType.MUSIC
    )


def test_browse_uses_site_only_refresh_entry():
    """站点浏览只调用站点专用入口，避免插件被按站点逐个调用。"""
    chain = TorrentsChain()
    chain.refresh_site_torrents = Mock(return_value=[])
    chain.refresh_plugin_torrents = Mock(return_value=[])
    sites_helper = Mock()
    sites_helper.get_indexer.return_value = {"id": 1, "domain": "example.com"}

    with patch("app.chain.torrents.SitesHelper", return_value=sites_helper):
        result = chain.browse(domain="example.com", page=1)

    assert result == []
    chain.refresh_site_torrents.assert_called_once_with(
        site={"id": 1, "domain": "example.com"}, keyword=None, cat=None, page=1, mtype=None
    )
    chain.refresh_plugin_torrents.assert_not_called()


def _saving_chain(plugin_torrents):
    """构造打桩了缓存读写、站点目录与识别链的刷新链，返回链与保存快照。"""
    chain = TorrentsChain()
    saved = {}
    patches = (
        patch.object(chain, "load_cache", return_value=None),
        patch.object(
            chain,
            "save_cache",
            side_effect=lambda data, filename: saved.__setitem__(filename, copy.deepcopy(data)),
        ),
        patch.object(chain, "refresh_plugin_torrents", return_value=plugin_torrents),
        patch.object(chain, "browse", return_value=[]),
        patch("app.chain.torrents.MediaChain"),
    )
    return chain, saved, patches


def test_refresh_stores_plugin_torrents_without_sites():
    """没有 PT 站点时插件资源仍进入独立缓存桶并返回给订阅匹配。"""
    chain, saved, patches = _saving_chain([_plugin_torrent()])
    sites_helper = Mock()
    sites_helper.get_indexers.return_value = []
    facts = []

    with (
        patches[0], patches[1], patches[2] as plugin_refresh, patches[3], patches[4],
        patch("app.chain.torrents.SitesHelper", return_value=sites_helper),
    ):
        result = chain.refresh(stype="spider", site_observer=facts.append)

    plugin_refresh.assert_called_once_with()
    assert [context.torrent_info.title for context in result[PLUGIN_TORRENT_SOURCE_DOMAIN]] == [
        "Plugin.Movie.2026.1080p"
    ]
    assert result[PLUGIN_TORRENT_SOURCE_DOMAIN][0].resource_source == "spider"
    assert PLUGIN_TORRENT_SOURCE_DOMAIN in saved[TorrentsChain._spider_file]
    assert facts == [
        {"id": None, "name": PLUGIN_TORRENT_SOURCE_NAME, "candidates": 1, "status": "checked"}
    ]


def test_refresh_keeps_plugin_bucket_when_filtering_sites():
    """按订阅站点过滤缓存时插件桶必须保留。"""
    chain, _saved, patches = _saving_chain([_plugin_torrent()])
    sites_helper = Mock()
    sites_helper.get_indexers.return_value = [
        {"id": 1, "name": "站点一", "domain": "https://example.com"},
        {"id": 2, "name": "站点二", "domain": "https://other.example"},
    ]

    with (
        patches[0], patches[1], patches[2], patches[3] as browse, patches[4],
        patch("app.chain.torrents.SitesHelper", return_value=sites_helper),
    ):
        result = chain.refresh(stype="spider", sites=[1])

    assert PLUGIN_TORRENT_SOURCE_DOMAIN in result
    assert "other.example" not in result
    # 只刷新了被选中的站点，browse 调用两页中第一页为空即停止
    browse.assert_called_once_with(domain="example.com", page=0)


def test_refresh_without_plugin_results_leaves_cache_untouched():
    """插件没有返回资源时不创建缓存键，也不上报插件事实。"""
    chain, saved, patches = _saving_chain([])
    sites_helper = Mock()
    sites_helper.get_indexers.return_value = []
    facts = []

    with (
        patches[0], patches[1], patches[2], patches[3], patches[4],
        patch("app.chain.torrents.SitesHelper", return_value=sites_helper),
    ):
        result = chain.refresh(stype="rss", site_observer=facts.append)

    assert result == {}
    assert PLUGIN_TORRENT_SOURCE_DOMAIN not in saved[TorrentsChain._rss_file]
    assert facts == []


def test_refresh_splits_plugin_music_into_music_cache():
    """插件返回的音乐分类资源写入音乐缓存文件，不占用影视缓存。"""
    chain, saved, patches = _saving_chain([
        _plugin_torrent(),
        _plugin_torrent(title="Daft Punk - Discovery [FLAC]", category=MediaType.MUSIC.value),
    ])
    sites_helper = Mock()
    sites_helper.get_indexers.return_value = []

    with (
        patches[0], patches[1], patches[2], patches[3], patches[4],
        patch("app.chain.torrents.SitesHelper", return_value=sites_helper),
    ):
        result = chain.refresh(stype="spider")

    titles = {context.torrent_info.title for context in result[PLUGIN_TORRENT_SOURCE_DOMAIN]}
    assert titles == {"Plugin.Movie.2026.1080p", "Daft Punk - Discovery [FLAC]"}
    assert [c.torrent_info.title for c in saved[TorrentsChain._spider_file][PLUGIN_TORRENT_SOURCE_DOMAIN]] == [
        "Plugin.Movie.2026.1080p"
    ]
    assert [c.torrent_info.title for c in saved[TorrentsChain._music_spider_file][PLUGIN_TORRENT_SOURCE_DOMAIN]] == [
        "Daft Punk - Discovery [FLAC]"
    ]
