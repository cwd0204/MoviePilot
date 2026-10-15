"""Jackett 本地插件测试：Torznab 解析、请求构造与插件资源源守卫，全程离线。"""

import asyncio
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.adapters.network.http import AsyncRequestUtils, RequestUtils
from app.domain.context import TorrentInfo
from app.schemas.types import MediaSource, MediaType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "jackett"


def _expose_local_plugins() -> None:
    """把本地插件仓源码挂到运行时 app.plugins 命名空间，和宿主加载方式一致。"""
    import app.plugins

    source = str(PROJECT_ROOT / "local-plugins" / "plugins.v3")
    if source not in app.plugins.__path__:
        app.plugins.__path__.insert(0, source)


_expose_local_plugins()
from app.plugins.jackett import Jackett, torznab  # noqa: E402


def _fixture(name: str) -> bytes:
    """读取 Jackett 回放数据。"""
    return (FIXTURES / name).read_bytes()


def _local_time(rfc2822: str) -> str:
    """按插件相同规则把 RFC 2822 时间转成本地时间文本，避免测试依赖时区。"""
    return parsedate_to_datetime(rfc2822).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def test_parse_results_maps_torznab_fields():
    """标准 item 映射到 TorrentInfo 字段：下载链接、做种/下载者、因子、身份与分类。"""
    releases = torznab.parse_results(_fixture("search.xml"), default_indexer_name="all")

    assert [item["title"] for item in releases] == [
        "Some.Movie.2026.1080p.BluRay.x264-GRP",
        "Some.Show.S01E02.1080p.WEB-DL",
    ]
    movie = releases[0]
    assert movie["site_name"] == "[Jackett] Demo Tracker"
    assert movie["enclosure"].startswith("http://jackett:9117/dl/demo/?jackett_apikey=KEY")
    assert movie["page_url"] == "https://tracker.example/torrent/1001"
    assert movie["description"] == "Some Movie 2026 中字"
    assert movie["size"] == 1073741824
    assert movie["seeders"] == 10
    assert movie["peers"] == 3
    assert movie["grabs"] == 12
    assert movie["pubdate"] == _local_time("Tue, 14 Oct 2026 10:00:00 +0000")
    assert movie["downloadvolumefactor"] == 0.0
    assert movie["uploadvolumefactor"] == 1.0
    assert movie["media_source"] == MediaSource.IMDb
    assert movie["media_id"] == "tt0123456"
    assert movie["category"] == MediaType.MOVIE.value

    show = releases[1]
    assert show["site_name"] == "[Jackett] Public Tracker"
    assert show["enclosure"] == "magnet:?xt=urn:btih:abcdef"
    assert show["page_url"] is None
    assert show["description"] == ""
    assert show["peers"] == 0
    assert show["grabs"] == 0
    assert show["downloadvolumefactor"] is None
    assert show["media_source"] == MediaSource.TVDB
    assert show["media_id"] == "81189"
    assert show["category"] == MediaType.TV.value


def test_parse_results_raises_on_error_document():
    """Jackett 的 <error> 响应抛出 TorznabError 并带上错误码。"""
    with pytest.raises(torznab.TorznabError) as excinfo:
        torznab.parse_results(_fixture("error.xml"), default_indexer_name="all")
    assert "100" in str(excinfo.value)
    assert "Invalid API Key" in str(excinfo.value)


def test_parse_results_rejects_empty_or_invalid_xml():
    """空响应或非 XML 响应同样以 TorznabError 报告。"""
    with pytest.raises(torznab.TorznabError):
        torznab.parse_results(b"", default_indexer_name="all")
    with pytest.raises(torznab.TorznabError):
        torznab.parse_results(b"<html>login</html", default_indexer_name="all")


def test_parse_indexers_reads_configured_indexers_only():
    """t=indexers 响应解析为索引器列表，跳过 configured=false 的条目。"""
    indexers = torznab.parse_indexers(_fixture("indexers.xml"))

    assert [(item.id, item.name, item.type, item.language) for item in indexers] == [
        ("demo", "Demo Tracker", "private", "en-US"),
        ("pub", "Public Tracker", "public", "zh-CN"),
    ]


def test_build_search_params_by_media_type_and_imdb():
    """关键字、IMDb ID、媒体类型分别映射到 Torznab 参数。"""
    assert torznab.build_search_params("KEY", keyword="Some Movie", mtype=MediaType.MOVIE) == {
        "apikey": "KEY", "t": "search", "q": "Some Movie", "cat": "2000",
    }
    assert torznab.build_search_params("KEY", keyword="tt0123456", mtype=MediaType.TV) == {
        "apikey": "KEY", "t": "tvsearch", "imdbid": "tt0123456", "cat": "5000",
    }
    assert torznab.build_search_params("KEY", keyword="tt0123456", mtype=None) == {
        "apikey": "KEY", "t": "movie", "imdbid": "tt0123456",
    }
    assert torznab.build_search_params("KEY", keyword=None, mtype=MediaType.MUSIC) == {
        "apikey": "KEY", "t": "search", "cat": "3000",
    }
    assert torznab.build_indexers_params("KEY") == {"apikey": "KEY", "t": "indexers", "configured": "true"}


def test_normalize_host_and_torznab_url():
    """地址补协议、去尾斜杠，并拼出单索引器与 all 聚合端点。"""
    assert torznab.normalize_host(" 192.168.1.10:9117/ ") == "http://192.168.1.10:9117"
    assert torznab.normalize_host("https://nas.local/jackett/") == "https://nas.local/jackett"
    assert torznab.normalize_host("") == ""
    assert torznab.torznab_url("http://nas.local/jackett", "demo") == (
        "http://nas.local/jackett/api/v2.0/indexers/demo/results/torznab/api"
    )
    assert torznab.torznab_url("http://nas.local", torznab.ALL_INDEXER_ID).endswith(
        "/indexers/all/results/torznab/api"
    )


def test_map_category_uses_torznab_ranges():
    """2000/5000/3000 大类映射媒体类型，其它值返回 None。"""
    assert torznab.map_category(["2040"]) == MediaType.MOVIE.value
    assert torznab.map_category(["5070", "2000"]) == MediaType.TV.value
    assert torznab.map_category(["3010"]) == MediaType.MUSIC.value
    assert torznab.map_category(["8000", "abc", None]) is None


def _plugin(**overrides) -> Jackett:
    """构造已启用的 Jackett 插件实例。"""
    config = {
        "enabled": True,
        "host": "http://jackett:9117/",
        "api_key": "KEY",
        "indexers": [],
        "timeout": 30,
        "proxy": False,
        "priority": 5,
    }
    config.update(overrides)
    plugin = Jackett()
    plugin.init_plugin(config)
    return plugin


def _response(content: bytes, status_code: int = 200) -> SimpleNamespace:
    """构造最小 HTTP 响应替身。"""
    return SimpleNamespace(status_code=status_code, content=content, text=content.decode("utf-8", "ignore"))


def test_init_plugin_normalizes_config():
    """地址去尾斜杠、超时夹在范围内、优先级与索引器列表规范化。"""
    plugin = _plugin(host="nas.local:9117/", timeout="2", priority="3", indexers=["demo", " ", 7])

    assert plugin.get_state() is True
    assert plugin._host == "http://nas.local:9117"
    assert plugin._timeout == 5
    assert plugin._priority == 3
    assert plugin._indexers == ["demo", "7"]
    assert _plugin(timeout="abc")._timeout == 30
    assert _plugin(timeout=9999)._timeout == 300
    assert _plugin(api_key="").get_state() is False
    assert _plugin(enabled=False).get_state() is False


def test_get_module_declares_search_and_refresh_hooks():
    """插件以 get_module 声明四个资源源方法。"""
    plugin = _plugin()
    module = plugin.get_module()

    assert set(module) == {
        "search_torrents", "async_search_torrents", "refresh_torrents", "async_refresh_torrents",
    }
    assert module["search_torrents"] == plugin.search_torrents


def test_search_torrents_queries_all_endpoint_and_builds_torrent_info():
    """未选索引器时请求 all 聚合端点，并把结果投影为带插件来源信息的 TorrentInfo。"""
    plugin = _plugin()
    calls = []

    def fake_get_res(self, url, params=None, **_kwargs):
        calls.append((url, params))
        return _response(_fixture("search.xml"))

    with patch.object(RequestUtils, "get_res", fake_get_res):
        results = plugin.search_torrents(site={}, keyword="Some Movie", mtype=MediaType.MOVIE, page=0)

    assert calls == [(
        "http://jackett:9117/api/v2.0/indexers/all/results/torznab/api",
        {"apikey": "KEY", "t": "search", "q": "Some Movie", "cat": "2000"},
    )]
    assert len(results) == 2
    movie = results[0]
    assert isinstance(movie, TorrentInfo)
    assert movie.site is None
    assert movie.site_name == "[Jackett] Demo Tracker"
    assert movie.site_order == 5
    assert movie.site_proxy is False
    assert movie.site_ua
    assert movie.enclosure.startswith("http://jackett:9117/dl/demo/")
    assert movie.media_source == MediaSource.IMDb
    assert movie.media_id == "tt0123456"
    assert movie.volume_factor == "免费"
    assert results[1].enclosure.startswith("magnet:")


def test_search_torrents_guards_skip_requests():
    """未启用、空关键字、非首页或按站点调用时不发请求。"""
    with patch.object(RequestUtils, "get_res") as get_res:
        assert _plugin(enabled=False).search_torrents(site={}, keyword="x") == []
        plugin = _plugin()
        assert plugin.search_torrents(site={}, keyword="") == []
        assert plugin.search_torrents(site={}, keyword="x", page=1) == []
        assert plugin.search_torrents(site={"id": 1, "domain": "pt.example"}, keyword="x") == []
        assert plugin.refresh_torrents(site={"id": 1, "domain": "pt.example"}) == []
        assert plugin.refresh_torrents(site={}, page=1) == []
    get_res.assert_not_called()


def test_selected_indexers_are_queried_separately_and_failures_isolated():
    """选中多个索引器时逐个请求，单个无响应或错误不影响其它索引器。"""
    plugin = _plugin(indexers=["demo", "down", "bad"])

    def fake_get_res(self, url, params=None, **_kwargs):
        if "/indexers/demo/" in url:
            return _response(_fixture("search.xml"))
        if "/indexers/bad/" in url:
            return _response(_fixture("error.xml"))
        return None

    with patch.object(RequestUtils, "get_res", fake_get_res):
        results = plugin.search_torrents(site={}, keyword="Some Movie")

    assert len(results) == 2
    assert {item.site_name for item in results} == {"[Jackett] Demo Tracker", "[Jackett] Public Tracker"}


def test_search_torrents_handles_http_errors_and_exceptions():
    """非 200 状态码或请求异常都返回空列表而不抛出。"""
    plugin = _plugin()
    with patch.object(RequestUtils, "get_res", return_value=_response(b"", status_code=401)):
        assert plugin.search_torrents(site={}, keyword="x") == []
    with patch.object(RequestUtils, "get_res", side_effect=RuntimeError("boom")):
        assert plugin.search_torrents(site={}, keyword="x") == []


def test_refresh_torrents_requests_latest_without_keyword():
    """刷新最新种子时不带 q，按媒体类型附带分类。"""
    plugin = _plugin()
    calls = []

    def fake_get_res(self, url, params=None, **_kwargs):
        calls.append(params)
        return _response(_fixture("search.xml"))

    with patch.object(RequestUtils, "get_res", fake_get_res):
        results = plugin.refresh_torrents(site={}, mtype=MediaType.TV)

    assert calls == [{"apikey": "KEY", "t": "search", "cat": "5000"}]
    assert len(results) == 2


def test_async_search_torrents_merges_indexers():
    """异步搜索并发请求选中的索引器并合并结果。"""
    plugin = _plugin(indexers=["demo", "pub"])
    seen_urls = []

    async def fake_get_res(self, url, params=None, **_kwargs):
        seen_urls.append(url)
        return _response(_fixture("search.xml"))

    with patch.object(AsyncRequestUtils, "get_res", fake_get_res):
        results = asyncio.run(plugin.async_search_torrents(site={}, keyword="Some Movie"))

    assert sorted(seen_urls) == [
        "http://jackett:9117/api/v2.0/indexers/demo/results/torznab/api",
        "http://jackett:9117/api/v2.0/indexers/pub/results/torznab/api",
    ]
    assert len(results) == 4


def test_async_refresh_torrents_isolates_failures():
    """异步刷新时单个索引器异常不影响其它索引器。"""
    plugin = _plugin(indexers=["demo", "down"])

    async def fake_get_res(self, url, params=None, **_kwargs):
        if "/indexers/down/" in url:
            raise RuntimeError("timeout")
        return _response(_fixture("search.xml"))

    with patch.object(AsyncRequestUtils, "get_res", fake_get_res):
        results = asyncio.run(plugin.async_refresh_torrents(site={}))

    assert len(results) == 2


def _find_components(nodes, component):
    """递归收集指定组件名的 JSON 节点。"""
    found = []
    for node in nodes or []:
        if node.get("component") == component:
            found.append(node)
        found.extend(_find_components(node.get("content"), component))
    return found


def test_get_form_lists_discovered_indexers_and_connection_status():
    """表单的索引器下拉来自 Jackett 发现结果，顶部提示显示连接成功与数量。"""
    plugin = _plugin(indexers=["legacy"])

    with patch.object(RequestUtils, "get_res", return_value=_response(_fixture("indexers.xml"))):
        form, defaults = plugin.get_form()

    select = _find_components(form, "VSelect")[0]
    assert select["props"]["model"] == "indexers"
    assert [item["value"] for item in select["props"]["items"]] == ["demo", "pub", "legacy"]
    alert = _find_components(form, "VAlert")[0]
    assert alert["props"]["type"] == "success"
    assert "2" in alert["props"]["text"]
    assert defaults == {
        "enabled": False, "host": "", "api_key": "", "indexers": [], "timeout": 30, "proxy": False, "priority": 0,
    }
    assert {field["props"]["model"] for field in _find_components(form, "VTextField")} == {
        "host", "api_key", "timeout", "priority",
    }
    assert {field["props"]["model"] for field in _find_components(form, "VSwitch")} == {"enabled", "proxy"}
    assert plugin.get_data("indexers") == [
        {"id": "demo", "name": "Demo Tracker", "type": "private", "language": "en-US"},
        {"id": "pub", "name": "Public Tracker", "type": "public", "language": "zh-CN"},
    ]


def test_get_form_reports_connection_failure_and_falls_back_to_cache():
    """连接失败时提示为警告，并沿用上次缓存的索引器列表。"""
    plugin = _plugin()
    plugin.save_data("indexers", [{"id": "cached", "name": "Cached", "type": "public", "language": ""}])

    with patch.object(RequestUtils, "get_res", return_value=None):
        form, _defaults = plugin.get_form()

    alert = _find_components(form, "VAlert")[0]
    assert alert["props"]["type"] == "warning"
    assert "无响应" in alert["props"]["text"]
    select = _find_components(form, "VSelect")[0]
    assert [item["value"] for item in select["props"]["items"]] == ["cached"]


def test_get_page_renders_indexer_table():
    """详情页列出索引器名称、类型、语言以及是否纳入搜索。"""
    plugin = _plugin(indexers=["pub"])

    with patch.object(RequestUtils, "get_res", return_value=_response(_fixture("indexers.xml"))):
        page = plugin.get_page()

    rows = _find_components(page, "tr")
    cells = [[cell.get("text") for cell in row["content"]] for row in rows[1:]]
    assert cells == [
        ["Demo Tracker", "demo", "private", "en-US", "否"],
        ["Public Tracker", "pub", "public", "zh-CN", "是"],
    ]


def test_get_page_shows_alert_when_unconfigured():
    """未配置时详情页只给出提示。"""
    plugin = Jackett()
    plugin.init_plugin({})

    page = plugin.get_page()

    assert _find_components(page, "VAlert")[0]["props"]["type"] == "info"
