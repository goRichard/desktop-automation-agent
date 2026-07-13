"""
多模态视觉分析工具：截图/图片 → vLLM 多模态模型 → 文字描述/Markdown/OCR
通过 config.yaml 配置视觉模型，统一处理所有图像解析需求
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from llm import ProviderCapabilityError, get_llm_client
from .registry import tool
from .uia import normalize_element_records
from .winpeekaboo import (
    capture_image,
    click,
    list_elements,
    snapshot_windows,
    wait_for_new_window,
    window_activate,
)
import asyncio

# 固定的截图临时目录，避免每次随机创建新目录
_SCREENSHOT_DIR = Path(os.environ.get("TEMP", Path.home() / "tmp")) / "desktop-agent-screenshots"


def _screenshot_path(name: str = "screen", suffix: str = ".png") -> str:
    """返回固定的截图文件路径，不每次创建随机临时目录"""
    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return str(_SCREENSHOT_DIR / f"{name}{suffix}")


async def _visual_verify(
    prompt: str,
    window: Optional[str] = None,
    region: Optional[str] = None,
    screenshot_name: str = "visual",
) -> str:
    """
    截图并用多模态模型分析的公共底层逻辑。
    供 analyze_screen 和 verify_action_result 内部调用。
    """
    client = get_llm_client()
    client.ensure_vision_available()
    tmp_path = _screenshot_path(screenshot_name)
    await capture_image(output=tmp_path, window=window, region=region)
    return await client.vision(image_path=tmp_path, prompt=prompt)


async def _capture_for_coordinates(
    window: Optional[str],
    screenshot_name: str,
) -> str:
    """Capture the full desktop so model coordinates remain screen-relative."""
    if window:
        await window_activate(window)
        await asyncio.sleep(0.25)
    tmp_path = _screenshot_path(screenshot_name)
    await capture_image(output=tmp_path)
    return tmp_path


# ══════════════════════════════════════════════════════
# 新窗口检测：点击后自动识别并激活新弹出的窗口
# ══════════════════════════════════════════════════════

async def _snapshot_windows() -> dict:
    """获取当前可见窗口快照。"""
    try:
        return await asyncio.to_thread(snapshot_windows) or {}
    except Exception:
        return {}


async def _detect_and_activate_new_window(
    before: dict,
    delay: float = 0.8,
    source_window: Optional[str] = None,
    timeout_seconds: float = 1.2,
) -> Optional[str]:
    """
    等待 delay 秒后，比较窗口列表，找到新出现的窗口并激活。
    返回新窗口的标题，如果没有新窗口则返回 None。
    """
    await asyncio.sleep(min(delay, 0.25))
    try:
        title = await asyncio.to_thread(
            wait_for_new_window,
            before,
            source_window,
            timeout_seconds,
        )
        if not title:
            return None
        await window_activate(title)
        return title
    except Exception:
        return None


# ══════════════════════════════════════════════════════
# 通用视觉分析
# ══════════════════════════════════════════════════════

@tool(
    description="截取当前屏幕或指定窗口，通过多模态视觉模型分析图像内容，返回文字描述或分析结果。仅用于理解界面内容（UI 布局、操作状态、页面信息），不用于定位元素坐标。prompt 为分析提示词（如'描述当前界面'或'这个页面有哪些操作选项'），window 为可选的目标窗口，region 为可选区域（x,y,width,height）。",
    risk="read",
)
async def analyze_screen(
    prompt: str,
    window: Optional[str] = None,
    region: Optional[str] = None,
) -> str:
    """
    截图 → base64 → vLLM Vision 模型分析 → 返回文字描述
    """
    try:
        return await _visual_verify(prompt, window=window, region=region, screenshot_name="analyze_screen")
    except Exception as e:
        return f"视觉分析失败: {type(e).__name__}: {e}"


@tool(
    description="对指定图片文件进行视觉分析。image_path 为图片文件的绝对路径，prompt 为分析提示词。支持 PNG/JPG/WEBP 等格式。",
    risk="read",
)
async def analyze_image(image_path: str, prompt: str) -> str:
    """
    对已有图片文件调用 vLLM Vision 模型进行分析
    """
    if not Path(image_path).exists():
        return f"错误：图片文件不存在 - {image_path}"

    try:
        client = get_llm_client()
        return await client.vision(image_path=image_path, prompt=prompt)
    except Exception as e:
        return f"图像分析失败: {type(e).__name__}: {e}"


@tool(
    description="将图片（PNG/JPG/WEBP等）中的文字内容提取为 Markdown 格式。支持 OCR 文字识别、表格提取、结构化信息解析等场景。image_path 为图片文件路径，prompt 为可选的自定义提示词（默认提取所有文字和表格为 Markdown）。",
    risk="read",
)
async def parse_image_to_markdown(
    image_path: str,
    prompt: Optional[str] = None,
) -> str:
    """
    通过多模态视觉模型将图片中的文字/表格/结构化信息提取为 Markdown 格式。
    替代原有的 MinerU OCR 功能，统一使用 vLLM 部署的多模态模型。
    适用场景：文档扫描件、截图中的文字提取、表格识别、表单内容提取等。
    """
    if not Path(image_path).exists():
        return f"错误：图片文件不存在 - {image_path}"

    default_prompt = (
        "请将图片中的所有内容转换为 Markdown 格式。"
        "要求：\n"
        "1. 保留所有文字内容，包括标题、正文、注释等\n"
        "2. 识别并保留表格结构（使用 Markdown 表格语法）\n"
        "3. 识别标题层级（使用 # 标题语法）\n"
        "4. 识别列表项（使用 - 或 1. 列表语法）\n"
        "5. 尽量保持原始排版逻辑"
    )
    analysis_prompt = prompt or default_prompt

    try:
        client = get_llm_client()
        return await client.vision(image_path=image_path, prompt=analysis_prompt)
    except Exception as e:
        return f"图片解析失败: {type(e).__name__}: {e}"


@tool(
    description="从图片中提取纯文字内容（OCR 功能）。适合只需要文字不需要格式的场景。image_path 为图片文件路径。",
    risk="read",
)
async def extract_text_from_image(image_path: str) -> str:
    """
    通过多模态视觉模型从图片中提取纯文字，不保留格式。
    适用于简单 OCR 场景：读取截图中的文字、识别验证码等。
    """
    if not Path(image_path).exists():
        return f"错误：图片文件不存在 - {image_path}"

    try:
        client = get_llm_client()
        return await client.vision(
            image_path=image_path,
            prompt="请提取图片中的所有文字内容，按阅读顺序输出，不要添加任何格式或解释。"
        )
    except Exception as e:
        return f"文字提取失败: {type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════
# 动态 UI 识别与点击
# ══════════════════════════════════════════════════════

# 可交互的 ControlType 集合（过滤 Pane/Text/Document 等结构性元素）
_INTERACTIVE_TYPES = {
    "Button", "MenuItem", "Tab", "TabItem", "CheckBox",
    "RadioButton", "ComboBox", "Edit", "ListItem", "TreeItem",
    "Slider", "Menu", "Link", "ListBox", "Document", "Custom",
    "DataItem", "Header", "HeaderItem", "ToolBar",
}
_NAMED_STRUCTURAL_TYPES = {"Pane", "Text", "Group"}
_UIA_CACHE_TTL_SECONDS = 2.0
_uia_cache: dict[str, tuple[float, List[Dict]]] = {}
_uia_scan_metrics: dict[str, dict[str, Any]] = {}


def invalidate_uia_cache(window: Optional[str] = None) -> None:
    """Invalidate cached UIA records after any desktop mutation."""
    if window is None:
        _uia_cache.clear()
        _uia_scan_metrics.clear()
        return
    cache_key = window.casefold()
    _uia_cache.pop(cache_key, None)
    _uia_scan_metrics.pop(cache_key, None)


async def _get_interactive_elements(window: Optional[str]) -> List[Dict]:
    """
    Stage 1a: 激活窗口 → UIA 扫描 → 过滤可交互元素。
    返回 list of {name, control_type, automation_id, bounds, center}。
    UIA 扫描前必须先激活窗口，否则元素树可能不完整。
    """
    if not window:
        return []

    cache_key = window.casefold()
    cached = _uia_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] <= _UIA_CACHE_TTL_SECONDS:
        _uia_scan_metrics.setdefault(cache_key, {})["cache_hit"] = True
        return cached[1]

    # 1. 激活窗口前置（UIA 扫描之前）
    try:
        await window_activate(window)
        await asyncio.sleep(0.4)
    except Exception:
        pass

    # 2. 统一通过 WinPeekaboo CLI 扫描和规范化
    try:
        elements = normalize_element_records(await list_elements(window=window))
        result = []
        for e in elements:
            control_type = e["control_type"]
            if (
                control_type not in _INTERACTIVE_TYPES
                and not (
                    control_type in _NAMED_STRUCTURAL_TYPES
                    and (e["name"] or e["automation_id"])
                )
            ):
                continue
            if e["is_visible"] is False or e["is_enabled"] is False:
                continue
            bounds = e["bounds"]
            if (
                e["center"] is None
                or not bounds
                or bounds["width"] <= 0
                or bounds["height"] <= 0
            ):
                continue
            result.append(e)
        _uia_cache[cache_key] = (time.monotonic(), result)
        _uia_scan_metrics[cache_key] = {
            "raw": len(elements),
            "interactive": len(result),
            "cache_hit": False,
        }
        return result
    except Exception:
        return []


async def _llm_select_element(
    elements: List[Dict],
    query: str,
) -> Optional[Dict]:
    """
    Stage 1b: 用对话模型（非视觉）从 UIA 元素列表中语义匹配 query。
    返回模型选择的唯一元素记录，无匹配时返回 None。
    """
    if not elements:
        return None

    ranked = _rank_element_candidates(elements, query)
    candidates = [element for _, element in ranked[:12]]
    if not candidates:
        return None

    # 使用唯一 key，避免同名和空名称控件被解析为列表中的第一个。
    lines = []
    by_key = {}
    for index, e in enumerate(candidates):
        key = str(e.get("element_key") or f"E{index + 1:04d}")
        by_key[key] = e
        parts = [
            f"[{key}] name={json.dumps(e['name'], ensure_ascii=False)}",
            f"type={e['control_type']}",
        ]
        if e["automation_id"]:
            parts.append(
                f"id={json.dumps(e['automation_id'], ensure_ascii=False)}"
            )
        bounds = e.get("bounds") or {}
        parts.append(
            "bounds="
            f"{bounds.get('x', 0)},{bounds.get('y', 0)},"
            f"{bounds.get('width', 0)},{bounds.get('height', 0)}"
        )
        lines.append("- " + "  ".join(parts))
    elements_text = "\n".join(lines)

    prompt = (
        f"以下是 Windows 窗口中所有可交互的 UI 元素：\n\n"
        f"{elements_text}\n\n"
        f"用户想操作：{query}\n\n"
        f"请从上方列表中找到最匹配的元素，只返回方括号中的 element key。\n"
        f"规则：\n"
        f"1. 只输出类似 E0001 的 key，不要引号，不要解释\n"
        f"2. 如果没有任何匹配的元素，输出：NOT_FOUND"
    )

    client = get_llm_client()
    messages = [
        {"role": "system", "content": "你是 UI 元素匹配助手，根据用户意图从元素列表中选出最匹配元素的 name 值。"},
        {"role": "user", "content": prompt},
    ]

    try:
        response = await client.chat(messages)
        result = (response.content or "").strip().strip("`\"'")
        selected = by_key.get(result)
        if selected:
            return {
                **selected,
                "_semantic_candidate_count": len(candidates),
            }
        return None
    except Exception:
        return None


def _match_by_automation_id(elements: List[Dict], target: str) -> Optional[Dict]:
    """
    通过 automation_id 精确匹配（大小写不敏感）。
    automation_id 是语言无关的控件标识符，是最可靠的跨语言匹配方式。
    如 Outlook 的 Send 按钮，中文版叫 "发送" 英文版叫 "Send"，但 automation_id 始终是 "Send"。
    """
    expected = _normalize_match_text(target)
    matches = [
        element
        for element in elements
        if _normalize_match_text(str(element.get("automation_id") or "")) == expected
    ]
    return matches[0] if len(matches) == 1 else None


# 单元素定位 prompt
VISION_BBOX_PROMPT = (
    "请分析截图，找到\"{target}\"的位置。\n"
    "\n"
    "要求：\n"
    "1. 返回该元素的中心坐标 [cx, cy]\n"
    "2. cx 和 cy 是基于你所看到的图片的像素坐标\n"
    "3. 只返回 JSON 数组格式，不要其他文字\n"
    "4. 坐标值必须是整数\n"
    "\n"
    "示例输出: [450, 320]"
)

# 批量元素定位 prompt
VISION_BATCH_PROMPT = (
    "请分析截图，找到以下所有UI元素的位置。\n"
    "\n"
    "目标元素:\n"
    "{targets_text}\n"
    "\n"
    "要求：\n"
    "1. 返回每个元素的中心坐标 [cx, cy]，cx/cy 基于你所看到的图片的像素坐标\n"
    "2. 严格按以下 JSON 格式返回，不要其他文字:\n"
    "{{\n"
    '  "1": [cx1, cy1],\n'
    '  "2": [cx2, cy2]\n'
    "}}\n"
    "3. 编号与上方目标元素列表一致\n"
    "4. 坐标值必须是整数\n"
    "5. 如果某个元素在截图中不存在，对应值设为 null\n"
    "\n"
    '示例输出: {{"1": [450, 320], "2": [510, 320], "3": null}}'
)


def _parse_vision_bbox(response: str) -> Optional[Tuple[int, int]]:
    """
    解析多模态模型返回的单个坐标
    支持格式: [450, 320] 或 [450,320]
    """
    match = re.search(r'\[\s*(\d+)\s*,\s*(\d+)\s*\]', response)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def _parse_vision_batch(response: str) -> Dict[str, Optional[Tuple[int, int]]]:
    """
    解析多模态模型返回的批量坐标
    支持格式: {"1": [450, 320], "2": null, "3": [510, 320]}
    """
    # 尝试提取 JSON 对象
    json_match = re.search(r'\{[^{}]+\}', response, re.DOTALL)
    if not json_match:
        return {}

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return {}

    result = {}
    for key, value in data.items():
        if value is None:
            result[key] = None
        elif isinstance(value, list) and len(value) == 2:
            try:
                result[key] = (int(value[0]), int(value[1]))
            except (ValueError, TypeError):
                result[key] = None
        else:
            result[key] = None

    return result


# ────────────────────────────────────────────────────────
# 核心定位函数：统一实现 UIA+LLM 优先、VLM 兜底
# ────────────────────────────────────────────────────────


async def _locate_element(
    target: str,
    window: Optional[str] = None,
    elements: Optional[List[Dict]] = None,
    automation_id: Optional[str] = None,
) -> Optional[Dict]:
    """
    核心定位函数：统一实现 UI 元素定位逻辑。

    Stage 0: automation_id 直接匹配（零模型调用，确定性 100%）
    Stage 1: automation_id + name 快速匹配（零模型调用，跨语言可靠）
    Stage 2: LLM 语义匹配（处理语义模糊的场景）
    Stage 3: VLM 视觉兜底（覆盖图标/自定义渲染 UI）

    Args:
        target: 目标元素描述（如 "文件菜单"、"Send"）
        window: 目标窗口标题（可选）
        elements: 预扫描的 UIA 元素列表（避免批量场景重复扫描）
        automation_id: UIA AutomationId 精确匹配（可选，传此参数时跳过 LLM 语义匹配）

    Returns:
        {name, control_type, bounds, center, source: "UIA"|"VLM"} 或 None
    """
    # ── Stage 0: automation_id 直接匹配（确定性 100%，零模型调用）────
    if automation_id:
        if elements is None:
            elements = await _get_interactive_elements(window)
        if elements:
            matched = _match_by_automation_id(elements, automation_id)
            if matched:
                return {**matched, "source": "UIA", "_match_stage": "automation_id"}
        # 显式 automation_id 是严格确定性约束，未命中时禁止静默改用视觉坐标。
        return None

    # ── Stage 1: automation_id + name 快速匹配（零模型调用）────
    if elements is None:
        elements = await _get_interactive_elements(window)

    if elements:
        # 先尝试 automation_id + name 快速匹配（不含 LLM），命中则直接返回
        fast_matched = _simple_match_element(elements, target)
        if fast_matched:
            return {**fast_matched, "source": "UIA", "_match_stage": "deterministic"}

    # ── Stage 2: LLM 语义匹配 ─────────────────────────────
    if elements:
        matched = await _llm_select_element(elements, target)
        if matched:
            return {**matched, "source": "UIA", "_match_stage": "semantic"}

    # ── Stage 3: VLM 视觉兜底 ───────────────────────────────
    tmp_path = await _capture_for_coordinates(window, "locate")

    client = get_llm_client()
    prompt = VISION_BBOX_PROMPT.format(target=target)
    response, scale_x, scale_y = await client.vision_for_coords(
        image_path=tmp_path, prompt=prompt
    )

    coords = _parse_vision_bbox(response)
    if coords is None:
        return None

    point = _scale_vision_point(tmp_path, coords, scale_x, scale_y)
    if point is None:
        return None
    cx, cy = point

    return {
        "name": target,
        "control_type": "Unknown",
        "automation_id": "",
        "bounds": {"x": cx - 10, "y": cy - 10, "width": 20, "height": 20},
        "center": (cx, cy),
        "source": "VLM",
        "_match_stage": "vision",
    }


# 数字映射表：阿拉伯数字 ↔ 中文数字（用于计算器等跨语言场景）
_NUMBER_MAPPING = {
    "0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
    "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
    "零": "0", "一": "1", "二": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}


def _simple_match_element(elements: List[Dict], target: str) -> Optional[Dict]:
    """
    快速确定性匹配。候选分数接近时拒绝猜测，交给语义阶段消歧。
    """
    ranked = _rank_element_candidates(elements, target)
    if not ranked or ranked[0][0] < 500:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 30:
        return None
    return ranked[0][1]


def _rank_element_candidates(
    elements: List[Dict],
    target: str,
) -> list[tuple[int, Dict]]:
    ranked = [
        (_element_match_score(element, target), element)
        for element in elements
    ]
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def _element_match_score(element: Dict, target: str) -> int:
    target_text = _normalize_match_text(target)
    name = _normalize_match_text(str(element.get("name") or ""))
    automation_id = _normalize_match_text(
        str(element.get("automation_id") or "")
    )
    if not target_text:
        return 0

    score = 0
    if automation_id and automation_id == target_text:
        score = 1000
    elif name and name == target_text:
        score = 900
    elif target_text in _NUMBER_MAPPING and name == _NUMBER_MAPPING[target_text]:
        score = 880
    elif name and target_text in name:
        score = 700 + min(100, len(target_text) * 100 // max(1, len(name)))
    elif name and len(name) >= 2 and name in target_text:
        score = 620 + min(
            160,
            len(name) * 160 // max(1, len(target_text)),
        )
    elif name:
        target_tokens = set(target_text.split())
        name_tokens = set(name.split())
        overlap = target_tokens & name_tokens
        if overlap:
            score = 400 + int(
                180 * len(overlap) / len(target_tokens | name_tokens)
            )

    preferred_types = _preferred_control_types(target_text)
    if preferred_types and element.get("control_type") in preferred_types:
        score += 40
    if element.get("is_visible") is False or element.get("is_enabled") is False:
        score -= 500
    return score


def _normalize_match_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("&", "")
    return " ".join(re.sub(r"[\W_]+", " ", value).split())


def _preferred_control_types(target: str) -> set[str]:
    if any(term in target for term in ("button", "按钮", "按鈕")):
        return {"Button"}
    if any(
        term in target
        for term in ("input", "field", "textbox", "输入", "輸入")
    ):
        return {"Edit", "Document"}
    if any(term in target for term in ("link", "链接", "連結")):
        return {"Link"}
    if any(term in target for term in ("menu", "菜单", "選單")):
        return {"Menu", "MenuItem"}
    return set()


def _validated_click_point(element: Dict) -> Optional[tuple[int, int]]:
    center = element.get("center")
    if not isinstance(center, (list, tuple)) or len(center) != 2:
        return None
    try:
        x, y = int(center[0]), int(center[1])
    except (TypeError, ValueError):
        return None
    bounds = element.get("bounds")
    if isinstance(bounds, dict):
        try:
            left = int(bounds["x"])
            top = int(bounds["y"])
            right = left + int(bounds["width"])
            bottom = top + int(bounds["height"])
        except (KeyError, TypeError, ValueError):
            return None
        if right <= left or bottom <= top:
            return None
        if not (left <= x < right and top <= y < bottom):
            return None
    return x, y


def _scale_vision_point(
    image_path: str,
    coords: tuple[int, int],
    scale_x: float,
    scale_y: float,
) -> Optional[tuple[int, int]]:
    from PIL import Image

    try:
        with Image.open(image_path) as image:
            width, height = image.size
    except Exception:
        return None
    x = int(coords[0] * scale_x)
    y = int(coords[1] * scale_y)
    if not (0 <= x < width and 0 <= y < height):
        return None
    return x, y


# ══════════════════════════════════════════════════════
# 定位类工具：定位 UI 元素位置（不点击）
# ══════════════════════════════════════════════════════

@tool(
    description="压缩检查指定窗口的可交互 UIA 元素。仅返回最多 30 个候选的 key、name、control type 和 automation id，不返回完整 UIA JSON。通常应直接使用 find_and_click；只有目标名称或 automation_id 不明确时才调用本工具。query 可选，用于在本地优先排序候选；limit 默认 20。",
    risk="read",
)
async def inspect_elements(
    window: str,
    query: Optional[str] = None,
    limit: int = 20,
) -> str:
    elements = await _get_interactive_elements(window)
    bounded_limit = max(1, min(int(limit), 30))
    if query:
        elements = [element for _, element in _rank_element_candidates(elements, query)]
    candidates = elements[:bounded_limit]
    lines = []
    for element in candidates:
        key = str(element.get("element_key") or "")
        name = str(element.get("name") or "").replace("\r", " ").replace("\n", " ")[:120]
        control_type = str(element.get("control_type") or "Unknown")
        automation_id = str(element.get("automation_id") or "")[:120]
        line = f"[{key}] name={json.dumps(name, ensure_ascii=False)} type={control_type}"
        if automation_id:
            line += f" id={json.dumps(automation_id, ensure_ascii=False)}"
        lines.append(line)

    metrics = _uia_scan_metrics.get(window.casefold(), {})
    header = (
        "UIA compact inspection: "
        f"raw={metrics.get('raw', len(elements))}, "
        f"interactive={metrics.get('interactive', len(elements))}, "
        f"returned={len(candidates)}, "
        f"cache_hit={str(bool(metrics.get('cache_hit'))).lower()}"
    )
    if len(elements) > len(candidates):
        header += f", omitted={len(elements) - len(candidates)}"
    return header + ("\n" + "\n".join(lines) if lines else "\n(no matching elements)")

@tool(
    description="定位窗口中指定 UI 元素的位置，不执行点击。优先使用规范化 UIA 候选评分；候选有歧义时由 LLM 返回唯一 element key；无 automation_id 时才允许视觉兜底。显式 automation_id 为严格约束，未命中直接失败，不调用模型。",
    risk="read",
)
async def find_element(
    target: str,
    window: Optional[str] = None,
    automation_id: Optional[str] = None,
) -> str:
    """定位元素位置，返回结构化结果（不点击）。"""
    try:
        element = await _locate_element(target, window, automation_id=automation_id)
        if element is None:
            aid_hint = f" (automation_id={automation_id})" if automation_id else ""
            detail = (
                "严格 UIA automation_id 未命中；未启用视觉降级。"
                if automation_id
                else "UIA 未找到匹配元素，视觉模型也无法识别坐标。"
            )
            return (
                f"❌ 无法定位 '{target}'{aid_hint}。\n"
                f"{detail}"
            )
        point = _validated_click_point(element)
        if point is None:
            return f"❌ 定位到 '{target}'，但控件坐标无效。"
        cx, cy = point
        aid_info = f" aid={element.get('automation_id', '')}" if element.get('automation_id') else ""
        return (
            f"✅ [{element['source']}] 找到 '{target}'\n"
            f"   元素: name=\"{element['name']}\" type={element['control_type']}{aid_info}\n"
            f"   坐标: ({cx}, {cy})"
        )
    except Exception as e:
        return f"find_element 失败: {type(e).__name__}: {e}"


@tool(description="""一次性定位窗口中多个 UI 元素，返回所有元素的名称、坐标等结构化信息，不执行点击。
用于对同一窗口连续操作多个元素的场景：一次 UIA 扫描 + 一次匹配调用，取回所有坐标后逐步点击/输入，避免每个元素都重新扫描。
支持两种 targets 格式：
1. 简单字符串数组：["To","Cc","Subject","Send"]
2. 带 automation_id 的对象数组（确定性匹配，零模型调用）：[{"target":"To","automation_id":"4142"},{"target":"Send","automation_id":"4098"}]
两种格式可混用，无 automation_id 的目标自动走文本匹配。
适用场景：填写表单（多个输入框+按钮）、邮件窗口（To/Cc/Subject/Send）等。
返回格式：每个元素的 name、坐标(cx,cy)、来源(UIA/VLM)。""", risk="read")
async def batch_locate_elements(
    targets: str,
    window: Optional[str] = None,
) -> str:
    """
    批量定位多个元素（不点击）：
    1. UIA 一次扫描
    2. automation_id 精确匹配优先 → 字符串快速匹配 → VLM 批量兜底
    3. 返回所有元素坐标列表
    """
    try:
        # 1. 解析目标列表
        try:
            raw_targets = json.loads(targets)
        except json.JSONDecodeError:
            return "targets 格式错误，需要 JSON 数组字符串，如 '[\"To\",\"Cc\"]'"

        if not raw_targets or not isinstance(raw_targets, list):
            return "targets 不能为空，且必须是 JSON 数组"

        # 2. 标准化 target 格式：统一为 {"target": str, "automation_id": str|None}
        normalized: List[Dict] = []
        for item in raw_targets:
            if isinstance(item, str):
                normalized.append({"target": item, "automation_id": None})
            elif isinstance(item, dict):
                normalized.append({
                    "target": item.get("target", item.get("name", "?")),
                    "automation_id": item.get("automation_id") or item.get("aid"),
                })
            else:
                normalized.append({"target": str(item), "automation_id": None})

        # 3. UIA 一次扫描
        elements = await _get_interactive_elements(window)

        # 4. 匹配：automation_id 优先 → 字符串匹配 → LLM 语义匹配 → VLM 兜底
        matched: Dict[int, Dict] = {}  # index -> element
        unmatched_indices: List[int] = []
        strict_unmatched: set[int] = set()

        for i, item in enumerate(normalized):
            t = item["target"]
            aid = item["automation_id"]

            # automation_id 精确匹配（确定性，零模型调用）
            if aid:
                if elements:
                    m = _match_by_automation_id(elements, aid)
                    if m:
                        matched[i] = m
                        continue
                strict_unmatched.add(i)
                unmatched_indices.append(i)
                continue
            if elements:
                # 字符串快速匹配（含数字映射）
                m = _simple_match_element(elements, t)
                if m:
                    matched[i] = m
                    continue

            unmatched_indices.append(i)

        # 5. LLM 语义匹配（处理跨语言、同义词等字符串匹配失败的场景）
        if unmatched_indices and elements:
            llm_unmatched = list(strict_unmatched)
            for i in unmatched_indices:
                if i in strict_unmatched:
                    continue
                t = normalized[i]["target"]
                # 用 LLM 从元素列表中语义匹配
                m = await _llm_select_element(elements, t)
                if m:
                    matched[i] = m
                else:
                    llm_unmatched.append(i)
            unmatched_indices = llm_unmatched

        # 6. 仍未匹配的目标用 VLM 批量兜底
        visual_indices = [
            index for index in unmatched_indices
            if index not in strict_unmatched
        ]
        if visual_indices:
            tmp_path = await _capture_for_coordinates(window, "batch_locate")

            unmatched_targets = [normalized[i]["target"] for i in visual_indices]
            targets_text = "\n".join(
                f"  {j+1}. {t}" for j, t in enumerate(unmatched_targets)
            )
            prompt = VISION_BATCH_PROMPT.format(targets_text=targets_text)

            client = get_llm_client()
            response, scale_x, scale_y = await client.vision_for_coords(
                image_path=tmp_path, prompt=prompt
            )
            batch_coords = _parse_vision_batch(response)

            for j, orig_idx in enumerate(visual_indices):
                key = str(j + 1)
                coords = batch_coords.get(key)
                if coords:
                    point = _scale_vision_point(
                        tmp_path,
                        coords,
                        scale_x,
                        scale_y,
                    )
                    if point is None:
                        continue
                    cx, cy = point
                    matched[orig_idx] = {
                        "name": normalized[orig_idx]["target"],
                        "center": (cx, cy),
                    }

        # 6. 构造返回结果
        lines = ["\U0001f4cb 批量定位结果："]
        uia_count = 0
        vlm_count = 0
        for i, item in enumerate(normalized):
            t = item["target"]
            m = matched.get(i)
            if m is None:
                lines.append(f"  \u274c [{i+1}] '{t}': 未找到")
            else:
                cx, cy = m["center"]
                source = "UIA" if m.get("control_type") else "VLM"
                elem_info = f'name=\"{m.get("name", "")}\"'
                if m.get("control_type"):
                    elem_info += f' type={m["control_type"]}'
                lines.append(f"  \u2705 [{i+1}] '{t}' \u2192 {elem_info}  坐标=({cx},{cy})  [{source}]")
                if source == "UIA":
                    uia_count += 1
                else:
                    vlm_count += 1

        lines.append(f"\n共 {len(normalized)} 个目标，UIA 匹配 {uia_count} 个，VLM 兜底 {vlm_count} 个")
        lines.append("请按顺序使用以上坐标执行 click + type_text 操作。")
        return "\n".join(lines)

    except Exception as e:
        return f"batch_locate_elements 失败: {type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════
# 组合类工具：定位 + 点击
# ══════════════════════════════════════════════════════

@tool(
    description="【推荐】在指定窗口中找到目标 UI 元素并点击。先使用规范化 UIA 候选评分，歧义时由 LLM 返回唯一 element key，无 UIA 候选时才使用视觉坐标。显式 automation_id 为严格约束，未命中直接失败。点击前校验坐标；可按需检测并激活同一应用进程的新窗口。",
    risk="medium",
    side_effect=True,
)
async def find_and_click(
    target: str,
    window: Optional[str] = None,
    automation_id: Optional[str] = None,
    detect_new_window: bool = True,
    new_window_timeout_seconds: float = 1.2,
) -> str:
    """定位元素 + 执行点击，并检测新窗口。"""
    try:
        element = await _locate_element(target, window, automation_id=automation_id)
        if element is None:
            aid_hint = f" (automation_id={automation_id})" if automation_id else ""
            detail = (
                "严格 UIA automation_id 未命中；未启用视觉降级。"
                if automation_id
                else "UIA 未找到匹配元素，视觉模型也无法识别坐标。"
            )
            return (
                f"❌ 无法定位 '{target}'{aid_hint}。\n"
                f"{detail}"
            )
        point = _validated_click_point(element)
        if point is None:
            return f"❌ 定位到 '{target}'，但控件坐标无效，已拒绝点击。"
        cx, cy = point
        scan_metrics = dict(_uia_scan_metrics.get((window or "").casefold(), {}))
        match_stage = str(element.get("_match_stage") or element.get("source") or "unknown")

        # 只有调用方需要跟踪弹窗时才采集窗口快照。
        before_windows = await _snapshot_windows() if detect_new_window else {}

        await click(on=f"{cx},{cy}", window=window)

        aid_info = f" aid={element.get('automation_id', '')}" if element.get('automation_id') else ""
        result = (
            f"✅ [{element['source']}] 成功点击 '{target}'\n"
            f"   匹配元素: name=\"{element['name']}\" type={element['control_type']}{aid_info}\n"
            f"   坐标: ({cx}, {cy})\n"
            f"   定位指标: stage={match_stage} raw={scan_metrics.get('raw', 0)} "
            f"interactive={scan_metrics.get('interactive', 0)} "
            f"semantic_candidates={element.get('_semantic_candidate_count', 0)}"
        )

        # 点击后检测新窗口
        new_win = None
        if detect_new_window:
            new_win = await _detect_and_activate_new_window(
                before_windows,
                delay=0.8,
                source_window=window,
                timeout_seconds=max(0.1, new_window_timeout_seconds),
            )
        if new_win:
            result += f"\n🔄 检测到新窗口已弹出，已自动激活: \"{new_win}\""

        return result
    except Exception as e:
        return f"find_and_click 失败: {type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════
# 批量类工具：UIA 一次扫描 + 逐个匹配 + VLM 兜底
# ══════════════════════════════════════════════════════

@tool(
    description="在指定窗口中找到多个 UI 元素并按顺序点击。UIA 只扫描一次，字符串匹配覆盖大部分场景（零模型调用），仅未匹配的目标才触发 VLM 批量兜底。targets 为 JSON 数组字符串，如 '[\"1\",\"2\",\"3\",\"+\",\"=\"]'。window 为可选窗口标题。",
    risk="medium",
    side_effect=True,
)
async def find_and_click_batch(
    targets: str,
    window: Optional[str] = None,
) -> str:
    """
    批量定位 + 按顺序点击：
    1. UIA 一次扫描，字符串匹配所有目标
    2. 未匹配的目标用 VLM 批量兜底（一次截图、一次模型调用）
    3. 按原始顺序逐个点击
    """
    import asyncio

    try:
        # 1. 解析目标列表
        try:
            target_list = json.loads(targets)
        except json.JSONDecodeError:
            return "targets 格式错误，需要 JSON 数组字符串，如 '[\"1\",\"2\",\"3\"]'"

        if not target_list or not isinstance(target_list, list):
            return "targets 不能为空，且必须是数组"

        # 2. UIA 一次扫描
        elements = await _get_interactive_elements(window)

        # 3. 字符串快速匹配（零模型调用，含数字映射）
        matched_results: Dict[int, Dict] = {}  # index -> element
        unmatched_indices: List[int] = []

        for i, t in enumerate(target_list):
            if elements:
                matched = _simple_match_element(elements, t)
                if matched:
                    matched_results[i] = {**matched, "source": "UIA"}
                    continue
            unmatched_indices.append(i)

        # 4. LLM 语义匹配（处理跨语言、同义词等字符串匹配失败的场景）
        if unmatched_indices and elements:
            llm_unmatched = []
            for i in unmatched_indices:
                t = target_list[i]
                m = await _llm_select_element(elements, t)
                if m:
                    matched_results[i] = {**m, "source": "UIA"}
                else:
                    llm_unmatched.append(i)
            unmatched_indices = llm_unmatched

        # 5. 仍未匹配的目标用 VLM 批量兜底
        if unmatched_indices:
            tmp_path = await _capture_for_coordinates(window, "batch_click")

            unmatched_targets = [target_list[i] for i in unmatched_indices]
            targets_text = "\n".join(
                f"  {j+1}. {t}" for j, t in enumerate(unmatched_targets)
            )
            prompt = VISION_BATCH_PROMPT.format(targets_text=targets_text)

            client = get_llm_client()
            response, scale_x, scale_y = await client.vision_for_coords(
                image_path=tmp_path, prompt=prompt
            )
            batch_coords = _parse_vision_batch(response)

            for j, orig_idx in enumerate(unmatched_indices):
                key = str(j + 1)
                coords = batch_coords.get(key)
                if coords:
                    point = _scale_vision_point(
                        tmp_path,
                        coords,
                        scale_x,
                        scale_y,
                    )
                    if point is None:
                        continue
                    cx, cy = point
                    matched_results[orig_idx] = {
                        "name": target_list[orig_idx],
                        "control_type": "Unknown",
                        "center": (cx, cy),
                        "source": "VLM",
                    }

        # 5. 按原始顺序逐个点击
        results = []
        for i, t in enumerate(target_list):
            element = matched_results.get(i)
            if element is None:
                results.append(f"  ❌ [{i+1}] '{t}': 未找到")
                continue

            point = _validated_click_point(element)
            if point is None:
                results.append(f"  ❌ [{i+1}] '{t}': 坐标无效，已拒绝点击")
                continue
            cx, cy = point

            # 点击前快照窗口
            before_windows = await _snapshot_windows()

            await click(on=f"{cx},{cy}", window=window)
            results.append(f"  ✅ [{i+1}] '{t}': [{element['source']}] 已点击 ({cx}, {cy})")

            # 检测新窗口
            new_win = await _detect_and_activate_new_window(
                before_windows,
                delay=0.5,
                source_window=window,
            )
            if new_win:
                results.append(f"  🔄 检测到新窗口已弹出，已自动激活: \"{new_win}\"。剩余 {len(target_list)-i-1} 个目标将在新窗口中操作，请确认是否继续。")
                break
            await asyncio.sleep(0.15)

        # 统计信息
        uia_count = sum(1 for e in matched_results.values() if e.get("source") == "UIA")
        vlm_count = sum(1 for e in matched_results.values() if e.get("source") == "VLM")
        summary = (
            f"批量点击完成（{len(target_list)} 个目标，"
            f"UIA 匹配 {uia_count} 个，VLM 兜底 {vlm_count} 个）:\n"
        )
        summary += "\n".join(results)
        return summary

    except Exception as e:
        return f"find_and_click_batch 失败: {type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════
# 步骤验证：多模态模型判断操作是否成功
# ══════════════════════════════════════════════════════

async def verify_action_result(
    expected: str,
    window: Optional[str] = None,
    wait_seconds: float = 1.0,
) -> str:
    """
    截图并用多模态模型验证当前操作是否达到预期效果。

    参数：
        expected: 预期效果的描述（如 "收件人栏已填写 test@email.com"）
        window: 目标窗口标题（可选），传入后截图前会自动激活该窗口
        wait_seconds: 截图前等待时间（默认 1 秒，给 UI 踨定时间）

    返回：
        验证结果描述（如 "✅ 验证通过：..." 或 "❌ 验证失败：...")
    """
    try:
        # 1. 等待 UI 踨定
        await asyncio.sleep(wait_seconds)

        # 2. 如果指定了窗口，先尝试找到并激活它
        target_window = window
        if window:
            try:
                await window_activate(window)
                await asyncio.sleep(0.3)  # 等待激活动画
            except Exception:
                pass  # 激活失败不阻断验证
        else:
            # 未指定窗口，截全屏
            target_window = None

        # 3. 用固定 prompt 调用公共视觉验证
        prompt = (
            f"请观察当前屏幕是否满足以下预期效果，如实描述你的观察。\n\n"
            f"## 预期效果\n{expected}\n\n"
            f"请用以下格式返回（根据实际观察选择一种）：\n"
            f"- ✅ 符合预期：[简要说明你看到了什么]\n"
            f"- ⚠️ 无法确定：[说明遮挡、加载中或无法判断的原因]\n"
            f"- ❌ 不符合预期：[说明当前可见状态与预期的明确冲突]"
        )
        result = await _visual_verify(prompt, window=target_window, screenshot_name="verify")
        return result.strip()

    except ProviderCapabilityError as e:
        return f"⚠️ 无法确定：Vision 模型不可用：{e}"
    except Exception as e:
        return f"⚠️ 验证过程出错: {type(e).__name__}: {e}"
