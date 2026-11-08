# 作品回链与数据统计同步 — 设计 spec

日期：2026-11-01
状态：草稿，待用户审阅

## 1. 背景与目标

### 1.1 现状问题

| # | 问题 | 证据 | 严重性 |
|---|---|---|---|
| A | `publish_details.publish_url` 是预留字段，发布成功后**从未回填**（仅京东填了创作者中心地址，不是作品页）。前端「查看发布作品 →」链接实际从不显示 | `task_queue.py` 的 `_update_db` 写 `task.publish_url`，但无任何代码对其赋值；`PublishHistoryDetail.vue:90-98` 的链接 `v-if="selectedItem.publish_url"` 永假 | 高 |
| B | 发布记录与平台上的真实作品没有关联，无法跟踪播放量/点赞/评论等数据表现 | 无任何作品级数据表 | 高 |
| C | 前端已预留统计 UI 但永远是占位符 | `PublishHistoryDetail.vue:137-141`、`PublishHistory.vue:211-213` 的 `<PublishStats>` 组件四格（播放/点赞/收藏/评论）props 全空，tooltip 显示「数据统计功能开发中」 | 中 |
| D | 已有账号级资料同步（`sync_profile`），没有作品级数据同步 | `app.py:596 /syncProfile` | — |

### 1.2 目标

1. **回链**：发布成功后自动把该账号在该平台的作品页面 URL 回填到 `publish_details.publish_url`，并尽量保存平台作品唯一 ID（`work_id`）。
2. **统计同步**：按可配置间隔定期同步每个已发布作品的播放/点赞/收藏/评论数据，在发布历史列表（批次聚合）和详情页（单作品）展示。
3. **手动入口**：详情页「立即同步 / 重新关联」按钮；存量历史记录可补绑。
4. **三种发布类型全覆盖**：视频（video）、图文/图集（image）、文章（article）统一走同一套契约与调度，按类型路由到各平台对应的作品列表页。
5. 平台页面改版或未实现时优雅降级，不影响发布主流程。

### 1.3 严格范围

> **不修改任何平台的发布函数**（`publish_video` / `publish_image` / `publish_article`，21 个平台目录 / registry 注册 20 个，jd 由京麦委托复用，全部零改动）。登录、素材库、任务队列的既有逻辑不动，只在成功路径上追加钩子。

## 2. 核心设计：统一的 `list_works` 契约

### 2.1 关键决策

各平台发布成功后，页面都会跳转到创作者中心的「内容管理」页（如抖音 `creator.douyin.com/creator-micro/content/manage`，`douyin/platform.py:719-723`）。该页同时承载**作品链接**与**统计数据**。

**为什么不在发布函数里抓链接**（调研确认）：21 个平台实现中，发布成功后无一抓取过作品 ID/链接；且多数平台 `publish_video` 无条件 `return True`（youtube L390、tencent_video L497、vivo L378、jd L350、光合 L732），成功语义本身不可靠。若走"发布函数返回 dict"路线，需同时改 21 个实现 + 全部调用方，爆炸半径过大。因此本设计**不改发布流程**，新增一个平台能力方法，发布回链和统计同步都从它派生：

```python
# base_platform.py 新增（可选实现，默认抛 NotImplementedError）
async def list_works(
    self, cookie_file: str, limit: int = 20, work_type: str = ""
) -> list[dict]:
    """抓取创作者中心「内容管理」页的作品列表（按发布时间倒序）。

    Args:
        work_type: 'video' | 'image' | 'article'（与 publish_batches.type 同义）。
            同一平台不同类型的作品往往在不同的管理页/tab（如微博视频在主页
            视频 tab、文章在 card.weibo.com），必须按类型路由；传空 = 平台
            单一列表页通吃时才允许（如抖音视频+图集同页）。平台不支持该
            类型 → 返回 []。

    返回 dict 字段约定：
      work_id      str   平台作品唯一 ID（页面拿不到就空串）
      work_type    str   'video' | 'image' | 'article'（回填调用方）
      title        str   作品标题
      publish_time str   平台展示的发布时间文本（原样保存）
      url          str   公开作品页 URL（平台无法提供时为空串，统计仍可同步）
      views        int|None   播放/阅读量
      likes        int|None   点赞/获赞
      favorites    int|None   收藏
      comments     int|None   评论
      share        int|None   转发/分享
    页面不可见/未过审/抓取失败 → 返回 []（不抛异常）。
    """

    raise NotImplementedError(...)
```

**三种发布类型全覆盖**（本项目 `publish_batches.type` 共三种，均写同一套表）：

| 类型 | 发布入口 | 写入路径 | 绑定/同步覆盖方式 |
|---|---|---|---|
| `video` 视频 | 发布中心 → TaskQueue | `task_queue.py` worker 写 success | 中央绑定协调器轮询 DB（§2.2），三类型统一 |
| `image` 图文/图集 | ImagePublish / douyin_image / kuaishou_image 等 Blueprint | `image_publish_bp` 等直接写 `publish_details`（不经 TaskQueue） | 同上 |
| `article` 文章 | ArticlePublish → `article_publish_bp.py` | 直接写 `publish_details`（不经 TaskQueue，L68-100 自带状态聚合） | 同上 |

各平台按类型路由到哪个列表页，见 §2.4 路由表。

实现方式与 `sync_profile` 完全同构：headless 浏览器 + cookie storage_state 加载 + 页面 scrape，复用 `_browser.py` 现有基建。

**三个现成资产**（调研实证）：
1. 各平台发布成功后的落地页白名单已集中在 `backend/impl/_manual_review.py` 的 `is_success_url`（L43-60）——`list_works` 抓取的正是这些列表页，URL 常量直接复用各平台已有的 `_MANAGE_URL` / `_KS_MANAGE_URL_PATTERN` 等。
2. TikTok 已实现内容管理页首行作品 ID 抓取 `_get_last_video_id`（tiktok/platform.py:927-949，抓到后仅写日志丢弃）——证明「落地列表页抓首行作品链接」路线可行，抖音/快手/B站页面同构。
3. 单作品统计抓取的项目内参考：头条 `_fetch_platform_json`（toutiao/platform.py:3320-3346，页面上下文 fetch + credentials 直调 JSON 接口）、大鱼号 ResponseCapture（init_script 注入记录响应，L235-283）、微博页内 fetch（L2751-2831）。

### 2.2 作品回链（bind）

两条路径，同一套匹配逻辑：

- **自动绑定（中央绑定协调器）**：运行在 `work_stats_sync` daemon 线程内的快速轮询（30~60s 一拍），扫描 `status='success' AND publish_url='' AND created_at >= 近 workStatsBindWindowDays（默认 2 天）` 的 `publish_details`，按 `(平台, work_type, 账号)` 分组批量调 `list_works(limit=5)` → 匹配 → 回填 `publish_url + work_id`。
  - **为什么轮询 DB 而不在发布成功处挂钩子**：三种发布类型写库路径不同——视频经 TaskQueue worker，图文/文章在各自 Blueprint 直接写库（`article_publish_bp.py:68-100`，不经 TaskQueue）。挂钩子要改三处且违背 §1.3 最小改动原则；轮询方案一处覆盖三种类型，且对既有代码**零侵入**。
  - **重试退避**：新成功 detail 首次尝试在发布后 ~1 分钟内；失败按 1min → 5min → 30min 退避（B站转码/视频号过审等场景靠此覆盖），退避到底后降为随定时同步每轮顺带重试；平台连续 N 次返回空列表后置 `bind_exhausted`（新列）停止尝试，手动补绑可复位。
- **手动补绑**：`POST /api/v2/history/<batch_id>/bind-works`，前端「重新关联」按钮触发；用于存量记录和 `bind_exhausted` 的记录，调用时清空退避计数立即重试。

**匹配规则**（防误绑）：
1. **类型一致**：detail 的类型（`publish_batches.type`）必须等于 work 的 `work_type`——同一账号可能发过同名视频和同名图文，类型是第一道过滤；
2. 标题归一化（去首尾空白、压缩连续空白）后全等比较；
3. 多条命中时取发布时间与 `finished_at` 最接近的一条；
4. 限定 `list_works(limit=5)` 的近端窗口，只可能匹配到刚发布的作品；
5. 命中后写 `work_id`，后续同步优先按 `work_id` 定位。

**降级路径**：部分平台发布后作品需过审才出现在内容管理页（B站转码/视频号审核等）→ 回链延迟不等于发布失败：发布状态一律不回改，`publish_url` 留空待退避重试命中；`bind_exhausted` 后仅手动「重新关联」可复位重试。

### 2.3 统计同步调度

参照 `services/duration_repair.py:325 start_repair_in_background` 的 daemon 线程模式，新增 `services/work_stats_sync.py`：

```
启动：app.py 启动时拉起 daemon 线程
两类节拍（同一线程内调度）：
  [快速拍] 每 30~60s：§2.2 绑定协调器（只查近 2 天未绑定 success，代价低）
  [同步拍] 每轮：
    1. 读 settings.workStatsSyncInterval（分钟，0=关闭，默认 60），为 0 则本轮跳过
    2. SELECT 近 workStatsSyncWindowDays（默认 30）天 d.status='success' 的
       publish_details JOIN publish_batches b ON d.batch_id=b.id 取 b.type AS work_type，
       按 (account_id, work_type) 二元组分组（同一账号可能既有视频又有图文/文章）
    3. 若 task_queue 有 running 任务 → 本轮跳过（见 §8 风控）
    4. 逐 (账号, 类型)：get_platform(account.type).list_works(
           cookie, limit=50, work_type=<该组类型>)
       → 按 work_id（优先）/标题匹配到组内各 detail → 写 publish_work_stats 快照
       → 对无 work_id 的行顺带执行 §2.2 绑定
    5. 组与组之间随机 sleep 3~8 秒；单组失败不影响其它组
```

手动触发：`POST /api/v2/works/sync`（body 可选 `detail_ids`），复用同一轮询函数。

### 2.4 按发布类型的作品列表页路由表

`list_works(work_type=...)` 的路由依据。**视频（video）**全平台支持；**图文/图集（image）**支持平台：抖音、小红书、快手、微博、公众号、支付宝；**文章（article）**支持平台：微博、百家号、头条、CSDN、知乎、公众号、大鱼号。其余组合平台直接返回 `[]`。

| 平台 | video 视频列表页 | image 图文/图集列表页 | article 文章列表页 |
|---|---|---|---|
| 抖音 | `creator-micro/content/manage`（L719-722 实证） | 同页图集 tab（发布终点同页，L1382-1391 实证） | 不支持 |
| 小红书 | 笔记管理 `/new/note-manager`（发布跳 success 页后主动 goto） | 同页（混排/tab 结构需踩点） | 不支持 |
| 快手 | `article/manage/video?status=2`（L43 实证） | 同页 `tabType=2`（发布终点同页，L589-592 实证） | 不支持 |
| B站 | 稿件管理 `upload-manager/article`（`BILIBILI_MANAGE_URL` L41 实证） | 不支持 | 不支持 |
| 视频号 | `platform/post/list`（L47 实证） | 不支持 | 不支持 |
| 微博 | 个人主页视频 tab（发布不导航，**需侦察**）或拦截发布接口 | 个人主页 feed（**需侦察**） | card.weibo.com 文章管理列表（**需侦察**；发表响应拦截基建已有 L2425-2443） |
| TikTok | `tiktokstudio/content`（L916-919 实证） | 不支持 | 不支持 |
| 百家号 | 内容管理页（发布跳 `/builder/rc/clue`，列表页 **需侦察**） | 不支持 | 同一内容管理的文章 tab（**需侦察**） |
| 头条 | `mp.toutiao.com` 内容管理视频 tab（**需侦察**） | 不支持 | 同页文章 tab（**需侦察**） |
| CSDN | `mp.csdn.net` 内容管理视频 tab（**需侦察**） | 不支持 | 同页文章 tab（**需侦察**） |
| 知乎 | 创作中心视频列表（**需侦察**） | 不支持 | 文章管理列表（**需侦察**；`creator/analytics/work/all` 按作品维度可作统计补充源） |
| 公众号 | 素材库视频列表（cgi-bin+token 基建已有，L61-64/L185-222） | 素材库图集（同基建） | 已发表列表 `cgi-bin/appmsg?action=list`（token 基建已有） |
| 大鱼号 | `dashboard/contents`（**L42 常量实证**，发布终点即此页） | 不支持 | 同页 contents 列表 |
| 支付宝 | 创作中心内容列表（**需侦察**） | 同页 | 不支持 |
| YouTube | Studio 内容列表 `studio.youtube.com/channel/<id>/videos`（channelId 需先从 studio 页 URL 提取） | 不支持 | 不支持 |
| 腾讯视频 | `mp.v.qq.com` 内容管理（**需侦察**） | 不支持 | 不支持 |
| 爱奇艺 | `creator.iqiyi.com` 内容管理（**需侦察**，注意 L1092-1098 失败判定） | 不支持 | 不支持 |
| 京东/京麦 | `dr.jd.com/jm/#/n/` 视频管理页（hash 路由，内容可能 iframe，用京麦双 scope 模式） | 不支持 | 不支持 |
| 淘宝光合 | 工作台/内容管理（**需侦察**；DOM 定位必须用 `data-autolog-*`） | 不支持 | 不支持 |
| vivo | 跳离 `#/content/uploads` 后的列表页（读 `page.url` 即得入口） | 不支持 | 不支持 |

> **「需侦察」条目的统一踩点流程**（实现每个平台的第一步）：发一条真实测试作品 → 手动进内容管理页记录 URL 与 DOM 结构 → 确定该类型列表页常量与抓取选择器 → 写 `list_works`。视频+图文同页的平台（抖音/快手）一次踩点覆盖两类型。

## 3. 数据模型

```sql
-- 迁移 1：明细表加作品 ID + 绑定退避标记（init_db.migrate_database 幂等追加）
ALTER TABLE publish_details ADD COLUMN work_id TEXT DEFAULT '';
ALTER TABLE publish_details ADD COLUMN bind_attempts INTEGER DEFAULT 0;
ALTER TABLE publish_details ADD COLUMN bind_exhausted INTEGER DEFAULT 0;

-- 迁移 2：统计快照表（每次同步 1 行，天然支持趋势回看）
CREATE TABLE IF NOT EXISTS publish_work_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detail_id TEXT NOT NULL,            -- FK → publish_details.id
    batch_id  TEXT NOT NULL DEFAULT '', -- 冗余，批次聚合免 join
    work_type TEXT NOT NULL DEFAULT '', -- 'video' | 'image' | 'article'
    work_id   TEXT DEFAULT '',
    views     INTEGER,
    likes     INTEGER,
    favorites INTEGER,
    comments  INTEGER,
    share     INTEGER,
    raw       TEXT DEFAULT '{}',        -- 平台原始 scrape 结果 JSON，排查用
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pws_detail ON publish_work_stats(detail_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_pws_batch  ON publish_work_stats(batch_id);
```

- **最新值**：每 `detail_id` 取 `captured_at` 最大的一行（`MAX(captured_at)` 窗口查询）。
- **快照清理**：同步轮内顺手删除 90 天前的快照（可配，防表膨胀）。

## 4. API 设计（全部挂 `ext_api` 蓝图，前缀 `/api/v2`）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/history`、`/history/<batch_id>` | GET | **现有端点扩展**：`_normalize_detail_row` 注入 `item.stats = {views, likes, favorites, comments, synced_at}`（最新快照，无则 null） |
| `/history/<batch_id>/bind-works` | POST | 手动绑定一个批次所有成功明细的作品链接，返回每个 detail 的绑定结果 |
| `/works/sync` | POST | 立即触发一轮同步；body `{detail_ids?: []}` 缺省同步全部；返回 `{"started": true}`（异步执行） |
| `/works/sync-status` | GET | `{running: bool, last_synced_at, enabled, interval_minutes}`，供历史页/设置页展示 |

响应体在批次级追加 `aggregate_stats`（列表页卡片聚合用）：

```json
"aggregate_stats": {"views": 12000, "likes": 300, "favorites": 45, "comments": 67}
// = 各 detail 最新快照求和；有任一 detail 无数据时仍返回已有部分
```

## 5. 前端改动

| 文件 | 改动 |
|---|---|
| `src/api/v2.js` | 新增 `worksApi = { sync(detailIds?), syncStatus(), bindWorks(batchId) }` |
| `src/views/PublishHistoryDetail.vue` | ① `<PublishStats :views="item.stats?.views" ... />` 接线；② 账号信息头加「同步数据」图标按钮（转圈 loading + 最近同步时间 tooltip）；③ `publish_url` 已有链接区旁加「重新关联」入口（仅成功且未绑定时显示） |
| `src/views/PublishHistory.vue` | 卡片 `<PublishStats compact :views="batch.aggregate_stats?.views" ... />` |
| `src/components/PublishStats.vue` | props 有值时移除「开发中」tooltip；占位语义保留（null → `--`）；可选增补 share 格（紧凑模式不下） |
| `src/views/Settings.vue` | 「数据同步」区：开关 + 间隔（分钟）+ 窗口天数 + 上次同步时间 |
| SSE（可选，二期） | 同步完成后经现有 `_sse_subscribers` 推事件，历史页静默刷新 |

## 6. 平台实现矩阵

> 调研基于代码静态分析；标 ⚠️ 的条目需实现时现场踩点确认。`list_works` 各平台统一复用 `sync_profile` 的骨架：headless + `storage_state=cookiesFile/<cookie_file>` + goto 内容管理页 + evaluate 抓 DOM。

### 6.1 Phase 1 组调研结论（抖音/小红书/快手/B站/视频号/微博/TikTok）

| 平台 | 发布落地页（代码实证） | 作品链接难度 | 统计可抓性 | 实现注意点 |
|---|---|---|---|---|
| 抖音 (3) | `creator-micro/content/manage`（视频 L719-722、图集 L1382-1391） | 中：落地列表页抓首行，公开 URL 拼 `www.douyin.com/video/{aweme_id}` | 创作中心首页仅账号级 3 项；manage 列表页行内数据需踩点 | 直接复用 `_parse_count_text`（L41-57）的「1.2万/3亿」→ int 解析 |
| 小红书 (1) | `/publish/success` 或 `/new/note-manager`（白名单 `_manual_review.py:52-55`） | 中：去笔记管理抓首条笔记，公开 URL `explore/{note_id}` | 创作中心首页账号级 | 视频发布是同步 def；发布点击用 CDP 穿透 closed shadow DOM（L1035-1087） |
| 快手 (4) | `article/manage/video?status=2&from=publish`（L43/L867） | 中：落地列表页抓首行，公开 URL `short-video/{photoId}` | 仅上传页 popover 账号级 | 视频与图集落同一 manage 列表页，一套抓取通吃 |
| B站 (5) | success 页或稿件管理 `upload-manager/article`（`BILIBILI_MANAGE_URL` L41） | 中偏易：稿件管理页抓 bvid，公开 URL `/video/{bvid}` | `member.bilibili.com/platform/home` 账号级 8 项（含聚合播放/点赞/评论） | 作品有转码期，绑定依赖 §2.2 重试机制 |
| 视频号 (2) | `platform/post/list`（L47/L1239） | **难**：DOM 不含公开链接（finderUsername+objectId+nonceId 三元组），需拦截接口响应 | 创作中心仅 2 项账号级（视频数/关注者） | Phase 1 唯一特例：先做「标题匹配 + 列表页统计同步」，`url` 留空；链接捕获二期用响应拦截攻关 |
| 微博 (11) | 视频/图文不导航（toast/按钮态判定）；文章编辑器已拦截响应 | 文章=**易**（draft_id 已抓 L2589-2618、发表响应体已在手 L2425-2443）；视频/图文=难 | 无创作中心统计页，仅个人主页账号级 | 响应拦截 + `_safe_json`（L2545-2552）是项目内最佳参考实现 |
| TikTok (7) | `/tiktokstudio/content`（L916-919） | **易**：`_get_last_video_id`（L927-949）已实现，仅差透传落库 | Studio 首页仅昵称头像；Analytics 页需踩点 | `sync_profile` 仍是旧 2 元组约定（L177/L202），做 `list_works` 时一并对齐 dict 约定 |

通用可行路径排序（调研结论）：① 落地列表页抓首行作品链接（TikTok 已验证，抖音/快手/B站页面同构）；② 发布 XHR 响应拦截（微博文章已验证）；③ URL 模板拼接（依赖 ①/② 先拿到 ID）。

### 6.2 Phase 2 组调研结论（百家号/头条/CSDN/知乎/公众号/大鱼号/支付宝）

| 平台 | 发布落地页（代码实证） | 作品链接难度 | 统计可抓性 | 实现注意点 |
|---|---|---|---|---|
| 百家号 (6) | 视频：`/builder/rc/clue`（L655-658）；文章：编辑器内软等待 | 文章=中偏易：`/pcui/article/publish` 响应体已在手（L2755-2799），公开 URL `baijiahao.baidu.com/s?id=<id>` 需实测解析；视频=难：需内容列表页抓（列表页 URL 需侦察） | 首页 6 项账号级累计数据（L266-295）；单作品页未找到 | 文章响应解析只差「挑 id 字段」一步 |
| 头条 (13) | 视频：弱判定不校验跳转（L720-725）；文章：监听 `/mp/agw/article/publish` | 文章=**易**：`pgc_id` 已捕获（L3737）但仅日志丢弃；视频=难→中：仿文章监听视频提交接口 | 创作中心 `.data-board` 3 项账号级（L250-293） | **`_fetch_platform_json`（L3320-3346）页面内 fetch + credentials 直调 JSON 接口是全项目最可复用的统计抓取模式** |
| CSDN (15) | 视频：60s 未跳转也算成功（L997-998）；文章：监听 `saveArticle` | 文章=**易**：saveArticle 响应 `article_id`+`url` 已拿到（L2526-2528，仅日志）；视频=中：需列表页抓 | `mp.csdn.net` 用户卡片 6 项账号级（L220-304） | payload.url 形态（编辑页 vs 公开页）需实测一次 |
| 知乎 (14) | 视频：监听 `/api/v4/content/publish` 后不导航（L1443-1447）；文章：发表后跳 `/p/<id>` 但不读（L2923-2933） | 文章=**易**：跳转后读 `page.url` 即得公开文章页；视频=难：需创作中心列表抓 | **全项目唯一作品分析页**：`zhihu.com/creator/analytics/work/all`（L236/L321-357，按作品维度列流量+互动，当前只抓累计 tab） | `list_works` 可先抓作品列表页；分析页的单作品明细是最接近目标的统计源 |
| 公众号 (17) | 发表成功 = 跳 `/cgi-bin/home` 或「已发送操作申请」弹窗（L2049-2092）；文章草稿 ID 在 URL `appmsgid=`（L3891） | 草稿=易；发表后=难：跳首页，公开链接 `mp.weixin.qq.com/s/xxx` 需内容管理列表抓；视频/图集=难 | `/cgi-bin/home` 仅原创内容+总用户数（L479-499） | **cgi-bin「页面内 fetch + token」基建已有**（token 解析 L185-222、fetch 上传 L92-126）；注意发表=群发、订阅号每日 1 次需管理员扫码，该平台数据稀疏属正常 |
| 大鱼号 (21) | 视频：成功后恰停在内容管理页 `/dashboard/contents`（L42/L931-933）；文章：ResponseCapture 捕获 `content_id/_id`（L2615-2616） | 文章=**易**：content_id 已捕获仅日志；视频=中：落地页就是内容管理列表，按标题抓 | 首页 3 项账号级（L532-536）；单作品页未找到 | ResponseCapture（init_script 注入 localStorage 记录响应，L235-283）是通用 JSON 捕获器，可复用于统计接口 |
| 支付宝 (12) | 跳离发布页或「发布成功」文案即成功（L1949-1965） | **难**（Phase 2 最差）：无 ID 无跳转，生活号内容主要在 App 内消费，Web 公开作品页存疑 | 创作中心 `.numBox` 粉丝/获赞（L260-288） | 建议最后实现；`list_works` 可能长期返回 `[]`，属预期降级 |

> 说明：头条/CSDN/大鱼号/知乎/百家号的文章发布在 publish 内部已捕获作品 ID（当前丢弃）。本 spec 按 §1.3 范围**不改发布函数**，统一走内容管理列表页抓取；「把内部 ID 从 helper 透传出来」可作为后续可选优化（每平台约一行 return 的改动），不阻塞本方案。

### 6.3 Phase 3 组调研结论（YouTube/腾讯视频/爱奇艺/京东/京麦/光合/vivo）

| 平台 | 发布终点（代码实证） | 作品链接难度 | 统计可抓性 | 实现注意点 |
|---|---|---|---|---|
| YouTube (8) | Studio 上传对话框页，无跳转 | 中：Done 对话框 DOM 含 `watch?v=` 链接；公开 URL 可拼 | Studio analytics 页可抓（代码未涉及，需踩点） | channelId 目前未提取，需从 studio 页 URL 取 |
| 腾讯视频 (9) | mp.v.qq.com 域内（未记录） | 中：内容管理列表抓 | ✅ 已有账号级分析页 `mp.v.qq.com/analysis/overview/data`（L194）；单作品页需踩点 | `page.on("request")` 监听 tRPC 响应截 ID 是现成模式（L530-538） |
| 爱奇艺 (10) | creator.iqiyi.com 成功页/管理页 | 中：内容管理列表抓（列表页 URL 需踩点） | 仅账号级（首页 user-info）；单作品页需踩点 | ⚠️ 现代码把"跳到管理页"判为发布失败（L1092-1098），`list_works` 与该判定无关但踩点时注意 |
| 京东 (20) | dr.jd.com/jm/#/n/ 管理页（hash 路由） | 难：公开作品页形态不明，仅管理页预览/分享入口 | 统计页未找到；管理页可能又是 iframe | 公开 URL 拿不到时可只回填管理页深链或不回填 |
| 京东京麦 (19) | 同京东（publish_video 直接委托 JdPlatform L354-356） | 难：同京东 | `_ACCOUNT_INFO_URL` 已定义未使用（L48） | 必抄京麦"主页面+iframe 双 scope 轮询"模式（L265-289） |
| 淘宝光合 (18) | /page/workspace/tb 工作台 | 中：工作台/内容管理列表抓 | 仅账号级；DOM 定位必须用 `data-autolog-*` 埋点属性（L508-577 黄金法则） | ⚠️ 60s 未跳转也 return True（L1759-1760），绑定失败属常态，依赖重试 |
| vivo (16) | kaixinkan.com.cn 跳离 `#/content/uploads` 后的列表页 | 中：读 page.url 即得列表入口 | 仅账号级（首页） | ⚠️ 未跳转仅 warning 仍算成功（L574-575） |

### 6.4 分期

分期原则：
- **Phase 1（主力短视频）**：抖音、小红书、快手、B站、视频号、微博；TikTok 随本期附带交付（`_get_last_video_id` 现成）
- **Phase 2（图文/文章/海外）**：YouTube、百家号、今日头条、知乎、CSDN、微信公众号、大鱼号
- **Phase 3（长视频/电商/其它）**：腾讯视频、爱奇艺、京东京麦、淘宝光合、VIVO、支付宝（最后：支付宝公开作品页存疑，允许长期降级为「仅统计不回链」）

> 补充：知乎的 `creator/analytics/work/all` 是全项目唯一按作品维度展示的数据页，是单作品统计的最佳实现参照；大鱼号是唯一已有内容管理页常量的平台（`/dashboard/contents`，且视频发布成功恰好停在该页）。`get_statistics` 全代码库无任何实现（仅基类 stub），本 spec 的 `list_works` 是第一个作品级数据方法。

## 7. 里程碑与验收

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| M1 基础设施 | DB 迁移、`list_works` 契约、绑定协调器 + 调度器、API、前端接线；**三类型各一试点**：抖音（视频+图集同页，一石二鸟）+ 大鱼号（文章，`contents` 常量现成） | 发 1 条视频 + 1 条图文（抖音）、1 篇文章（大鱼号）→ ≤2 分钟内 `publish_url` 全部回填；手动同步后详情页四格出现真实数字 |
| M2 主力平台 | Phase 1 全部平台（含快手/小红书的图文路径）+ 批次聚合 + 绑定退避 | 6 平台视频各发 1 条 + 图文平台各发 1 条 → 历史卡片显示聚合播放量；过审期作品（B站）经退避后自动补绑 |
| M3 全量 + 存量 | Phase 2/3 平台（含微博/知乎/CSDN/头条/百家号/公众号文章路径）+ 存量补绑入口 + 设置页 | 三种类型的存量近 30 天记录均可手动补绑出链接 |

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| 频繁打开浏览器触发平台风控 | 与发布互斥（task_queue running>0 跳过本轮）；默认间隔 60min；账号间随机 3-8s；headless 与 sync_profile 同配置 |
| 平台页面改版导致抓取失败 | `list_works` 内部 try/except 返回 `[]`，只影响单平台；`raw` 存原始结果便于排查；失败计数日志 |
| 标题匹配误绑 | 类型一致 + 归一化 + 时间窗 + `work_id` 优先四重约束；绑错可手动「重新关联」覆盖 |
| 同步拖慢主服务 | 全部在 daemon 线程；快照表带索引；90 天外快照自动清理 |
| cookie 失效 | `list_works` 检测到登录页即返回 `[]` 并记日志，不在同步链路里做重登录 |

## 9. 明确不做（本版）

- 不做绕过浏览器直调平台 HTTP 接口（项目内无先例；抖音 X-Bogus、快手 __NSig3 等签名方案风险高。统计抓取统一走登录态浏览器页面 scrape，必要时用页内 `fetch()`——已有先例：微博 L2751-2831、头条 `_fetch_platform_json`、公众号 cgi-bin+token）
- 不做作品数据趋势图表（快照表已具备能力，UI 二期再说）
- 不做评论/弹幕内容抓取
- 不做跨账号作品去重分析
- 不改任何平台的发布函数与发布状态机
