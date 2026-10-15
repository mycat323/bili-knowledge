"""三层内容的提示词。

这里的约束不是「写得好」，而是「写得能被 build-content.mjs 正确消费」：
原子笔记必须产出 `## 一句话定义`（本地搜索只索引标题和这一节），
双链必须使用原子笔记的文件名（`toSlug` 后的 `域-子题-概念`），
否则链接和反链会指向不存在的页面。
"""

from __future__ import annotations

# 原子笔记的命名域。第一段是知识库域，第二段是子题，前两段决定站点的分组。
# 这份清单同时用于标题分组规则，改动后应同步 build-content.mjs 的 ARTICLE_GROUP_RULES。
ATOM_DOMAINS: dict[str, list[str]] = {
    "科学科普": [
        "光学现象",
        "力学运动",
        "流体与热",
        "声学振动",
        "数学概率",
        "数学几何",
        "化学反应",
        "材料结构",
        "生活实验",
        "食品科学",
        "科学思维",
        "科学史",
    ],
}

# 文章分组规则：articles 分组的兜底是「其他」，所以这里必须覆盖常见的题目形态。
ARTICLE_GROUPS: list[tuple[str, str]] = [
    ("生活实验", "实验|挑战|实测|测试|自制|手工|搭建|发明|改造"),
    ("数理科普", "数学|物理|化学|概率|方程|函数|量子|力学|统计|公式|几何|光|声|热"),
    ("脑洞问题", "为什么|如果|假如|能不能|到底|究竟|有多|真的|居然"),
    ("科学史话", "历史|故事|发明|科学家|诺奖|世纪|起源|祖师爷"),
    ("科技与生活", "手机|电脑|算法|AI|芯片|电池|材料|能源|工程|机械"),
]

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


def article_prompt(video_title: str, uploader: str, transcript: str) -> list[dict]:
    system = (
        f"你是中文科普编辑，负责把 {uploader} 的 B 站视频转写稿整理成一篇可读的文章。\n\n"
        "输出格式要求：\n"
        "- 保留原视频的叙事顺序、推理过程、实验步骤和案例，这是「逐视频文章」，"
        "不是知识提炼，读者应该能顺着读到一个完整的论证过程。\n"
        "- 用 `## 一、`、`## 二、` 这样的二级标题切分 3~6 个部分，标题要具体，"
        "不要用「引言」「总结」这类空标题。\n"
        "- 篇幅按转写稿长度决定，通常是转写稿字数的 55%~70%，不要为了简短丢掉论证链条。\n"
        "- 可以用 Markdown 表格、有序列表、**加粗**。不要使用任何 HTML 标签"
        "（只允许 <sub>，但尽量别用）。\n\n"
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
) -> list[dict]:
    domain_lines = "\n".join(
        f"- {domain}：" + "、".join(subdomains or ["（自由发挥）"])
        for domain, subdomains in ATOM_DOMAINS.items()
    )
    existing = "\n".join(f"- {name}" for name in existing_atoms[:400]) or "（当前知识库还没有原子笔记）"
    system = (
        "你是中文科普知识库编辑，负责把一期视频拆成可以独立搜索、独立引用的原子笔记。\n\n"
        "每张原子笔记只回答一个清晰的问题，粒度是「一个概念 / 一条原理 / 一个可复现的结论」。\n\n"
        "一、命名（最重要，站点按它分组）：\n"
        "`name` 必须是 `域-子题-概念` 三段，用半角连字符连接，全部小写英文或中文，"
        "不含空格和标点。前两段必须来自下面的域清单，同一类内容必须复用完全相同的"
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
        "JSON 结构：{\"atoms\": [{\"name\": \"科学科普-光学现象-单缝衍射\", "
        "\"title\": \"单缝衍射\", \"definition\": \"...\", \"details\": \"...\", "
        "\"related\": [\"科学科普-光学现象-衍射条纹间距\"]}]}\n"
        "一期视频产出 4~10 张原子笔记，按信息密度决定，不要硬凑。"
    )
    user = (
        f"视频标题：{video_title}\n\n"
        f"该视频整理出的文章摘要：\n{article_summary}\n\n"
        f"已有原子笔记（可被 related 引用）：\n{existing}\n\n"
        f"转写稿：\n{transcript}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def knowledge_prompt(vault_name: str, subject: str, digest: str) -> list[dict]:
    system = (
        f"你是中文知识库主编，负责把 {vault_name}（{subject}）的一批视频素材"
        "重组成体系化的长文章节。\n\n"
        "这是「体系化阅读」层，与逐视频文章的区别是：这里要打散视频边界，"
        "按知识自身的逻辑重新组织，去掉重复表达，并把概念之间的连接补上。\n\n"
        "输出格式要求：\n"
        "- 每章一个条目，`index` 从 1 开始的连续整数，`title` 是章名（不要带「第N章」前缀）。\n"
        "- `intro` 是章节导语，说清这一章解决什么问题、按什么顺序读、读完之后能获得什么，"
        "200~400 字。\n"
        "- `sections` 是 `## N.1 标题` 级别的小节，`body` 里用 `### N.1.1` 做更细的切分。"
        "小节标题要具体，不要「概述」「小结」。\n"
        "- 正文里引用素材时使用双链语法 `[[原子笔记name|显示文本]]` 或 "
        "`[[文章slug|文章标题]]`，素材清单里给了可用的 name 和 slug，不要写别的链接形式。\n"
        "- 正文总量按素材量决定，宁可长不可干：每个核心概念至少走"
        "「现象 → 原理 → 可验证的推论」三步。\n\n"
        f"{_NO_FABRICATION}\n\n{_JSON_RULE}\n"
        "JSON 结构：{\"chapters\": [{\"index\": 1, \"title\": \"...\", \"intro\": \"...\", "
        "\"sections\": [{\"heading\": \"1.1 标题\", \"body\": \"Markdown 正文\"}]}]}"
    )
    user = f"素材清单：\n{digest}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
