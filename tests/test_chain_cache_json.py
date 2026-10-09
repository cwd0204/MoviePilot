"""Chain 文件缓存 JSON 编解码器的往返与拒绝契约测试。"""

import json
import pickle
from pathlib import Path

import pytest

from app.chain.cache import JsonChainCacheCodec
from app.domain.context import Context, MediaInfo, MusicInfo, SubtitleInfo, TorrentInfo
from app.domain.meta.metaanime import MetaAnime
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.domain.meta.metavideo import MetaVideo
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaSource, MediaType


@pytest.fixture
def codec() -> JsonChainCacheCodec:
    """被测编解码器。"""
    return JsonChainCacheCodec()


def _video_context() -> Context:
    """带已修正季集与目标媒体回填的影视候选。"""
    meta = MetaInfo("Some.Show.S01E02.1080p.WEB-DL.H264-GROUP")
    meta.begin_season = 2
    meta.apply_words = ["替换词"]
    media = MediaInfo(
        type=MediaType.TV,
        title="Some Show",
        year="2024",
        tmdb_id=123,
        tmdb_info={"id": 123, "name": "Some Show"},
    )
    media.seasons = {1: [1, 2, 3], 2: [1]}
    torrent = TorrentInfo(
        site=1,
        site_name="站点",
        title="Some.Show.S01E02.1080p.WEB-DL.H264-GROUP",
        enclosure="https://example.test/t.torrent",
        size=1024,
        seeders=5,
    )
    return Context(
        meta_info=meta,
        media_info=media,
        torrent_info=torrent,
        resource_source="spider",
        match_source="tmdb",
        candidate_recognized=True,
        allowed_episodes={2, 5},
        selected_episodes=[2],
    )


def test_roundtrip_site_torrents_cache(codec: JsonChainCacheCodec) -> None:
    """站点种子缓存（域名 -> Context 列表）原样往返，嵌套对象类型与修正后的状态保留。"""
    payload = {"site.example": [_video_context()]}

    data = codec.dumps(payload)
    restored = codec.loads(data)

    assert json.loads(data)  # 纯 JSON
    context = restored["site.example"][0]
    assert isinstance(context, Context)
    assert isinstance(context.meta_info, MetaVideo)
    assert context.meta_info.begin_season == 2
    assert context.meta_info.begin_episode == 2
    assert context.meta_info.apply_words == ["替换词"]
    assert context.meta_info.type is MediaType.TV
    assert context.meta_info.season_episode == payload["site.example"][0].meta_info.season_episode
    assert isinstance(context.media_info, MediaInfo)
    assert context.media_info.tmdb_id == 123
    assert context.media_info.tmdb_info == {"id": 123, "name": "Some Show"}
    assert context.media_info.seasons == {1: [1, 2, 3], 2: [1]}
    assert context.media_info.type is MediaType.TV
    assert isinstance(context.torrent_info, TorrentInfo)
    assert context.torrent_info.enclosure == "https://example.test/t.torrent"
    assert context.torrent_info.seeders == 5
    assert context.allowed_episodes == {2, 5}
    assert context.selected_episodes == [2]
    assert context.resource_source == "spider"
    assert context.candidate_recognized is True


def test_roundtrip_preserves_episode_tristate(codec: JsonChainCacheCodec) -> None:
    """allowed_episodes 的 None / 空集 / 非空集三态在往返后保持。"""
    contexts = [
        Context(allowed_episodes=None),
        Context(allowed_episodes=set()),
        Context(allowed_episodes={1}),
    ]

    restored = codec.loads(codec.dumps(contexts))

    assert restored[0].allowed_episodes is None
    assert restored[1].allowed_episodes == set()
    assert restored[2].allowed_episodes == {1}


def test_roundtrip_anime_and_music_contexts(codec: JsonChainCacheCodec) -> None:
    """动漫与音乐解析结果按原类型还原，音乐媒体信息走 MusicInfo.from_dict。"""
    anime_meta = MetaInfo("[Sub] Some Anime - 05 [1080p].mkv")
    music_meta = MetaInfo("Artist - Song.flac", mtype=MediaType.MUSIC)
    music = MusicInfo(title="Song", artists=["Artist"], media_source="musicbrainz", media_id="rec-1")
    contexts = [Context(meta_info=anime_meta), Context(meta_info=music_meta, media_info=music)]

    restored = codec.loads(codec.dumps(contexts))

    assert isinstance(restored[0].meta_info, MetaAnime)
    assert restored[0].meta_info.org_string == anime_meta.org_string
    assert restored[0].meta_info.begin_episode == anime_meta.begin_episode
    assert isinstance(restored[1].meta_info, MetaMusic)
    assert restored[1].meta_info.type is MediaType.MUSIC
    assert isinstance(restored[1].media_info, MusicInfo)
    assert restored[1].media_info.media_id == "rec-1"
    assert restored[1].media_info.artists == ["Artist"]


def test_roundtrip_subtitle_results_and_plain_payloads(codec: JsonChainCacheCodec) -> None:
    """字幕结果列表、搜索参数字典与重启标记等普通载荷同样可往返。"""
    subtitle = SubtitleInfo(site=1, title="Some.Show.S01E02.chs.srt", language="简体", size=12.5)
    params = {"keyword": "show", "type": "电视剧", "sites": "1,2"}
    marker = {"channel": "telegram", "userid": 10001}

    assert codec.loads(codec.dumps(params)) == params
    assert codec.loads(codec.dumps(marker)) == marker
    assert codec.loads(codec.dumps(42)) == 42
    restored = codec.loads(codec.dumps([subtitle]))
    assert isinstance(restored[0], SubtitleInfo)
    assert restored[0].title == subtitle.title
    assert restored[0].size == 12.5


def test_containers_enums_and_paths_keep_semantics(codec: JsonChainCacheCodec) -> None:
    """非字符串键字典、集合、枚举与路径通过标签保留语义。"""
    value = {
        "ints": {1: "a", 2: "b"},
        "set": {3, 1, 2},
        "enum": MediaType.MOVIE,
        "source": MediaSource.TMDB,
        "path": Path("/tmp/x"),
        "nested": [{"k": (1, 2)}],
    }

    restored = codec.loads(codec.dumps(value))

    assert restored["ints"] == {1: "a", 2: "b"}
    assert restored["set"] == {1, 2, 3}
    assert restored["enum"] is MediaType.MOVIE
    assert restored["source"] is MediaSource.TMDB
    assert restored["path"] == Path("/tmp/x")
    assert restored["nested"] == [{"k": [1, 2]}]


def test_rejects_legacy_pickle_and_unknown_tags(codec: JsonChainCacheCodec) -> None:
    """旧版 pickle 载荷与未登记标签/枚举/类型一律抛 ValueError。"""
    with pytest.raises(ValueError):
        codec.loads(pickle.dumps({"a": 1}))
    with pytest.raises(ValueError):
        codec.loads(b"PICKLE1\x00deadbeef\x00" + pickle.dumps({"a": 1}))
    with pytest.raises(ValueError):
        codec.loads(json.dumps({"__mp__": "Whatever", "data": {}}).encode("utf-8"))
    with pytest.raises(ValueError):
        codec.loads(json.dumps({"__mp__": "enum", "cls": "NotAnEnum", "value": 1}).encode("utf-8"))
    with pytest.raises(ValueError):
        codec.loads(json.dumps({"__mp__": "MetaBase", "cls": "Evil", "data": {}}).encode("utf-8"))


def test_unsupported_object_raises_type_error(codec: JsonChainCacheCodec) -> None:
    """未登记对象不会被静默降级，直接报 TypeError。"""

    class _Opaque:
        """任意非领域对象。"""

    with pytest.raises(TypeError):
        codec.dumps({"x": _Opaque()})


def test_metabase_restore_state_skips_derived_and_restores_enums() -> None:
    """restore_state 不重新解析，派生字段忽略，枚举按值还原。"""
    meta = MetaInfo("Movie.Name.2020.1080p.BluRay.x264-GRP.mkv")
    meta.year = "2021"
    data = meta.to_dict()
    assert data["type"] == meta.type.value
    data["media_source"] = MediaSource.TMDB.value

    restored = type(meta).restore_state(data)

    assert isinstance(restored, MetaBase)
    assert type(restored) is type(meta)
    assert restored.year == "2021"
    assert restored.type is meta.type
    assert restored.media_source is MediaSource.TMDB
    assert restored.name == meta.name
    assert "season_episode" not in vars(restored)
