"""规范化序列化与内容哈希。

发布快照以同一套规范化规则序列化，保证"同一依赖闭包同一哈希、
不同依赖闭包不同哈希"，冲突仲裁因此是确定性的。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: Any) -> str:
    """生成跨进程稳定的规范化 JSON 文本。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    """计算课程方案内容的 sha256 哈希（十六进制）。"""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
