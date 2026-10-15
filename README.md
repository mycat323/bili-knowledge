# bili-knowledge

将 B 站 UP 主的视频知识库导出为可搜索、可跳转的 VitePress 静态站点。

- 在线访问：https://mycat323.github.io/bili-knowledge/
- 小Lin说：7 章体系化成文 + 161 篇逐视频文章 + 959 个原子笔记
- 戴师兄：8 章体系化成文 + 127 篇逐视频文章 + 342 个原子笔记
- 大冰连麦：7 章体系化成文 + 127 篇逐视频文章 + 379 个原子笔记
- 毕导：5 章体系化成文 + 5 篇逐视频文章 + 36 个原子笔记

## 目录结构

```
├── docs/                    # VitePress 站点内容（由脚本预生成后提交）
│   ├── index.md             # 首页
│   ├── about.md             # 来源声明
│   ├── xiaolin/             # 小Lin说（knowledge / articles / atoms）
│   ├── daishixiong/         # 戴师兄（knowledge / articles / atoms）
│   ├── dabing/              # 大冰连麦（knowledge / articles / atoms）
│   ├── bidao/               # 毕导（knowledge / articles / atoms）
│   └── .vitepress/          # 配置、主题、slug 映射、侧边栏
├── scripts/
│   ├── vaults.json          # 知识库注册表（站点侧与抓取侧共用）
│   ├── build-content.mjs    # 内容同步脚本（本地运行，需知识库源文件）
│   ├── fetch_vault.py       # 抓取流水线入口：空间链接 / UID → 三层知识库
│   ├── lib/                 # 流水线模块（B 站接口 / 转写 / 模型 / 落盘）
│   └── validate-vault.mjs   # 源知识库校验（BOM、frontmatter 围栏）
├── .env                     # 流水线密钥（不进版本库，参考 .env.example）
└── .github/workflows/deploy.yml # GitHub Pages 自动部署
```

## 本地开发

```bash
npm install
npm run sync     # 从知识库源文件重新生成站点内容
npm run dev      # 本地预览
npm run build    # 构建
```

`npm run sync` 需要本地存在源知识库（不在本仓库中），
运行后会重新生成 `docs/` 下的全部页面。更新内容的流程：本地 sync → build 验证 → 提交 push，CI 只负责 build 和部署。

### 只重建部分知识库

源知识库不在本机时，直接 sync 会把对应知识库已提交的页面清空并生成空页面，
因此 sync 会在动手前检查每个知识库的源目录：

```bash
npm run sync                       # 缺源目录 → 报错退出，不动 docs/
npm run sync -- --allow-partial    # 只重建有源目录的知识库，其余保留现有页面与索引
```

> **sync 之后必须重启 `npm run dev`。**
> `config.mts` 在启动时用 `fs.readFileSync` 把 `sidebar.generated.json` 读成了常量，
> 侧边栏属于 `themeConfig`，VitePress 不会热更新配置。
> 不重启的表现很有迷惑性：新知识库的页面能打开（Vite 按需读磁盘上的 md），
> 首页与目录页也会变成 4 个（`vaults.generated.json` 是 ES 模块，会被 HMR），
> **只有侧边栏还是旧的**——新知识库的页面会没有左侧导航。
> `npm run build` 不受影响，它每次都是全新进程。

### 抓取一个 UP 主的内容

`scripts/fetch_vault.py` 是唯一入口：给一个 B 站空间链接或 UID，它就把该 UP 主的视频
抓成三层知识库语料，产出直接落在 `sync` 读取的位置。**不需要预先注册** ——
未登记过的知识库会自动写入 `scripts/vaults.json`，昵称与头像自动从 B 站取。

```bash
# 首次：建虚拟环境并安装依赖
uv venv .venv --python 3.12
uv pip install --python .venv/Scripts/python.exe -r scripts/requirements.txt
cp .env.example .env        # 填入转写与撰写模型的密钥

# 只看会抓到什么（不下载、不调用模型）
.venv/Scripts/python.exe scripts/fetch_vault.py https://space.bilibili.com/254463269 --all --dry-run

# 抓全部视频
.venv/Scripts/python.exe scripts/fetch_vault.py https://space.bilibili.com/254463269 --all

# 先抓最近 5 条试水，并指定主题
.venv/Scripts/python.exe scripts/fetch_vault.py 254463269 --limit 5 --subject 科学科普
```

第一个位置参数接受这些写法，都会解析出同一个 UID：

```
https://space.bilibili.com/254463269?spm_id_from=333.40138.feed-card.all.click
space.bilibili.com/254463269/video
254463269
```

也支持在代码里直接调用（`scripts/lib/` 就是一组可复用模块）：

```python
import sys; sys.path.insert(0, 'scripts')
from lib import bilibili

mid = bilibili.parse_up_id('https://space.bilibili.com/254463269')
videos = bilibili.list_videos(mid, 50)           # 投稿列表
detail = bilibili.video_detail(videos[0].bvid)   # 单视频详情（含分 P / cid）
bilibili.download_audio(detail.bvid, detail.parts[0].cid, Path('audio.mp3'))
```

常用开关：

| 开关 | 作用 |
| --- | --- |
| `--all` | 处理该 UP 主的全部视频 |
| `--limit N` | 只抓最近 N 条，默认 5 |
| `--vault KEY` | 指定知识库 key，默认 `up<UID>` |
| `--name` / `--dir` / `--subject` / `--description` / `--tags` | 覆盖自动推导的知识库元信息 |
| `--no-register` | 只出语料，不写 `scripts/vaults.json` |
| `--bvid BV...` | 只处理指定视频，可重复 |
| `--dry-run` | 只列出将抓取的视频 |
| `--no-stt` | 只用转写缓存，不发起新的转写 |
| `--refresh-llm` | 忽略文章/原子笔记缓存，重新生成 |
| `--only-knowledge` | 不处理视频，只用已有缓存重写体系长文 |
| `--no-knowledge` | 跳过体系长文阶段 |

中间产物缓存在 `.cache/fetch/<BV号>/`：转写是整条流水线里最贵的一步，
重复运行会自动复用，不会重复计费。**中断后重新执行同一条命令即可续跑。**

## 新增一个 UP 主知识库

```bash
.venv/Scripts/python.exe scripts/fetch_vault.py https://space.bilibili.com/254463269 --all
npm run sync -- --allow-partial
npm run build
```

一条命令就够了。自动完成的事情：

| 事项 | 说明 |
| --- | --- |
| 解析 UID | 空间链接、带 `?spm_id_from=...` 的分享链接、纯数字都认 |
| 登记知识库 | 写入 `scripts/vaults.json`（站点与抓取共用的注册表），`sync` 立刻认得它 |
| 昵称与头像 | 取 B 站昵称；头像存到 `assets/images/avatar-<key>.jpg` |
| 文章分组 | 每篇文章按该期原子笔记的子题写入 frontmatter 的「分组」，不需要手写正则 |
| 目录名 | 默认 `<昵称>-<主题>`，可用 `--dir` 覆盖 |

还想再调：

- **主题、简介、标签**：`--subject 科学科普 --description '……' --tags 科学科普,生活实验`
- **首页雕版线稿**：默认回落到通用插图。要专属的，把米色底雕版图放进 `assets/sources/`，
  在 `scripts/make-lineart.mjs` 末尾加一行 `await toLineArt(...)`，再把生成的文件名填进
  `scripts/vaults.json` 的 `illustration`
- **文章分组正则**：`build-content.mjs` 的 `ARTICLE_GROUP_RULES`。这是可选的老机制，
  只在文章没有「分组」字段时兜底 —— 三个早期知识库的源语料来自外部流程，靠它分组

语料格式要点（详见 `scripts/lib/vault.py` 与 `docs/` 下已有页面）：原子笔记名必须是
`域-子题-概念`（前两段决定站点分组），正文必须有 `## 一句话定义`（本地搜索只索引
标题和这一节），双链写作 `[[原子笔记名]]`。

## 部署

推 `main` 就触发 `.github/workflows/deploy.yml`：`npm ci` → `npm run build` → GitHub Pages。
线上地址：https://mycat323.github.io/bili-knowledge/

### 不要重跑旧提交的工作流

**GitHub Pages 只认「最后一次成功的部署」，跟提交新旧无关。** 一个旧提交的 run
如果比新提交的 run 晚完成，会把线上站点回滚成旧内容。实测：

| run | 提交 | 内容 | 部署完成 |
| --- | --- | --- | --- |
| push | `fc9bf43` | 7 个知识库 | 17:36:20 |
| 手动重跑 | `eeed44f` | 4 个知识库 | **17:39:32** ← 最后上线，站点被盖回 4 个 |

所以：

- 要重新部署，就**推一个新提交**，或者重跑**最新提交**的那次 run —— 不要重跑旧提交。
- 工作流已设 `concurrency.cancel-in-progress: true`，新提交会取消仍在跑的旧部署；
  但它拦不住「在推送之后才启动」的旧 run，那种情况只能靠上面这条纪律。

排查线上是哪个提交在上线，看最后完成的部署：

```bash
curl -s "https://api.github.com/repos/mycat323/bili-knowledge/deployments?environment=github-pages&per_page=3" \
  | grep -o '"sha": "[a-f0-9]*"' | head -1
```

## 视觉样式入口

后续调整网页视觉时，按下面顺序修改，避免内容同步覆盖设计：

1. `docs/.vitepress/theme/tokens.css`：颜色、字体、圆角、阴影与动效速度。
2. `docs/.vitepress/theme/custom.css`：导航、侧边栏、正文和响应式布局。
3. `docs/.vitepress/theme/components/HomeHero.vue`：首页结构与首屏视觉。
4. `docs/.vitepress/config.mts`：站点名称、导航、搜索和构建配置。

`npm run sync` 只重建内容页，不会覆盖以上主题文件。

## 版权声明

所有内容整理自 B 站 UP 主公开视频，版权归原作者与原 UP 主所有，本站仅供个人学习。
