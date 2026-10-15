"""把生成结果写成语料库目录结构。

产出目录与 build-content.mjs 的 SOURCE_SECTIONS 严格对应：

    知识库/<UP主目录>/
    ├── 00_raw/        原始转写稿，不发布，只作追溯
    ├── 01_articles/   逐视频文章
    ├── 02_atoms/      原子笔记
    └── 04_knowledge/  体系化长文（章节）

文件名一律走 to_slug，保证与站点 URL 一致。
"""

from __future__ import annotations

import re
from pathlib import Path

from .slug import to_slug

SECTIONS = {
    "raw": "00_raw",
    "articles": "01_articles",
    "atoms": "02_atoms",
    "knowledge": "04_knowledge",
}

_CN_NUMERALS = "零一二三四五六七八九十"


class VaultError(RuntimeError):
    pass


def chinese_numeral(value: int) -> str:
    if value <= 0 or value > 99:
        return str(value)
    if value <= 10:
        return _CN_NUMERALS[value]
    tens, ones = divmod(value, 10)
    head = "十" if tens == 1 else f"{_CN_NUMERALS[tens]}十"
    return head if ones == 0 else f"{head}{_CN_NUMERALS[ones]}"


def ensure_sections(vault_dir: Path) -> None:
    for name in SECTIONS.values():
        (vault_dir / name).mkdir(parents=True, exist_ok=True)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return path


def _clean_inline(text: str) -> str:
    """压掉换行与多余空格，用于必须单行的字段。"""
    return re.sub(r"\s+", " ", (text or "").strip())


def write_transcript(vault_dir: Path, bvid: str, title: str, transcript: str) -> Path:
    """原始转写稿落在 00_raw，不参与站点构建，仅用于事后核对。"""
    header = f"# {title}\n\n- BV 号：{bvid}\n- 来源：https://www.bilibili.com/video/{bvid}\n\n---\n\n"
    return _write(vault_dir / SECTIONS["raw"] / f"{to_slug(title)}.md", header + transcript)


def write_article(
    vault_dir: Path, title: str, bvid: str, summary: str, body: str
) -> Path:
    frontmatter = f"---\ntitle: {_clean_inline(title)}\nBV号: {bvid}\n---\n\n"
    content = f"{frontmatter}# {_clean_inline(title)}\n\n**摘要**：{_clean_inline(summary)}\n\n{body}"
    return _write(vault_dir / SECTIONS["articles"] / f"{to_slug(title)}.md", content)


def validate_atom_name(name: str) -> None:
    """原子笔记名必须已经是规范 slug，否则分组与双链都会错位。"""
    if not name or "-" not in name:
        raise VaultError(f"原子笔记名必须以「域-子题-概念」三段命名，当前为 {name!r}")
    parts = [part for part in name.split("-") if part]
    if len(parts) < 3:
        raise VaultError(f"原子笔记名少于三段，站点分组会失效：{name!r}")
    if to_slug(name) != name:
        raise VaultError(
            f"原子笔记名不是规范 slug（含大写、空格或标点）：{name!r} → {to_slug(name)!r}"
        )


def write_atom(
    vault_dir: Path,
    *,
    name: str,
    title: str,
    domain: str,
    definition: str,
    details: str,
    related: list[str],
    source_title: str,
) -> Path:
    validate_atom_name(name)
    links = "\n".join(f"- [[{item}]]" for item in related if item)
    related_block = f"\n\n## 相关原子\n\n{links}" if links else ""
    frontmatter = (
        f"---\ntitle: {_clean_inline(title)}\n"
        f"标签: [{_clean_inline(domain)}]\n"
        f"来源文章: {_clean_inline(source_title)}\n---\n\n"
    )
    content = (
        f"{frontmatter}# {_clean_inline(title)}\n\n"
        f"## 一句话定义\n\n{definition.strip()}\n\n"
        f"## 详细说明\n\n{details.strip()}"
        f"{related_block}"
    )
    return _write(vault_dir / SECTIONS["atoms"] / f"{name}.md", content)


def write_knowledge_chapter(
    vault_dir: Path,
    *,
    index: int,
    title: str,
    intro: str,
    sections: list[dict],
    known_names: set[str] | None = None,
) -> Path:
    heading = f"第{chinese_numeral(index)}章 · {title}"
    body_parts = [f"# {heading}\n\n> {resolve_wikilinks(intro.strip(), known_names)}"]
    position = 0
    for section in sections:
        title_text = _clean_inline(section.get("heading") or "").lstrip("# ").strip()
        if not title_text:
            continue
        position += 1
        # 模型经常自己就写成 `1.1 xxx` 了。已经带编号的不要再加一层，
        # 否则会渲染出 `## 1.1 1.1 xxx`。
        if not re.match(r"^\d+([.．]\d+)*[.．、\s]", title_text):
            title_text = f"{index}.{position} {title_text}"
        body = resolve_wikilinks((section.get("body") or "").strip(), known_names)
        body_parts.append(f"## {title_text}\n\n{body}")
    content = f"---\ntitle: {heading}\n---\n\n" + "\n\n".join(part for part in body_parts if part)
    return _write(
        vault_dir / SECTIONS["knowledge"] / f"{to_slug(f'{index:02d}-{title}')}.md", content
    )


def clear_knowledge(vault_dir: Path) -> None:
    """重写章节前先清空，避免上一次跑出来的章节变成残留。"""
    directory = vault_dir / SECTIONS["knowledge"]
    if not directory.is_dir():
        return
    for path in directory.glob("*.md"):
        path.unlink()


def resolve_wikilinks(text: str, known_names: set[str] | None) -> str:
    """把指向不存在的原子笔记的双链降级成纯文本。

    模型偶尔会把名字写错（少一个字、换个同义词）。build-content.mjs 对无法
    解析的双链也会退化成纯文本，在生成侧就做掉，语料里就不会留下坏链。
    """
    if not text:
        return text

    def replace(match: re.Match) -> str:
        target = match.group(1).strip()
        display = (match.group(2) or target).strip()
        if known_names is None or target in known_names:
            return match.group(0)
        return display

    return re.sub(r"\[\[([^\]|\n]+)(?:\|([^\]\n]+))?\]\]", replace, text)


def existing_atom_names(vault_dir: Path) -> list[str]:
    """已写入的原子笔记名，用于让新视频的双链能连回旧内容。"""
    atoms_dir = vault_dir / SECTIONS["atoms"]
    if not atoms_dir.is_dir():
        return []
    return sorted(path.stem for path in atoms_dir.glob("*.md"))


def existing_video_bvids(vault_dir: Path) -> set[str]:
    """已入库文章对应的 BV 号，用于增量抓取时跳过。"""
    articles_dir = vault_dir / SECTIONS["articles"]
    if not articles_dir.is_dir():
        return set()
    found: set[str] = set()
    pattern = re.compile(r"^BV号:\s*(\S+)\s*$", re.M)
    for path in articles_dir.glob("*.md"):
        match = pattern.search(path.read_text(encoding="utf-8")[:600])
        if match:
            found.add(match.group(1))
    return found
