"""快手创作者平台相关 API 代理(浏览器自动化方案)。

仿 ``vivo_bp.py`` 的模式:用 CloakBrowser 打开快手视频发布页 → 上传测试
视频触发表单渲染 → 选定地区 → 在「添加地点」详细地址搜索框输入关键词 →
拦截 ``/rest/zt/location/wi/poi/search`` 响应转发给前端。

POI 搜索接口踩点(2026-11-07 发布页抓包):
    POST https://cp.kuaishou.com/rest/zt/location/wi/poi/search
    参数 kpn/subBiz/kpf/kuaishou.web.cp.api_ph/__NS_hxfalcon(JS 签名)/caver
    → __NS_hxfalcon 为页内 JS 生成的风控签名,纯 HTTP 无法复刻,故走浏览器代发。
    响应: {"result":1,"pcursor":"...","locations":[{id,title,address,city,
    category,latitude,longitude,idString}]}
    下拉项文本 = title + address 拼接。

    地区级联:首级只有 IP 定位城市一项(接口 /rest/cp/works/v2/common/pc/ip2poi),
    点开级联点唯一项即选定城市,随后详细地址下拉解锁。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from flask import Blueprint, request, jsonify

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from conf import BASE_DIR
from util._logger import get_channel_logger
from impl._browser import create_browser, close_browser, create_context
from services.test_video import get_test_video
from blueprints._platform_cache import get_cached, set_cached

logger = get_channel_logger("kuaishou")

kuaishou_bp = Blueprint('kuaishou', __name__, url_prefix='/api/kuaishou')

# 快手视频发布页(与 impl/kuaishou/platform.py 同源)
_KS_UPLOAD_URL = "https://cp.kuaishou.com/article/publish/video"
# POI 搜索接口(拦截关键字)
_KS_POI_SEARCH_URL_KEYWORD = "/rest/zt/location/wi/poi/search"


def _get_cookie_path(cookie_file: str) -> str:
    return str(Path(BASE_DIR / "cookiesFile" / cookie_file))


def _get_account_cookie_file(account_id: str) -> str | None:
    """从数据库按账号 id 取 cookie 文件名;account_id 为空则取任意快手账号。"""
    conn = sqlite3.connect(str(Path(BASE_DIR / "db" / "database.db")))
    cursor = conn.cursor()
    if account_id:
        cursor.execute("SELECT filePath FROM user_info WHERE id = ? AND type = 4", (account_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]
        conn = sqlite3.connect(str(Path(BASE_DIR / "db" / "database.db")))
    # fallback：任意一个可用的快手账号(type=4)
    cursor = conn.cursor()
    cursor.execute("SELECT filePath FROM user_info WHERE type = 4 LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return row[0]


def run_async(coro):
    """在新事件循环里跑协程(避免与 Flask 线程冲突)。"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import threading
            result = {}

            def _run():
                new_loop = asyncio.new_event_loop()
                try:
                    result["v"] = new_loop.run_until_complete(coro)
                finally:
                    new_loop.close()

            t = threading.Thread(target=_run)
            t.start()
            t.join()
            return result.get("v")
    except RuntimeError:
        pass
    return asyncio.run(coro)


# ======================================================================
# 添加地点搜索 API
# ======================================================================

@kuaishou_bp.route('/search-position', methods=['GET'])
def search_position():
    """搜索快手「添加地点」的 POI。

    Query params:
        account_id: 账号 id(用于取 cookie)
        keyword:    地点关键词(必填)

    流程(2026-11-07 发布页踩点):
        1. 用账号 cookie 打开快手视频发布页
        2. 上传测试视频触发表单渲染(添加地点区块要表单渲染后才出现)
        3. 点「添加地点」的地区级联 → 点唯一项(IP 定位城市)
        4. 点详细地址下拉 → 输入关键词 → 拦截 poi/search 响应
        5. 不点选、不提交,直接返回 locations 列表

    Returns:
        {"code": 200, "data": {"position_list": [{name, address, city, id}...]}}

    缓存:POI 数据低频变化,按 账号+关键词 TTL 缓存 5 分钟 —— 同关键词重复
    搜索不再走浏览器流程(单次 30-60s)。
    """
    account_id = request.args.get('account_id')
    keyword = request.args.get('keyword', '').strip()
    logger.info(f"[位置搜索] 收到请求: account_id={account_id}, keyword={keyword}")

    if not keyword:
        return jsonify({"code": 400, "msg": "缺少keyword参数"}), 400

    cache_key = f"kuaishou:position:{account_id}:{keyword}"
    cached = get_cached(cache_key)
    if cached is not None:
        logger.info(f"[位置搜索] 缓存命中: account_id={account_id}, keyword={keyword}")
        return jsonify(cached)

    try:
        cookie_file = _get_account_cookie_file(account_id)
        if not cookie_file:
            logger.warning(f"[位置搜索] 账号不存在: {account_id}")
            return jsonify({"code": 404, "msg": "没有可用的快手账号"}), 404

        result = run_async(_fetch_positions_via_browser(cookie_file, keyword))
        if result.get("success"):
            resp_body = {"code": 200, "data": result["data"]}
            set_cached(cache_key, resp_body)
            logger.info(
                f"[位置搜索] 成功,共 {len(result['data'].get('position_list', []))} 个结果"
            )
            return jsonify(resp_body)
        return jsonify({"code": 500, "msg": result.get("error", "请求失败")}), 500
    except Exception as e:
        logger.error(f"[位置搜索] 异常: {e}", exc_info=True)
        return jsonify({"code": 500, "msg": str(e)}), 500


async def _fetch_positions_via_browser(cookie_file: str, keyword: str) -> dict:
    """打开快手视频发布页 → 上传测试视频 → 选地区 → 搜索 POI → 拦截响应。"""
    test_video = get_test_video()
    if not test_video:
        return {"success": False, "error": "未找到可用的测试视频文件,无法触发表单渲染"}

    cookie_path = _get_cookie_path(cookie_file)
    browser = await create_browser(headless=True)
    try:
        context = await create_context(browser, storage_state=cookie_path)
        try:
            page = await context.new_page()

            captured = []

            async def handle_response(response):
                if _KS_POI_SEARCH_URL_KEYWORD not in response.url:
                    return
                try:
                    data = await response.json()
                    req = response.request
                    try:
                        post_kw = (req.post_data or "")
                    except Exception:
                        post_kw = ""
                    captured.append(data)
                    # 响应结构(2026-11-07 抓包): locations 在顶层,无 data 包裹
                    # {"result":1,"pcursor":"...","locations":[{id,title,address,city,...}]}
                    logger.info(
                        "[拦截] 命中POI搜索接口, post=%s, locations 长度: %d",
                        post_kw[:120],
                        len(data.get("locations") or []),
                    )
                except Exception as e:
                    # 响应可能不是 JSON(风控页等),记下原文前 200 字便于排查
                    try:
                        raw = await response.text()
                    except Exception:
                        raw = "<binary>"
                    logger.error(f"[拦截] 解析失败: {e}; raw={raw[:200]}")

            page.on("response", handle_response)

            # 1. 打开视频发布页
            logger.info("[位置搜索] 打开快手视频发布页...")
            await page.goto(_KS_UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)

            # 2. 关引导弹层 + 上传测试视频
            know_btn = page.locator('button span:text("我知道了")').first
            try:
                if await know_btn.count() and await know_btn.is_visible():
                    await know_btn.click()
            except Exception:
                pass

            upload_btn = page.locator("button[class^='_upload-btn']").first
            await upload_btn.wait_for(state="visible", timeout=15000)
            async with page.expect_file_chooser() as fc_info:
                await upload_btn.click()
            fc = await fc_info.value
            await fc.set_files(str(test_video))
            logger.info("[位置搜索] 测试视频已选择, 等待表单渲染...")

            # 3. 等表单渲染(「描述」标签出现即就绪;小视频上传一般 20-40s)
            # 注意:页面 label 实际文案是「作品描述」,必须用子串匹配
            # (与 impl/kuaishou/platform.py _upload_single 同款),exact=True 永远匹配不到
            desc_label = page.get_by_text("描述").first
            for i in range(240):
                if await desc_label.count() > 0:
                    break
                await asyncio.sleep(1)
            else:
                return {"success": False, "error": "发布表单渲染超时(240s)"}
            await asyncio.sleep(3)
            logger.info("[位置搜索] 发布表单已渲染")

            # 4. 「添加地点」区块
            loc_item = page.locator(
                '[class*="_edit-form-item"]',
                has=page.get_by_text("添加地点", exact=True),
            ).first
            if not await loc_item.count():
                return {"success": False, "error": "未找到「添加地点」区块"}
            await loc_item.scroll_into_view_if_needed()

            # 5. 地区级联:点开 → 点唯一项(IP 定位城市)
            cascader = loc_item.locator(".ant-select.ant-cascader").first
            await cascader.click()
            await asyncio.sleep(1.5)
            city_option = page.locator(".ant-cascader-menu-item").first
            if not await city_option.count():
                return {"success": False, "error": "地区级联未出现选项"}
            await city_option.click()
            await asyncio.sleep(1.5)
            logger.info("[位置搜索] 已选地区(级联第一项)")

            # 6. 详细地址下拉 → 输入关键词 → 等拦截结果
            detail = loc_item.locator(".ant-select").nth(1)
            await detail.click()
            await asyncio.sleep(1.5)
            captured.clear()  # 丢弃选完地区自动按城市名预搜的请求
            search_input = detail.locator("input.ant-select-selection-search-input").first
            await search_input.type(keyword, delay=150)
            logger.info(f"[位置搜索] 已输入关键词: {keyword}")

            # 等「有结果」的搜索响应:输入防抖的中间态响应可能 locations 为空,
            # 直接取最后一次会把空结果当最终结果;最多等 20s,超时但有过响应
            # 则按(可能真无结果)最后一次返回
            for _ in range(40):
                if any((c.get("locations") or []) for c in captured):
                    break
                await asyncio.sleep(0.5)
            if not captured:
                return {"success": False, "error": "未拦截到POI搜索结果"}

            # 优先取最近一个有结果的响应;全空则按无结果返回(code 200, 空列表)
            locations = []
            for c in reversed(captured):
                locs = c.get("locations") or []
                if locs:
                    locations = locs
                    break
            position_list = [
                {
                    "name": (loc.get("title") or "").strip(),
                    "address": (loc.get("address") or "").strip(),
                    "city": (loc.get("city") or "").strip(),
                    "id": loc.get("idString") or str(loc.get("id") or ""),
                }
                for loc in locations
                if loc.get("title")
            ]
            return {"success": True, "data": {"position_list": position_list}}
        finally:
            try:
                await context.close()
            except Exception:
                pass
    finally:
        await close_browser(browser, is_close_by_code=True)
