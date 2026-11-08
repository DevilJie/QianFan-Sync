"""
测试作品回链与数据统计同步（services/work_stats_sync.py + ext_api 注入字段）。

覆盖：
- 标题归一化匹配（_normalize_title / _match_work / normalize_works）
- 描述兜底匹配（平台标题=描述+#话题时按描述前缀兜底，含时间窗约束）
- 类型不一致不匹配
- 绑定退避状态流转（_bind_due / _bind_one：命中清零、未命中+1、超 30 分钟置 exhausted）
- /history 的 detail.stats 注入与批次级 aggregate_stats 求和
- /works/sync、/works/sync-status、/history/<id>/bind-works 端点行为
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# 把 backend 目录加进 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services import work_stats_sync as wss


def _setup_db(db_path: Path):
    """初始化临时 DB：b1（3 个成功明细）+ b2（1 个成功明细，无统计快照）。

    不能直接调 init_db.init_database()，因为 init_db.DB_PATH 在 import 时已绑定。
    直接对临时 DB 跑 schema 脚本（与 init_db.py 保持一致，含本次新增的
    publish_details 三列 + publish_work_stats 表）。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA_SQL)
    now = datetime.now()
    conn.execute(
        "INSERT INTO user_info (id, type, filePath, userName) VALUES (1, 3, 'acc1.json', '账号A')")
    conn.execute(
        "INSERT INTO publish_batches (id, type, title, status, account_count, success_count, "
        "failed_count, created_at) VALUES ('b1', 'video', '测试视频1', 'success', 3, 3, 0, "
        "?)", (now.isoformat(timespec="seconds"),))
    conn.execute(
        "INSERT INTO publish_batches (id, type, title, status, account_count, success_count, "
        "failed_count, created_at) VALUES ('b2', 'image', '测试图文1', 'success', 1, 1, 0, "
        "?)", (now.isoformat(timespec="seconds"),))
    for did in ('d1', 'd2', 'd3'):
        conn.execute(
            "INSERT INTO publish_details (id, batch_id, account_id, account_name, platform, "
            "account_configs, status, finished_at) VALUES (?, 'b1', 1, '账号A', '抖音', ?, "
            "'success', ?)",
            (did, json.dumps({"title": "测试视频1"}), now.isoformat(timespec="seconds")))
    conn.execute(
        "INSERT INTO publish_details (id, batch_id, account_id, account_name, platform, "
        "account_configs, status, finished_at) VALUES ('d4', 'b2', 1, '账号A', '抖音', ?, "
        "'success', ?)",
        (json.dumps({"title": "测试图文1"}), now.isoformat(timespec="seconds")))
    # 统计快照：d1 两条（验证取最新一行）；d2 一条；d3/d4 无快照
    conn.execute(
        "INSERT INTO publish_work_stats (detail_id, batch_id, work_type, work_id, views, likes, "
        "favorites, comments, share, captured_at) VALUES "
        "('d1', 'b1', 'video', 'w1', 100, 10, 1, 2, 3, '2026-10-01 04:00:00')")
    conn.execute(
        "INSERT INTO publish_work_stats (detail_id, batch_id, work_type, work_id, views, likes, "
        "favorites, comments, share, captured_at) VALUES "
        "('d1', 'b1', 'video', 'w1', 250, 30, NULL, NULL, NULL, '2026-10-02 04:00:00')")
    conn.execute(
        "INSERT INTO publish_work_stats (detail_id, batch_id, work_type, work_id, views, likes, "
        "favorites, comments, share, captured_at) VALUES "
        "('d2', 'b1', 'video', 'w2', 40, 5, 2, NULL, NULL, '2026-10-02 04:00:00')")
    conn.commit()
    conn.close()


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type INTEGER NOT NULL,
    filePath TEXT NOT NULL,
    userName TEXT NOT NULL,
    status INTEGER DEFAULT 0,
    avatar TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS publish_batches (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    video_material_id TEXT DEFAULT '',
    image_material_ids TEXT DEFAULT '[]',
    landscape_cover_material_id TEXT DEFAULT '',
    portrait_cover_material_id TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    account_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    schedule_time TEXT DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    draft_id INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS publish_details (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    account_id INTEGER,
    account_name TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    account_configs TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    error_message TEXT NOT NULL DEFAULT '',
    publish_url TEXT NOT NULL DEFAULT '',
    work_id TEXT DEFAULT '',
    bind_attempts INTEGER DEFAULT 0,
    bind_exhausted INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    FOREIGN KEY (batch_id) REFERENCES publish_batches(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS publish_work_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detail_id TEXT NOT NULL,
    batch_id TEXT NOT NULL DEFAULT '',
    work_type TEXT NOT NULL DEFAULT '',
    work_id TEXT DEFAULT '',
    views INTEGER,
    likes INTEGER,
    favorites INTEGER,
    comments INTEGER,
    share INTEGER,
    raw TEXT DEFAULT '{}',
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pws_detail ON publish_work_stats(detail_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_pws_batch ON publish_work_stats(batch_id);
"""


def _make_work(**over):
    """构造一条符合 list_works 契约的作品 dict。"""
    w = {"work_id": "", "work_type": "video", "title": "", "publish_time": "",
         "url": "", "views": None, "likes": None, "favorites": None,
         "comments": None, "share": None}
    w.update(over)
    return w


class TestWorkMatching(unittest.TestCase):
    """标题归一化匹配 / 类型过滤 / 字段清洗（纯函数，不需要 DB）。"""

    def test_normalize_title(self):
        """去首尾空白 + 压缩连续空白"""
        self.assertEqual(wss._normalize_title("  测试   视频  "), "测试 视频")
        self.assertEqual(wss._normalize_title("标题"), "标题")
        self.assertEqual(wss._normalize_title(None), "")
        self.assertEqual(wss._normalize_title(""), "")

    def test_match_by_normalized_title(self):
        """归一化后全等即命中（空白差异不影响）"""
        works = [_make_work(work_id="w1", title="  我的   视频 ", url="https://v/1")]
        got = wss._match_work("我的 视频", works, "video", None)
        self.assertIsNotNone(got)
        self.assertEqual(got["work_id"], "w1")

    def test_type_mismatch_not_matched(self):
        """类型不一致不匹配（同名视频和图文并存的第一道过滤）"""
        works = [_make_work(work_id="w1", work_type="image", title="同名作品", url="https://p/1")]
        self.assertIsNone(wss._match_work("同名作品", works, "video", None))

    def test_prefix_match_unique_hit(self):
        """全等无命中时前缀兜底：平台列表标题比发布标题短（如知乎 pin 首块文本），
        唯一前缀命中可用"""
        works = [_make_work(work_id="w1", title="新建女孩", url="https://p/1")]
        got = wss._match_work("新建女孩 大美新疆", works, "video", None)
        self.assertIsNotNone(got)
        self.assertEqual(got["work_id"], "w1")
        # 反向：平台标题比发布标题长（截断拼接）
        works2 = [_make_work(work_id="w2", title="旅行 vlog 第一天 出发", url="https://p/2")]
        got2 = wss._match_work("旅行 vlog 第一天", works2, "video", None)
        self.assertIsNotNone(got2)
        self.assertEqual(got2["work_id"], "w2")

    def test_prefix_match_multiple_hits_not_matched(self):
        """前缀命中多于一条时不采用（防误绑）：两条作品都是明细标题的前缀"""
        works = [
            _make_work(work_id="w1", title="旅行 vlog 第一天", url="u1"),
            _make_work(work_id="w2", title="旅行 vlog 第一天 番外 加更", url="u2"),
        ]
        # "旅行 vlog 第一天 番外" 以 w1 开头，同时被 w2 开头 → 两案并存弃用
        self.assertIsNone(wss._match_work("旅行 vlog 第一天 番外", works, "video", None))

    def test_prefix_match_short_title_not_matched(self):
        """较短一方 <6 字符时不用前缀兜底（防短标题误绑）"""
        works = [_make_work(work_id="w1", title="测试", url="u1")]
        self.assertIsNone(wss._match_work("测试视频 2026", works, "video", None))

    def test_multi_hit_picks_closest_publish_time(self):
        """多条命中取发布时间与 finished_at 最接近的一条"""
        works = [
            _make_work(work_id="w_far", title="T", publish_time="2026-11-01 08:00", url="u1"),
            _make_work(work_id="w_near", title="T", publish_time="2026-11-01 09:59", url="u2"),
        ]
        got = wss._match_work("T", works, "video", "2026-11-01T10:00:00")
        self.assertEqual(got["work_id"], "w_near")


class TestDescFallbackMatching(unittest.TestCase):
    """描述兜底匹配（_match_work detail_desc / _match_by_desc / _detail_desc）。

    百家号视频发布页无独立标题输入, 平台作品标题 = 描述 + #话题后缀,
    系统批次标题与平台标题必然不同 → 标题匹配失败后用描述前缀兜底。
    """

    FINISHED_AT = "2026-11-02T21:16:34"

    def _video(self, **over):
        over.setdefault("publish_time", "2026-11-02 21:17:47")
        return _make_work(work_type="video", **over)

    def test_desc_fallback_unique_hit(self):
        """标题匹配失败 + 描述前缀唯一命中 + 同时间窗 → 兜底命中"""
        works = [self._video(
            work_id="w1", title="杨氏之子之国学之美 #杨氏之子#国学之美",
            url="https://mbd/1")]
        got = wss._match_work(
            "系统批次标题", works, "video", self.FINISHED_AT,
            detail_desc="杨氏之子之国学之美")
        self.assertIsNotNone(got)
        self.assertEqual(got["work_id"], "w1")

    def test_desc_fallback_title_truncated_by_platform(self):
        """平台标题被截断得比描述短（描述以平台标题开头）→ 同样可兜底"""
        works = [self._video(
            work_id="w1", title="杨氏之子之国学之美讲杨氏之子", url="u1")]
        got = wss._match_work(
            "系统批次标题", works, "video", self.FINISHED_AT,
            detail_desc="杨氏之子之国学之美讲杨氏之子 这篇课文很有意思")
        self.assertIsNotNone(got)
        self.assertEqual(got["work_id"], "w1")

    def test_desc_fallback_rejects_out_of_window_work(self):
        """同描述的旧作品超出时间窗（>2 天）→ 不兜底（防误绑历史作品）"""
        works = [self._video(
            work_id="w_old", title="杨氏之子之国学之美 #旧话题", url="u1",
            publish_time="2026-10-30 21:17:47")]
        got = wss._match_work(
            "系统批次标题", works, "video", self.FINISHED_AT,
            detail_desc="杨氏之子之国学之美")
        self.assertIsNone(got)

    def test_desc_fallback_requires_parseable_publish_time(self):
        """作品发布时间解析不了 → 不放行（时间窗约束的严格面）"""
        works = [self._video(
            work_id="w1", title="杨氏之子之国学之美 #话题", url="u1",
            publish_time="刚刚")]
        got = wss._match_work(
            "系统批次标题", works, "video", self.FINISHED_AT,
            detail_desc="杨氏之子之国学之美")
        self.assertIsNone(got)

    def test_desc_fallback_without_finished_at_not_matched(self):
        """明细无 finished_at → 无时间约束, 描述兜底不参与"""
        works = [self._video(
            work_id="w1", title="杨氏之子之国学之美 #话题", url="u1")]
        got = wss._match_work(
            "系统批次标题", works, "video", None,
            detail_desc="杨氏之子之国学之美")
        self.assertIsNone(got)

    def test_desc_fallback_multiple_hits_picks_closest(self):
        """描述命中多条（同描述复用连发）→ 收紧时间窗后取发布时间最近的一条"""
        works = [
            self._video(work_id="w_old", title="旅行 vlog 第一天 #旅行", url="u1",
                        publish_time="2026-11-01 09:00:00"),
            self._video(work_id="w_new", title="旅行 vlog 第一天 番外 #旅行", url="u2"),
        ]
        got = wss._match_work(
            "系统批次标题", works, "video", self.FINISHED_AT,
            detail_desc="旅行 vlog 第一天")
        self.assertIsNotNone(got)
        self.assertEqual(got["work_id"], "w_new")

    def test_title_prefix_multi_hits_picks_closest(self):
        """标题前缀多命中（快手发布页无标题, caption=描述, 同账号多条同描述
        作品）→ 收紧 finished_at 时间窗后取发布时间最近的一条"""
        works = [
            self._video(work_id="w_near",
                        title="一分钟讲明白隆基绿能 #基金 #隆基",
                        publish_time="2026-11-02 21:17:00"),
            self._video(work_id="w_far",
                        title="一分钟讲明白隆基绿能 #基金 #隆基",
                        publish_time="2026-11-02 23:30:00"),
        ]
        got = wss._match_work(
            "一分钟讲明白隆基绿能", works, "video", self.FINISHED_AT,
            detail_desc="一分钟讲明白隆基绿能")
        self.assertIsNotNone(got)
        self.assertEqual(got["work_id"], "w_near")

    def test_desc_fallback_short_desc_not_matched(self):
        """描述归一化后不足 4 字符（_prefix_match 下限）→ 不兜底"""
        works = [self._video(work_id="w1", title="测试视频 #话题", url="u1")]
        got = wss._match_work(
            "系统批次标题", works, "video", self.FINISHED_AT,
            detail_desc="测试")
        self.assertIsNone(got)

    def test_desc_fallback_respects_type_filter(self):
        """描述兜底同样受「类型一致」约束：同描述的图文不绑给视频明细"""
        works = [_make_work(
            work_id="w1", work_type="article",
            title="杨氏之子之国学之美 #话题", url="u1",
            publish_time="2026-11-02 21:17:47")]
        got = wss._match_work(
            "系统批次标题", works, "video", self.FINISHED_AT,
            detail_desc="杨氏之子之国学之美")
        self.assertIsNone(got)

    def test_desc_equal_title_falls_back_to_nothing_new(self):
        """描述与标题相同 → 描述兜底等价标题匹配, 不会额外命中"""
        works = [self._video(work_id="w1", title="另一个作品", url="u1")]
        got = wss._match_work(
            "同一标题", works, "video", self.FINISHED_AT,
            detail_desc="同一标题")
        self.assertIsNone(got)

    def test_detail_desc_priority(self):
        """_detail_desc：account_configs.description 优先, 回退批次描述"""
        self.assertEqual(
            wss._detail_desc({
                "account_configs": json.dumps({"description": "账号级描述"}),
                "batch_description": "批次描述",
            }),
            "账号级描述")
        self.assertEqual(
            wss._detail_desc({
                "account_configs": json.dumps({"description": "  "}),
                "batch_description": "批次描述",
            }),
            "批次描述")
        self.assertEqual(wss._detail_desc({"account_configs": "{}"}), "")
        self.assertEqual(wss._detail_desc({}), "")

    def test_normalize_works_cleans_field_types(self):
        """normalize_works：字符串字段转 str、统计字段转 int|None"""
        got = wss.normalize_works([
            {"work_id": 123, "title": "x", "views": "1,200", "likes": 3.0,
             "favorites": "abc", "comments": None, "share": ""},
            "not-a-dict",
            {"work_type": "", "views": None},
        ], work_type="video")
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["work_id"], "123")
        self.assertEqual(got[0]["views"], 1200)
        self.assertEqual(got[0]["likes"], 3)
        self.assertIsNone(got[0]["favorites"])
        self.assertIsNone(got[0]["comments"])
        self.assertIsNone(got[0]["share"])
        self.assertEqual(got[0]["work_type"], "video")
        # work_type 缺失 → 回填调用方参数
        self.assertEqual(got[1]["work_type"], "video")
        self.assertIsNone(got[1]["views"])
        # 非 list 输入 → 空列表
        self.assertEqual(wss.normalize_works(None), [])


class TestBindBackoff(unittest.TestCase):
    """绑定退避状态流转（临时内存 DB + 直接调 _bind_due/_bind_one）。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL)
        # _bind_one 按 id UPDATE，插一行真实明细便于读回验证
        self.conn.execute(
            "INSERT INTO publish_details (id, batch_id, status) VALUES ('d1', 'b1', 'success')")
        self.conn.commit()
        self.now = datetime.now()

    def tearDown(self):
        self.conn.close()

    def _detail(self, **over):
        """构造与 _DETAIL_SELECT 输出同构的明细 dict（默认 2 分钟前完成）。"""
        d = {
            "id": "d1", "batch_id": "b1", "account_id": 1,
            "account_configs": json.dumps({"title": "标题"}),
            "publish_url": "", "work_id": "", "bind_attempts": 0,
            "bind_exhausted": 0,
            "finished_at": (self.now - timedelta(minutes=2)).isoformat(timespec="seconds"),
            "created_at": (self.now - timedelta(minutes=3)).isoformat(timespec="seconds"),
            "work_type": "video", "batch_title": "批次标题",
            "platform_id": 3, "cookie_file": "acc1.json",
        }
        d.update(over)
        return d

    def _row(self):
        return self.conn.execute(
            "SELECT bind_attempts, bind_exhausted, publish_url, work_id"
            " FROM publish_details WHERE id='d1'").fetchone()

    def test_bind_due_windows(self):
        """退避窗口：1min / 5min / 30min，退避到底后不再随快速拍重试"""
        fresh = self._detail(
            finished_at=(self.now - timedelta(seconds=30)).isoformat(timespec="seconds"))
        self.assertFalse(wss._bind_due(fresh, self.now))  # 发布 1 分钟内不试
        self.assertTrue(wss._bind_due(self._detail(), self.now))  # 2 分钟 → 第 1 次可试
        once = self._detail(
            bind_attempts=1,
            finished_at=(self.now - timedelta(minutes=2)).isoformat(timespec="seconds"))
        self.assertFalse(wss._bind_due(once, self.now))  # 失败 1 次后等 5 分钟
        once_due = self._detail(
            bind_attempts=1,
            finished_at=(self.now - timedelta(minutes=6)).isoformat(timespec="seconds"))
        self.assertTrue(wss._bind_due(once_due, self.now))
        twice = self._detail(
            bind_attempts=2,
            finished_at=(self.now - timedelta(minutes=10)).isoformat(timespec="seconds"))
        self.assertFalse(wss._bind_due(twice, self.now))  # 失败 2 次后等 30 分钟
        bottom = self._detail(bind_attempts=3)
        self.assertFalse(wss._bind_due(bottom, self.now))  # 退避到底 → 降为同步顺带重试

    def test_miss_increments_attempts(self):
        """未命中：bind_attempts+1，不置 exhausted（未超 30 分钟）"""
        res = wss._bind_one(self.conn, self._detail(), [], self.now)
        self.conn.commit()
        self.assertFalse(res["bound"])
        row = self._row()
        self.assertEqual(row["bind_attempts"], 1)
        self.assertEqual(row["bind_exhausted"], 0)

    def test_exhausted_after_30min_when_list_seen_nonempty(self):
        """超 30 分钟仍未命中且该组列表曾返回非空 → 置 bind_exhausted=1"""
        d = self._detail(
            bind_attempts=2,
            finished_at=(self.now - timedelta(minutes=40)).isoformat(timespec="seconds"))
        with patch.object(wss, "_groups_seen_nonempty", {(3, "video", 1)}):
            res = wss._bind_one(self.conn, d, [], self.now)
        self.conn.commit()
        row = self._row()
        self.assertEqual(row["bind_attempts"], 3)
        self.assertEqual(row["bind_exhausted"], 1)
        self.assertIn("重新关联", res["reason"])

    def test_not_exhausted_without_nonempty_history(self):
        """列表从未返回非空（管理页可能没抓到）→ 不置 exhausted"""
        d = self._detail(
            bind_attempts=2,
            finished_at=(self.now - timedelta(minutes=40)).isoformat(timespec="seconds"))
        with patch.object(wss, "_groups_seen_nonempty", set()):
            wss._bind_one(self.conn, d, [], self.now)
        self.conn.commit()
        self.assertEqual(self._row()["bind_exhausted"], 0)

    def test_hit_resets_attempts_and_fills_url_work_id(self):
        """命中：回填 publish_url + work_id，bind_attempts 清零"""
        works = [_make_work(work_id="w9", title="标题", url="https://v/9")]
        res = wss._bind_one(
            self.conn, self._detail(bind_attempts=2), works, self.now)
        self.conn.commit()
        self.assertTrue(res["bound"])
        self.assertEqual(res["publish_url"], "https://v/9")
        self.assertEqual(res["work_id"], "w9")
        row = self._row()
        self.assertEqual(row["bind_attempts"], 0)
        self.assertEqual(row["publish_url"], "https://v/9")
        self.assertEqual(row["work_id"], "w9")

    def test_hit_without_url_only_saves_work_id(self):
        """命中但平台给不出公开 URL → 只存 work_id 也算绑定成功"""
        works = [_make_work(work_id="w9", title="标题", url="")]
        res = wss._bind_one(self.conn, self._detail(), works, self.now)
        self.conn.commit()
        self.assertTrue(res["bound"])
        self.assertEqual(self._row()["work_id"], "w9")
        self.assertEqual(self._row()["publish_url"], "")

    def test_match_without_url_and_id_counts_as_miss(self):
        """命中的作品 url/work_id 全空 → 对回链无意义，按未命中处理"""
        works = [_make_work(work_id="", title="标题", url="")]
        res = wss._bind_one(self.conn, self._detail(), works, self.now)
        self.conn.commit()
        self.assertFalse(res["bound"])
        self.assertEqual(self._row()["bind_attempts"], 1)

    def test_desc_fallback_binds_platform_titled_work(self):
        """描述兜底端到端：明细标题与平台标题不同（平台标题=描述+#话题），
        描述前缀唯一命中且同时间窗 → 绑定并回填 share_url 链接"""
        works = [_make_work(
            work_id="w9", work_type="video",
            title="杨氏之子之国学之美 #杨氏之子#国学之美",
            url="https://mbd.baidu.com/newspage/data/dtlandingwise?nid=dt_1",
            publish_time=self.now.strftime("%Y-%m-%d %H:%M:%S"))]
        d = self._detail(account_configs=json.dumps({
            "title": "系统批次标题", "description": "杨氏之子之国学之美"}))
        res = wss._bind_one(self.conn, d, works, self.now)
        self.conn.commit()
        self.assertTrue(res["bound"])
        self.assertEqual(res["work_id"], "w9")
        self.assertEqual(
            res["publish_url"],
            "https://mbd.baidu.com/newspage/data/dtlandingwise?nid=dt_1")
        self.assertEqual(self._row()["bind_attempts"], 0)


class TestStatsEndpoints(unittest.TestCase):
    """/history stats 注入、aggregate_stats 聚合、works 相关端点（临时 DB）。"""

    @classmethod
    def setUpClass(cls):
        # 临时数据目录 + DB 路径都建在 setUpClass 里，避免模块级 setup 污染其他测试
        cls._tmpdir = tempfile.mkdtemp()
        os.environ['SAU_DATA_DIR'] = cls._tmpdir
        cls.DB_PATH = Path(cls._tmpdir) / "db" / "database.db"
        _setup_db(cls.DB_PATH)
        from ext_api import app
        cls.client = app.test_client()

    def setUp(self):
        # 强制 ext_api / work_stats_sync 用测试 DB（两者在 import 时已绑定路径）
        self._patches = [
            patch('ext_api.DB_PATH', self.DB_PATH),
            patch('services.work_stats_sync.DB_PATH', self.DB_PATH),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _batch(self, batch_id):
        resp = self.client.get(f'/api/v2/history/{batch_id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()['data']

    def test_detail_stats_injected_latest_snapshot_wins(self):
        """detail.stats 注入最新一行快照（d1 有两条 → 取较新的 views=250）"""
        data = self._batch('b1')
        by_id = {it['id']: it for it in data['items']}
        stats = by_id['d1']['stats']
        self.assertEqual(stats['views'], 250)
        self.assertEqual(stats['likes'], 30)
        self.assertIsNone(stats['favorites'])
        self.assertIsNone(stats['comments'])
        self.assertIsNone(stats['share'])

    def test_detail_stats_synced_at_beijing(self):
        """synced_at = 最新快照 captured_at 转北京时间（UTC +8h）"""
        data = self._batch('b1')
        stats = {it['id']: it for it in data['items']}['d1']['stats']
        self.assertEqual(stats['synced_at'], '2026-10-02T12:00:00+08:00')

    def test_detail_without_stats_all_null(self):
        """无快照的明细：stats 各字段全 null（含 synced_at）"""
        data = self._batch('b1')
        stats = {it['id']: it for it in data['items']}['d3']['stats']
        self.assertEqual(
            stats, {"views": None, "likes": None, "favorites": None,
                    "comments": None, "share": None, "synced_at": None})

    def test_aggregate_stats_sum_omits_empty_keys(self):
        """批次级 aggregate_stats：各 detail 最新快照求和，全 null 指标省略该键"""
        data = self._batch('b1')
        # d1 最新(views=250, likes=30) + d2(views=40, likes=5, favorites=2)，d3 无数据
        self.assertEqual(
            data['aggregate_stats'], {"views": 290, "likes": 35, "favorites": 2})

    def test_aggregate_stats_empty_when_no_snapshots(self):
        """整批无任何快照 → aggregate_stats 为空 dict"""
        data = self._batch('b2')
        self.assertEqual(data['aggregate_stats'], {})

    def test_sync_status_shape(self):
        """GET /works/sync-status：running/last_synced_at/interval_minutes/enabled"""
        resp = self.client.get('/api/v2/works/sync-status')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['code'], 200)
        status = data['data']
        self.assertIn('running', status)
        self.assertIn('last_synced_at', status)
        self.assertEqual(status['interval_minutes'], 60)  # settings 空 → 默认 60
        self.assertTrue(status['enabled'])

    def test_works_sync_returns_started(self):
        """POST /works/sync 异步触发：返回 started=true（mock 掉真实同步轮）"""
        with patch.object(wss, '_sync_round', MagicMock(return_value=True)) as m:
            resp = self.client.post('/api/v2/works/sync', json={"detail_ids": ["d1"]})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()['data']['started'])
            m.assert_called_once_with(["d1"], True)

    def test_works_sync_rejects_bad_detail_ids(self):
        """POST /works/sync：detail_ids 非数组 → 400"""
        resp = self.client.post('/api/v2/works/sync', json={"detail_ids": "d1"})
        self.assertEqual(resp.status_code, 400)

    def test_bind_works_endpoint_skips_group_without_cookie(self):
        """POST /history/<id>/bind-works：cookie 文件缺失 → 整组跳过，逐明细返回结果"""
        resp = self.client.post('/api/v2/history/b1/bind-works')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['code'], 200)
        results = data['data']
        self.assertEqual(len(results), 3)
        self.assertEqual({r['detail_id'] for r in results}, {'d1', 'd2', 'd3'})
        for r in results:
            self.assertFalse(r['bound'])
            self.assertIn('cookie', r['reason'])

    def test_bind_works_endpoint_unknown_batch_returns_empty(self):
        """POST /history/<id>/bind-works：不存在的批次 → 200 + 空结果"""
        resp = self.client.post('/api/v2/history/no-such-batch/bind-works')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['data'], [])

    def test_settings_defaults_for_work_stats(self):
        """GET /settings 默认值补齐 3 个 workStats 配置且为数值类型"""
        resp = self.client.get('/api/v2/settings')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()['data']
        self.assertEqual(data['workStatsSyncInterval'], 60)
        self.assertEqual(data['workStatsSyncWindowDays'], 30)
        self.assertEqual(data['workStatsBindWindowDays'], 2)


if __name__ == '__main__':
    unittest.main()
