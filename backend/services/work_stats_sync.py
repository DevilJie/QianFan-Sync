"""作品回链与数据统计同步服务。

对应设计 spec：docs/superpowers/specs/2026-11-01-work-link-and-stats-sync-design.md
（§2.2 作品回链 / §2.3 统计同步调度 / §3 数据模型 / §8 风险对策）

参照 services/duration_repair.py 的 daemon 线程模式，同一线程内调度两类节拍：

- 快速拍（30~60s）：绑定协调器 —— 扫描近 workStatsBindWindowDays（默认 2 天）
  内 status='success' 且未绑定的 publish_details，按 (平台, work_type, 账号)
  分组调 ``list_works(limit=5)``，按「类型一致 → 标题归一化全等 → 标题前缀
  唯一 → 描述前缀唯一（发布时间同窗，百家号视频等平台标题=描述+#话题）→
  finished_at 时间最近」匹配后回填 ``publish_url + work_id``。
  未命中按 1/5/30 分钟退避；超 30 分钟仍未命中且平台列表曾返回非空的
  置 ``bind_exhausted=1``（仅手动补绑可复位）。

- 同步拍（settings.workStatsSyncInterval 分钟，0=关闭，默认 60）：
  扫近 workStatsSyncWindowDays（默认 30）天 success 明细，按
  (account_id, work_type) 分组调 ``list_works(limit=50)``，按 work_id
  （优先）/标题匹配写 publish_work_stats 快照（raw 存原始结果 JSON）；
  顺带对未绑定行执行绑定；90 天外快照清理。

风控保护（§8）：task_queue 有 running 任务时本轮跳过；组间随机 sleep 3~8 秒；
单组失败不影响其它组；list_works 抛 NotImplementedError 的平台记一次日志后跳过。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from conf import BASE_DIR
from util._logger import get_channel_logger

# 使用项目统一的日志体系（标准库 logging，按 channel 分文件），
# 与 services/duration_repair.py 相同的 channel。
logger = get_channel_logger("backend")

DB_PATH = BASE_DIR / "db" / "database.db"

# ---------------------------------------------------------------------------
# 模块级状态（供 API 查询，见 ext_api /works/sync-status）
# ---------------------------------------------------------------------------

#: 是否有绑定/同步轮正在执行
is_running = False

#: 已排队但还没开始执行的同步轮数量（trigger_sync 返回前同步 +1，
#: 轮结束 -1）。前端以 running===false 判定"同步完成"，若只在轮真正
#: 开始后才置 is_running，排队期会被误判完成 —— running 需并入排队数。
_pending_syncs = 0

#: 最近一次完成统计同步的时刻（本地时间 ISO 字符串；None = 尚未同步过）
last_synced_at = None

# 互斥锁：绑定/同步轮全局串行（daemon 快速拍、同步拍、手动触发共用），
# 同一时刻只允许一个轮开浏览器抓页面，避免多浏览器并发触发平台风控。
_round_lock = threading.Lock()

# list_works 抛 NotImplementedError 的平台 id 集合：
# 记一次日志后跳过，避免每轮都刷「平台未实现」日志。
_unimplemented_platforms: set[int] = set()

# (platform_id, work_type, account_id)：本进程内该组 list_works 曾返回过非空
# 列表。bind_exhausted 判定依据之一：列表能抓到说明管理页正常，只是匹配不上，
# 继续快速拍重试意义不大。
_groups_seen_nonempty: set[tuple] = set()

# 绑定退避窗口（分钟）：第 1/2/3 次尝试需距 finished_at 至少这么久
# （1min → 5min → 30min，覆盖 B站转码/视频号过审等场景，§2.2）
_BIND_BACKOFF_MINUTES = (1, 5, 30)

# 快速拍间隔（秒）。规范 §2.2：30~60s 一拍。
_FAST_TICK_SECONDS = 45

# 统计快照保留天数（超期清理，防表膨胀；规范 §3）
_STATS_RETENTION_DAYS = 90

# 明细查询公共列（绑定协调器与统计同步共用；account_id 决定平台与 cookie）
_DETAIL_SELECT = """
    SELECT d.id, d.batch_id, d.account_id, d.account_configs,
           d.publish_url, d.work_id, d.bind_attempts, d.bind_exhausted,
           d.finished_at, d.created_at,
           b.type AS work_type, b.title AS batch_title,
           b.description AS batch_description,
           u.type AS platform_id, u.filePath AS cookie_file
    FROM publish_details d
    JOIN publish_batches b ON d.batch_id = b.id
    LEFT JOIN user_info u ON d.account_id = u.id
"""


# ---------------------------------------------------------------------------
# 基础工具（匹配规则 §2.2 / 字段清洗 §2.1）
# ---------------------------------------------------------------------------

def _normalize_title(title) -> str:
    """标题归一化：去首尾空白、压缩连续空白（§2.2 匹配规则第 2 条）。"""
    return re.sub(r"\s+", " ", str(title or "").strip())


def _to_int_or_none(value):
    """统计字段类型清洗：int/float/纯数字字符串 → int，其余 → None。

    平台实现返回的统计通常已是 int|None；这里只兜底清洗，不解析「1.2万」
    这类展示文本（平台契约要求返回数值，§2.1）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        if not s:
            return None
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def normalize_works(works, work_type: str = "") -> list[dict]:
    """平台 list_works 返回结果的字段类型清洗（平台实现可直接调用）。

    确保返回 list[dict]：字符串字段转 str、统计字段转 int|None；
    work_type 缺失时回填调用方传入的 work_type（§2.1「回填调用方」）。
    """
    if not isinstance(works, list):
        return []
    cleaned = []
    for w in works:
        if not isinstance(w, dict):
            continue
        cleaned.append({
            "work_id": str(w.get("work_id", "") or ""),
            "work_type": str(w.get("work_type", "") or work_type or ""),
            "title": str(w.get("title", "") or ""),
            "publish_time": str(w.get("publish_time", "") or ""),
            "url": str(w.get("url", "") or ""),
            "views": _to_int_or_none(w.get("views")),
            "likes": _to_int_or_none(w.get("likes")),
            "favorites": _to_int_or_none(w.get("favorites")),
            "comments": _to_int_or_none(w.get("comments")),
            "share": _to_int_or_none(w.get("share")),
        })
    return cleaned


_PUBLISH_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
)


def _parse_dt(text) -> datetime | None:
    """尽力解析 ISO 时间 / 平台展示的发布时间文本。失败返回 None。"""
    if not text:
        return None
    s = str(text).strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in _PUBLISH_TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _detail_title(d: dict) -> str:
    """明细标题：account_configs.title 优先（各账号可个性化），回退批次标题。"""
    try:
        cfg = json.loads(d.get("account_configs") or "{}")
        title = cfg.get("title")
        if title and str(title).strip():
            return str(title)
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return str(d.get("batch_title") or "")


def _detail_desc(d: dict) -> str:
    """明细描述：account_configs.description 优先，回退批次描述。

    百家号视频等平台发布页无独立标题输入，平台作品标题 = 描述 + #话题后缀，
    系统批次标题与平台标题必然不同 → 标题匹配失败时用描述做兜底匹配
    （见 _match_work 描述兜底分支）。
    """
    try:
        cfg = json.loads(d.get("account_configs") or "{}")
        desc = cfg.get("description")
        if desc and str(desc).strip():
            return str(desc)
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return str(d.get("batch_description") or "")


def _prefix_match(a: str, b: str) -> bool:
    """前缀匹配兜底：一方以另一方开头，且较短一方 ≥4 字符（防短标题误绑）。

    背景：部分平台列表标题与发布标题不一致（如知乎 pin 的标题取想法首块
    文本，可能比发布标题短），全等匹配会漏；唯一前缀命中时可用。
    阈值 4：中文字符信息密度高（"新建女孩"=4 字已是完整短语），纯数字/
    单字标题不放行。
    """
    if not a or not b:
        return False
    if min(len(a), len(b)) < 4:
        return False
    return a.startswith(b) or b.startswith(a)


def _match_work(
    detail_title: str, works, work_type: str, finished_at, detail_desc: str = ""
) -> dict | None:
    """按规范 §2.2 匹配规则从 works 中选出目标作品。

    1. 类型一致：detail 的 work_type 必须 == work 的 work_type（第一道过滤，
       同一账号可能发过同名视频和同名图文）；
    2. 标题归一化（去首尾空白、压缩连续空白）后全等比较；全等无命中时做
       前缀兜底（_prefix_match）：唯一命中直接采用；多条命中收紧到
       finished_at 时间窗（_DESC_MATCH_WINDOW_SECONDS）内再择优；
    3. 标题兜底仍无命中时做描述兜底（detail_desc 非空才参与）：百家号/快手
       视频等平台发布页无标题概念，平台作品标题 = 描述 + #话题后缀，平台
       标题以描述为前缀。命中须在时间窗内（防历史同描述旧作品误绑）；
    4. 多条命中时取发布时间与 finished_at 最接近的一条（时间解析不了的排最后）。

    没有命中返回 None。
    """
    norm = _normalize_title(detail_title)
    if not norm:
        return None
    same_type = [
        w for w in works
        if str(w.get("work_type") or work_type or "") == (work_type or "")
    ]
    candidates = [w for w in same_type if _normalize_title(w.get("title")) == norm]
    if not candidates:
        prefix = [w for w in same_type if _prefix_match(norm, _normalize_title(w.get("title")))]
        if len(prefix) == 1:
            candidates = prefix
        elif prefix:
            # 多条前缀命中（如快手 caption=描述, 同一账号存在多条同描述作品）:
            # 收紧到 finished_at 时间窗内的候选,交给统一「取时间最近」逻辑;
            # 时间窗一条都没有(含 publish_time 解析失败)则维持放弃,防误绑
            base = _parse_dt(finished_at)
            if base is not None:
                in_window = []
                for w in prefix:
                    t = _parse_dt(w.get("publish_time"))
                    if t is None:
                        continue
                    if abs((t - base).total_seconds()) <= _DESC_MATCH_WINDOW_SECONDS:
                        in_window.append(w)
                if in_window:
                    candidates = in_window
    if not candidates:
        candidates = _match_by_desc(same_type, detail_desc, norm, finished_at)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    base = _parse_dt(finished_at)
    if base is None:
        return candidates[0]

    def _dist(w):
        t = _parse_dt(w.get("publish_time"))
        # (解析失败标记, 距离)：解析失败的排最后，成功的按时间差升序
        return (1, 0.0) if t is None else (0, abs((t - base).total_seconds()))

    return min(candidates, key=_dist)


# 描述兜底匹配的时间窗（秒）：平台作品发布时间与明细 finished_at 的最大允许
# 偏差。默认 2 天，与自动绑定窗口 workStatsBindWindowDays 一致 —— 描述常被
# 复用，靠时间窗把历史旧作品挡在外面（发布时间解析不了的同样不放行）。
_DESC_MATCH_WINDOW_SECONDS = 2 * 24 * 3600


def _match_by_desc(same_type, detail_desc: str, norm_title: str, finished_at) -> list:
    """描述兜底匹配：平台作品标题以描述为前缀（描述+#话题后缀被截断也兼容）。

    约束：描述与标题归一化后相同则不重复兜底（标题匹配已覆盖）；作品
    publish_time 与 finished_at 偏差必须在时间窗内（时间解析失败一律不
    采用）。命中多条时全部返回 —— 上层 _match_work 统一按「发布时间与
    finished_at 最接近」择优（同账号多条同描述作品取最近一条）。
    """
    norm_desc = _normalize_title(detail_desc)
    if not norm_desc or norm_desc == norm_title:
        return []
    base = _parse_dt(finished_at)
    if base is None:
        return []
    hits = []
    for w in same_type:
        if not _prefix_match(norm_desc, _normalize_title(w.get("title"))):
            continue
        t = _parse_dt(w.get("publish_time"))
        if t is None:
            continue
        if abs((t - base).total_seconds()) <= _DESC_MATCH_WINDOW_SECONDS:
            hits.append(w)
    return hits


def _elapsed_minutes(d: dict, now: datetime) -> float:
    """明细距 finished_at（缺省 created_at）的分钟数。时间解析不了返回 +inf。"""
    base = _parse_dt(d.get("finished_at")) or _parse_dt(d.get("created_at"))
    if base is None:
        return float("inf")
    return (now - base).total_seconds() / 60.0


def _bind_due(d: dict, now: datetime) -> bool:
    """按退避窗口判断该明细本轮（快速拍）是否允许尝试绑定。

    第 1 次尝试发布后 ~1 分钟即可；失败 1 次后等 5 分钟、2 次后等 30 分钟；
    3 次（退避到底）后不再随快速拍重试，降为统计同步每轮顺带重试（§2.2）。
    """
    attempts = int(d.get("bind_attempts") or 0)
    if attempts >= len(_BIND_BACKOFF_MINUTES):
        return False
    return _elapsed_minutes(d, now) >= _BIND_BACKOFF_MINUTES[attempts]


# ---------------------------------------------------------------------------
# 平台调用
# ---------------------------------------------------------------------------

def _publish_running() -> bool:
    """task_queue 有 running 发布任务时返回 True（本轮跳过，风控保护 §8）。"""
    try:
        from ext_api.task_queue import get_task_queue
        status = get_task_queue().get_status()
        return int(status.get("running", 0) or 0) > 0
    except Exception as exc:
        logger.warning("[WorkStatsSync] 读取任务队列状态失败（按无任务处理）: %s", exc)
        return False


def _call_list_works(platform_id, cookie_file: str, limit: int, work_type: str):
    """调平台 list_works 并做字段清洗。返回 list；未实现/调用失败返回 None。

    - NotImplementedError：视为该平台未实现，记一次日志后记住（集合），
      后续轮次直接跳过，不反复刷日志；
    - 其它异常：记 warning，返回 None，下轮再试。
    """
    from impl.registry import get_platform

    if platform_id in _unimplemented_platforms:
        return None
    platform = get_platform(platform_id)
    if platform is None:
        logger.info("[WorkStatsSync] 平台 %s 未注册，跳过", platform_id)
        return None
    try:
        works = asyncio.run(platform.list_works(cookie_file, limit=limit, work_type=work_type))
    except NotImplementedError:
        _unimplemented_platforms.add(platform_id)
        logger.info("[WorkStatsSync] 平台 %s 未实现 list_works，后续轮次跳过", platform_id)
        return None
    except Exception as exc:
        logger.warning("[WorkStatsSync] 平台 %s list_works 调用失败: %s", platform_id, exc)
        return None
    return normalize_works(works, work_type)


def _get_setting_int(key: str, default: int) -> int:
    """读 settings 表整型配置（workStatsSyncInterval / 窗口天数等）。

    缺失或非法值回退默认值；DB_PATH 为模块全局，测试可 patch。
    """
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row and str(row[0]).strip() != "":
            return int(float(row[0]))
    except (sqlite3.Error, ValueError, TypeError):
        pass
    return default


# ---------------------------------------------------------------------------
# 绑定协调器（快速拍 §2.2）
# ---------------------------------------------------------------------------

def _bind_result(d: dict, bound: bool, reason: str) -> dict:
    """构造单个 detail 的绑定结果（手动补绑 API 返回用）。"""
    return {
        "detail_id": d.get("id"),
        "batch_id": d.get("batch_id") or "",
        "bound": bound,
        "publish_url": d.get("publish_url") or "",
        "work_id": d.get("work_id") or "",
        "reason": reason,
    }


def _bind_one(conn, d: dict, works: list[dict], now: datetime) -> dict:
    """对单个明细执行匹配并落库（命中回填 / 未命中退避流转）。

    命中：回填 publish_url + work_id，bind_attempts 清零（§2.2 规则 5）。
    未命中：bind_attempts+1；距 finished_at 超 30 分钟且该组列表曾返回
    非空的置 bind_exhausted=1，停止自动重试（仅手动补绑可复位）。
    只写库不 commit（调用方负责）。
    """
    detail_id = d["id"]
    group_key = (d.get("platform_id"), d.get("work_type") or "", d.get("account_id"))
    work = _match_work(
        _detail_title(d), works, d.get("work_type") or "", d.get("finished_at"),
        _detail_desc(d),
    )

    if work is not None and (work.get("url") or work.get("work_id")):
        # 命中：url / work_id 有其一即可回填（平台拿不到公开链接时仍存 work_id，
        # 后续统计同步优先按 work_id 定位）
        new_url = work.get("url") or d.get("publish_url") or ""
        new_work_id = work.get("work_id") or d.get("work_id") or ""
        conn.execute(
            "UPDATE publish_details SET publish_url = ?, work_id = ?, bind_attempts = 0"
            " WHERE id = ?",
            (new_url, new_work_id, detail_id),
        )
        result = _bind_result(d, True, "匹配成功")
        result["publish_url"] = new_url
        result["work_id"] = new_work_id
        return result

    # 未命中：退避流转。注意 matched 但 url/work_id 全空的情形对回链无意义，
    # 同样按未命中处理。
    attempts = int(d.get("bind_attempts") or 0) + 1
    exhausted = 0
    if _elapsed_minutes(d, now) > 30 and group_key in _groups_seen_nonempty:
        exhausted = 1
    conn.execute(
        "UPDATE publish_details SET bind_attempts = ?, bind_exhausted = ? WHERE id = ?",
        (attempts, exhausted, detail_id),
    )
    reason = f"未命中（第 {attempts} 次尝试）"
    if exhausted:
        reason = "连续未匹配且已超 30 分钟，停止自动重试（可手动重新关联）"
    return _bind_result(d, False, reason)


def _bind_details(conn, rows, now: datetime) -> list[dict]:
    """对给定明细执行一轮绑定（分组调 list_works + §2.2 匹配规则）。

    按 (account_id, work_type) 分组（account_id 决定平台与 cookie）；
    组间随机 sleep 3~8 秒；单组失败不影响其它组。
    只写库不 commit（调用方负责），返回每个 detail 的绑定结果。
    """
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        d = dict(r)
        groups.setdefault((d.get("account_id"), d.get("work_type") or ""), []).append(d)

    results: list[dict] = []
    for idx, ((account_id, work_type), group) in enumerate(groups.items()):
        first = group[0]
        platform_id = first.get("platform_id")
        cookie_file = (first.get("cookie_file") or "").strip()
        group_key = (platform_id, work_type, account_id)
        try:
            # user_info 查不到或 cookie 文件不存在 → 整组跳过
            cookie_path = (
                Path(BASE_DIR) / "cookiesFile" / cookie_file if cookie_file else None
            )
            if not platform_id or not cookie_path or not cookie_path.is_file():
                for d in group:
                    results.append(_bind_result(d, False, "账号不存在或 cookie 文件缺失"))
                continue

            if idx > 0:
                time.sleep(random.uniform(3, 8))  # 组间随机间隔（风控保护）

            # limit=5：只看近端窗口，只可能匹配到刚发布的作品（§2.2 规则 4）
            works = _call_list_works(platform_id, cookie_file, 5, work_type)
            if works is None:
                # 平台未实现 / 调用失败：本轮不动退避计数，下轮再试
                for d in group:
                    results.append(_bind_result(d, False, "平台未实现 list_works 或调用失败"))
                continue
            if works:
                _groups_seen_nonempty.add(group_key)

            for d in group:
                if not _bind_due(d, now):
                    results.append(_bind_result(d, False, "退避窗口未到，本轮跳过"))
                    continue
                results.append(_bind_one(conn, d, works, now))
        except Exception as exc:
            # 单组失败不影响其它组（§2.3 步骤 5）
            logger.warning(
                "[WorkStatsSync] 绑定组 (%s, %s) 执行失败: %s", account_id, work_type, exc
            )
    return results


def _bind_round() -> None:
    """快速拍：绑定协调器（规范 §2.2）。

    扫 status='success' 且未绑定（publish_url 与 work_id 均为空）、未耗尽、
    近 workStatsBindWindowDays 天内的明细，分组调 list_works(limit=5) 回填。
    task_queue 有 running 任务或已有轮在执行时本轮跳过。
    """
    global is_running
    if _publish_running():
        return  # 有发布任务运行，本轮跳过（风控保护）
    if not _round_lock.acquire(blocking=False):
        return  # 已有手动/同步轮在执行
    try:
        is_running = True
        window_days = _get_setting_int("workStatsBindWindowDays", 2)
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                _DETAIL_SELECT
                + " WHERE d.status='success' AND d.publish_url=''"
                " AND COALESCE(d.work_id,'')='' AND COALESCE(d.bind_exhausted,0)=0"
                " AND d.created_at >= datetime('now', ?)",
                (f"-{window_days} day",),
            ).fetchall()
            if rows:
                logger.info("[WorkStatsSync] 绑定轮: %d 个明细待绑定", len(rows))
                _bind_details(conn, rows, datetime.now())
                conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[WorkStatsSync] 绑定轮执行失败: %s", exc)
    finally:
        is_running = False
        _round_lock.release()


# ---------------------------------------------------------------------------
# 统计同步（同步拍 §2.3）
# ---------------------------------------------------------------------------

def _sync_one(conn, d: dict, works: list[dict]) -> None:
    """单个明细：按 work_id（优先）/标题匹配作品并写统计快照。

    无 work_id 的行顺带执行绑定（§2.3 步骤 4，退避到底后靠这里兜底重试）；
    bind_exhausted 的行不再顺带绑定（仅手动补绑可复位）。
    只写库不 commit（调用方按组提交）。
    """
    detail_id = d["id"]
    work_type = d.get("work_type") or ""
    wid = (d.get("work_id") or "").strip()

    work = None
    if wid:
        # 已绑定过：优先按 work_id 定位（§2.2 规则 5）
        work = next((w for w in works if w.get("work_id") == wid), None)
    if work is None:
        work = _match_work(
            _detail_title(d), works, work_type, d.get("finished_at"),
            _detail_desc(d),
        )
        # 顺带绑定：还没绑定的行匹配成功 → 回填 publish_url + work_id
        can_bind = (
            work is not None
            and (work.get("url") or work.get("work_id"))
            and not wid
            and not (d.get("publish_url") or "")
            and not int(d.get("bind_exhausted") or 0)
        )
        if can_bind:
            conn.execute(
                "UPDATE publish_details SET publish_url = ?, work_id = ?, bind_attempts = 0"
                " WHERE id = ?",
                (work.get("url") or "", work.get("work_id") or "", detail_id),
            )
    if work is None:
        return

    # 写统计快照（每次同步 1 行；raw 存原始结果 JSON，排查用）
    conn.execute(
        """INSERT INTO publish_work_stats
           (detail_id, batch_id, work_type, work_id,
            views, likes, favorites, comments, share, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            detail_id, d.get("batch_id") or "", work_type,
            work.get("work_id") or "",
            work.get("views"), work.get("likes"), work.get("favorites"),
            work.get("comments"), work.get("share"),
            json.dumps(work, ensure_ascii=False),
        ),
    )


def _sync_round(detail_ids=None, wait_lock: bool = False) -> bool:
    """统计同步一轮（同步拍 §2.3，手动触发复用本函数）。

    detail_ids 给定时只处理这些明细（手动场景，不限时间窗口）；
    否则处理近 workStatsSyncWindowDays 天的 success 明细。
    task_queue 有 running 任务时本轮跳过（风控保护）。

    返回 True 表示本轮实际执行了。
    """
    global is_running, last_synced_at

    if _publish_running():
        logger.debug("[WorkStatsSync] 有发布任务运行中，本轮同步跳过（风控保护）")
        return False
    if not _round_lock.acquire(blocking=wait_lock):
        return False
    try:
        is_running = True
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            conds = ["d.status = 'success'"]
            params: list = []
            if detail_ids:
                placeholders = ",".join("?" * len(detail_ids))
                conds.append(f"d.id IN ({placeholders})")
                params.extend(detail_ids)
            else:
                window_days = _get_setting_int("workStatsSyncWindowDays", 30)
                conds.append("d.created_at >= datetime('now', ?)")
                params.append(f"-{window_days} day")
            rows = conn.execute(
                _DETAIL_SELECT + " WHERE " + " AND ".join(conds), params
            ).fetchall()

            # 顺手清理超期快照（§3：90 天外删除，防表膨胀）
            conn.execute(
                "DELETE FROM publish_work_stats WHERE captured_at < datetime('now', ?)",
                (f"-{_STATS_RETENTION_DAYS} day",),
            )

            groups: dict[tuple, list[dict]] = {}
            for r in rows:
                d = dict(r)
                groups.setdefault((d.get("account_id"), d.get("work_type") or ""), []).append(d)

            for idx, ((account_id, work_type), group) in enumerate(groups.items()):
                try:
                    first = group[0]
                    platform_id = first.get("platform_id")
                    cookie_file = (first.get("cookie_file") or "").strip()
                    # user_info 查不到或 cookie 文件不存在 → 该组跳过
                    cookie_path = (
                        Path(BASE_DIR) / "cookiesFile" / cookie_file if cookie_file else None
                    )
                    if not platform_id or not cookie_path or not cookie_path.is_file():
                        continue
                    if idx > 0:
                        time.sleep(random.uniform(3, 8))  # 组间随机间隔（风控保护）

                    works = _call_list_works(platform_id, cookie_file, 50, work_type)
                    if works is None:
                        continue
                    if works:
                        _groups_seen_nonempty.add((platform_id, work_type, account_id))

                    for d in group:
                        _sync_one(conn, d, works)
                    conn.commit()
                except Exception as exc:
                    # 单组失败不影响其它组（§2.3 步骤 5）
                    logger.warning(
                        "[WorkStatsSync] 同步组 (%s, %s) 执行失败: %s",
                        account_id, work_type, exc,
                    )
        finally:
            conn.close()
        last_synced_at = datetime.now().isoformat(timespec="seconds")
        return True
    finally:
        is_running = False
        _round_lock.release()


# ---------------------------------------------------------------------------
# daemon 线程与手动触发入口
# ---------------------------------------------------------------------------

def _daemon_loop():
    """daemon 主循环：快速拍（绑定协调器）+ 到点的同步拍（统计同步）。"""
    time.sleep(10.0)  # 让 Waitress 先起来，避免与启动期高频请求争抢 DB
    # 首次统计同步在启动后一个间隔（默认 60 分钟）再跑，避免启动期集中开浏览器
    last_sync_tick = time.time()
    while True:
        # ---- 快速拍：绑定协调器（只查近窗口期未绑定 success，代价低）----
        try:
            _bind_round()
        except Exception as exc:
            logger.warning("[WorkStatsSync] 绑定轮异常: %s", exc)
        # ---- 同步拍：settings.workStatsSyncInterval 分钟到点执行（0=关闭）----
        try:
            interval_min = _get_setting_int("workStatsSyncInterval", 60)
            if interval_min > 0 and time.time() - last_sync_tick >= interval_min * 60:
                if _sync_round():
                    last_sync_tick = time.time()
        except Exception as exc:
            logger.warning("[WorkStatsSync] 统计同步轮异常: %s", exc)
        time.sleep(_FAST_TICK_SECONDS)


def start_work_stats_sync():
    """在 daemon 线程中启动作品回链 + 统计同步调度，立即返回（app.py 启动钩子）。"""
    thread = threading.Thread(
        target=_daemon_loop, daemon=True, name="work-stats-sync",
    )
    thread.start()
    logger.info("[Startup] 作品回链与数据统计同步后台任务已启动")
    return thread


def trigger_sync(detail_ids=None) -> bool:
    """手动触发一轮统计同步（异步执行，复用 _sync_round 逻辑）。

    detail_ids 非空时只同步这些明细；否则按窗口期同步全部。
    立即返回 True（轮已在后台线程排队，等当前轮结束后执行）。
    返回前同步 +1 排队计数，保证排队期间 get_sync_status().running 为 True
    （前端轮询以 running===false 判定完成，见 ext_api /works/sync 契约）。
    """
    global _pending_syncs
    _pending_syncs += 1

    def _run():
        global _pending_syncs
        try:
            _sync_round(detail_ids, True)
        finally:
            _pending_syncs -= 1

    threading.Thread(target=_run, daemon=True, name="work-stats-sync-manual").start()
    return True


def trigger_bind(batch_id=None) -> list[dict]:
    """手动补绑：立即对目标明细执行一次绑定，同步返回每个 detail 的结果。

    - batch_id 给定：该批次全部 success 明细，并先清空退避计数
      （bind_exhausted 复位，§2.2「手动补绑」；绑错的重新关联也会被覆盖）；
    - batch_id 为 None：按自动绑定窗口扫一轮（等价于手动跑一次快速拍）。

    已有绑定/同步轮在执行时抛 RuntimeError（API 层转 409）。
    """
    if not _round_lock.acquire(blocking=False):
        raise RuntimeError("已有绑定/同步任务在进行中，请稍后再试")
    global is_running
    try:
        is_running = True
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            if batch_id:
                # 手动补绑：清空退避计数，bind_exhausted 复位后立即重试
                conn.execute(
                    "UPDATE publish_details SET bind_attempts = 0, bind_exhausted = 0"
                    " WHERE batch_id = ? AND status = 'success'",
                    (batch_id,),
                )
                conn.commit()
                rows = conn.execute(
                    _DETAIL_SELECT + " WHERE d.batch_id = ? AND d.status = 'success'",
                    (batch_id,),
                ).fetchall()
            else:
                window_days = _get_setting_int("workStatsBindWindowDays", 2)
                rows = conn.execute(
                    _DETAIL_SELECT
                    + " WHERE d.status='success' AND d.publish_url=''"
                    " AND COALESCE(d.work_id,'')='' AND COALESCE(d.bind_exhausted,0)=0"
                    " AND d.created_at >= datetime('now', ?)",
                    (f"-{window_days} day",),
                ).fetchall()
            results = _bind_details(conn, rows, datetime.now())
            conn.commit()
            return results
        finally:
            conn.close()
    finally:
        is_running = False
        _round_lock.release()


def get_sync_status() -> dict:
    """同步运行状态（供 GET /works/sync-status，历史页/设置页展示用）。"""
    interval = _get_setting_int("workStatsSyncInterval", 60)
    return {
        "running": bool(is_running or _pending_syncs > 0),
        "last_synced_at": last_synced_at,
        "interval_minutes": interval,
        "enabled": interval > 0,
    }
