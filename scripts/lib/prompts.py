"""三层内容的提示词。

这里的约束不是「写得好」，而是「写得能被 build-content.mjs 正确消费」：
原子笔记必须产出 `## 一句话定义`（本地搜索只索引标题和这一节），
双链必须使用原子笔记的文件名（`toSlug` 后的 `域-子题-概念`），
否则链接和反链会指向不存在的页面。
"""

from __future__ import annotations

# 原子笔记的命名域：第一段是域，第二段是子题，前两段决定站点分组。
#
# 这份清单必须按知识库来，不能全局硬编码 —— 之前写死成「科学科普 + 光学/力学/
# 化学…」，结果抓一个讲 AI 副业的博主时，内容被强行塞进「科学科普-科学思维」，
# 域和子题完全对不上，分组也就失去意义。
#
# 取值优先级：vaults.json 的 atomDomains > .cache 里自动推导的清单 > 这份兜底。
# 自动推导见 taxonomy_prompt。
FALLBACK_DOMAINS: dict[str, list[str]] = {"综合": ["概念", "方法", "案例"]}

_JSON_RULE = (
    "只输出一个 JSON 对象，不要输出任何解释、前后缀或 Markdown 代码围栏。"
    "JSON 中的字符串必须合法转义，不要在字符串里出现未转义的换行。"
)

_NO_FABRICATION = (
    "严禁虚构：所有数字、案例、实验现象、人物和结论都必须来自转写稿。"
    "转写稿里没说过的原理细节不要补充，宁可少写。"
    "转写稿里的口语、重复、口头语（「对吧」「就是说」「然后呢」）要清理掉，"
    "但不能改变原意。"
)


def article_prompt(
    video_title: str, uploader: str, transcript: str, subject: str = ""
) -> list[dict]:
    # 不要写死「科普编辑」：同一个流水线要服务财经、职场、连麦等不同领域的博主。
    scope = f"，领域是「{subject}」" if subject else ""
    system = (
        f"你是中文内容编辑，负责把 {uploader} 的 B 站视频转写稿整理成一篇可读的文章"
        f"{scope}。\n\n"
        "输出格式要求：\n"
        "- 保留原视频的叙事顺序、推理过程、演示步骤和案例，这是「逐视频文章」，"
        "不是知识提炼，读者应该能顺着读到一个完整的论证过程。\n"
        "- 用 `## 一、`、`## 二、` 这样的二级标题切分 3~6 个部分，标题要具体，"
        "不要用「引言」「总结」这类空标题。\n"
        "- 篇幅按转写稿长度决定，通常是转写稿字数的 55%~70%，不要为了简短丢掉论证链条。\n"
        "- 可以用 Markdown 表格、有序列表、**加粗**。不要使用任何 HTML 标签。\n\n"
        f"{_NO_FABRICATION}\n\n{_JSON_RULE}\n"
        "JSON 结构：{\"summary\": \"一到两句话的摘要\", "
        "\"body\": \"以 `## 一、` 开头的 Markdown 正文\"}"
    )
    user = f"视频标题：{video_title}\n\n转写稿：\n{transcript}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def atoms_prompt(
    video_title: str,
    transcript: str,
    article_summary: str,
    existing_atoms: list[str],
    atom_domains: dict[str, list[str]] | None = None,
) -> list[dict]:
    domains = atom_domains or FALLBACK_DOMAINS
    example_domain = next(iter(domains))
    example_sub = (domains[example_domain] or ["通用"])[0]
    domain_lines = "\n".join(
        f"- {domain}：" + "、".join(subdomains or ["（自行命名子题，但同域内容要统一口径）"])
        for domain, subdomains in domains.items()
    )
    existing = "\n".join(f"- {name}" for name in existing_atoms[:400]) or "（当前知识库还没有原子笔记）"
    system = (
        "你是中文知识库编辑，负责把一期视频拆成可以独立搜索、独立引用的原子笔记。\n\n"
        "每张原子笔记只回答一个清晰的问题，粒度是「一个概念 / 一条原理 / 一个可复现的结论」。\n\n"
        "一、命名（最重要，站点按它分组）：\n"
        "`name` 必须是 `域-子题-概念` 三段，用半角连字符连接，全部小写英文或中文，"
        "不含空格和标点。前两段必须取自下面的域清单，同一类内容必须复用完全相同的"
        "前两段，否则站点的主题分组会被打散。\n"
        f"{domain_lines}\n\n"
        "`title` 是给人看的短标题（不含域前缀），`name` 去掉前两段后的第三段应与 title 呼应。\n\n"
        "二、正文要求：\n"
        "- `definition`：一句话定义，40~80 字，把概念说清楚，不要以「指」「是指」开头，"
        "直接陈述结论。\n"
        "- `details`：Markdown 详细说明，200~500 字。可以写原理、推导、常见误解、"
        "可动手验证的方法。可以包含 Markdown 表格。不要使用 HTML 标签。\n\n"
        "三、双链：\n"
        "- `related` 是本笔记相关的其他原子笔记的 `name` 数组（0~4 个）。\n"
        "- 优先引用下面「已有原子笔记」里的条目（跨视频连接是这套知识库的价值所在），"
        "同批次生成的 `name` 也可以互相引用。\n"
        "- 不要编造不存在的 `name`。\n\n"
        f"{_NO_FABRICATION}\n\n{_JSON_RULE}\n"
        f"JSON 结构：{{\"atoms\": [{{\"name\": \"{example_domain}-{example_sub}-示例概念\", "
        "\"title\": \"示例概念\", \"definition\": \"...\", \"details\": \"...\", "
        f"\"related\": [\"{example_domain}-{example_sub}-相关概念\"]}}]}}\n"
        "一期视频产出 4~10 张原子笔记，按信息密度决定，不要硬凑。"
    )
    user = (
        f"视频标题：{video_title}\n\n"
        f"该视频整理出的文章摘要：\n{article_summary}\n\n"
        f"已有原子笔记（可被 related 引用）：\n{existing}\n\n"
        f"转写稿：\n{transcript}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def taxonomy_prompt(vault_name: str, subject: str, titles: list[str]) -> list[dict]:
    """先看一遍全部视频标题，为这个博主提一套原子笔记的命名域。

    每个博主的领域不同，硬编码一份域清单只适用于某一个。先归纳出稳定口径，
    后续所有视频共用，站点分组才不会散成一地。
    """
    listing = "\n".join(f"- {title}" for title in titles)
    system = (
        f"你在为一个 B 站 UP 主（{vault_name}，主题：{subject}）的知识库设计分类体系。"
        "知识库会把视频拆成「原子笔记」，每张笔记名必须是 `域-子题-概念` 三段，"
        "其中前两段取自你这次给出的清单，并且会直接成为站点上的主题分组。\n\n"
        "要求：\n"
        "- 给出 1~3 个「域」（第一段），覆盖面要能把下面所有标题装进去。\n"
        "- 每个域下给 6~12 个「子题」（第二段）。子题之间界限清晰、不要互相包含，"
        "名称控制在 4~6 个汉字。\n"
        "- 子题的粒度按「读者会想按这个来找内容」来定，不要用「其他」「综合」这类兜底词。\n"
        "- 只用中文，不要标点、空格或连字符（它们会破坏 `域-子题-概念` 的分段）。\n\n"
        f"{_JSON_RULE}\n"
        "JSON 结构：{\"domains\": {\"域\": [\"子题一\", \"子题二\"]}}"
    )
    user = f"该 UP 主的视频标题（{len(titles)} 条）：\n{listing}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def knowledge_prompt(
    vault_name: str, subject: str, digest: str, focus: str | None = None
) -> list[dict]:
    # 174 条视频的素材清单塞不进一次调用（输出会被截断），所以按主题分批，
    # 每次调用只负责一个主题的一章。focus 为空表示这一批主题比较分散。
    scope = (
        f"这批素材集中讨论《{focus}》这一个主题。只围绕它组织章节，"
        "章节标题要具体到这一主题内部分歧与层次，不要跨主题泛谈。"
        if focus
        else "这批素材主题比较分散，请先归纳出少数几条主线再组织章节。"
    )
    system = (
        f"你是中文知识库主编，负责把 {vault_name}（{subject}）的一批视频素材"
        "重组成体系化的长文章节。\n\n"
        "这是「体系化阅读」层，与逐视频文章的区别是：这里要打散视频边界，"
        "按知识自身的逻辑重新组织，去掉重复表达，并把概念之间的连接补上。\n\n"
        f"{scope}\n\n"
        "输出格式要求：\n"
        "- `title` 是章名（不要带「第N章」前缀），要能概括这一章的核心问题。\n"
        "- `intro` 是章节导语，说清这一章解决什么问题、按什么顺序读、读完之后能获得什么，"
        "200~400 字。\n"
        "- `sections` 是 `## N.1 标题` 级别的小节，`body` 里用 `### N.1.1` 做更细的切分。"
        "小节标题要具体，不要「概述」「小结」。\n"
        "- 正文里引用素材时使用双链语法 `[[原子笔记name|显示文本]]` 或 "
        "`[[文章slug|文章标题]]`，素材清单里给了可用的 name 和 slug，不要写别的链接形式。\n"
        "- 素材清单里的所有视频都属于同一个 UP 主，不要引入清单之外的内容。\n"
        "- 正文总量按素材量决定，宁可长不可干：每个核心概念至少走"
        "「现象 → 原理 → 可验证的推论」三步，但同一件事不要在两节里重复讲。\n\n"
        f"{_NO_FABRICATION}\n\n{_JSON_RULE}\n"
        "JSON 结构：{\"chapters\": [{\"title\": \"...\", \"intro\": \"...\", "
        "\"sections\": [{\"heading\": \"标题\", \"body\": \"Markdown 正文\"}]}]}"
    )
    user = f"素材清单：\n{digest}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
