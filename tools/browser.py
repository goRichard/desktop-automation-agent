"""
浏览器自动化工具：基于 Playwright，提供网页导航、元素交互、状态查询等能力。
首次调用任何 browser_* 工具时自动启动 Microsoft Edge（非 headless，用户可见）。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from .registry import tool

# ══════════════════════════════════════════════════════
# 浏览器生命周期管理
# ══════════════════════════════════════════════════════

_SCREENSHOT_DIR = Path(os.environ.get("TEMP", Path.home() / "tmp")) / "desktop-agent-screenshots"


class _BrowserState:
    """模块级浏览器状态单例"""

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    @property
    def is_ready(self) -> bool:
        return self.page is not None and not self.page.is_closed()


_state = _BrowserState()


async def _ensure_browser():
    """
    确保浏览器已启动并有一个可用的 page。
    首次调用时自动 launch；后续调用时具备自愈能力：
    - page 失效但 context 仍活 → 复用或新建 page
    - context 失效 → 重建 context + page
    - browser 断连 → 全部重建
    """
    if _state.is_ready:
        return _state.page

    # ── 首次启动 ──
    if _state.browser is None:
        from config import get_settings
        from playwright.async_api import async_playwright

        browser_config = get_settings().browser
        channel = browser_config.get("channel", "msedge")
        executable_path = browser_config.get("executable_path")
        launch_options = {
            "headless": False,
            "args": ["--no-first-run", "--no-default-browser-check", "--start-maximized"],
            "timeout": 30000,
        }
        if executable_path:
            launch_options["executable_path"] = executable_path
        else:
            launch_options["channel"] = channel

        _state.playwright = await async_playwright().start()
        _state.browser = await _state.playwright.chromium.launch(**launch_options)

    # ── 确保 context 存活 ──
    if _state.context is None or not _state.browser.is_connected():
        try:
            if _state.context is not None:
                await _state.context.close()
        except Exception:
            pass
        _state.context = await _state.browser.new_context(no_viewport=True)

    # ── 确保 page 存活：优先复用 context 里现有的未关闭 page ──
    if _state.page is None or _state.page.is_closed():
        reused = None
        try:
            for p in reversed(_state.context.pages):
                if not p.is_closed():
                    reused = p
                    break
        except Exception:
            pass
        _state.page = reused if reused is not None else await _state.context.new_page()

    return _state.page


_NOT_READY_MSG = "❌ 浏览器未启动。请先调用 browser_navigate(url) 打开网页。"


def _get_ready_page():
    """
    返回已就绪的 page，未启动则返回 None（不自动启动浏览器）。
    具备自愈能力：page 失效但 browser/context 仍活时，自动复用 context 里其他 page。
    """
    # Case 1: 完全就绪
    if _state.page is not None and not _state.page.is_closed():
        return _state.page

    # Case 2: page 死了，但 context 还在 → 复用 context 里现有的 page
    if _state.context is not None and _state.browser is not None and _state.browser.is_connected():
        try:
            pages = _state.context.pages  # 同步属性，无需 await
            # 找到第一个未关闭的 page
            for p in reversed(pages):  # 优先取最新的
                if not p.is_closed():
                    _state.page = p
                    return p
            # 所有 page 都关了，标记 context 需重建（下次 browser_navigate 会处理）
        except Exception:
            pass

    # Case 3: browser/context 已断连
    return None


async def _close_browser():
    """关闭浏览器并清理资源"""
    if _state.page and not _state.page.is_closed():
        await _state.page.close()
    if _state.context:
        await _state.context.close()
    if _state.browser:
        await _state.browser.close()
    if _state.playwright:
        await _state.playwright.stop()
    _state.page = None
    _state.context = None
    _state.browser = None
    _state.playwright = None


# ══════════════════════════════════════════════════════
# DOM 元素扫描（类 UIA 的结构化数据提取）
# ══════════════════════════════════════════════════════

_SCAN_ELEMENTS_JS = """
() => {
    const selectors = [
        'a', 'button', 'input', 'select', 'textarea',
        '[role="button"]', '[role="link"]', '[role="textbox"]',
        '[role="menuitem"]', '[role="tab"]', '[role="checkbox"]',
        '[role="combobox"]', '[role="option"]', '[onclick]',
    ];
    const seen = new Set();
    const results = [];

    for (const sel of selectors) {
        for (const el of document.querySelectorAll(sel)) {
            if (seen.has(el)) continue;
            seen.add(el);

            const rect = el.getBoundingClientRect();
            // 过滤不可见元素
            if (rect.width === 0 || rect.height === 0) continue;
            if (rect.bottom < 0 || rect.top > window.innerHeight) continue;

            const tag = el.tagName.toLowerCase();
            const text = (
                el.innerText || el.textContent || ''
            ).trim().slice(0, 80);
            const ariaLabel = el.getAttribute('aria-label') || '';
            const placeholder = el.getAttribute('placeholder') || '';
            const title = el.getAttribute('title') || '';
            const type = el.getAttribute('type') || '';
            const role = el.getAttribute('role') || '';
            const id = el.id || '';
            const name = el.getAttribute('name') || '';
            const href = tag === 'a' ? (el.getAttribute('href') || '') : '';

            // 确定展示名（优先级：text > aria-label > placeholder > title > id）
            const displayName = text || ariaLabel || placeholder || title || id || name || '';

            results.push({
                tag,
                role: role || tag,
                text: displayName,
                type,
                placeholder,
                ariaLabel,
                href: href.slice(0, 200),
                id,
                bounds: {
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                },
            });

            if (results.length >= 100) break;
        }
        if (results.length >= 100) break;
    }
    return results;
}
"""


async def _get_page_elements(page) -> list[dict]:
    """扫描当前页面可交互 DOM 元素"""
    try:
        return await page.evaluate(_SCAN_ELEMENTS_JS)
    except Exception:
        return []


def _format_elements(elements: list[dict]) -> str:
    """将元素列表格式化为可读文本"""
    if not elements:
        return "  （页面无可交互元素）"

    lines = []
    for i, el in enumerate(elements, 1):
        role = el.get("role", el.get("tag", "?"))
        text = el.get("text", "")
        extra = ""
        if el.get("placeholder"):
            extra = f' placeholder="{el["placeholder"]}"'
        elif el.get("href"):
            href = el["href"][:60]
            extra = f" → {href}"
        lines.append(f"  [{i:>2}] {role:<12} \"{text}\"{extra}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════
# 浏览器工具
# ══════════════════════════════════════════════════════

@tool(description="打开指定 URL。首次调用时自动启动 Microsoft Edge（非 headless，用户可见）。url 为要访问的完整网址。仅当任务需要后续网页交互/自动化时使用。")
async def browser_navigate(url: str) -> str:
    """导航到指定 URL"""
    try:
        page = await _ensure_browser()
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        status = response.status if response else "unknown"
        await page.wait_for_load_state("networkidle", timeout=10000)
        return f"✅ 已导航到: {await page.title()}\n   URL: {page.url}\n   状态码: {status}"
    except Exception as e:
        return f"browser_navigate 失败: {type(e).__name__}: {e}"


@tool(description="获取当前浏览器页面状态：标题、URL、可交互元素列表。仅当浏览器已启动时可用。")
async def browser_get_state() -> str:
    """返回页面结构化信息"""
    try:
        page = _get_ready_page()
        if page is None:
            return _NOT_READY_MSG
        title = await page.title()
        url = page.url
        elements = await _get_page_elements(page)

        output = f"页面标题: {title}\nURL: {url}\n\n可交互元素 ({len(elements)} 个):\n"
        output += _format_elements(elements)
        return output
    except Exception as e:
        return f"browser_get_state 失败: {type(e).__name__}: {e}"


async def _wait_for_page_ready(page, timeout: int = 8000) -> None:
    """等待页面加载完成：networkidle → domcontentloaded 兜底"""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout // 2)
        except Exception:
            pass


async def _try_playwright_locators(
    page,
    locators: list,
    action: str = "click",
    text: Optional[str] = None,
    timeout: int = 15000,
) -> Optional[dict]:
    """
    遍历定位器列表尝试操作。
    返回 {"locator": ..., "force": bool, "count": int} 或 None。
    action: 'click' | 'clear' | 'fill'
    """
    diagnostics = []
    for locator in locators:
        try:
            count = await locator.count()
        except Exception:
            count = 0
        if count == 0:
            continue

        # 先尝试普通操作，失败再 force
        for force in (False, True):
            try:
                target = locator.first if count > 1 else locator
                if action == "click":
                    await target.click(timeout=timeout, force=force)
                elif action == "clear":
                    await target.clear(timeout=timeout, force=force)
                elif action == "fill":
                    await target.fill(text or "", timeout=timeout, force=force)
                return {"locator": locator, "force": force, "count": count}
            except Exception as e:
                diagnostics.append(f"  - force={force}: {type(e).__name__}: {str(e)[:80]}")
                continue
    return None


@tool(description="点击浏览器页面中的元素。description 为元素的文字描述（如'搜索按钮'、'登录'、'下一页'）。优先使用 Playwright 定位器精确匹配，失败时回退到 DOM 扫描 + 语义匹配，最后用视觉模型兜底。操作前会自动等待页面加载完成。")
async def browser_click(description: str) -> str:
    """三层定位策略点击，加健壮的重试与诊断"""
    try:
        page = _get_ready_page()
        if page is None:
            return _NOT_READY_MSG

        # 等待页面加载完成
        await _wait_for_page_ready(page)

        # ── Layer 1: Playwright 内置定位器（多种角色 + 文本）──
        locators = [
            page.get_by_role("button", name=description),
            page.get_by_role("link", name=description),
            page.get_by_role("menuitem", name=description),
            page.get_by_role("tab", name=description),
            page.get_by_role("checkbox", name=description),
            page.get_by_role("radio", name=description),
            page.get_by_text(description, exact=True),
            page.get_by_text(description, exact=False),
            page.get_by_label(description),
        ]
        result = await _try_playwright_locators(page, locators, action="click")
        if result:
            await _wait_for_page_ready(page, timeout=3000)
            force_note = "（force=True）" if result["force"] else ""
            count_note = f"（共 {result['count']} 个匹配，已取第一个）" if result["count"] > 1 else ""
            return (
                f"✅ [Playwright] 已点击 '{description}'{force_note}{count_note}\n"
                f"   当前页面: {await page.title()}"
            )

        # ── Layer 2: DOM 扫描 + LLM 语义选择 ──
        elements = await _get_page_elements(page)
        if elements:
            matched = await _llm_select_browser_element(elements, description)
            if matched:
                bounds = matched["bounds"]
                cx = bounds["x"] + bounds["width"] // 2
                cy = bounds["y"] + bounds["height"] // 2
                await page.mouse.click(cx, cy)
                await asyncio.sleep(0.5)
                await _wait_for_page_ready(page, timeout=3000)
                return (
                    f"✅ [DOM+LLM] 已点击 '{description}'\n"
                    f"   匹配元素: {matched.get('role', '?')} \"{matched.get('text', '')}\"\n"
                    f"   坐标: ({cx}, {cy})"
                )

        # ── Layer 3: VLM 视觉兜底 ──
        return await _vlm_click_fallback(page, description)

    except Exception as e:
        return (
            f"❌ browser_click 失败: {type(e).__name__}: {e}\n"
            f"建议：\n"
            f"1. 先用 browser_get_state 查看页面元素，确认目标是否存在\n"
            f"2. 检查描述是否精确（例如用 '登录按钮' 而非 '按钮'）\n"
            f"3. 用 browser_scroll 滚动后重试（元素可能在视口外）"
        )


@tool(description="在已打开的浏览器页面的输入框中输入文字。description 为输入框描述（如'搜索框'、'用户名'、'密码'），text 为要输入的文本。clear 为是否先清空（默认 True）。仅当浏览器已启动时可用。")
async def browser_type(description: str, text: str, clear: Optional[str] = "true") -> str:
    """定位输入框并输入文字"""
    should_clear = clear.lower() in ("true", "1", "yes") if clear else True

    try:
        page = _get_ready_page()
        if page is None:
            return _NOT_READY_MSG

        # 等待页面加载完成
        await _wait_for_page_ready(page)

        # ── Layer 1: Playwright 定位器（多种策略）──
        locators = [
            page.get_by_label(description),
            page.get_by_placeholder(description),
            page.get_by_role("textbox", name=description),
            page.get_by_role("searchbox", name=description),
            page.get_by_role("combobox", name=description),
            page.locator(f'input[aria-label*="{description}"]'),
            page.locator(f'[placeholder*="{description}"]'),
        ]

        # 先尝试 clear（如果需要）
        if should_clear:
            clear_result = await _try_playwright_locators(
                page, locators, action="clear", timeout=10000,
            )
            if clear_result is None:
                # 没有可 clear 的定位器，直接尝试 fill（会自动覆盖）
                pass

        fill_result = await _try_playwright_locators(
            page, locators, action="fill", text=text, timeout=15000,
        )
        if fill_result:
            force_note = "（force=True）" if fill_result["force"] else ""
            count_note = f"（共 {fill_result['count']} 个匹配，已取第一个）" if fill_result["count"] > 1 else ""
            return (
                f"✅ [Playwright] 已输入 '{text}' 到 '{description}'{force_note}{count_note}\n"
                f"   当前页面: {await page.title()}"
            )

        # ── Layer 2: DOM 扫描 + 定位 input ──
        elements = await _get_page_elements(page)
        inputs = [e for e in elements if e.get("tag") in ("input", "textarea")]
        if inputs:
            matched = await _llm_select_browser_element(inputs, description)
            if matched:
                bounds = matched["bounds"]
                cx = bounds["x"] + bounds["width"] // 2
                cy = bounds["y"] + bounds["height"] // 2
                await page.mouse.click(cx, cy)
                await asyncio.sleep(0.2)
                if should_clear:
                    await page.keyboard.press("Control+a")
                    await page.keyboard.press("Backspace")
                await page.keyboard.type(text, delay=30)
                return (
                    f"✅ [DOM+LLM] 已输入 '{text}' 到 '{description}'\n"
                    f"   匹配元素: {matched.get('tag', '?')} placeholder=\"{matched.get('placeholder', '')}\"\n"
                    f"   坐标: ({cx}, {cy})"
                )

        return (
            f"❌ 未找到输入框 '{description}'\n"
            f"建议：\n"
            f"1. 先用 browser_get_state 查看页面可交互元素，确认输入框是否存在\n"
            f"2. 如果输入框在视口外，用 browser_scroll 滚动后重试\n"
            f"3. 尝试更精确的描述（如 '搜索输入框' 而非 '搜索'）"
        )

    except Exception as e:
        return (
            f"❌ browser_type 失败: {type(e).__name__}: {e}\n"
            f"建议：先用 browser_get_state 确认输入框是否存在，或尝试其他描述。"
        )


@tool(description="截取当前浏览器页面截图，保存到临时目录。返回截图文件路径。")
async def browser_screenshot() -> str:
    """截取页面截图"""
    try:
        page = _get_ready_page()
        if page is None:
            return _NOT_READY_MSG
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = str(_SCREENSHOT_DIR / "browser_screenshot.png")
        await page.screenshot(path=path, full_page=False)
        return f"✅ 截图已保存: {path}\n   页面: {await page.title()}"
    except Exception as e:
        return f"browser_screenshot 失败: {type(e).__name__}: {e}"


@tool(description="滚动当前已打开的浏览器页面。direction 可选值：up（向上）、down（向下）、top（回到顶部）、bottom（到底部）。仅当浏览器已经打开（通过 browser_navigate 等）时可用，否则请用 scroll。")
async def browser_scroll(direction: str = "down") -> str:
    """滚动页面"""
    try:
        page = _get_ready_page()
        if page is None:
            return "❌ 浏览器未启动，无法使用 browser_scroll。请先使用 browser_navigate(url) 打开网页，或使用 scroll 工具滚动桌面窗口。"

        scroll_map = {
            "down": "window.scrollBy(0, 600)",
            "up": "window.scrollBy(0, -600)",
            "top": "window.scrollTo(0, 0)",
            "bottom": "window.scrollTo(0, document.body.scrollHeight)",
        }

        js = scroll_map.get(direction.lower(), scroll_map["down"])
        await page.evaluate(js)
        await asyncio.sleep(0.3)

        scroll_y = await page.evaluate("window.scrollY")
        scroll_h = await page.evaluate("document.body.scrollHeight")
        return f"✅ 已滚动 {direction}，当前位置: {scroll_y}/{scroll_h}px"

    except Exception as e:
        return f"browser_scroll 失败: {type(e).__name__}: {e}"


@tool(description="浏览器返回上一页。")
async def browser_go_back() -> str:
    """返回上一页"""
    try:
        page = _get_ready_page()
        if page is None:
            return _NOT_READY_MSG
        await page.go_back(wait_until="domcontentloaded", timeout=10000)
        return f"✅ 已返回上一页\n   当前: {await page.title()} ({page.url})"
    except Exception as e:
        return f"browser_go_back 失败: {type(e).__name__}: {e}"


@tool(description="在浏览器中按下键盘按键。key 为按键名称，如 'Enter'、'Tab'、'Escape'、'ArrowDown'、'Control+a' 等。")
async def browser_press_key(key: str) -> str:
    """按下键盘按键"""
    try:
        page = _get_ready_page()
        if page is None:
            return _NOT_READY_MSG
        await page.keyboard.press(key)
        await asyncio.sleep(0.3)
        return f"✅ 已按下: {key}"
    except Exception as e:
        return f"browser_press_key 失败: {type(e).__name__}: {e}"


@tool(description="关闭浏览器，释放资源。仅当浏览器已启动时可用。")
async def browser_close() -> str:
    """关闭浏览器"""
    try:
        if not _state.is_ready:
            return "ℹ️ 浏览器未启动，无需关闭。"
        await _close_browser()
        return "✅ 浏览器已关闭"
    except Exception as e:
        return f"browser_close 失败: {type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════
# 辅助函数：LLM 语义匹配 + VLM 兜底
# ══════════════════════════════════════════════════════

async def _llm_select_browser_element(elements: list[dict], query: str) -> Optional[dict]:
    """
    用对话模型从 DOM 元素列表中选出最匹配 query 的元素。
    返回匹配的元素 dict 或 None。
    """
    from llm import get_llm_client

    if not elements:
        return None

    # 构造元素列表文本
    lines = []
    for i, el in enumerate(elements):
        role = el.get("role", el.get("tag", "?"))
        text = el.get("text", "")
        extra_parts = []
        if el.get("placeholder"):
            extra_parts.append(f'placeholder="{el["placeholder"]}"')
        if el.get("ariaLabel"):
            extra_parts.append(f'aria-label="{el["ariaLabel"]}"')
        if el.get("href"):
            extra_parts.append(f'href="{el["href"][:60]}"')
        extra = "  " + "  ".join(extra_parts) if extra_parts else ""
        lines.append(f'- [{i+1}] role={role}  text="{text}"{extra}')

    elements_text = "\n".join(lines)

    prompt = (
        f"以下是浏览器页面中可交互的 DOM 元素：\n\n"
        f"{elements_text}\n\n"
        f"用户想操作：{query}\n\n"
        f"请返回最匹配元素的编号（数字）。\n"
        f"规则：\n"
        f"1. 只输出编号数字，如 3\n"
        f"2. 如果没有匹配的元素，输出：0"
    )

    client = get_llm_client()
    messages = [
        {"role": "system", "content": "你是浏览器元素匹配助手，根据用户意图从元素列表中选出最匹配的元素编号。"},
        {"role": "user", "content": prompt},
    ]

    try:
        response = await client.chat(messages)
        result = (response.content or "").strip()
        # 提取数字
        import re
        match = re.search(r'\d+', result)
        if match:
            idx = int(match.group()) - 1  # 1-based → 0-based
            if 0 <= idx < len(elements):
                return elements[idx]
        return None
    except Exception:
        return None


async def _vlm_click_fallback(page, description: str) -> str:
    """VLM 视觉兜底：截图 → 视觉模型识别坐标 → 点击（含边界检查 + 重试）"""
    import re
    from llm import get_llm_client

    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    # 获取视口尺寸（用于边界检查）
    try:
        viewport = page.viewport_size or {"width": 1920, "height": 1080}
    except Exception:
        viewport = {"width": 1920, "height": 1080}

    for attempt in range(2):  # 最多重试 2 次
        tmp_path = str(_SCREENSHOT_DIR / f"browser_vlm_click_{attempt}.png")
        await page.screenshot(path=tmp_path, full_page=False)

        client = get_llm_client()
        extra_hint = ""
        if attempt == 1:
            extra_hint = (
                f"\n上次尝试的坐标无效或点击失败。"
                f"视口尺寸: {viewport['width']}x{viewport['height']}px。"
                f"请确保坐标在视口内并精确对准目标中心。"
            )

        prompt = (
            f'请分析截图，找到"{description}"的位置。\n'
            f"要求：\n"
            f"1. 返回该元素的中心坐标 [cx, cy]\n"
            f"2. cx 和 cy 是基于你所看到的图片的像素坐标\n"
            f"3. 只返回 JSON 数组格式，不要其他文字\n"
            f"4. 坐标值必须是整数\n"
            f"5. 必须保证 0 <= cx <= {viewport['width']}，0 <= cy <= {viewport['height']}"
            f"{extra_hint}\n\n"
            f"示例输出: [450, 320]"
        )

        response, scale_x, scale_y = await client.vision_for_coords(
            image_path=tmp_path, prompt=prompt
        )

        match = re.search(r'\[\s*(\d+)\s*,\s*(\d+)\s*\]', response)
        if not match:
            continue  # 重试

        raw_cx, raw_cy = int(match.group(1)), int(match.group(2))
        cx = int(raw_cx * scale_x)
        cy = int(raw_cy * scale_y)

        # 边界 clamp
        cx = max(0, min(cx, viewport["width"] - 1))
        cy = max(0, min(cy, viewport["height"] - 1))

        await page.mouse.click(cx, cy)
        await asyncio.sleep(0.5)

        return (
            f"✅ [VLM] 已点击 '{description}' 于坐标 ({cx}, {cy})\n"
            f"   （Playwright 定位器和 DOM 扫描均失败，使用视觉模型兜底）"
        )

    # 两次重试都失败
    return (
        f"❌ 无法定位 '{description}'。\n"
        f"Playwright 定位器、DOM 扫描、视觉模型均无法识别。\n"
        f"模型返回: {response[:200]}\n"
        f"建议：\n"
        f"1. 先用 browser_get_state 查看页面元素\n"
        f"2. 用 browser_scroll 滚动后重试\n"
        f"3. 尝试更精确的描述"
    )
