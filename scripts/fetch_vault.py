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
from lib.settings import (
    CACHE_ROOT,
    SITE_ROOT,
    ConfigError,
    Settings,
    VaultSpec,
    load_settings,
    read_vault_registry,
    register_vault,
)
from lib.slug import to_slug
from lib.transcribe import transcribe


def log(message: str = "") -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------- 缓存


def cache_dir(vault_key: str, bvid: str) -> Path:
    """缓存路径按知识库分目录。

    早期版本只按 bvid 分目录，导致体系长文阶段读取「全部缓存」时会把别的
    UP 主的素材一起算进来（实测把毕导的 12 个章节混进了另一个知识库）。
    路径里带上 vault_key 后，每个知识库的缓存天然隔离。
    """
    path = CACHE_ROOT / vault_key / bvid
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_cache(vault_key: str, bvid: str, name: str) -> dict | None:
    path = cache_dir(vault_key, bvid) / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log(f"    [警告] 缓存 {path.name} 损坏，将重新生成")
        return None


def write_cache(vault_key: str, bvid: str, name: str, value: object) -> None:
    (cache_dir(vault_key, bvid) / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_text_cache(vault_key: str, bvid: str, name: str) -> str | None:
    path = cache_dir(vault_key, bvid) / name
    return path.read_text(encoding="utf-8") if path.exists() else None


# ------------------------------------------------------------------- 单条视频处理


def process_video(
    brief: bilibili.VideoBrief,
    vault_dir: Path,
    vault_key: str,
    atom_domains: dict[str, list[str]],
    subject: str,
    settings: Settings,
    *,
    refresh_llm: bool,
    knowledge_only_skip_stt: bool,
) -> dict | None:
    """返回该视频的小结，供体系长文阶段使用；跳过时返回 None。"""
    bvid = brief.bvid
    log(f"\n▶ {brief.title}")
    log(f"  {brief.bvid} · {brief.duration // 60}分{brief.duration % 60}秒 · {brief.view:,} 播放")

    detail = read_cache(vault_key, bvid, "meta.json")
    if not detail:
        fresh = bilibili.video_detail(brief.bvid)
        detail = asdict(fresh)
        write_cache(vault_key, bvid, "meta.json", detail)
    if len(detail["parts"]) > 1:
        log(f"  [跳过] 多 P 视频（{len(detail['parts'])} P）暂不支持，避免只入库一部分内容")
        return None
    cid = detail["parts"][0]["cid"]

    # 1. 转写（最贵的一步，优先命中缓存）
    transcript = read_text_cache(vault_key, bvid, "transcript.txt")
    if transcript:
        log(f"  转写：命中缓存（{len(transcript)} 字）")
    elif knowledge_only_skip_stt:
        log("  [跳过] --no-stt 且没有转写缓存")
        return None
    else:
        audio = cache_dir(vault_key, bvid) / "audio.mp3"
        if not audio.exists():
            log("  下载音频…")
            bilibili.download_audio(brief.bvid, cid, audio)
            log(f"  音频：{audio.stat().st_size / 1_000_000:.1f} MB")
        log("  语音转写…")
        started = time.time()
        transcript = transcribe(audio, cache_dir(vault_key, bvid), settings, log=log)
        (cache_dir(vault_key, bvid) / "transcript.txt").write_text(transcript, encoding="utf-8")
        log(f"  转写完成：{len(transcript)} 字，用时 {time.time() - started:.0f}s")

    # 2. 逐视频文章
    article = None if refresh_llm else read_cache(vault_key, bvid, "article.json")
    if not article:
        log("  生成文章…")
        article = chat_json(
            prompts.article_prompt(brief.title, detail["uploader"], transcript, subject),
            settings,
            log=log,
        )
        write_cache(vault_key, bvid, "article.json", article)
    log(f"  文章：{len(article.get('body') or '')} 字")

    # 3. 原子笔记（带上已有笔记名，让双链能跨视频连接）
    atoms = None if refresh_llm else read_cache(vault_key, bvid, "atoms.json")
    if not atoms:
        log("  生成原子笔记…")
        atoms = chat_json(
            prompts.atoms_prompt(
                brief.title,
                transcript,
                article.get("summary") or "",
                vault.existing_atom_names(vault_dir),
                atom_domains,
            ),
            settings,
            log=log,
        )
        write_cache(vault_key, bvid, "atoms.json", atoms)
    items = atoms.get("atoms") or []
    log(f"  原子笔记：{len(items)} 张")

    # 4. 落盘
    # 先收集本批次全部合法名字，再写入：同一批次的笔记可以互相引用，
    # 而模型偶尔给出的不存在名字必须丢掉，否则会留下解析不了的双链。
    known = set(vault.existing_atom_names(vault_dir))
    batch: list[str] = []
    raw_names: dict[str, str] = {}
    for item in items:
        raw = (item.get("name") or "").strip()
        try:
            slug = vault.normalize_atom_name(raw)
        except vault.VaultError as exc:
            log(f"    [警告] 丢弃一张原子笔记：{exc}")
            continue
        if slug in raw_names:
            log(f"    [警告] 丢弃重名的原子笔记：{slug}")
            continue
        batch.append(slug)
        raw_names[slug] = raw
    known |= set(batch)

    # 文章分组取本期原子笔记的主子题。没有正则分组规则的新 UP 主靠这个字段
    # 分组，否则上百篇文章会全部落进兜底的「其他」。
    # 用模型写的原始子题做标签（保留 AI 这种大写），显示比 slug 好看。
    group_counts: dict[str, int] = {}
    for slug in batch:
        parts = raw_names[slug].split("-")
        if len(parts) >= 2 and parts[1].strip():
            label = parts[1].strip()
            group_counts[label] = group_counts.get(label, 0) + 1
    group = max(group_counts, key=lambda key: (group_counts[key], key)) if group_counts else ""

    vault.write_transcript(vault_dir, brief.bvid, brief.title, transcript)
    vault.write_article(
        vault_dir,
        brief.title,
        brief.bvid,
        article.get("summary") or "",
        article.get("body") or "",
        group=group,
    )
    written = 0
    dropped_links = 0

    for item in items:
        try:
            name = vault.normalize_atom_name((item.get("name") or "").strip())
        except vault.VaultError:
            continue
        if name not in batch:
            continue
        # related 也要走同一套归一化，否则 `[[AI副业-...]]` 与文件名对不上。
        related = []
        for value in item.get("related") or []:
            try:
                related.append(vault.normalize_atom_name(str(value).strip()))
            except vault.VaultError:
                related.append(str(value).strip())
        kept = [value for value in related if value in known]
        dropped_links += len(related) - len(kept)
        vault.write_atom(
            vault_dir,
            name=name,
            title=item.get("title") or raw_names.get(name, name).split("-")[-1],
            domain=name.split("-")[0],
            definition=item.get("definition") or "",
            details=item.get("details") or "",
            related=kept,
            source_title=brief.title,
        )
        written += 1
    if dropped_links:
        log(f"    [提示] 忽略了 {dropped_links} 条指向不存在笔记的双链")
    log(f"  已写入：1 篇文章 + {written} 张原子笔记（分组：{group or '其他'}）")

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


# 每批素材的字符上限。一次调用要同时容纳素材清单和生成正文，
# 太大时输出会被截断，所以按主题拆开、超限再切块。
KNOWLEDGE_BATCH_CHARS = 24_000
# 少于这个视频数的主题不单独成章，并入零散专题，避免出现只有一张原子笔记的薄章。
KNOWLEDGE_MIN_VIDEOS = 2


def atom_theme(digest: dict) -> str:
    """取本期素材的主子题（原子笔记名的第二段）。"""
    themes = []
    for atom in digest.get("atoms") or []:
        parts = (atom.get("name") or "").split("-")
        if len(parts) >= 2 and parts[1]:
            themes.append(parts[1])
    if not themes:
        return "综合"
    return max(set(themes), key=themes.count)


def plan_knowledge_batches(digests: list[dict]) -> list[tuple[str | None, list[dict]]]:
    """把素材按主题分批。

    5 条视频时一次调用就能覆盖；174 条视频时清单是 35 倍，单次调用产出的正文
    必然被截断，因此按子题分组，每组一次调用只生成一章。视频数过少的主题并入
    「零散专题」，同组超过字符上限时再切块。
    """
    buckets: dict[str, list[dict]] = {}
    for digest in digests:
        buckets.setdefault(atom_theme(digest), []).append(digest)

    major = [(theme, items) for theme, items in buckets.items() if len(items) >= KNOWLEDGE_MIN_VIDEOS]
    minor = [digest for theme, items in buckets.items() if len(items) < KNOWLEDGE_MIN_VIDEOS for digest in items]

    # 按素材量从多到少排，章序即重要度序。
    major.sort(key=lambda pair: -len(pair[1]))
    groups: list[tuple[str | None, list[dict]]] = [(theme, items) for theme, items in major]
    if minor:
        groups.append((None, sorted(minor, key=lambda digest: digest["title"])))

    batched: list[tuple[str | None, list[dict]]] = []
    for theme, items in groups:
        current: list[dict] = []
        size = 0
        for digest in items:
            cost = len(render_digest(digest)) + 200
            if current and size + cost > KNOWLEDGE_BATCH_CHARS:
                batched.append((theme, current))
                current, size = [], 0
            current.append(digest)
            size += cost
        if current:
            batched.append((theme, current))
    return batched


def render_digest(digest: dict) -> str:
    lines = [f"### 视频：{digest['title']}"]
    lines.append(f"- 文章 slug：{to_slug(digest['title'])}")
    lines.append(f"- 摘要：{digest['summary']}")
    if digest.get("atoms"):
        lines.append("- 本期原子笔记：")
        lines += [f"  - {item['name']}（{item['title']}）" for item in digest["atoms"]]
    return "\n".join(lines) + "\n"


def build_knowledge(
    vault_dir: Path, vault_name: str, subject: str, digests: list[dict], settings: Settings
) -> None:
    if not digests:
        log("\n[体系长文] 没有可用素材，跳过")
        return

    known = set(vault.existing_atom_names(vault_dir))
    batches = plan_knowledge_batches(digests)
    log(f"\n▶ 体系长文（{len(digests)} 期素材 → {len(batches)} 批）")
    for theme, items in batches:
        log(f"  · {theme or '零散专题'}：{len(items)} 期")

    # 先全部生成再清空目录，中途失败不会把上一轮的章节删掉。
    results: list[tuple[str, str, str, list[dict]]] = []
    for theme, items in batches:
        lines = [f"## 知识库：{vault_name}（{subject}）", ""]
        lines += [render_digest(digest) for digest in items]
        # 全量原子笔记名太长时可省略：跨章双链靠素材清单里的名字已经足够。
        if known and len(known) <= 600:
            lines.append("## 知识库中已有的原子笔记（可被双链引用）")
            lines.append("")
            lines += [f"- {name}" for name in sorted(known)]
        log(f"  生成章节：{theme or '零散专题'}")
        payload = chat_json(
            prompts.knowledge_prompt(vault_name, subject, "\n".join(lines), theme),
            settings,
            log=log,
        )
        for chapter in payload.get("chapters") or []:
            title = (chapter.get("title") or "未命名章节").strip()
            results.append(
                (
                    title,
                    (chapter.get("intro") or "").strip(),
                    "",
                    chapter.get("sections") or [],
                )
            )

    if not results:
        log("  [警告] 模型没有返回任何章节，保留原有章节")
        return

    vault.clear_knowledge(vault_dir)
    for index, (title, intro, _, sections) in enumerate(results, 1):
        path = vault.write_knowledge_chapter(
            vault_dir,
            index=index,
            title=title,
            intro=intro,
            sections=sections,
            known_names=known,
        )
        log(f"  {path.name}")


EPILOG_EXAMPLES = (
    "示例：",
    "  fetch_vault.py https://space.bilibili.com/254463269 --all",
    "  fetch_vault.py 254463269 --limit 20 --subject 科学科普",
    "  fetch_vault.py 254463269 --vault bidao --only-knowledge",
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fetch_vault.py",
        description=(
            "把 B 站 UP 主的视频抓成三层知识库语料"
            "（体系化阅读 / 逐视频文章 / 原子笔记）。"
            "只给 UP 主空间链接或 UID 即可；未登记过的知识库会自动写入 "
            "scripts/vaults.json，昵称与头像会自动从 B 站取。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=chr(10).join(EPILOG_EXAMPLES),
    )
    parser.add_argument("up", nargs="?", help="UP 主空间链接或 UID")
    parser.add_argument("--uid", type=int, help="UID（与位置参数等价，保留兼容）")
    parser.add_argument("--bvid", action="append", default=[], help="只处理指定 BV 号，可重复")
    parser.add_argument("--vault", help="知识库 key，省略时用 up<UID> 自动生成")
    parser.add_argument("--name", help="知识库显示名，默认取 UP 主昵称")
    parser.add_argument("--dir", help="源语料目录名，默认 <昵称>-<主题>")
    parser.add_argument("--subject", help="主题，默认「综合」")
    parser.add_argument("--description", help="目录页上的简介")
    parser.add_argument("--tags", help="逗号分隔的标签，默认取主题")
    parser.add_argument(
        "--no-register", action="store_true", help="不写入 scripts/vaults.json（只出语料）"
    )
    parser.add_argument("--all", action="store_true", help="处理该 UP 主的全部视频")
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


def resolve_taxonomy(
    spec: VaultSpec, titles: list[str], settings: Settings, *, refresh: bool = False
) -> dict[str, list[str]]:
    """拿到这个知识库的原子笔记命名域。

    优先级：vaults.json 里人工写死的 > 上次自动推导并缓存的 > 让模型看一遍
    全部标题现推一份。推导结果会写回 vaults.json，方便之后人工微调。
    """
    if spec.atom_domains:
        log(f"  原子笔记域清单：取自 vaults.json，{len(spec.atom_domains)} 个域")
        return spec.atom_domains

    cache_path = CACHE_ROOT / spec.key / "_taxonomy.json"
    if cache_path.exists() and not refresh:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached:
                log(f"  原子笔记域清单：命中缓存，{len(cached)} 个域")
                return cached
        except json.JSONDecodeError:
            log("  [警告] 域清单缓存损坏，重新推导")

    log(f"  推导原子笔记域清单（让模型看一遍这 {len(titles)} 条标题）…")
    try:
        payload = chat_json(
            prompts.taxonomy_prompt(spec.name, spec.subject, titles), settings, log=log
        )
        domains = {
            str(domain).strip(): [str(sub).strip() for sub in (subs or []) if str(sub).strip()]
            for domain, subs in (payload.get("domains") or {}).items()
            if str(domain).strip()
        }
    except Exception as exc:  # noqa: BLE001 - 推导失败不该让整批抓取停摆
        log(f"  [警告] 域清单推导失败，用兜底清单：{type(exc).__name__}: {exc}")
        return prompts.FALLBACK_DOMAINS
    if not domains:
        log("  [警告] 模型没给出域清单，用兜底清单")
        return prompts.FALLBACK_DOMAINS

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(domains, ensure_ascii=False, indent=2), encoding="utf-8")
    spec.atom_domains = domains
    register_vault(spec)
    log("  域清单：" + "；".join(f"{d}（{len(s)} 个子题）" for d, s in domains.items()))
    return domains


def resolve_up_id(args: argparse.Namespace) -> int | None:
    if args.uid:
        return args.uid
    if args.up:
        return bilibili.parse_up_id(args.up)
    return None


def resolve_vault(args: argparse.Namespace, mid: int | None) -> VaultSpec:
    """定位或新建知识库。只给 UID/链接时，剩下的字段都自动补上。"""
    registry = read_vault_registry()
    key = args.vault or (f"up{mid}" if mid else None)
    if not key:
        raise ConfigError("需要 --vault（无法从 BV 号推出知识库 key）")

    existing = registry.get(key)
    overrides = any([args.name, args.dir, args.subject, args.description, args.tags])
    if existing and not overrides:
        # 已登记且没有覆盖字段：完全沿用，不再打扰 B 站接口。
        return existing

    profile = None
    if mid:
        try:
            profile = bilibili.up_profile(mid)
        except bilibili.BilibiliError as exc:
            log(f"  [提示] 取 UP 主资料失败，改用占位信息：{exc}")

    name = args.name or (existing.name if existing else "") or (profile.name if profile else "") or key
    subject = args.subject or (existing.subject if existing else "") or "综合"
    directory = args.dir or (existing.dir if existing else "") or f"{name}-{subject}"
    description = (
        args.description
        or (existing.description if existing else "")
        or f"来自 {name} 的 B 站视频，按体系化阅读、逐视频文章与原子笔记三层结构整理。"
    )
    tags = (
        [part.strip() for part in args.tags.split(",") if part.strip()]
        if args.tags
        else (existing.tags if existing and existing.tags else [subject])
    )

    avatar = existing.avatar if existing else ""
    if profile and profile.face:
        target = SITE_ROOT / "assets" / "images" / f"avatar-{key}.jpg"
        if not target.exists():
            try:
                bilibili.download_image(profile.face, target)
                log(f"  头像已保存：assets/images/{target.name}")
            except (bilibili.BilibiliError, OSError) as exc:
                log(f"  [提示] 头像下载失败，可稍后手动放入 assets/images/：{exc}")
        if target.exists():
            avatar = target.name

    spec = VaultSpec(
        key=key,
        name=name,
        dir=directory,
        subject=subject,
        description=description,
        tags=tags,
        illustration=existing.illustration if existing else "",
        avatar=avatar,
    )
    if not args.no_register:
        register_vault(spec)
        log(f"  已写入 scripts/vaults.json：{key} → {spec.dir}")
    return spec


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    try:
        mid = resolve_up_id(args)
    except bilibili.BilibiliError as exc:
        log(f"[参数错误] {exc}")
        return 2
    if not args.only_knowledge and not mid and not args.bvid:
        log("需要给出 UP 主空间链接/UID，或用 --bvid 指定视频")
        return 2

    try:
        settings = load_settings()
    except ConfigError as exc:
        log(f"[配置错误] {exc}")
        return 2

    try:
        spec = resolve_vault(args, mid)
    except ConfigError as exc:
        log(f"[配置错误] {exc}")
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
                    # 有缓存就不请求接口：重跑单条视频时能完全绕开 B 站，
                    # 也就不会被风控挡住（列表接口被限流时这是唯一的补救通道）。
                    cached = read_cache(spec.key, bvid, "meta.json")
                    if cached and cached.get("parts"):
                        briefs.append(
                            bilibili.VideoBrief(
                                bvid=bvid,
                                aid=int(cached.get("aid") or 0),
                                title=cached.get("title") or bvid,
                                duration=int(cached["parts"][0].get("duration") or 0),
                                pubdate=int(cached.get("pubdate") or 0),
                                view=0,
                            )
                        )
                        continue
                    brief = bilibili.video_detail(bvid)
                    briefs.append(
                        bilibili.VideoBrief(
                            bvid=brief.bvid,
                            aid=brief.aid,
                            title=brief.title,
                            duration=brief.parts[0].duration,
                            pubdate=brief.pubdate,
                            view=0,
                        )
                    )
            else:
                briefs = bilibili.list_videos(mid, 1_000_000 if args.all else args.limit)
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
    atom_domains = resolve_taxonomy(spec, [b.title for b in briefs], settings) if briefs else {}
    log(f"\n输出目录已就绪：{vault_dir}")
    log(f"知识库中已有原子笔记 {len(vault.existing_atom_names(vault_dir))} 张\n")

    digests: list[dict] = []
    failures: list[tuple[str, str]] = []
    for brief in briefs:
        try:
            digest = process_video(
                brief,
                vault_dir,
                spec.key,
                atom_domains,
                spec.subject,
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
        for path in sorted((CACHE_ROOT / spec.key).glob("BV*/article.json")):
            bvid = path.parent.name
            if bvid in seen:
                continue
            article = read_cache(spec.key, bvid, "article.json")
            atoms = read_cache(spec.key, bvid, "atoms.json") or {}
            if not article:
                continue
            meta = read_cache(spec.key, bvid, "meta.json") or {}
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
