# -*- coding: utf-8 -*-
"""作品评论服务：一键评论（浏览器自动化发评/置顶）+ 评论同步（抓取评论区）。

任务模式与 work_stats_sync 一致：浏览器自动化耗时 30~90s，
HTTP 层立即返回 task_id，前端轮询 get_task() 拿状态与结果。

评论数据落在 work_comments 表（同明细按平台 comment_id 幂等）。
"""

import asyncio
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from conf import BASE_DIR
from util._logger import get_channel_logger

logger = get_channel_logger("comments")

DB_PATH = BASE_DIR / "db" / "database.db"

# 明细 → 平台/cookie/work 定位（与 work_stats_sync._DETAIL_SELECT 同构，只取需要的列）
_COMMENT_DETAIL_SELECT = """
    SELECT d.id, d.batch_id, d.account_id, d.account_name,
           d.work_id, d.publish_url, d.status,
           u.type AS platform_id, u.filePath AS cookie_file
    FROM publish_details d
    LEFT JOIN user_info u ON d.account_id = u.id
    WHERE d.id = ?
"""

# 任务表：task_id → {status: running|done|error, result, error, created_at}
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 平台调用
# ---------------------------------------------------------------------------

def _call_platform(platform_id, cookie_file, method: str, **kwargs):
    """调平台评论方法。返回 (ok, payload)。

    NotImplementedError → 平台未支持评论（返回明确提示，不重试）；
    其它异常 → 记日志返回错误。浏览器自动化慢操作全程在后台线程执行。
    """
    from impl.registry import get_platform

    platform = get_platform(platform_id)
    if platform is None:
        return False, {"ok": False, "message": f"平台 {platform_id} 未注册"}
    fn = getattr(platform, method, None)
    try:
        payload = asyncio.run(fn(cookie_file, **kwargs))
        return True, payload
    except (NotImplementedError, AttributeError):
        return False, {"ok": False, "message": "该平台暂未支持评论功能"}
    except Exception as exc:
        logger.warning("[Comments] 平台 %s %s 调用失败: %s", platform_id, method, exc)
        return False, {"ok": False, "message": f"调用失败: {exc}"}


def _load_detail(detail_id: str) -> dict | None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(_COMMENT_DETAIL_SELECT, (detail_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _resolve_work_id(detail: dict) -> str:
    """work_id 优先；未绑定时从 publish_url 兜底提取（B站 BV 号）。"""
    wid = (detail.get("work_id") or "").strip()
    if wid:
        return wid
    url = (detail.get("publish_url") or "").strip()
    # https://www.bilibili.com/video/BVxxxx/?p=1 → BVxxxx
    for marker in ("/video/", "/short-video/", "/explore/", "/note/", "/video"):
        if marker in url:
            tail = url.split(marker, 1)[1].strip("/")
            wid = tail.split("?")[0].split("/")[0]
            if wid:
                return wid
    return ""


def _upsert_comment_row(detail: dict, c: dict, conn=None) -> None:
    """单条评论落库（幂等：同 detail + comment_id 覆盖）。

    conn 为 None 时自开自关；调用方传入连接时复用（批量同步在同一事务内）。
    """
    own = conn is None
    if own:
        conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            """INSERT INTO work_comments
               (detail_id, batch_id, account_id, platform_id, work_id,
                comment_id, parent_id, author, avatar, content,
                like_count, reply_count, publish_time, location,
                is_author, pinned)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(detail_id, comment_id) DO UPDATE SET
                author=excluded.author, avatar=excluded.avatar,
                content=excluded.content, like_count=excluded.like_count,
                reply_count=excluded.reply_count,
                publish_time=excluded.publish_time,
                location=excluded.location,
                is_author=excluded.is_author, pinned=excluded.pinned""",
            (
                detail["id"], detail.get("batch_id") or "", detail.get("account_id"),
                detail.get("platform_id"), detail.get("work_id") or "",
                str(c.get("comment_id") or ""), str(c.get("parent_id") or ""),
                c.get("author") or "", c.get("avatar") or "", c.get("content") or "",
                int(c.get("like_count") or 0), int(c.get("reply_count") or 0),
                c.get("publish_time") or "", c.get("location") or "",
                1 if c.get("is_author") else 0, 1 if c.get("pinned") else 0,
            ),
        )
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 单明细执行核心（单条任务与批量任务共用）
# ---------------------------------------------------------------------------

def _post_one_detail(detail: dict, content: str, pinned: bool, tag: str) -> dict:
    """对单个明细执行发评，返回该明细的结果摘要（不抛异常）。"""
    base = {
        "detail_id": detail["id"],
        "account_name": detail.get("account_name") or "",
        "platform_id": detail.get("platform_id"),
    }
    platform_id = detail.get("platform_id")
    cookie_file = (detail.get("cookie_file") or "").strip()
    if not platform_id or not cookie_file:
        return {**base, "ok": False, "message": "账号不存在或 cookie 配置缺失"}
    work_id = _resolve_work_id(detail)
    if not work_id:
        return {**base, "ok": False, "message": "未找到作品 ID（请先执行「重新关联」回填作品链接）"}

    detail["work_id"] = work_id
    ok, result = _call_platform(
        platform_id, cookie_file, "post_comment",
        work_id=work_id, content=content, pinned=pinned,
    )
    if not ok:
        return {**base, "ok": False, "message": result.get("message") or "调用失败"}
    if not result.get("ok"):
        return {**base, "ok": False, "message": result.get("message") or "发评失败"}

    # 自己的评论落库（置顶标记以平台实际返回为准）
    pinned_ok = bool(result.get("pinned"))
    try:
        _upsert_comment_row(detail, {
            "comment_id": result.get("comment_id") or f"local-{tag[:8]}",
            "parent_id": "", "author": detail.get("account_name") or "",
            "avatar": "", "content": content,
            "like_count": 0, "reply_count": 0,
            "publish_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "location": "", "is_author": True,
            "pinned": pinned_ok,
        })
    except Exception as exc:
        logger.warning("[Comments] 自己的评论落库失败(不影响结果): %s", exc)

    logger.info(
        "[Comments] 发评完成 detail=%s work=%s pinned_ok=%s",
        detail["id"], work_id, pinned_ok,
    )
    return {
        **base, "ok": True,
        "comment_id": result.get("comment_id") or "",
        "pinned": pinned_ok,
        "message": result.get("message") or ("评论已发布并置顶" if pinned_ok else "评论已发布"),
    }


def _sync_one_detail(detail: dict) -> dict:
    """对单个明细执行评论同步（抓取 + 整明细替换落库），返回结果摘要。"""
    base = {
        "detail_id": detail["id"],
        "account_name": detail.get("account_name") or "",
        "platform_id": detail.get("platform_id"),
    }
    platform_id = detail.get("platform_id")
    cookie_file = (detail.get("cookie_file") or "").strip()
    if not platform_id or not cookie_file:
        return {**base, "ok": False, "message": "账号不存在或 cookie 配置缺失"}
    work_id = _resolve_work_id(detail)
    if not work_id:
        return {**base, "ok": False, "message": "未找到作品 ID（请先执行「重新关联」回填作品链接）"}

    detail["work_id"] = work_id
    ok, comments = _call_platform(
        platform_id, cookie_file, "list_comments", work_id=work_id
    )
    if not ok:
        return {**base, "ok": False, "message": comments.get("message") or "调用失败"}
    if not comments:
        return {**base, "ok": False, "message": "评论区抓取失败（cookie 可能已失效）"}

    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            # 整明细替换：先删旧数据（平台上已删除的评论不再残留），再插入本轮快照
            # （delete + 全部 upsert 共用同一连接同一事务，避免自锁）
            conn.execute("DELETE FROM work_comments WHERE detail_id = ?", (detail["id"],))
            for c in comments:
                _upsert_comment_row(detail, c, conn)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        return {**base, "ok": False, "message": f"评论落库失败: {exc}"}

    logger.info(
        "[Comments] 评论同步完成 detail=%s work=%s count=%d",
        detail["id"], work_id, len(comments),
    )
    return {**base, "ok": True, "count": len(comments), "message": f"同步 {len(comments)} 条评论"}


# ---------------------------------------------------------------------------
# 一键评论（单明细）
# ---------------------------------------------------------------------------

def start_post_comment(detail_id: str, content: str, pinned: bool) -> str:
    """启动后台发评任务，返回 task_id（HTTP 层立即返回）。"""
    task_id = str(uuid.uuid4())
    with _tasks_lock:
        _tasks[task_id] = {
            "status": "running", "kind": "post", "detail_id": detail_id,
            "result": None, "error": "", "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    threading.Thread(
        target=_run_post, args=(task_id, detail_id, content, bool(pinned)),
        daemon=True, name=f"comment-post-{task_id[:8]}",
    ).start()
    return task_id


def _run_post(task_id: str, detail_id: str, content: str, pinned: bool) -> None:
    detail = _load_detail(detail_id)
    if not detail:
        _finish_error(task_id, "发布明细不存在")
        return
    if detail.get("status") != "success":
        _finish_error(task_id, "该明细未发布成功，无法评论")
        return

    r = _post_one_detail(detail, content, bool(pinned), task_id)
    if not r["ok"]:
        with _tasks_lock:
            _tasks[task_id].update(status="error", error=r["message"])
        return
    with _tasks_lock:
        _tasks[task_id].update(status="done", result={
            "ok": True, "comment_id": r.get("comment_id") or "",
            "pinned": bool(r.get("pinned")), "message": r["message"],
        })


# ---------------------------------------------------------------------------
# 评论同步（单明细）
# ---------------------------------------------------------------------------

def start_sync_comments(detail_id: str) -> str:
    """启动后台评论同步任务，返回 task_id。"""
    task_id = str(uuid.uuid4())
    with _tasks_lock:
        _tasks[task_id] = {
            "status": "running", "kind": "sync", "detail_id": detail_id,
            "result": None, "error": "", "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    threading.Thread(
        target=_run_sync, args=(task_id, detail_id),
        daemon=True, name=f"comment-sync-{task_id[:8]}",
    ).start()
    return task_id


def _run_sync(task_id: str, detail_id: str) -> None:
    detail = _load_detail(detail_id)
    if not detail:
        _finish_error(task_id, "发布明细不存在")
        return

    r = _sync_one_detail(detail)
    if not r["ok"]:
        _finish_error(task_id, r["message"])
        return
    with _tasks_lock:
        _tasks[task_id].update(status="done", result={"ok": True, "count": r["count"]})


# ---------------------------------------------------------------------------
# 批量执行（批次内全部 success 明细，串行跑浏览器）
# ---------------------------------------------------------------------------

_BATCH_SELECT = """
    SELECT d.id, d.batch_id, d.account_id, d.account_name,
           d.work_id, d.publish_url, d.status,
           u.type AS platform_id, u.filePath AS cookie_file
    FROM publish_details d
    LEFT JOIN user_info u ON d.account_id = u.id
    WHERE d.batch_id = ? AND d.status = 'success'
    ORDER BY d.created_at ASC
"""


def _load_batch_details_raw(batch_id: str) -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(_BATCH_SELECT, (batch_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def start_post_comment_batch(batch_id: str, content: str, pinned: bool) -> str:
    """批次级一键评论：批次内全部 success 明细逐个发评，返回 task_id。

    浏览器自动化串行执行（避免多浏览器并发争抢），任务带 progress 进度。
    """
    task_id = str(uuid.uuid4())
    with _tasks_lock:
        _tasks[task_id] = {
            "status": "running", "kind": "post-batch", "batch_id": batch_id,
            "result": None, "error": "",
            "progress": {"done": 0, "total": 0},
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    threading.Thread(
        target=_run_batch, args=(task_id, batch_id, "post", content, bool(pinned)),
        daemon=True, name=f"comment-postbatch-{task_id[:8]}",
    ).start()
    return task_id


def start_sync_comments_batch(batch_id: str) -> str:
    """批次级评论同步：批次内全部 success 明细逐个抓取，返回 task_id。"""
    task_id = str(uuid.uuid4())
    with _tasks_lock:
        _tasks[task_id] = {
            "status": "running", "kind": "sync-batch", "batch_id": batch_id,
            "result": None, "error": "",
            "progress": {"done": 0, "total": 0},
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    threading.Thread(
        target=_run_batch, args=(task_id, batch_id, "sync", None, False),
        daemon=True, name=f"comment-syncbatch-{task_id[:8]}",
    ).start()
    return task_id


def _load_batch_details(batch_id: str):
    """批次内 success 明细筛选：可评论的返回执行列表，其余归入跳过清单。

    跳过规则（用户预期：不可评论的直接跳过，不算失败）：
      - 未发布成功 → 不参与（调用方已按 status='success' 过滤）
      - 平台未注册 / 未实现评论体系 → 跳过「平台暂未支持评论功能，敬请期待」
      - 未关联作品链接(work_id/publish_url 均无) → 跳过「未关联作品」
    """
    keep, skipped = [], []
    for d in _load_batch_details_raw(batch_id):
        platform_id = d.get("platform_id")
        supported = False
        if platform_id:
            from impl.registry import get_platform
            p = get_platform(platform_id)
            supported = bool(p and getattr(p, "supports_comment", False))
        if not supported:
            skipped.append({
                "account_name": d.get("account_name") or "",
                "reason": "平台暂未支持评论功能，敬请期待",
            })
        elif not _resolve_work_id(d):
            skipped.append({
                "account_name": d.get("account_name") or "",
                "reason": "未关联作品链接",
            })
        else:
            keep.append(d)
    return keep, skipped


def _run_batch(task_id: str, batch_id: str, kind: str,
               content: str | None, pinned: bool) -> None:
    details, skipped = _load_batch_details(batch_id)
    if not details:
        _finish_error(
            task_id,
            "该批次没有可评论的明细"
            + (f"（跳过 {len(skipped)} 个：未发布成功/未关联作品/平台暂未支持）"
               if skipped else ""),
        )
        return
    total = len(details)
    with _tasks_lock:
        _tasks[task_id]["skipped"] = skipped
    results: list[dict] = []
    for i, detail in enumerate(details):
        with _tasks_lock:
            _tasks[task_id]["progress"] = {"done": i, "total": total}
        try:
            if kind == "post":
                r = _post_one_detail(detail, content or "", pinned, task_id)
            else:
                r = _sync_one_detail(detail)
        except Exception as exc:
            logger.warning("[Comments] 批量项执行异常 detail=%s: %s", detail["id"], exc)
            r = {
                "detail_id": detail["id"],
                "account_name": detail.get("account_name") or "",
                "platform_id": detail.get("platform_id"),
                "ok": False, "message": f"执行异常: {exc}",
            }
        results.append(r)
        logger.info(
            "[Comments] 批量%s进度 %d/%d detail=%s ok=%s",
            "发评" if kind == "post" else "同步", i + 1, total,
            detail["id"], r.get("ok"),
        )

    succeed = sum(1 for r in results if r.get("ok"))
    with _tasks_lock:
        _tasks[task_id].update(
            status="done",
            progress={"done": total, "total": total},
            result={"ok": True, "results": results,
                    "succeed": succeed, "total": total,
                    "skipped": skipped},
        )
    logger.info(
        "[Comments] 批量%s完成 batch=%s 成功 %d/%d 跳过 %d",
        "发评" if kind == "post" else "同步", batch_id, succeed, total, len(skipped),
    )


# ---------------------------------------------------------------------------
# 删除评论（单条）
# ---------------------------------------------------------------------------

def start_delete_comment(detail_id: str, comment_id: str) -> str:
    """启动后台删评任务（浏览器自动化），返回 task_id。"""
    task_id = str(uuid.uuid4())
    with _tasks_lock:
        _tasks[task_id] = {
            "status": "running", "kind": "delete", "detail_id": detail_id,
            "comment_id": comment_id,
            "result": None, "error": "", "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    threading.Thread(
        target=_run_delete, args=(task_id, detail_id, comment_id),
        daemon=True, name=f"comment-del-{task_id[:8]}",
    ).start()
    return task_id


def _run_delete(task_id: str, detail_id: str, comment_id: str) -> None:
    detail = _load_detail(detail_id)
    if not detail:
        _finish_error(task_id, "发布明细不存在")
        return
    platform_id = detail.get("platform_id")
    cookie_file = (detail.get("cookie_file") or "").strip()
    if not platform_id or not cookie_file:
        _finish_error(task_id, "账号不存在或 cookie 配置缺失")
        return
    work_id = _resolve_work_id(detail)
    if not work_id:
        _finish_error(task_id, "未找到作品 ID（请先执行「重新关联」回填作品链接）")
        return

    ok, result = _call_platform(
        platform_id, cookie_file, "delete_comment",
        work_id=work_id, comment_id=comment_id,
    )
    if not ok:
        _finish_error(task_id, result.get("message") or "该平台暂未支持删除评论")
        return
    if not result.get("ok"):
        with _tasks_lock:
            _tasks[task_id].update(status="error", error=result.get("message") or "删除失败")
        return

    # 平台删除成功 → 同步删本地行；删顶级评论时其楼中楼一并删除
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute(
                "DELETE FROM work_comments WHERE detail_id = ? AND (comment_id = ? OR parent_id = ?)",
                (detail_id, comment_id, comment_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[Comments] 本地评论行删除失败(不影响平台结果): %s", exc)

    with _tasks_lock:
        _tasks[task_id].update(status="done", result={"ok": True, "message": "评论已删除"})
    logger.info("[Comments] 删评完成 detail=%s comment=%s", detail_id, comment_id)


# ---------------------------------------------------------------------------
# 回复评论（单条）
# ---------------------------------------------------------------------------

def start_reply_comment(detail_id: str, comment_id: str, content: str) -> str:
    """启动后台回复任务（浏览器自动化），返回 task_id。"""
    task_id = str(uuid.uuid4())
    with _tasks_lock:
        _tasks[task_id] = {
            "status": "running", "kind": "reply", "detail_id": detail_id,
            "comment_id": comment_id,
            "result": None, "error": "", "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    threading.Thread(
        target=_run_reply, args=(task_id, detail_id, comment_id, content),
        daemon=True, name=f"comment-reply-{task_id[:8]}",
    ).start()
    return task_id


def _run_reply(task_id: str, detail_id: str, comment_id: str, content: str) -> None:
    detail = _load_detail(detail_id)
    if not detail:
        _finish_error(task_id, "发布明细不存在")
        return
    platform_id = detail.get("platform_id")
    cookie_file = (detail.get("cookie_file") or "").strip()
    if not platform_id or not cookie_file:
        _finish_error(task_id, "账号不存在或 cookie 配置缺失")
        return
    work_id = _resolve_work_id(detail)
    if not work_id:
        _finish_error(task_id, "未找到作品 ID（请先执行「重新关联」回填作品链接）")
        return

    # 目标评论的 parent_id 决定 root：目标是顶级评论 → root=parent=目标；
    # 目标是楼中楼 → root=其所属顶级评论, parent=目标
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT parent_id FROM work_comments WHERE detail_id = ? AND comment_id = ?",
            (detail_id, comment_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        _finish_error(task_id, "目标评论不存在（请先同步评论）")
        return
    root_id = (row["parent_id"] or "").strip() or comment_id

    ok, result = _call_platform(
        platform_id, cookie_file, "reply_comment",
        work_id=work_id, root_id=root_id, parent_id=comment_id, content=content,
    )
    if not ok:
        _finish_error(task_id, result.get("message") or "该平台暂未支持回复评论")
        return
    if not result.get("ok"):
        with _tasks_lock:
            _tasks[task_id].update(status="error", error=result.get("message") or "回复失败")
        return

    # 自己的回复落库（挂到顶级评论下作为楼中楼）
    detail["work_id"] = work_id
    try:
        _upsert_comment_row(detail, {
            "comment_id": result.get("comment_id") or f"local-{task_id[:8]}",
            "parent_id": root_id,
            "author": detail.get("account_name") or "",
            "avatar": "", "content": content,
            "like_count": 0, "reply_count": 0,
            "publish_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "location": "", "is_author": True, "pinned": False,
        })
    except Exception as exc:
        logger.warning("[Comments] 回复落库失败(不影响结果): %s", exc)

    with _tasks_lock:
        _tasks[task_id].update(status="done", result={
            "ok": True,
            "comment_id": result.get("comment_id") or "",
            "message": "回复已发布",
        })
    logger.info(
        "[Comments] 回复完成 detail=%s root=%s parent=%s", detail_id, root_id, comment_id,
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def get_task(task_id: str) -> dict:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if not task:
            return {"status": "not_found", "result": None, "error": "任务不存在或已过期"}
        return dict(task)


def list_comments(detail_id: str) -> dict:
    """读库返回已同步的评论（前端渲染结构）+ 平台能力标记。"""
    detail = _load_detail(detail_id)
    caps = {
        "supports_comment": False,
        "supports_pin_comment": False,
        "supports_delete_comment": False,
    }
    if detail and detail.get("platform_id"):
        from impl.registry import get_platform
        p = get_platform(detail["platform_id"])
        if p is not None:
            caps = {
                "supports_comment": bool(getattr(p, "supports_comment", False)),
                "supports_pin_comment": bool(getattr(p, "supports_pin_comment", False)),
                "supports_delete_comment": bool(getattr(p, "supports_delete_comment", False)),
            }

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM work_comments WHERE detail_id = ? ORDER BY id ASC",
            (detail_id,),
        ).fetchall()
    finally:
        conn.close()

    tops, replies = [], {}
    for r in rows:
        d = dict(r)
        item = {
            "id": d["comment_id"],
            "author": d["author"],
            "avatar": d["avatar"],
            "content": d["content"],
            "like_count": d["like_count"] or 0,
            "reply_count": d["reply_count"] or 0,
            "time": d["publish_time"] or "",
            "location": d["location"] or "",
            "isAuthor": bool(d["is_author"]),
            "pinned": bool(d["pinned"]),
            "ts": d["id"],  # 稳定排序键
            "replies": [],
        }
        if d["parent_id"]:
            replies.setdefault(d["parent_id"], []).append(item)
        else:
            tops.append(item)
    for t in tops:
        t["replies"] = replies.get(t["id"], [])
        t["reply_count"] = max(t["reply_count"], len(t["replies"]))
    return {**caps, "items": tops, "total": len(rows)}


def _finish_error(task_id: str, message: str) -> None:
    with _tasks_lock:
        _tasks[task_id].update(status="error", error=message)
    logger.info("[Comments] 任务 %s 失败: %s", task_id[:8], message)
