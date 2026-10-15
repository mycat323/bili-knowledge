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
│   ├── build-content.mjs    # 内容同步脚本（本地运行，需知识库源文件）
│   ├── fetch_vault.py       # 抓取流水线入口：UID → 三层知识库
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

### 抓取新的内容

`scripts/fetch_vault.py` 负责「B 站 UID → 三层知识库语料」，产出直接落在 `sync` 读取的位置。

```bash
# 首次：建虚拟环境并安装依赖
uv venv .venv --python 3.12
uv pip install --python .venv/Scripts/python.exe -r scripts/requirements.txt
cp .env.example .env        # 填入转写与撰写模型的密钥

# 看看会抓到什么（不下载、不调用模型）
.venv/Scripts/python.exe scripts/fetch_vault.py --uid <UID> --vault <key> --limit 5 --dry-run

# 正式抓取
.venv/Scripts/python.exe scripts/fetch_vault.py --uid <UID> --vault <key> --limit 5
```

常用开关：

| 开关 | 作用 |
| --- | --- |
| `--limit N` | 抓取条数，按发布时间从新到旧 |
| `--bvid BV...` | 只处理指定视频，可重复 |
| `--dry-run` | 只列出将抓取的视频 |
| `--no-stt` | 只用转写缓存，不发起新的转写 |
| `--refresh-llm` | 忽略文章/原子笔记缓存，重新生成 |
| `--only-knowledge` | 不处理视频，只用已有缓存重写体系长文 |
| `--no-knowledge` | 跳过体系长文阶段 |

中间产物缓存在 `.cache/fetch/<BV号>/`：转写是整条流水线里最贵的一步，
重复运行会自动复用，不会重复计费。

## 新增一个 UP 主知识库

以「毕导」（UID 254463269）为例，总共三步。

**1. 在 `scripts/build-content.mjs` 注册**（站点侧的唯一事实来源，抓取脚本会读它）

在 `VAULTS` 数组追加一项，字段顺序必须是 `key / name / dir / subject`：

```js
{
  key: 'bidao',                 // 决定 URL 前缀与 docs 目录名
  name: '毕导',
  dir: '毕导-科学科普',          // 源知识库子目录名，须与磁盘一致
  subject: '科学科普',
  description: '……',
  tags: ['科学科普', '生活实验', '数学物理'],
  illustration: 'science-lineart.png',
  avatar: 'avatar-bidao.jpg',
}
```

**2. 补 `ARTICLE_GROUP_RULES`**

同一文件里给新 key 加一份分组规则。不加的话该博主的所有文章都会落进兜底的「其他」分组，
侧边栏与分区页会退化成一个长列表。

**3. 准备素材**

- `assets/images/avatar-<key>.jpg`：UP 主头像（`sync` 会自动拷到 `docs/public/images/`）
- `assets/images/<xxx>-lineart.png`：首页卡片用的雕版线稿（可留空，回落到 `knowledge-still-life.png`）。
  补专属线稿：把米色底雕版图放进 `assets/sources/`，在 `scripts/make-lineart.mjs` 末尾加一行
  `await toLineArt(...)`，再回来填文件名

**4. 抓内容并发布**

```bash
.venv/Scripts/python.exe scripts/fetch_vault.py --uid 254463269 --vault bidao --limit 5
npm run sync -- --allow-partial
npm run build
```

源知识库的目录约定与文件格式见 `scripts/lib/vault.py` 与 `docs/` 下已有页面，
要点是：原子笔记名必须是 `域-子题-概念`（前两段决定站点分组），正文必须有
`## 一句话定义`（本地搜索只索引标题和这一节），双链写作 `[[原子笔记名]]`。

## 视觉样式入口

后续调整网页视觉时，按下面顺序修改，避免内容同步覆盖设计：

1. `docs/.vitepress/theme/tokens.css`：颜色、字体、圆角、阴影与动效速度。
2. `docs/.vitepress/theme/custom.css`：导航、侧边栏、正文和响应式布局。
3. `docs/.vitepress/theme/components/HomeHero.vue`：首页结构与首屏视觉。
4. `docs/.vitepress/config.mts`：站点名称、导航、搜索和构建配置。

`npm run sync` 只重建内容页，不会覆盖以上主题文件。

## 版权声明

所有内容整理自 B 站 UP 主公开视频，版权归原作者与原 UP 主所有，本站仅供个人学习。
