"""Chain 文件缓存的 JSON 编解码器：领域对象按类型标签编码，不使用 pickle。"""

import json
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from app.domain.context import Context, MediaInfo, MusicInfo, SubtitleInfo, TorrentInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.types import MediaSource, MediaType

# 标签键；普通字典不会使用该键
_TAG = "__mp__"
# MediaInfo.to_dict 为减小体积会丢弃的原始来源字典，缓存需要原样保留
_MEDIA_RAW_INFO_KEYS = ("tmdb_info", "douban_info", "bangumi_info", "anilist_info")
# 允许按值还原的枚举
_ENUMS: dict[str, type[Enum]] = {
    MediaType.__name__: MediaType,
    MediaSource.__name__: MediaSource,
}


class JsonChainCacheCodec:
    """
    把 Chain 缓存里的 Context 集合及其嵌套领域对象编码为带类型标签的 JSON。

    对象通过各自的 ``to_dict`` 输出编码，回读时用 ``Context.from_dict``、既有 ``from_dict``
    与 ``MetaBase.restore_state`` 还原；非字符串键的字典、集合、枚举、路径与日期用标签保留
    语义。遇到未登记的对象类型抛出 TypeError，由调用方记录并放弃本次缓存，不做静默降级。
    """

    def dumps(self, value: Any) -> bytes:
        """编码为 UTF-8 JSON 字节。"""
        return json.dumps(self._encode(value), ensure_ascii=False).encode("utf-8")

    def loads(self, data: bytes) -> Any:
        """解码；非 JSON（含旧版 pickle 载荷）或未知标签抛出 ValueError。"""
        try:
            payload = json.loads(data)
        except (UnicodeDecodeError, ValueError) as err:
            raise ValueError("缓存载荷不是 JSON，拒绝解码") from err
        return self._decode(payload)

    def _encode(self, value: Any) -> Any:
        """递归编码：枚举与基础类型直出，领域对象与容器分别带标签。"""
        # str/int 派生的枚举也是基础类型实例，必须先于基础类型判断
        if isinstance(value, Enum):
            return {_TAG: "enum", "cls": type(value).__name__, "value": self._encode(value.value)}
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        encoded = self._encode_domain(value)
        if encoded is not None:
            return encoded
        return self._encode_container(value)

    def _encode_domain(self, value: Any) -> Optional[dict[str, Any]]:
        """领域对象先转字典再带类型标签；非领域对象返回 None。"""
        if isinstance(value, Context):
            payload = value.to_dict()
            # 嵌套对象保留实例，交给递归编码带上各自标签
            payload["meta_info"] = value.meta_info
            payload["media_info"] = value.media_info
            payload["torrent_info"] = value.torrent_info
            return {_TAG: "Context", "data": self._encode(payload)}
        if isinstance(value, MediaInfo):
            payload = value.to_dict()
            for key in _MEDIA_RAW_INFO_KEYS:
                raw = getattr(value, key, None)
                if raw:
                    payload[key] = raw
            return {_TAG: "MediaInfo", "data": self._encode(payload)}
        for tag, cls in (("MusicInfo", MusicInfo), ("TorrentInfo", TorrentInfo), ("SubtitleInfo", SubtitleInfo)):
            if isinstance(value, cls):
                return {_TAG: tag, "data": self._encode(value.to_dict())}
        if isinstance(value, MetaBase):
            return {_TAG: "MetaBase", "cls": type(value).__name__, "data": self._encode(value.to_dict())}
        return None

    def _encode_container(self, value: Any) -> Any:
        """容器、路径与日期编码；非字符串键字典与集合用标签保留语义。"""
        if isinstance(value, dict):
            if _TAG not in value and all(isinstance(key, str) for key in value):
                return {key: self._encode(item) for key, item in value.items()}
            return {_TAG: "pairs", "items": [[self._encode(key), self._encode(item)] for key, item in value.items()]}
        if isinstance(value, (list, tuple)):
            return [self._encode(item) for item in value]
        if isinstance(value, (set, frozenset)):
            return {_TAG: "set", "items": [self._encode(item) for item in sorted(value, key=repr)]}
        if isinstance(value, Path):
            return {_TAG: "path", "value": str(value)}
        if isinstance(value, datetime):
            return {_TAG: "datetime", "value": value.isoformat()}
        if isinstance(value, date):
            return {_TAG: "date", "value": value.isoformat()}
        raise TypeError(f"Chain 缓存不支持的对象类型：{type(value).__name__}")

    def _decode(self, value: Any) -> Any:
        """递归解码，带标签的字典还原为对应对象。"""
        if isinstance(value, list):
            return [self._decode(item) for item in value]
        if isinstance(value, dict):
            tag = value.get(_TAG)
            if tag is None:
                return {key: self._decode(item) for key, item in value.items()}
            return self._decode_tagged(str(tag), value)
        return value

    def _decode_tagged(self, tag: str, value: dict[str, Any]) -> Any:
        """按标签还原容器、枚举与领域对象。"""
        if tag == "pairs":
            return {self._decode(key): self._decode(item) for key, item in value.get("items", [])}
        if tag == "set":
            return set(self._decode(value.get("items", [])))
        if tag == "enum":
            enum_cls = _ENUMS.get(str(value.get("cls")))
            if enum_cls is None:
                raise ValueError(f"未登记的缓存枚举类型：{value.get('cls')}")
            return enum_cls(self._decode(value.get("value")))
        if tag == "path":
            return Path(str(value.get("value")))
        if tag == "datetime":
            return datetime.fromisoformat(str(value.get("value")))
        if tag == "date":
            return date.fromisoformat(str(value.get("value")))
        data = self._decode(value.get("data"))
        if not isinstance(data, dict):
            raise ValueError(f"缓存标签 {tag} 的载荷不是字典")
        return self._decode_object(tag, value.get("cls"), data)

    @staticmethod
    def _decode_object(tag: str, class_name: Optional[Any], data: dict[str, Any]) -> Any:
        """用领域对象自身的恢复入口重建实例。"""
        if tag == "Context":
            return Context.from_dict(data)
        if tag == "MediaInfo":
            info = MediaInfo()
            info.from_dict(data)
            return info
        if tag == "MusicInfo":
            return MusicInfo.from_dict(data)
        if tag == "TorrentInfo":
            torrent = TorrentInfo()
            torrent.from_dict(data)
            return torrent
        if tag == "SubtitleInfo":
            subtitle = SubtitleInfo()
            subtitle.from_dict(data)
            return subtitle
        if tag == "MetaBase":
            meta_cls = _meta_classes().get(str(class_name))
            if meta_cls is None:
                raise ValueError(f"未登记的识别信息类型：{class_name}")
            return meta_cls.restore_state(data)
        raise ValueError(f"未知缓存标签：{tag}")


def _meta_classes() -> dict[str, type[MetaBase]]:
    """按类名索引已加载的 MetaBase 子类（MetaVideo / MetaAnime / MetaMusic）。"""
    return {cls.__name__: cls for cls in MetaBase.__subclasses__()}
