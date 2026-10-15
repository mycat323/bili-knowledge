"""文件名 slug。

必须与 scripts/build-content.mjs 的 toSlug 逐字节一致：源知识库的文件名
就是站点 URL 的最后一段，两边算法不一致会让双链和反链指向不存在的页面。
行为已用现有知识库的真实文件名（含超长截断 + hash 分支）回归验证。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

SLUG_MAX_BYTES = 90

_ILLEGAL = re.compile(r'[<>:"/\\|?*#&%]')
_WHITESPACE = re.compile(r"[\s\u3000]+")
_DASH_RUN = re.compile(r"-+")


def to_slug(name: str) -> str:
    full = name
    if full.lower().endswith(".md"):
        full = full[:-3]

    # 注意顺序：这批字符是「删除」而不是「替换成 -」，与 JS 版保持一致。
    full = _ILLEGAL.sub("", full)
    full = _WHITESPACE.sub("-", full)
    full = "".join("-" if unicodedata.category(ch)[0] in "PS" else ch for ch in full)
    full = _DASH_RUN.sub("-", full)
    full = full.strip("-")
    full = full.lower()

    if len(full.encode("utf-8")) <= SLUG_MAX_BYTES:
        return full

    # 超长时截断并追加短 hash 保证唯一性（Linux 文件名上限 255 字节，
    # 中文每字符 3 字节，构建时还会再拼目录前缀与资源 hash）。
    digest = hashlib.md5(full.encode("utf-8")).hexdigest()[:8]
    truncated = ""
    for ch in full:
        if len((truncated + ch).encode("utf-8")) > SLUG_MAX_BYTES - 9:
            break
        truncated += ch
    return f"{_DASH_RUN.sub('-', truncated).rstrip('-')}-{digest}"
