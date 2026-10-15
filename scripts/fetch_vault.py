#!/usr/bin/env python3
"""从一个 B 站 UP 主的 UID 抓取视频，生成三层结构的知识库语料。

    # 首次：抓最新 5 条（会自动跑完 列表 → 音频 → 转写 → 文章/原子笔记 → 体系长文）
    .venv/Scripts/python.exe scripts/fetch_vault.py --uid 254463269 --vault bidao --limit 5

    # 只看会抓到什么，不下载不调用模型
    .venv/Scripts/python.exe scripts/fetch_vault.py --uid 254463269 --vault bidao --limit 5 --dry-run

    # 转写很贵，重复运行时会自动复用 .cache/ 里的结果
    .venv/Scripts/python.exe scripts/fetch_vault.py --uid 254463269 --vault bidao --limit 5 --refresh-llm

产出写到 `<仓库上级>/知识库/<UP主目录>/`，这是 `npm run sync` 读取的位置。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from lib import bilibili, prompts, vault
from lib.llm import chat_json
from lib.settings import CACHE_ROOT, ConfigError, Settings, load_settings, read_vault_registry
from lib.slug import to_slug
from lib.transcribe import transcribe


def log(message: str = "") -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------- 缓存


def cache_dir(key: str) -> Path:
    path = CACHE_ROOT / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_cache(key: str, name: str) -> dict | None:
    path = cache_dir(key) / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log(f"    [警告] 缓存 {path.name} 损坏，将重新生成")
        return None


def write_cache(key: str, name: str, value: object) -> None:
    (cache_dir(key) / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_text_cache(key: str, name: str) -> str | None:
    path = cache_dir(key) / name
    return path.read_text(encoding="utf-8") if path.exists() else None


# ------------------------------------------------------------------- 单条视频处理


def process_video(
    brief: bilibili.VideoBrief,
    vault_dir: Path,
    settings: Settings,
    *,
    refresh_llm: bool,
    knowledge_only_skip_stt: bool,
) -> dict | None:
    """返回该视频的小结，供体系长文阶段使用；跳过时返回 None。"""
    key = brief.bvid
    log(f"\n▶ {brief.title}")
    log(f"  {brief.bvid} · {brief.duration // 60}分{brief.duration % 60}秒 · {brief.view:,} 播放")

    detail = read_cache(key, "meta.json")
    if not detail:
        fresh = bilibili.video_detail(brief.bvid)
        detail = asdict(fresh)
        write_cache(key, "meta.json", detail)
    if len(detail["parts"]) > 1:
        log(f"  [跳过] 多 P 视频（{len(detail['parts'])} P）暂不支持，避免只入库一部分内容")
        return None
    cid = detail["parts"][0]["cid"]

    # 1. 转写（最贵的一步，优先命中缓存）
    transcript = read_text_cache(key, "transcript.txt")
    if transcript:
        log(f"  转写：命中缓存（{len(transcript)} 字）")
    elif knowledge_only_skip_stt:
        log("  [跳过] --no-stt 且没有转写缓存")
        return None
    else:
        audio = cache_dir(key) / "audio.mp3"
        if not audio.exists():
            log("  下载音频…")
            bilibili.download_audio(brief.bvid, cid, audio)
            log(f"  音频：{audio.stat().st_size / 1_000_000:.1f} MB")
        log("  语音转写…")
        started = time.time()
        transcript = transcribe(audio, cache_dir(key), settings, log=log)
        (cache_dir(key) / "transcript.txt").write_text(transcript, encoding="utf-8")
        log(f"  转写完成：{len(transcript)} 字，用时 {time.time() - started:.0f}s")

    # 2. 逐视频文章
    article = None if refresh_llm else read_cache(key, "article.json")
    if not article:
        log("  生成文章…")
        article = chat_json(
            prompts.article_prompt(brief.title, detail["uploader"], transcript),
            settings,
            log=log,
        )
        write_cache(key, "article.json", article)
    log(f"  文章：{len(article.get('body') or '')} 字")

    # 3. 原子笔记（带上已有笔记名，让双链能跨视频连接）
    atoms = None if refresh_llm else read_cache(key, "atoms.json")
    if not atoms:
        log("  生成原子笔记…")
        atoms = chat_json(
            prompts.atoms_prompt(
                brief.title,
                transcript,
                article.get("summary") or "",
                vault.existing_atom_names(vault_dir),
            ),
            settings,
            log=log,
        )
        write_cache(key, "atoms.json", atoms)
    items = atoms.get("atoms") or []
    log(f"  原子笔记：{len(items)} 张")

    # 4. 落盘
    vault.write_transcript(vault_dir, brief.bvid, brief.title, transcript)
    vault.write_article(
        vault_dir,
        brief.title,
        brief.bvid,
        article.get("summary") or "",
        article.get("body") or "",
    )
    written = 0
    dropped_links = 0
    # 先收集本批次全部合法名字，再写入：同一批次的笔记可以互相引用，
    # 而模型偶尔给出的不存在名字必须丢掉，否则会留下解析不了的双链。
    known = set(vault.existing_atom_names(vault_dir))
    batch: list[str] = []
    for item in items:
        name = (item.get("name") or "").strip()
        try:
            vault.validate_atom_name(name)
            batch.append(name)
        except vault.VaultError as exc:
            log(f"    [警告] 丢弃一张原子笔记：{exc}")
    known |= set(batch)

    for item in items:
        name = (item.get("name") or "").strip()
        if name not in batch:
            continue
        related = [str(value).strip() for value in item.get("related") or []]
        kept = [value for value in related if value in known]
        dropped_links += len(related) - len(kept)
        vault.write_atom(
            vault_dir,
            name=name,
            title=item.get("title") or name.split("-")[-1],
            domain=name.split("-")[0],
            definition=item.get("definition") or "",
            details=item.get("details") or "",
            related=kept,
            source_title=brief.title,
        )
        written += 1
    if dropped_links:
        log(f"    [提示] 忽略了 {dropped_links} 条指向不存在笔记的双链")
    log(f"  已写入：1 篇文章 + {written} 张原子笔记")

    return {
        "bvid": brief.bvid,
        "title": brief.title,
        "summary": article.get("summary") or "",
        "atoms": [
            {"name": (item.get("name") or "").strip(), "title": item.get("title") or ""}
            for item in items
        ],
    }


# ------------------------------------------------------------------- 体系长文阶段


def build_knowledge(vault_dir: Path, vault_name: str, subject: str, digests: list[dict], settings: Settings) -> None:
    if not digests:
        log("\n[体系长文] 没有可用素材，跳过")
        return
    log(f"\n▶ 体系长文（{len(digests)} 期视频的素材）")
    atoms = vault.existing_atom_names(vault_dir)
    lines = [f"## 知识库：{vault_name}（{subject}）", ""]
    for digest in digests:
        lines.append(f"### 视频：{digest['title']}")
        lines.append(f"- 文章 slug：{to_slug(digest['title'])}")
        lines.append(f"- 摘要：{digest['summary']}")
        if digest["atoms"]:
            lines.append("- 本期原子笔记：")
            lines += [f"  - {item['name']}（{item['title']}）" for item in digest["atoms"]]
        lines.append("")
    if atoms:
        lines.append("## 知识库中已有的全部原子笔记（可被双链引用）")
        lines.append("")
        lines += [f"- {name}" for name in atoms]

    payload = chat_json(
        prompts.knowledge_prompt(vault_name, subject, "\n".join(lines)), settings, log=log
    )
    chapters = payload.get("chapters") or []
    if not chapters:
        log("  [警告] 模型没有返回任何章节")
        return
    vault.clear_knowledge(vault_dir)
    known = set(atoms)
    for offset, chapter in enumerate(chapters, 1):
        index = int(chapter.get("index") or offset)
        path = vault.write_knowledge_chapter(
            vault_dir,
            index=index,
            title=(chapter.get("title") or f"章节 {index}").strip(),
            intro=(chapter.get("intro") or "").strip(),
            sections=chapter.get("sections") or [],
            known_names=known,
        )
        log(f"  {path.name}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 B 站 UID 抓取视频并生成三层知识库语料",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--uid", type=int, help="UP 主的 UID（空间页 space.bilibili.com/<uid>）")
    parser.add_argument("--bvid", action="append", default=[], help="只处理指定 BV 号，可重复")
    parser.add_argument("--vault", required=True, help="知识库 key，需已在 build-content.mjs 的 VAULTS 中注册")
    parser.add_argument("--limit", type=int, default=5, help="抓取条数，默认 5")
    parser.add_argument("--dry-run", action="store_true", help="只列出将抓取的视频，不下载不调用模型")
    parser.add_argument("--refresh-llm", action="store_true", help="忽略文章/原子笔记缓存，重新生成")
    parser.add_argument("--no-stt", action="store_true", help="只用转写缓存，不发起新的转写")
    parser.add_argument("--no-knowledge", action="store_true", help="跳过体系长文阶段")
    parser.add_argument(
        "--only-knowledge",
        action="store_true",
        help="不处理视频，只用已有缓存重新生成体系长文",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.only_knowledge and not args.uid and not args.bvid:
        log("需要 --uid 或 --bvid 之一")
        return 2

    try:
        settings = load_settings()
    except ConfigError as exc:
        log(f"[配置错误] {exc}")
        return 2

    try:
        spec = read_vault_registry()[args.vault]
    except ConfigError as exc:
        log(f"[配置错误] {exc}")
        return 2
    except KeyError:
        registry = read_vault_registry()
        log(f"[配置错误] VAULTS 中没有 key={args.vault!r}；现有：{', '.join(registry)}")
        return 2

    vault_dir = spec.source_dir
    log(f"知识库：{spec.name}（{spec.dir}）")
    log(f"输出目录：{vault_dir}")
    log(f"模型：转写 {settings.stt_model} / 撰写 {settings.llm_model}")

    # 1. 拉列表
    briefs: list[bilibili.VideoBrief] = []
    if not args.only_knowledge:
        try:
            if args.bvid:
                for bvid in args.bvid:
                    detail = bilibili.video_detail(bvid)
                    briefs.append(
                        bilibili.VideoBrief(
                            bvid=detail.bvid,
                            aid=detail.aid,
                            title=detail.title,
                            duration=detail.parts[0].duration,
                            pubdate=detail.pubdate,
                            view=0,
                        )
                    )
            else:
                briefs = bilibili.list_videos(args.uid, args.limit)
        except bilibili.BilibiliError as exc:
            log(f"[抓取失败] {exc}")
            return 1

        if not briefs:
            log("没有取到任何视频")
            return 1

        log(f"\n共 {len(briefs)} 条视频：")
        for index, brief in enumerate(briefs, 1):
            log(f"  {index:2d}. {brief.bvid}  {brief.duration // 60:>3d}分  {brief.title}")
        if args.dry_run:
            return 0

    vault.ensure_sections(vault_dir)
    log(f"\n输出目录已就绪：{vault_dir}")
    log(f"知识库中已有原子笔记 {len(vault.existing_atom_names(vault_dir))} 张\n")

    digests: list[dict] = []
    failures: list[tuple[str, str]] = []
    for brief in briefs:
        try:
            digest = process_video(
                brief,
                vault_dir,
                settings,
                refresh_llm=args.refresh_llm,
                knowledge_only_skip_stt=args.no_stt,
            )
            if digest:
                digests.append(digest)
        except Exception as exc:  # noqa: BLE001 - 单条失败不应中断整批
            log(f"  [失败] {type(exc).__name__}: {exc}")
            failures.append((brief.bvid, f"{type(exc).__name__}: {exc}"))

    # 2. 体系长文（读缓存里的全部素材，不只是本次抓取的，保证章节覆盖完整）
    if not args.no_knowledge:
        all_digests = list(digests)
        seen = {item["bvid"] for item in all_digests}
        for path in sorted(CACHE_ROOT.glob("BV*/article.json")):
            bvid = path.parent.name
            if bvid in seen:
                continue
            article = read_cache(bvid, "article.json")
            atoms = read_cache(bvid, "atoms.json") or {}
            if not article:
                continue
            meta = read_cache(bvid, "meta.json") or {}
            all_digests.append(
                {
                    "bvid": bvid,
                    "title": meta.get("title") or bvid,
                    "summary": article.get("summary") or "",
                    "atoms": [
                        {"name": (item.get("name") or "").strip(), "title": item.get("title") or ""}
                        for item in atoms.get("atoms") or []
                    ],
                }
            )
        try:
            build_knowledge(vault_dir, spec.name, spec.subject, all_digests, settings)
        except Exception as exc:  # noqa: BLE001
            log(f"  [失败] 体系长文：{type(exc).__name__}: {exc}")
            failures.append(("_knowledge", str(exc)))

    log("\n" + "=" * 60)
    log(f"完成：{len(digests)}/{len(briefs)} 条视频入库")
    if failures:
        log("未完成：")
        for name, reason in failures:
            log(f"  - {name}: {reason}")
    log(f"语料目录：{vault_dir}")
    log("下一步：npm run sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
