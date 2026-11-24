"""
iQiyi (爱奇艺) platform implementation — CloakBrowser.

Login URL: https://creator.iqiyi.com/
Publish URL: https://creator.iqiyi.com/publish/video/wemedia
"""

import asyncio
import json
import threading
import time
from pathlib import Path
from queue import Queue

from conf import BASE_DIR

from util._logger import bind_account_name, get_channel_logger
from .._browser import create_browser_sync, create_context_sync
from .._utils import clear_and_type, get_account_name_by_cookie_file, parse_schedule_time, raise_if_page_closed, save_login_result
from ..base_platform import BasePlatform

logger = get_channel_logger("iqiyi")

_LOGIN_URL = "https://creator.iqiyi.com/"
_PUBLISH_URL = "https://creator.iqiyi.com/publish/video/wemedia"
# 作品列表接口(2026-11 踩点): 创作者中心首页「最近作品」模块自发请求,
# 纯 cookie 鉴权无签名, 页内 fetch + credentials 直调即可, 见 list_works
_WORKS_LIST_API = "https://mp-api.iqiyi.com/video/api/1.0/upgc/mp/selfmedia/videos"

# ---------------------------------------------------------------------------
# Creation declaration mapping (radio values → human-readable labels)
# ---------------------------------------------------------------------------
CREATION_DECLARATION_MAP = {
    "含AI生成内容": "1",
    "含虚构演绎内容": "2",
    "内容含营销信息": "4",
    "内容为转载": "6",
    "个人观点，仅供参考": "5",
    "内容无需标注": "0",
}

# Reverse: value → label
CREATION_DECLARATION_REVERSE = {v: k for k, v in CREATION_DECLARATION_MAP.items()}

# ---------------------------------------------------------------------------
# Risk warning options
# ---------------------------------------------------------------------------
RISK_WARNING_OPTIONS = [
    "内容可能引人不适，请谨慎观看",
    "内容含有高危险行为，请勿模仿",
    "请理性适度消费",
    "未成年人请在监护人指导下浏览",
]


async def _scrape_iqiyi_profile(page) -> tuple[str, str]:
    """Scrape nickname and avatar from creator.iqiyi.com.

    DOM structure:
      - Avatar: div[class*="user-info"] img  → src
      - Nickname: span[class*="emoji-wrap"]  → text
    """
    name = ""
    avatar = ""

    try:
        await page.wait_for_selector('[class*="user-info"]', timeout=10000)
    except Exception:
        logger.warning("user-info section not found")

    try:
        name_el = page.locator('span[class*="emoji-wrap"]').first
        if await name_el.count() > 0:
            name = (await name_el.text_content() or "").strip()
    except Exception as e:
        logger.warning("Failed to scrape nickname: %s", e)

    try:
        avatar_el = page.locator('[class*="user-info"] img').first
        if await avatar_el.count() > 0:
            avatar = (await avatar_el.get_attribute("src") or "").strip()
    except Exception as e:
        logger.warning("Failed to scrape avatar: %s", e)

    return name, avatar


class IqiyiPlatform(BasePlatform):
    platform_id = 10
    platform_key = "iqiyi"
    platform_name = "爱奇艺"

    # 支持 cookie 字符串导入账号
    supports_cookie_import = True
    platform_cookie_domain = ".iqiyi.com"

    def _parse_cookie_to_storage_state(self, cookie_str):
        cookies = []
        expires = time.time() + BasePlatform._IMPORT_COOKIE_EXPIRES_SECONDS
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if not pair or "=" not in pair: continue
            name, _, value = pair.partition("=")
            cookies.append({
                "name": name.strip(), "value": value.strip(),
                "domain": self.platform_cookie_domain, "path": "/",
                "expires": expires, "httpOnly": True, "secure": False, "sameSite": "Lax",
            })
        return cookies, []

    # ------------------------------------------------------------------
    # login — open browser, wait for user to log in manually
    # ------------------------------------------------------------------

    async def login(self, id: str, status_queue: Queue, account_id=None) -> None:
        url_changed_event = asyncio.Event()

        async def _on_url_change():
            if page.url.strip("/") == _LOGIN_URL.strip("/"):
                # After login, the page reloads at the same URL with auth
                # Check for user-info to confirm
                try:
                    await page.wait_for_selector(
                        '[class*="user-info"]', timeout=5000
                    )
                    url_changed_event.set()
                except Exception:
                    pass

        browser = await self.create_browser(login_mode=True)
        try:
            context = await self.create_context(browser)
            try:
                page = await context.new_page()
                await page.goto(_LOGIN_URL)

                page.on(
                    "framenavigated",
                    lambda frame: asyncio.create_task(_on_url_change())
                    if frame == page.main_frame
                    else None,
                )

                # Wait up to 300 s for login
                try:
                    await asyncio.wait_for(url_changed_event.wait(), timeout=300)
                    logger.info("Login detected — user-info found")
                except asyncio.TimeoutError:
                    logger.warning("Login timed out (300 s)")
                    status_queue.put("500")
                    return

                await save_login_result(
                    context,
                    page,
                    platform_id=self.platform_id,
                    platform_name=self.platform_name,
                    status_queue=status_queue,
                    scrape_fn=_scrape_iqiyi_profile,
                    account_id=account_id,
                    # 登录成功后在同一个 session 内补抓 stats(获赞/关注/粉丝),
                    # 与 sync_profile 共用同一份抓取逻辑
                    stats_fn=self._login_stats_fn,
                )
            finally:
                await context.close()
        finally:
            await browser.close()

    # ------------------------------------------------------------------
    # check_cookie — verify stored cookie is still valid
    # ------------------------------------------------------------------

    async def check_cookie(self, cookie_file: str) -> bool:
        cookie_path = str(Path(BASE_DIR / "cookiesFile" / cookie_file))
        browser = await self.create_browser(headless=True)
        try:
            context = await self.create_context(browser, storage_state=cookie_path)
            try:
                page = await context.new_page()
                await page.goto(_LOGIN_URL, wait_until="domcontentloaded")
                await page.wait_for_load_state("networkidle")

                try:
                    await page.wait_for_selector(
                        '[class*="user-info"]', timeout=5000
                    )
                    return True
                except Exception:
                    return False
            finally:
                await context.close()
        except Exception as e:
            logger.warning("check_cookie failed: %s", e)
            return False
        finally:
            await browser.close()

    # ------------------------------------------------------------------
    # sync_profile — scrape user name and avatar
    # ------------------------------------------------------------------

    async def sync_profile(self, cookie_file: str) -> dict:
        cookie_path = str(Path(BASE_DIR / "cookiesFile" / cookie_file))
        browser = await self.create_browser(headless=True)
        try:
            context = await self.create_context(browser, storage_state=cookie_path)
            try:
                page = await context.new_page()
                await page.goto(_LOGIN_URL, wait_until="domcontentloaded")
                await page.wait_for_load_state("networkidle")
                name, avatar = await _scrape_iqiyi_profile(page)
                # 同一个 user-info 区块也包含 stats(获赞/关注/粉丝),用 evaluate 一次抓完
                stats = await self._scrape_iqiyi_stats(page)
                return {"name": name, "avatar": avatar, "stats": stats}
            finally:
                await context.close()
        except Exception as e:
            logger.warning("sync_profile failed: %s", e)
            return {"name": "", "avatar": "", "stats": []}
        finally:
            await browser.close()

    async def _scrape_iqiyi_stats(self, page) -> list:
        """抓取爱奇艺创作者中心首页的 3 项运营数据(获赞/关注/粉丝)。

        页面 DOM 结构(参见用户提供的 2026-07-20 抓取样本):
            <div class="user-info">
                <div class="user-info__avatar"><img src="..."></div>
                <div class="user-info__main">
                    <h2 class="user-info__name"><span>惡v魔</span></h2>
                    <div class="user-info__row user-info__row--stats">
                        <span class="user-info__label">获赞</span>
                        <span class="user-info__value">2</span>
                        <span class="user-info__label">关注</span>
                        <span class="user-info__value">0</span>
                        <div class="user-info__fans">
                            <span class="user-info__label">粉丝</span>
                            <span class="user-info__value user-info__value_link">1</span>
                        </div>
                    </div>
                </div>
            </div>

        Returns:
            list[dict]: 按 SORT 排序的运营数据列表
        """
        stats = []
        # label_map: label 文本 -> (ICON, SORT, 标准化 NAME)
        # SORT 顺序: 粉丝最重要(1)、获赞(2)、关注(3)
        label_map = {
            "粉丝": ("user",   1, "粉丝"),
            "获赞": ("like",   2, "获赞"),
            "关注": ("follow", 3, "关注"),
        }

        try:
            try:
                await page.wait_for_selector(".user-info__row--stats", timeout=10000)
            except Exception:
                logger.info("[iqiyi stats] 等待 .user-info__row--stats 超时")

            raw = await page.evaluate(
                '''() => {
                    const out = [];
                    const statsRow = document.querySelector('.user-info__row--stats');
                    if (!statsRow) return out;
                    // 按 DOM 顺序遍历,label 与 value 交替出现
                    const children = Array.from(statsRow.children);
                    for (let i = 0; i < children.length; i++) {
                        const el = children[i];
                        if (el.classList.contains('user-info__label')) {
                            const label = (el.textContent || '').trim();
                            // 找下一个 value
                            const next = children[i + 1];
                            if (next && next.classList.contains('user-info__value')) {
                                const num = (next.textContent || '').trim();
                                if (label && num) out.push({label, num});
                                i++; // 跳过 value
                            }
                        }
                        // 处理嵌套的 .user-info__fans div
                        if (el.classList.contains('user-info__fans')) {
                            const lblEl = el.querySelector('.user-info__label');
                            const valEl = el.querySelector('.user-info__value');
                            if (lblEl && valEl) {
                                const label = (lblEl.textContent || '').trim();
                                const num = (valEl.textContent || '').trim();
                                if (label && num) out.push({label, num});
                            }
                        }
                    }
                    return out;
                }'''
            )
            for item in raw:
                label = item.get('label', '')
                if label in label_map:
                    icon, sort_no, name = label_map[label]
                    try:
                        count = int(str(item.get('num', '0')).replace(',', '').replace(' ', '') or '0')
                    except (ValueError, TypeError):
                        count = 0
                    stats.append({"ICON": icon, "COUNT": count, "NAME": name, "SORT": sort_no})
        except Exception as exc:
            logger.info(f"[iqiyi stats] 抓取失败: {exc}")

        stats.sort(key=lambda x: x.get("SORT", 999))
        return stats

    async def _login_stats_fn(self, page, account_id) -> list:
        """登录成功后的 stats 抓取入口(供 save_login_result 调用)。

        与 sync_profile 内部共用 _scrape_iqiyi_stats 抓取逻辑。
        """
        try:
            return await self._scrape_iqiyi_stats(page)
        except Exception as exc:
            logger.info(f"[iqiyi login] _login_stats_fn 抓取失败: {exc}")
            return []

    # ------------------------------------------------------------------
    # open_creator_center — open visible browser with stored cookies
    # ------------------------------------------------------------------

    async def open_creator_center(self, cookie_file: str) -> None:
        cookie_path = str(Path(BASE_DIR / "cookiesFile" / cookie_file))
        url = _LOGIN_URL

        def _launch():
            browser = create_browser_sync(headless=False)
            try:
                context = create_context_sync(browser, storage_state=cookie_path)
                page = context.new_page()
                page.goto(url)
                try:
                    page.wait_for_event("close", timeout=0)
                except Exception:
                    pass
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

        thread = threading.Thread(target=_launch, daemon=True)
        thread.start()

    # ------------------------------------------------------------------
    # publish_video — full iQiyi upload pipeline
    # ------------------------------------------------------------------

    async def publish_video(self, **kwargs) -> bool:
        """Publish a video to iQiyi via CloakBrowser.

        Accepted keyword arguments:

        - ``title`` (*str*) -- video title
        - ``files`` (*list[str]*) -- video absolute file paths (resolved by app.py)
        - ``tags`` (*list[str]*) -- hashtags
        - ``account_file`` (*list[str]*) -- cookie file names
        - ``enableTimer`` (*bool*, optional)
        - ``schedule_time_str`` (*str*, optional)
        - ``desc`` (*str*, optional)
        - ``thumbnail_path`` (*str*, optional) -- cover image path
        - ``thumbnail_landscape_path`` (*str*, optional) -- landscape cover
        - ``thumbnail_portrait_path`` (*str*, optional) -- portrait cover
        - ``creation_declaration`` (*str*) -- creation declaration label
        - ``risk_warning`` (*str*, optional) -- risk warning label
        - ``enable_cash_activity`` (*bool*, optional) -- participate in cash activity
        - ``videos_per_day`` (*int*, optional)
        - ``daily_times`` (*list*, optional)
        - ``start_days`` (*int*, optional)
        """
        logger.info("=" * 60)
        logger.info("[发布视频] 开始爱奇艺视频发布流程")
        logger.info("=" * 60)

        # 打印所有接收到的参数
        logger.info("[发布参数] 接收到的所有参数:")
        for key, value in kwargs.items():
            logger.info("[发布参数]   %s = %s (类型: %s)", key, value, type(value).__name__)

        title = kwargs.get("title", "")
        files = kwargs.get("files", [])
        tags = kwargs.get("tags", []) or []
        account_file = kwargs.get("account_file", [])
        enableTimer = kwargs.get("enableTimer", False)
        schedule_time_str = kwargs.get("schedule_time_str", "")
        thumbnail_path = kwargs.get("thumbnail_path", "")
        thumbnail_landscape_path = kwargs.get("thumbnail_landscape_path", "")
        thumbnail_portrait_path = kwargs.get("thumbnail_portrait_path", "")
        # 16:9 横版封面:爱奇艺封面弹窗有 3 个 tab (竖 / 4:3 横 / 16:9 横)
        thumbnail_landscape_169_path = kwargs.get("thumbnail_landscape_169_path", "") or ""
        creation_declaration = kwargs.get("creation_declaration", "")
        risk_warning = kwargs.get("risk_warning", "")
        enable_cash_activity = kwargs.get("enable_cash_activity", False)
        desc = kwargs.get("desc", "")

        # 打印发布参数摘要
        logger.info("[发布参数] 标题: %s", title)
        logger.info("[发布参数] 文件数量: %d", len(files))
        logger.info("[发布参数] 标签: %s", tags)
        logger.info("[发布参数] 视频简介: %s", desc[:50] if desc else "无")
        logger.info("[发布参数] 账号数量: %d", len(account_file))
        logger.info("[发布参数] 定时发布: %s", enableTimer)
        logger.info("[发布参数] 横版封面: %s", thumbnail_landscape_path or "无")
        logger.info("[发布参数] 竖版封面: %s", thumbnail_portrait_path or "无")
        logger.info("[发布参数] 16:9横版封面: %s", thumbnail_landscape_169_path or "无")
        logger.info("[发布参数] 创作声明: %s", creation_declaration or "无")
        logger.info("[发布策略] 发布策略: %s", "scheduled" if enableTimer and schedule_time_str else "immediate")

        # Resolve full paths
        account_paths = [
            str(Path(BASE_DIR / "cookiesFile") / f) for f in account_file
        ]
        # files 已是绝对路径（app.py 通过 _resolve_material_path 处理过）
        file_paths = [str(f) for f in files]

        cover_path = ""
        for p in [thumbnail_portrait_path, thumbnail_path, thumbnail_landscape_path]:
            if p:
                # 已是绝对路径
                cover_path = str(p)
                break

        landscape_cover = ""
        if thumbnail_landscape_path:
            # 已是绝对路径
            landscape_cover = str(thumbnail_landscape_path)

        portrait_cover = ""
        if thumbnail_portrait_path:
            # 已是绝对路径
            portrait_cover = str(thumbnail_portrait_path)

        # Parse schedule times
        publish_datetimes = parse_schedule_time(
            schedule_time_str,
            len(file_paths),
            enableTimer,
            kwargs.get("videos_per_day", 1),
            kwargs.get("daily_times"),
            kwargs.get("start_days", 0),
        )

        overall_success = True
        for file_index, file_path in enumerate(file_paths):
            logger.info("-" * 40)
            logger.info("[发布进度] 处理第 %d/%d 个视频: %s", file_index + 1, len(file_paths), file_path)
            for cookie_index, cookie_path in enumerate(account_paths):
                cookie_name = Path(cookie_path).name
                nick = get_account_name_by_cookie_file(cookie_name)
                with bind_account_name(nick or "-"):
                    logger.info("[发布进度] 发布到第 %d/%d 个账号 (%s)", cookie_index + 1, len(account_paths), nick or "未知")
                    ok = await self._upload_one_video(
                        title=title,
                        file_path=file_path,
                        tags=tags,
                        publish_date=publish_datetimes[file_index],
                        account_file=cookie_path,
                        enableTimer=enableTimer,
                        cover_path=portrait_cover or cover_path or None,
                        landscape_cover=landscape_cover or None,
                        landscape_cover_169=thumbnail_landscape_169_path or None,
                        creation_declaration=creation_declaration,
                        risk_warning=risk_warning,
                        enable_cash_activity=enable_cash_activity,
                        desc=desc,
                    )
                    if not ok:
                        overall_success = False

        logger.info("=" * 60)
        logger.info("[发布视频] 视频发布流程完成!")
        logger.info("=" * 60)
        return overall_success

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _upload_one_video(
        self,
        title: str,
        file_path: str,
        tags: list,
        publish_date,
        account_file: str,
        enableTimer: bool = False,
        cover_path=None,
        landscape_cover=None,
        landscape_cover_169=None,
        creation_declaration="",
        risk_warning="",
        enable_cash_activity=False,
        desc="",
    ):
        """Upload a single video to one iQiyi account."""
        browser = await self.create_browser(headless=False)
        try:
            context = await self.create_context(browser, storage_state=account_file)
            try:
                page = await context.new_page()
                await page.goto(_PUBLISH_URL)
                await page.wait_for_load_state("networkidle")

                # 注册上传完成请求监听器（必须在 set_input_files 之前注册）
                upload_done = asyncio.Event()

                def _on_iqiyi_request(request):
                    if (
                        "mp-api.iqiyi.com/v-tool/api/1.0/upload/record"
                        in request.url
                        and not upload_done.is_set()
                    ):
                        upload_done.set()

                page.on("request", _on_iqiyi_request)

                # Step 1: Upload video file via input[type=file]
                logger.info("[上传视频] 开始上传视频: %s", file_path)
                file_input = page.locator('input[type="file"]').first
                await file_input.set_input_files(file_path)
                logger.info("[上传视频] 视频文件已选择, 等待上传完成...")

                # Step 1.5: 等待视频上传到服务器完成（监听 /upload/record 请求）
                await self._wait_video_upload_complete(page, upload_done)
                await asyncio.sleep(2)

                # Step 2: Wait for the publish form to appear after upload
                await page.wait_for_selector(
                    '[class*="wemedia-catalog-form"]',
                    timeout=120000,
                )
                logger.info("[上传视频] 视频上传成功! 发布表单已就绪")
                await asyncio.sleep(3)

                # Pre-form screenshot（表单渲染就绪后, 仿 bilibili, 供 E2E 报告佐证）
                try:
                    log_dir = Path(BASE_DIR / "logs")
                    log_dir.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(
                        path=str(log_dir / "iqiyi_before_form.png"),
                        full_page=True,
                    )
                except Exception as e:
                    logger.warning(
                        "[上传视频] before_form 截图失败(不影响发布): %s", e
                    )

                # Step 3: Fill title
                await self._fill_title(page, title or desc)

                # Step 4: Fill description (tags 以 #XXX 格式追加)
                full_desc = desc or ""
                if tags:
                    tag_str = " ".join(f"#{t}" for t in tags)
                    full_desc = f"{full_desc} {tag_str}".strip()
                await self._fill_description(page, full_desc)

                # Step 5: Click cash activity if enabled
                if enable_cash_activity:
                    await self._click_cash_activity(page)

                # Step 6: Set creation declaration (required — radio)
                if creation_declaration:
                    await self._set_creation_declaration(
                        page, creation_declaration
                    )

                # Step 7: Set risk warning (optional — select)
                if risk_warning:
                    await self._set_risk_warning(page, risk_warning)

                # Step 8: Upload cover image(s) (3 个 tab: 竖 / 4:3 横 / 16:9 横)
                logger.info(
                    "[上传视频] Step 8: cover_path=%s, landscape_cover=%s, landscape_cover_169=%s",
                    cover_path, landscape_cover, landscape_cover_169,
                )
                if cover_path or landscape_cover or landscape_cover_169:
                    logger.info(">>> Calling _upload_cover <<<")
                    await self._upload_cover(
                        page,
                        portrait_path=cover_path,
                        landscape_path=landscape_cover,
                        landscape_169_path=landscape_cover_169,
                    )
                else:
                    logger.warning("[上传视频] Step 8: SKIPPED — no cover paths provided")

                # Step 9: Handle scheduled publishing
                if enableTimer and publish_date != 0:
                    await self._set_schedule_time(page, publish_date)

                # Step 10: Click publish / submit
                publish_ok = await self._click_publish(page)

                # After-submit screenshot（成功判定完成后、流程收尾前）
                # 放在 _click_publish 返回之后, 不介入其内部"跳到管理页判失败"等判定逻辑
                try:
                    log_dir = Path(BASE_DIR / "logs")
                    log_dir.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(
                        path=str(log_dir / "iqiyi_after_submit.png"),
                        full_page=True,
                    )
                except Exception as e:
                    logger.warning(
                        "[上传视频] after_submit 截图失败(不影响发布): %s", e
                    )

                # Save updated cookie state regardless
                await context.storage_state(path=account_file)
                logger.info("[上传视频] Cookie state updated after publish")

                if not publish_ok:
                    logger.error("[上传视频] Publish failed for %s", account_file)
                    return False
                return True
            finally:
                await context.close()
        finally:
            await self.close_browser(browser, is_close_by_code=True)

    # ------------------------------------------------------------------
    # Upload wait helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _wait_video_upload_complete(
        page,
        upload_done: asyncio.Event,
    ) -> None:
        """等待视频上传到服务器完成。

        监听 ``/v-tool/api/1.0/upload/record`` HTTP 请求被触发
        （caller 负责在 ``set_input_files`` 之前注册监听器）。这是
        服务端确认上传完成的权威信号——DOM 提示可能提前消失，不能
        作为完成依据。

        上限 4 小时(防止网络异常/用户关浏览器时 watchdog 未生效而永久卡死)。
        大文件(≤16G)在弱网下可能要很久, 给足时间。
        """
        try:
            await asyncio.wait_for(upload_done.wait(), timeout=14400)
            logger.info(
                "检测到 /upload/record 请求，视频上传完成"
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[上传视频] 4 小时内未检测到 /upload/record, "
                "可能上传失败/网络异常/用户关浏览器, 继续后续步骤"
            )

    # ------------------------------------------------------------------
    # Form field helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------

    @staticmethod
    async def _fill_title(page, title: str):
        """Fill the video title."""
        if not title:
            return

        # Target: input.catalog-desc-title-input input[type="text"]
        # or just find by placeholder
        title_input = page.locator(
            'input[placeholder*="标题字数"]'
        ).first
        if await title_input.count() == 0:
            # Fallback: any title-related input
            title_input = page.locator(
                '.catalog-desc-title-input input[type="text"]'
            ).first
        if await title_input.count() == 0:
            logger.warning("[填写标题] 未找到标题输入框")
            return

        await title_input.wait_for(state="visible", timeout=10000)
        await title_input.click()
        await asyncio.sleep(0.3)

        # 清空后输入(跨平台:Mac 用 Cmd+A,其他用 Ctrl+A)
        # 传 element= 让 clear_and_type 走 fill('') 稳定路径(原生 <input> 元素)
        await clear_and_type(page, title[:30], element=title_input)
        logger.info("[填写标题] 标题已填写: %s", title[:30])

    @staticmethod
    async def _fill_description(page, desc: str):
        """Fill the video description."""
        if not desc:
            return

        desc_textarea = page.locator(
            'textarea[placeholder*="作品简介"]'
        ).first
        if await desc_textarea.count() == 0:
            logger.warning("[填写简介] Description textarea not found")
            return

        await desc_textarea.wait_for(state="visible", timeout=5000)
        await desc_textarea.click()
        await asyncio.sleep(0.3)

        # 清空后输入(跨平台:Mac 用 Cmd+A,其他用 Ctrl+A)
        # 传 element= 让 clear_and_type 走 fill('') 稳定路径(原生 <textarea> 元素)
        await clear_and_type(page, desc[:450], element=desc_textarea)
        logger.info("[填写简介] Description filled: %s", desc[:50])

    @staticmethod
    async def _set_creation_declaration(page, declaration: str):
        """Set the creation declaration (required — radio button).

        ``declaration`` can be a label (e.g. "含AI生成内容") or a value ("1").
        """
        # Determine the radio value to click
        value = CREATION_DECLARATION_MAP.get(declaration, declaration)

        logger.info("[设置声明] Setting creation declaration to value=%s", value)

        try:
            # Click the <label class="el-radio"> that wraps the matching radio input
            label = page.locator(
                f'.form-declare-group label.el-radio '
                f'input[type="radio"][value="{value}"] '
                f'>> xpath=ancestor::label'
            ).first
            if await label.count() == 0:
                # Fallback: find by text content
                decl_label = CREATION_DECLARATION_REVERSE.get(value, declaration)
                label = page.locator(
                    f'.form-declare-group label.el-radio:has-text("{decl_label}")'
                ).first

            if await label.count() > 0:
                await label.wait_for(state="visible", timeout=5000)
                await label.click()
                logger.info(
                    "Creation declaration set to: %s", declaration
                )
                await asyncio.sleep(0.5)
            else:
                logger.warning(
                    "Creation declaration label not found for value=%s", value
                )
        except Exception as e:
            logger.warning(
                "Failed to set creation declaration (non-blocking): %s", e
            )

    @staticmethod
    async def _set_risk_warning(page, warning: str):
        """Set the risk warning (optional — select dropdown)."""
        if warning not in RISK_WARNING_OPTIONS:
            logger.warning("[风险提示] Unknown risk warning: %s", warning)
            return

        logger.info("[风险提示] Setting risk warning: %s", warning)

        try:
            # Click the select trigger to open the dropdown
            select_trigger = page.locator(
                '.form-select-full .el-input__inner'
            ).first
            if await select_trigger.count() == 0:
                logger.warning("[风险提示] Risk warning select not found")
                return

            await select_trigger.click()
            await asyncio.sleep(1)

            # Wait for the dropdown to appear and click the option
            option = page.locator(
                f'.el-select-dropdown__item:has-text("{warning}")'
            ).first
            if await option.count() > 0:
                await option.wait_for(state="visible", timeout=5000)
                await option.click()
                logger.info("[风险提示] Risk warning set to: %s", warning)
                await asyncio.sleep(0.5)
            else:
                logger.warning("[风险提示] Risk warning option not found: %s", warning)
        except Exception as e:
            logger.warning(
                "Failed to set risk warning (non-blocking): %s", e
            )

    @staticmethod
    async def _click_cash_activity(page):
        """Click the cash activity (打卡挑战赛) radio to enable it."""
        logger.info("[现金活动] Clicking cash activity")
        try:
            activity = page.locator(
                '.activity-radio-option:not(.is-checked)'
            ).first
            if await activity.count() > 0:
                await activity.click()
                logger.info("[现金活动] Cash activity clicked")
                await asyncio.sleep(0.5)
            else:
                logger.info("[现金活动] Cash activity already checked or not found")
        except Exception as e:
            logger.warning("[现金活动] Failed to click cash activity (non-blocking): %s", e)

    @staticmethod
    async def _upload_cover(
        page,
        portrait_path=None,
        landscape_path=None,
        landscape_169_path=None,
        **kwargs,
    ):
        """Upload cover images on the iQiyi publish page.

        Dialog DOM structure (image-crop-dialog):
          - Tab bar: "竖封面" (portrait, default active) | "横封面" (landscape)
          - Two ``.crop-content`` panels — first visible (portrait), second hidden (landscape)
          - Each panel has a hidden ``input[type=file]`` (via Plupload/moxie shim)
          - Footer: "完成" (confirm) | "设置横封面" (alternative switch, NOT used here)

        Workflow:
          1. Click "选择封面" trigger → opens the crop dialog
          2. Upload portrait cover via the visible file input (first panel)
          3. Wait for server-side upload to complete
          4. Click the "横封面" tab to switch to the landscape panel
          5. Upload landscape cover via the now-visible file input (second panel)
          6. Wait for server-side upload to complete
          7. Click "完成" to confirm and close the dialog
        """
        portrait_path = portrait_path or kwargs.get("cover_path")
        landscape_path = landscape_path or kwargs.get("landscape_path")
        landscape_169_path = landscape_169_path or kwargs.get("landscape_169_path")

        logger.info(
            "[设置封面] 封面上传: 竖版=%s, 4:3横版=%s, 16:9横版=%s",
            portrait_path, landscape_path, landscape_169_path,
        )

        try:
            # ---------------------------------------------------------------
            # Step 1: Click "选择封面" to open the cover crop dialog
            # ---------------------------------------------------------------
            logger.info("[设置封面] Step 1: Opening cover dialog...")
            trigger = page.locator('div.main-edit-bar').first
            if await trigger.count() == 0:
                logger.warning("'选择封面' trigger not found, aborting cover upload")
                return
            await trigger.scroll_into_view_if_needed()
            await trigger.wait_for(state="visible", timeout=10000)
            await trigger.evaluate("el => el.click()")

            dialog = page.locator('.image-crop-dialog')
            await dialog.wait_for(state="visible", timeout=10000)
            logger.info("[设置封面] Step 1: Cover dialog opened")
            # 首次打开弹窗: React 渲染/3 个 tab lazy init, 等 10s 充分就绪
            await asyncio.sleep(10)

            # ---------------------------------------------------------------
            # Step 2: Upload portrait cover (竖封面)
            # ---------------------------------------------------------------
            if portrait_path:
                logger.info("[设置封面] Step 2: Uploading portrait cover: %s", portrait_path)
                # The first visible .crop-content panel contains the portrait upload
                portrait_panel = page.locator(
                    '.crop-content:not([style*="display: none"])'
                ).first
                await portrait_panel.wait_for(state="visible", timeout=5000)

                upload_btn = portrait_panel.locator('.upload-btn-wrap').first
                async with page.expect_file_chooser() as fc_info:
                    await upload_btn.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(portrait_path)
                logger.info("[设置封面] Step 2: Portrait file set, waiting for upload...")
                # 首次上传后等稍长(组件正在响应文件), 后续 tab 已熟可缩短
                await asyncio.sleep(3)
                logger.info("[设置封面] Step 2: Portrait upload complete")
            else:
                logger.info("[设置封面] Step 2: SKIPPED — no portrait_path")

            # ---------------------------------------------------------------
            # Step 3: Switch to landscape tab and upload (横封面)
            # ---------------------------------------------------------------
            if landscape_path:
                logger.info("[设置封面] Step 3: Switching to landscape (4:3) tab...")
                # 4:3 横封面 tab 文本是 "横封面（4:3 必填）", 用 "4:3" 精确匹配
                # 避免和 16:9 tab 同时匹配
                landscape_tab = page.locator('.tab-item:has-text("4:3")').first
                if await landscape_tab.count() > 0:
                    await landscape_tab.click()
                    logger.info("[设置封面] Step 3: Landscape tab clicked")
                    # tab 切换: 组件已就绪, 2s 足够
                    await asyncio.sleep(2)

                    logger.info("[设置封面] Step 3: Uploading landscape cover: %s", landscape_path)
                    landscape_panel = page.locator(
                        '.crop-content:not([style*="display: none"])'
                    ).first
                    await landscape_panel.wait_for(state="visible", timeout=5000)

                    upload_btn = landscape_panel.locator('.upload-btn-wrap').first
                    async with page.expect_file_chooser() as fc_info:
                        await upload_btn.click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(landscape_path)
                    logger.info("[设置封面] Step 3: Landscape file set, waiting for upload...")
                    await asyncio.sleep(3)
                    logger.info("[设置封面] Step 3: Landscape upload complete")
                else:
                    logger.warning("[设置封面] Step 3: Landscape tab not found")
            else:
                logger.info("[设置封面] Step 3: SKIPPED — no landscape_path")

            # ---------------------------------------------------------------
            # Step 3.5: 切到第三个 tab "横封面（16:9 必填）" 上传 16:9 横封面
            # ---------------------------------------------------------------
            if landscape_169_path:
                logger.info("[设置封面] Step 3.5: Switching to 16:9 landscape tab...")
                # 第三个 tab 文本是 "横封面（16:9 必填）", 精确匹配 16:9
                landscape_169_tab = page.locator(
                    '.tab-item:has-text("16:9")'
                ).first
                if await landscape_169_tab.count() > 0:
                    await landscape_169_tab.click()
                    logger.info("[设置封面] Step 3.5: 16:9 landscape tab clicked")
                    # tab 切换: 组件已就绪, 2s 足够
                    await asyncio.sleep(2)

                    landscape_169_panel = page.locator(
                        '.crop-content:not([style*="display: none"])'
                    ).first
                    await landscape_169_panel.wait_for(state="visible", timeout=5000)

                    upload_btn = landscape_169_panel.locator('.upload-btn-wrap').first
                    async with page.expect_file_chooser() as fc_info:
                        await upload_btn.click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(landscape_169_path)
                    logger.info("[设置封面] Step 3.5: 16:9 landscape file set, waiting for upload...")
                    await asyncio.sleep(3)
                    logger.info("[设置封面] Step 3.5: 16:9 landscape upload complete")
                else:
                    logger.warning("[设置封面] Step 3.5: 16:9 landscape tab not found")
            else:
                logger.info("[设置封面] Step 3.5: SKIPPED — no landscape_169_path")

            # ---------------------------------------------------------------
            # Step 4: Click "完成" to confirm
            # ---------------------------------------------------------------
            logger.info("[设置封面] Step 4: Clicking '完成'...")
            done_btn = page.locator('button:has-text("完成")').first
            if await done_btn.count() > 0:
                await done_btn.click()
                logger.info("[设置封面] Step 4: '完成' clicked — cover upload complete")
                await asyncio.sleep(2)
            else:
                logger.warning("[设置封面] Step 4: '完成' button not found")
        except Exception as e:
            logger.warning("[设置封面] 封面上传失败: %s", e, exc_info=True)

    @staticmethod
    async def _set_schedule_time(page, publish_date):
        """Enable scheduled publishing and set the date/time."""
        logger.info("[定时发布] Setting schedule time: %s", publish_date)
        try:
            # Click "定时发布" radio
            schedule_radio = page.locator(
                '.form-publish-block .el-radio-group '
                'label:has-text("定时发布")'
            ).first
            if await schedule_radio.count() > 0:
                await schedule_radio.click()
                logger.info("[定时发布] Schedule radio selected")
                await asyncio.sleep(1)

            # The date picker should appear — find the date input
            date_input = page.locator(
                '.form-publish-block input[placeholder*="选择日期"], '
                '.form-publish-block input[placeholder*="时间"]'
            ).first
            if await date_input.count() > 0:
                await date_input.click()
                await asyncio.sleep(1)

                # Format datetime
                date_str = publish_date.strftime("%Y-%m-%d")
                time_str = publish_date.strftime("%H:%M")

                # Type the date directly
                await date_input.fill(f"{date_str} {time_str}")
                logger.info(
                    "Schedule date set to: %s %s", date_str, time_str
                )
                await asyncio.sleep(1)

                # Press Enter to confirm
                await page.keyboard.press("Enter")
                await asyncio.sleep(1)
            else:
                logger.warning("[定时发布] Schedule date input not found")
        except Exception as e:
            logger.warning(
                "Schedule time setup failed (non-blocking): %s", e
            )

    @staticmethod
    async def _click_publish(page) -> bool:
        """Click the publish button and wait for navigation to the success page.

        **点击前先等待视频上传完成**:爱奇艺发布页的上传区域 ``.up-phone-card``
        在上传过程中会显示(含进度条/速度/剩余时间/取消按钮),上传完成后
        该区域会消失。为确保点发布时视频已真正上传完毕,会:

          1. 等待 ``.up-phone-card`` 不再可见(上传区域消失)
          2. 额外等 3 秒(让后端完成转码/校验等收尾)

        成功判定（两层，任一满足即返回 True）：
          1. URL 离开发布页（不再包含 /publish/video/wemedia）
             **且** 页面出现成功关键词（"发布成功" / "已发布" / "提交成功" / "发布完成"）
          2. URL 跳转到包含 success / published / done 的成功路径

        仅 URL 变化但页面无成功标志时，判定为失败（避免误判跳转到
        内容管理页 / 错误页的情况）。
        """
        # ---- 点击前:等待视频上传区域消失 ----
        # .up-phone-card 是上传进度卡片(含"上传过程中请不要删除/移动文件"提示、
        # 进度条、已上传/速度/剩余时间、取消上传按钮)。上传完成后该卡片消失。
        try:
            upload_card = page.locator('.up-phone-card').first
            if await upload_card.count() > 0:
                logger.info(
                    "[iqiyi] 检测到上传区域 .up-phone-card,等待其消失后再点发布"
                )
                # 轮询等待卡片消失(最长 30 分钟,大文件慢网络留余量)
                deadline = asyncio.get_event_loop().time() + 1800
                last_percent = -1
                while asyncio.get_event_loop().time() < deadline:
                    raise_if_page_closed(page)
                    try:
                        if await upload_card.count() == 0:
                            break
                        # 卡片仍可见,打印进度(便于排查卡住)
                        try:
                            percent_el = upload_card.locator(
                                '.up-progress-percent'
                            ).first
                            if await percent_el.count() > 0:
                                percent_text = await percent_el.text_content()
                                percent_text = (percent_text or '').strip()
                                if percent_text and percent_text != last_percent:
                                    last_percent = percent_text
                                    logger.info(
                                        "[iqiyi] 视频上传中 %s,等待完成...",
                                        percent_text,
                                    )
                        except Exception:
                            pass
                    except Exception:
                        break
                    await asyncio.sleep(5)
                else:
                    logger.warning(
                        "[iqiyi] 等待上传区域消失超时(30min),仍尝试点击发布"
                    )
                logger.info("[iqiyi] 上传区域已消失,额外等待 3s 后点击发布")
                await asyncio.sleep(3)
            else:
                logger.info("[iqiyi] 未检测到上传区域(可能已上传完),直接点发布")
        except Exception as e:
            logger.info(
                "[iqiyi] 等待上传区域异常(忽略,继续点发布): %s", e
            )

        logger.info("[发布] Clicking publish button")
        try:
            publish_btn = page.locator(
                'button:has-text("发布"), '
                'button:has-text("提交"), '
                'button[type="submit"]'
            ).first

            await publish_btn.wait_for(state="visible", timeout=10000)

            # Pre-submit screenshot（全部字段填写完成、点击发布按钮之前, full_page 测试报告核心佐证）
            try:
                log_dir = Path(BASE_DIR / "logs")
                log_dir.mkdir(parents=True, exist_ok=True)
                await page.screenshot(
                    path=str(log_dir / "iqiyi_before_submit.png"),
                    full_page=True,
                )
            except Exception as e:
                logger.warning(
                    "[发布] before_submit 截图失败(不影响发布): %s", e
                )

            await publish_btn.click()
            logger.info("[发布] Publish button clicked, waiting for navigation")

            # 等待 URL 离开发布页（最多 60s）
            try:
                await page.wait_for_function(
                    '!window.location.href.includes("/publish/video/wemedia")',
                    timeout=60000,
                )
            except Exception:
                logger.warning(
                    "Publish failed — still on publish page after 60s timeout. "
                    "URL: %s", page.url
                )
                return False

            logger.info("[发布] URL 离开发布页: %s", page.url)
            await asyncio.sleep(3)  # 等跳转后的页面稳定

            # 判定 1: URL 包含 success / published / done 路径
            current_url = page.url.lower()
            if any(kw in current_url for kw in ("/success", "published", "/done")):
                logger.info("[发布] URL 命中成功路径关键词: %s", page.url)
                logger.info("[发布] Video published successfully")
                return True

            # 判定 2: 页面文本包含成功关键词
            success_keywords = ("发布成功", "已发布", "提交成功", "发布完成")
            for kw in success_keywords:
                try:
                    if await page.get_by_text(kw, exact=False).count() > 0:
                        logger.info("页面文本命中成功关键词: %r", kw)
                        logger.info("[发布] Video published successfully")
                        return True
                except Exception:
                    continue

            # 所有判定都不满足 → 视为跳转到了非成功页（如内容管理页）
            logger.warning(
                "URL 已离开发布页，但未检测到成功标志（关键词 %s）"
                "。当前 URL: %s",
                success_keywords, page.url,
            )
            return False
        except Exception as e:
            logger.warning("[发布] Publish click failed: %s", e)
            raise

    # ------------------------------------------------------------------
    # list_works — 作品列表抓取(作品链接 + 播放量等统计, 回链/统计同步用)
    # ------------------------------------------------------------------

    async def list_works(
        self, cookie_file: str, limit: int = 20, work_type: str = ""
    ) -> list[dict]:
        """抓取创作者中心的作品列表(按发布时间倒序)。

        实现(2026-11 踩点, 参考 scratch/scout_iqiyi*.py): 创作者中心首页
        「最近作品」模块自身会请求 ``mp-api.iqiyi.com/.../selfmedia/videos``,
        该接口纯 cookie 鉴权、无签名参数, 且管理页 DOM 不含作品链接。
        因此仿头条/腾讯视频做法: 加载登录态后 goto 创作者中心首页,
        页内 fetch 直调同一接口(credentials 带登录态), Python 侧映射契约字段。

        work_type 仅支持 'video'(爱奇艺号创作者中心只有视频); 其它类型 → []。
        cookie 失效(跳登录页)或任何异常 → log + return [], 不抛出。
        """
        if work_type != "video":
            return []

        def _to_int(v):
            # 接口数值字段可能缺失/为字符串, 空/非法 → None(契约: 没有则 null)
            try:
                if v is None or str(v).strip() == "":
                    return None
                return int(str(v).strip())
            except (ValueError, TypeError):
                return None

        cookie_path = str(Path(BASE_DIR / "cookiesFile" / cookie_file))
        browser = await self.create_browser(headless=True)
        try:
            context = await self.create_context(browser, storage_state=cookie_path)
            try:
                page = await context.new_page()
                await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await asyncio.sleep(2)

                # cookie 失效: 被跳转到登录页 → return []
                current_url = page.url or ""
                if "passport" in current_url or "login" in current_url:
                    logger.info(
                        "[爱奇艺作品列表] cookie 失效(跳转登录页 %s)", current_url
                    )
                    return []

                # 页内 fetch 直调作品列表接口(与首页「最近作品」同一接口,
                # publishStatusSet=0,1,2 覆盖全部状态), 按 pageNo 翻页取满 limit
                raw = await page.evaluate(
                    """async ([api, maxItems]) => {
                        const items = [];
                        let totalCount = null;
                        // 单页上限保守取 50(首页实测 pageSize=5 正常), 最多翻 5 页
                        const pageSize = Math.min(Math.max(maxItems, 1), 50);
                        for (let pageNo = 1; pageNo <= 5 && items.length < maxItems; pageNo++) {
                            const url = `${api}?pageNo=${pageNo}&pageSize=${pageSize}`
                                + '&order=desc&orderBy=createTime&publishStatusSet=0,1,2';
                            const resp = await fetch(url, {credentials: 'include'});
                            if (!resp.ok) return {error: `http ${resp.status}`};
                            const j = await resp.json();
                            if (j.code !== 0) return {error: `code ${j.code} ${j.msg || ''}`};
                            const d = j.data || {};
                            totalCount = d.totalCount;
                            items.push(...(d.result || []));
                            if (pageNo >= (d.totalPages || 1)) break;
                        }
                        return {items: items.slice(0, maxItems), totalCount};
                    }""",
                    [_WORKS_LIST_API, max(int(limit or 20), 1)],
                )
                if not isinstance(raw, dict) or raw.get("error"):
                    logger.info(
                        "[爱奇艺作品列表] 接口返回异常(未登录/风控?): %s", raw
                    )
                    return []

                works = []
                for it in raw.get("items") or []:
                    qipu_id = it.get("qipuId")
                    works.append({
                        # qipuId 即平台作品唯一 ID, 公开页 URL 由 pageUrl 直接给出
                        "work_id": str(qipu_id) if qipu_id is not None else "",
                        "work_type": "video",
                        "title": str(it.get("displayName") or "").strip(),
                        "publish_time": str(it.get("publishTime") or "").strip(),
                        # 公开作品页, 接口原样返回(如 http://www.iqiyi.com/v_xxx.html)
                        "url": str(it.get("pageUrl") or "").strip(),
                        "views": _to_int(it.get("vvNum")),        # 播放量
                        "likes": _to_int(it.get("upCount")),       # 点赞(赞)
                        "favorites": _to_int(it.get("favCount")),  # 收藏
                        "comments": _to_int(it.get("commentCount")),  # 评论
                        "share": _to_int(it.get("shareCount")),    # 分享
                        # 契约外附带字段: 审核状态(已发布/审核中等), 排查用
                        "status": str(it.get("videoStatus") or "").strip(),
                    })

                logger.info(
                    "[爱奇艺作品列表] 抓到 %d 条作品(接口共 %s 条)",
                    len(works), raw.get("totalCount", "?"),
                )
                return works
            except Exception as e:
                logger.info("[爱奇艺作品列表] 抓取失败: %s", e)
                return []
            finally:
                try:
                    await context.close()
                except Exception:
                    pass
        except Exception as e:
            logger.info("[爱奇艺作品列表] list_works 失败: %s", e)
            return []
        finally:
            await browser.close()

    # ------------------------------------------------------------------
    # 作品评论（一键评论 / 评论同步）
    #
    # 踩点结论（2026-11，scratch/probe_iqiyi_comment*.py）：
    #   - 评论接口在 sns-comment.iqiyi.com/v3/comment/（无 CORS，页面内 JSONP
    #     callback 调用，业务侧 business_type=17）：
    #     读: get_comments.action / get_second_comments.action（楼中楼）
    #         get_bulk_count_data.action（计数）
    #     写: publish.action（发评/回复，reply_id=目标评论）、
    #         delete_comment.action（删评，reply_id=评论ID）
    #   - 写接口需要 authcookie（getLoginInfo 登录态）+ getSign 签名。
    #     getSign 是站点 webpack 模块 50855 导出的函数，www.iqiyi.com 首页即
    #     存在该模块，因此统一在首页页内 hook webpack require 取 getSign 现算。
    #   - 置顶：前台组件与创作者中心均未发现任何置顶/推荐评论的写接口
    #     （internal/get_top_comments 仅内部读），supports_pin_comment=False。
    # ------------------------------------------------------------------

    supports_comment = True
    supports_pin_comment = False
    supports_delete_comment = True

    _COMMENT_HOME_URL = "https://www.iqiyi.com/"
    _COMMENT_SNS_BASE = "https://sns-comment.iqiyi.com/v3/comment/"
    _COMMENT_SIGN_KEY = "rNCRezUM1eds69OC"

    # 页内公共 JS：hook webpack 取 getSign + getLoginInfo，并把
    # JSONP/签名工具挂到 window.__qfsync_boot（函数无法跨 evaluate 返回，
    # 只能常驻页内供后续 evaluate 调用）
    _COMMENT_BOOT_JS = """
        () => {
            const reqs = [];
            for (const k of Object.keys(window)
                    .filter(k => /^webpackChunk/.test(k))) {
                try {
                    window[k].push([[Math.random() + 'hook'], {},
                        (req) => reqs.push(req)]);
                } catch (e) {}
            }
            let getSign = null, authcookie = '', uid = '';
            for (const req of reqs) {
                try {
                    const m = req(50855);
                    if (m && typeof m.getSign === 'function') {
                        getSign = m.getSign;
                        break;
                    }
                } catch (e) {}
            }
            for (const req of reqs) {
                let done = false;
                for (const id of Object.keys(req.m)) {
                    let src = '';
                    try { src = req.m[id].toString(); } catch (e) { continue; }
                    if (/getLoginInfo\\s*[:=]/.test(src) && /authcookie/.test(src)) {
                        try {
                            const m = req(id);
                            if (m && typeof m.getLoginInfo === 'function') {
                                const li = m.getLoginInfo() || {};
                                authcookie = li.authcookie || '';
                                uid = String(li.uid || '');
                                done = true;
                            }
                        } catch (e) {}
                        break;
                    }
                }
                if (done) break;
            }
            if (!getSign) return {error: '未找到 getSign 模块（页面结构变化?）'};

            const jsonp = (url) => new Promise((resolve) => {
                const cb = 'qfsync_cb_' + Math.random().toString(36).slice(2);
                const s = document.createElement('script');
                const t = setTimeout(() => { cleanup(); resolve({__err: 'timeout'}); }, 15000);
                function cleanup() {
                    clearTimeout(t); s.remove();
                    try { delete window[cb]; } catch (e) {}
                }
                window[cb] = (data) => { cleanup(); resolve(data); };
                s.src = url + '&callback=' + cb;
                s.onerror = () => { cleanup(); resolve({__err: 'script error'}); };
                document.head.appendChild(s);
            });
            const signed = (params) => {
                const p = {...params};
                for (const k of Object.keys(p)) {
                    if (p[k] === '' || p[k] == null) delete p[k];
                }
                p.sign = getSign(p, __SIGN_KEY__, -1, true);
                return Object.entries(p)
                    .map(([k, v]) => k + '=' + encodeURIComponent(String(v)))
                    .join('&');
            };
            window.__qfsync_boot = {
                authcookie: authcookie,
                uid: uid,
                snsGet: async (path, params) =>
                    jsonp(__SNS_BASE__ + path + '?' + signed(params)),
            };
            return {authcookie, uid};
        }
    """

    async def _comment_page(self, cookie_file: str):
        """打开 www.iqiyi.com 首页并注入评论接口工具，返回
        (browser, context, page, boot)。boot = {authcookie, uid}。
        调用方负责关闭 browser。"""
        cookie_path = str(Path(BASE_DIR / "cookiesFile" / cookie_file))
        browser = await self.create_browser(headless=True)
        try:
            context = await self.create_context(browser, storage_state=cookie_path)
            try:
                page = await context.new_page()
                await page.goto(self._COMMENT_HOME_URL,
                                wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await asyncio.sleep(4)
                boot = await page.evaluate(
                    self._COMMENT_BOOT_JS.replace("__SIGN_KEY__",
                                                  json.dumps(self._COMMENT_SIGN_KEY))
                    .replace("__SNS_BASE__", json.dumps(self._COMMENT_SNS_BASE))
                )
                if not isinstance(boot, dict):
                    raise RuntimeError(f"页内工具初始化失败: {boot!r}")
                return browser, context, page, boot
            except Exception:
                try:
                    await context.close()
                except Exception:
                    pass
                raise
        except Exception:
            try:
                await browser.close()
            except Exception:
                pass
            raise

    @staticmethod
    def _fmt_comment_time(ts) -> str:
        try:
            from datetime import datetime, timezone, timedelta
            return datetime.fromtimestamp(
                int(ts), tz=timezone(timedelta(hours=8))
            ).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError, OSError):
            return ""

    async def list_comments(
        self, cookie_file: str, work_id: str, limit: int = 100
    ) -> list[dict]:
        """抓取视频评论区：页内 JSONP get_comments.action（无需登录态）。

        work_id 为爱奇艺 qipuId（即 list_works 返回的 tvid）。按时间倒序
        翻页（last_id 链式游标，page_size 上限 40），楼中楼走
        get_second_comments.action。置顶/热门评论（data.hot + isGood）
        合并进列表并标记 pinned。
        """
        work_id = str(work_id or "").strip()
        if not work_id:
            return []
        limit = max(int(limit or 100), 1)
        browser = None
        try:
            browser, context, page, boot = await self._comment_page(cookie_file)
            if boot.get("error"):
                logger.info("[iqiyi list_comments] %s", boot["error"])
                return []

            raw = await page.evaluate(
                """async ([tvId, maxItems, authcookie]) => {
                    const snsGet = window.__qfsync_boot.snsGet;
                    const common = {
                        business_type: '17', agenttype: '118',
                        agentversion: '9.11.5',
                        authcookie: authcookie || '',
                    };
                    const out = [];
                    let hotIds = [];
                    let lastId = '0';
                    for (let i = 0; i < 10 && out.length < maxItems; i++) {
                        const r = await snsGet('get_comments.action', {
                            content_id: tvId, types: 'time',
                            last_id: lastId,
                            page_size: String(Math.min(40, maxItems)),
                            ...common,
                        });
                        if (!r || r.__err || r.code !== 'A00000') {
                            return {error: (r && (r.msg || r.__err)) || '接口失败'};
                        }
                        const d = r.data || {};
                        hotIds = (d.hot || []).map(c => String(c.id));
                        const cs = d.comments || [];
                        for (const c of cs) out.push(c);
                        if (!cs.length) break;
                        lastId = String(cs[cs.length - 1].id);
                        const total = Number(d.totalCount || 0);
                        if (total && out.length >= total) break;
                    }
                    // 楼中楼（有回复的顶级评论）
                    for (const c of out) {
                        if (Number(c.replyCount || 0) > 0) {
                            const r = await snsGet('get_second_comments.action', {
                                content_id: tvId, reply_id: String(c.id),
                                last_id: '0', page_size: '20', ...common,
                            });
                            if (r && r.code === 'A00000') {
                                c.__replies = (r.data && r.data.comments)
                                    || (r.data && r.data.replies) || [];
                            }
                        }
                    }
                    return {comments: out, hotIds};
                }""",
                [work_id, limit, str(boot.get("authcookie") or "")],
            )
            if not isinstance(raw, dict) or raw.get("error"):
                logger.info(
                    "[iqiyi list_comments] %s 拉取失败: %s",
                    work_id, (raw or {}).get("error") if isinstance(raw, dict) else raw,
                )
                return []

            uid_s = str(boot.get("uid") or "")
            hot_ids = set(raw.get("hotIds") or [])
            comments: list[dict] = []

            def _map(c, parent_id):
                cid = str(c.get("id") or "")
                cu = c.get("userInfo") or {}
                pinned = bool(c.get("isGood") or c.get("isPublisherRecom")
                              or cid in hot_ids)
                return {
                    "comment_id": cid,
                    "parent_id": parent_id,
                    "author": str(cu.get("uname") or ""),
                    "avatar": str(cu.get("icon") or ""),
                    "content": str(c.get("content") or ""),
                    "like_count": int(c.get("likes") or 0),
                    "reply_count": int(c.get("replyCount") or 0),
                    "publish_time": self._fmt_comment_time(c.get("addTime")),
                    "location": str(c.get("location") or ""),
                    "is_author": bool(uid_s) and str(cu.get("uid") or "") == uid_s,
                    "pinned": pinned,
                }

            for c in raw.get("comments") or []:
                comments.append(_map(c, ""))
                for rc in (c.get("__replies") or []):
                    comments.append(_map(rc, str(c.get("id") or "")))

            logger.info("[iqiyi list_comments] %s 抓到 %d 条评论", work_id, len(comments))
            return comments
        except Exception as e:
            logger.info("[iqiyi list_comments] 抓取失败: %s", e)
            return []
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def _ensure_comment_auth(self, page, boot) -> dict | None:
        """写接口需要登录态 authcookie；getLoginInfo 为空时从 cookie 里
        兜底取 P00001。返回 {'authcookie', 'uid'} 或 None（无法写）。"""
        auth = str(boot.get("authcookie") or "")
        uid = str(boot.get("uid") or "")
        if not auth:
            try:
                for c in await page.context.cookies():
                    if c.get("name") == "P00001" and c.get("value"):
                        auth = c["value"]
                        break
                    if c.get("name") == "P00003" and c.get("value") and not uid:
                        uid = c["value"]
            except Exception:
                pass
        if not auth:
            return None
        return {"authcookie": auth, "uid": uid}

    async def post_comment(
        self, cookie_file: str, work_id: str, content: str, pinned: bool = False
    ) -> dict:
        """页内 JSONP publish.action 发评（business_type=17，getSign 签名）。

        爱奇艺前台/创作者中心均无置顶评论入口（踩点确认），pinned=True 时
        仍发布普通评论并在 message 中说明。
        """
        work_id = str(work_id or "").strip()
        if not work_id or not (content or "").strip():
            return {"ok": False, "message": "参数缺失（work_id / content）"}
        browser = None
        try:
            browser, context, page, boot = await self._comment_page(cookie_file)
            if boot.get("error"):
                return {"ok": False, "message": str(boot["error"])}
            auth = await self._ensure_comment_auth(page, boot)
            if not auth:
                msg = "无评论登录态（authcookie 缺失，前台 SSO 未生效）"
                logger.info("[iqiyi post_comment] %s 失败: %s", work_id, msg)
                return {"ok": False, "message": msg}

            result = await page.evaluate(
                """async ([tvId, text, authcookie]) => {
                    const snsGet = window.__qfsync_boot.snsGet;
                    const r = await snsGet('publish.action', {
                        reply_id: '', content_id: tvId, text: text,
                        authcookie: authcookie, device_id: '', appid: 'iqiyi_web',
                        business_type: '17', agenttype: '118',
                        agentversion: '9.11.5',
                    });
                    if (!r) return {ok: false, message: '接口无响应'};
                    if (r.__err) return {ok: false, message: '请求失败: ' + r.__err};
                    if (r.code !== 'A00000') {
                        return {ok: false, message: (r.msg || r.code)
                            + (r.data && typeof r.data === 'string'
                               ? ' ' + r.data : '')};
                    }
                    const d = r.data || {};
                    return {ok: true,
                            comment_id: String(d.reply_id || d.replyId
                                || d.id || d.comment_id || '')};
                }""",
                [work_id, content.strip(), auth["authcookie"]],
            )
            if not isinstance(result, dict) or not result.get("ok"):
                msg = (result or {}).get("message", "未知错误") \
                    if isinstance(result, dict) else str(result)
                logger.info("[iqiyi post_comment] %s 失败: %s", work_id, msg)
                return {"ok": False, "message": msg}
            note = "" if not pinned else "爱奇艺平台不支持置顶评论，已发布为普通评论"
            logger.info(
                "[iqiyi post_comment] %s 发布成功 id=%s pinned=%s",
                work_id, result.get("comment_id"), False,
            )
            return {
                "ok": True,
                "comment_id": result.get("comment_id") or "",
                "pinned": False,
                "message": note,
            }
        except Exception as e:
            logger.info("[iqiyi post_comment] 发布失败: %s", e)
            return {"ok": False, "message": str(e)}
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def delete_comment(
        self, cookie_file: str, work_id: str, comment_id: str
    ) -> dict:
        """页内 JSONP delete_comment.action 删评（reply_id=评论ID）。"""
        work_id = str(work_id or "").strip()
        cid = str(comment_id or "").strip()
        if not work_id or not cid:
            return {"ok": False, "message": "参数缺失（work_id / comment_id）"}
        browser = None
        try:
            browser, context, page, boot = await self._comment_page(cookie_file)
            if boot.get("error"):
                return {"ok": False, "message": str(boot["error"])}
            auth = await self._ensure_comment_auth(page, boot)
            if not auth:
                msg = "无评论登录态（authcookie 缺失，前台 SSO 未生效）"
                logger.info(
                    "[iqiyi delete_comment] %s %s 失败: %s", work_id, cid, msg
                )
                return {"ok": False, "message": msg}

            result = await page.evaluate(
                """async ([cid, authcookie]) => {
                    const snsGet = window.__qfsync_boot.snsGet;
                    const r = await snsGet('delete_comment.action', {
                        reply_id: cid, authcookie: authcookie,
                        appid: 'iqiyi_web', business_type: '17',
                    });
                    if (!r) return {ok: false, message: '接口无响应'};
                    if (r.__err) return {ok: false, message: '请求失败: ' + r.__err};
                    if (r.code !== 'A00000') return {ok: false, message: r.msg || r.code};
                    return {ok: true};
                }""",
                [cid, auth["authcookie"]],
            )
            if not isinstance(result, dict) or not result.get("ok"):
                msg = (result or {}).get("message", "未知错误") \
                    if isinstance(result, dict) else str(result)
                logger.info(
                    "[iqiyi delete_comment] %s %s 失败: %s", work_id, cid, msg
                )
                return {"ok": False, "message": msg}
            logger.info("[iqiyi delete_comment] %s %s 删除成功", work_id, cid)
            return {"ok": True, "message": ""}
        except Exception as e:
            logger.info("[iqiyi delete_comment] 删除失败: %s", e)
            return {"ok": False, "message": str(e)}
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def reply_comment(
        self, cookie_file: str, work_id: str,
        root_id: str, parent_id: str, content: str,
    ) -> dict:
        """页内 JSONP publish.action 回复评论（reply_id=目标评论）。

        爱奇艺评论区为两级结构（评论 + 楼中楼），不区分 root/parent，
        回复一律挂到目标评论（parent_id，为空则用 root_id）下。
        """
        work_id = str(work_id or "").strip()
        root_id = str(root_id or "").strip()
        parent_id = str(parent_id or "").strip()
        target_id = parent_id or root_id
        if not work_id or not target_id or not (content or "").strip():
            return {"ok": False,
                    "message": "参数缺失（work_id / root_id / parent_id / content）"}
        browser = None
        try:
            browser, context, page, boot = await self._comment_page(cookie_file)
            if boot.get("error"):
                return {"ok": False, "message": str(boot["error"])}
            auth = await self._ensure_comment_auth(page, boot)
            if not auth:
                msg = "无评论登录态（authcookie 缺失，前台 SSO 未生效）"
                logger.info("[iqiyi reply_comment] %s 失败: %s", work_id, msg)
                return {"ok": False, "message": msg}

            result = await page.evaluate(
                """async ([tvId, replyId, text, authcookie]) => {
                    const snsGet = window.__qfsync_boot.snsGet;
                    const r = await snsGet('publish.action', {
                        reply_id: replyId, content_id: tvId, text: text,
                        authcookie: authcookie, device_id: '', appid: 'iqiyi_web',
                        business_type: '17', agenttype: '118',
                        agentversion: '9.11.5',
                    });
                    if (!r) return {ok: false, message: '接口无响应'};
                    if (r.__err) return {ok: false, message: '请求失败: ' + r.__err};
                    if (r.code !== 'A00000') {
                        return {ok: false, message: (r.msg || r.code)
                            + (r.data && typeof r.data === 'string'
                               ? ' ' + r.data : '')};
                    }
                    const d = r.data || {};
                    return {ok: true,
                            comment_id: String(d.reply_id || d.replyId
                                || d.id || d.comment_id || '')};
                }""",
                [work_id, target_id, content.strip(), auth["authcookie"]],
            )
            if not isinstance(result, dict) or not result.get("ok"):
                msg = (result or {}).get("message", "未知错误") \
                    if isinstance(result, dict) else str(result)
                logger.info(
                    "[iqiyi reply_comment] %s 回复 %s 失败: %s",
                    work_id, target_id, msg,
                )
                return {"ok": False, "message": msg}
            logger.info(
                "[iqiyi reply_comment] %s 回复成功 id=%s target=%s",
                work_id, result.get("comment_id"), target_id,
            )
            return {
                "ok": True,
                "comment_id": result.get("comment_id") or "",
                "message": "",
            }
        except Exception as e:
            logger.info("[iqiyi reply_comment] 回复失败: %s", e)
            return {"ok": False, "message": str(e)}
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
