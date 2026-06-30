"""
多模态视觉分析工具：截图/图片 → vLLM 多模态模型 → 文字描述/Markdown/OCR
通过 config.yaml 配置视觉模型，统一处理所有图像解析需求
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from llm import get_llm_client
from .registry import tool
from .winpeekaboo import capture_image, click, list_windows, window_activate
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
    tmp_path = _screenshot_path(screenshot_name)
    await capture_image(output=tmp_path, window=window, region=region)
    client = get_llm_client()
    return await client.vision(image_path=tmp_path, prompt=prompt)


# ══════════════════════════════════════════════════════
# 新窗口检测：点击后自动识别并激活新弹出的窗口
# ══════════════════════════════════════════════════════

async def _snapshot_windows() -> set:
    """获取当前所有窗口的 hwnd 集合"""
    try:
        raw = await list_windows()
        wins = json.loads(raw)
        return {w["hwnd"] for w in wins if w.get("is_visible")}
    except Exception:
        return set()


async def _detect_and_activate_new_window(
    before: set,
    delay: float = 0.8,
) -> Optional[str]:
    """
    等待 delay 秒后，比较窗口列表，找到新出现的窗口并激活。
    返回新窗口的标题，如果没有新窗口则返回 None。
    """
    import asyncio
    await asyncio.sleep(delay)
    try:
        raw = await list_windows()
        wins = json.loads(raw)
        after = {w["hwnd"]: w for w in wins if w.get("is_visible")}
        new_hwnds = set(after.keys()) - before
        if not new_hwnds:
            return None
        # 取标题最长的（通常是主窗口，不是工具栏/菜单）
        new_wins = [after[h] for h in new_hwnds if after[h].get("title", "").strip()]
        if not new_wins:
            return None
        target = max(new_wins, key=lambda w: len(w.get("title", "")))
        title = target["title"]
        await window_activate(title)
        return title
    except Exception:
        return None


# ══════════════════════════════════════════════════════
# 通用视觉分析
# ══════════════════════════════════════════════════════

@tool(description="截取当前屏幕或指定窗口，通过多模态视觉模型分析图像内容，返回文字描述或分析结果。仅用于理解界面内容（UI 布局、操作状态、页面信息），不用于定位元素坐标。prompt 为分析提示词（如'描述当前界面'或'这个页面有哪些操作选项'），window 为可选的目标窗口，region 为可选区域（x,y,width,height）。")
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


@tool(description="对指定图片文件进行视觉分析。image_path 为图片文件的绝对路径，prompt 为分析提示词。支持 PNG/JPG/WEBP 等格式。")
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


@tool(description="将图片（PNG/JPG/WEBP等）中的文字内容提取为 Markdown 格式。支持 OCR 文字识别、表格提取、结构化信息解析等场景。image_path 为图片文件路径，prompt 为可选的自定义提示词（默认提取所有文字和表格为 Markdown）。")
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


@tool(description="从图片中提取纯文字内容（OCR 功能）。适合只需要文字不需要格式的场景。image_path 为图片文件路径。")
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
    "Slider", "Menu", "Link", "ListBox",
}


async def _get_interactive_elements(window: Optional[str]) -> List[Dict]:
    """
    Stage 1a: 激活窗口 → UIA 扫描 → 过滤可交互元素。
    返回 list of {name, control_type, automation_id, bounds, center}。
    UIA 扫描前必须先激活窗口，否则元素树可能不完整。
    """
    # 1. 激活窗口前置（UIA 扫描之前）
    if window:
        try:
            await window_activate(window)
            await asyncio.sleep(0.4)
        except Exception:
            pass

    # 2. UIA 扫描
    try:
        from winpeekaboo.uia.finder import ElementFinder
        finder = ElementFinder()
        if not finder.connect_by_title(window or ""):
            return []

        elements = finder.find_all_elements()

        # 3. 过滤可交互类型 + bounds 非零
        result = []
        for e in elements:
            if e.control_type not in _INTERACTIVE_TYPES:
                continue
            if e.bounds.width == 0 or e.bounds.height == 0:
                continue
            cx = e.bounds.x + e.bounds.width // 2
            cy = e.bounds.y + e.bounds.height // 2
            result.append({
                "name": e.name or "",
                "control_type": str(e.control_type),
                "automation_id": e.automation_id or "",
                "bounds": {
                    "x": e.bounds.x,
                    "y": e.bounds.y,
                    "width": e.bounds.width,
                    "height": e.bounds.height,
                },
                "center": (cx, cy),
            })

        return result
    except Exception:
        return []


async def _llm_select_element(elements: List[Dict], query: str) -> str:
    """
    Stage 1b: 用对话模型（非视觉）从 UIA 元素列表中语义匹配 query。
    返回最匹配元素的 name 字段原始值，无匹配时返回 "NOT_FOUND"。
    """
    if not elements:
        return "NOT_FOUND"

    # 构造元素列表文本
    lines = []
    for e in elements:
        parts = [f'name="{e["name"]}"', f'type={e["control_type"]}']
        if e["automation_id"]:
            parts.append(f'id={e["automation_id"]}')
        lines.append("- " + "  ".join(parts))
    elements_text = "\n".join(lines)

    prompt = (
        f"以下是 Windows 窗口中所有可交互的 UI 元素：\n\n"
        f"{elements_text}\n\n"
        f"用户想操作：{query}\n\n"
        f"请从上方列表中找到最匹配的元素，只返回该元素的 name 字段原始内容。\n"
        f"规则：\n"
        f"1. 只输出 name 字段的原始文本，不要引号，不要解释\n"
        f"2. 如果没有任何匹配的元素，输出：NOT_FOUND"
    )

    client = get_llm_client()
    messages = [
        {"role": "system", "content": "你是 UI 元素匹配助手，根据用户意图从元素列表中选出最匹配元素的 name 值。"},
        {"role": "user", "content": prompt},
    ]

    try:
        response = await client.chat(messages)
        result = (response.content or "").strip()
        return result if result else "NOT_FOUND"
    except Exception:
        return "NOT_FOUND"


def _match_by_automation_id(elements: List[Dict], target: str) -> Optional[Dict]:
    """
    通过 automation_id 精确匹配（大小写不敏感）。
    automation_id 是语言无关的控件标识符，是最可靠的跨语言匹配方式。
    如 Outlook 的 Send 按钮，中文版叫 "发送" 英文版叫 "Send"，但 automation_id 始终是 "Send"。
    """
    target_lower = target.lower()
    for e in elements:
        aid = e.get("automation_id", "")
        if aid and aid.lower() == target_lower:
            return e
    return None


def _match_element_by_name(elements: List[Dict], name: str) -> Optional[Dict]:
    """
    Stage 1c: 在元素列表中验证并查找 LLM 返回的 name。
    精确匹配优先，子串兜底，最后尝试 automation_id。
    返回 None 表示 LLM 出现幻觉（name 不在列表中）。
    """
    if not name or name == "NOT_FOUND":
        return None

    # 精确匹配
    for e in elements:
        if e["name"] == name:
            return e

    # 子串匹配（LLM 可能返回局部名称）
    for e in elements:
        if e["name"] and name in e["name"]:
            return e

    # automation_id 兜底（如 LLM 返回 "Send" 但元素 name 是 "发送"）
    matched = _match_by_automation_id(elements, name)
    if matched:
        return matched

    return None


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
                return {**matched, "source": "UIA"}
        # automation_id 未找到时，不降级到 LLM（因为 caller 明确指定了 ID）
        # 直接进入 VLM 视觉兜底
        tmp_path = _screenshot_path("locate_aid")
        await capture_image(output=tmp_path, window=window)
        client = get_llm_client()
        prompt = VISION_BBOX_PROMPT.format(target=target)
        response, scale_x, scale_y = await client.vision_for_coords(
            image_path=tmp_path, prompt=prompt
        )
        coords = _parse_vision_bbox(response)
        if coords is None:
            return None
        raw_cx, raw_cy = coords
        cx = int(raw_cx * scale_x)
        cy = int(raw_cy * scale_y)
        return {
            "name": target,
            "control_type": "Unknown",
            "automation_id": automation_id,
            "bounds": {"x": cx - 10, "y": cy - 10, "width": 20, "height": 20},
            "center": (cx, cy),
            "source": "VLM",
        }

    # ── Stage 1: automation_id + name 快速匹配（零模型调用）────
    if elements is None:
        elements = await _get_interactive_elements(window)

    if elements:
        # 先尝试 automation_id + name 快速匹配（不含 LLM），命中则直接返回
        fast_matched = _simple_match_element(elements, target)
        if fast_matched:
            return {**fast_matched, "source": "UIA"}

    # ── Stage 2: LLM 语义匹配 ─────────────────────────────
    if elements:
        selected_name = await _llm_select_element(elements, target)
        matched = _match_element_by_name(elements, selected_name)
        if matched:
            return {**matched, "source": "UIA"}

    # ── Stage 3: VLM 视觉兜底 ───────────────────────────────
    tmp_path = _screenshot_path("locate")
    await capture_image(output=tmp_path, window=window)

    client = get_llm_client()
    prompt = VISION_BBOX_PROMPT.format(target=target)
    response, scale_x, scale_y = await client.vision_for_coords(
        image_path=tmp_path, prompt=prompt
    )

    coords = _parse_vision_bbox(response)
    if coords is None:
        return None

    raw_cx, raw_cy = coords
    cx = int(raw_cx * scale_x)
    cy = int(raw_cy * scale_y)

    return {
        "name": target,
        "control_type": "Unknown",
        "automation_id": "",
        "bounds": {"x": cx - 10, "y": cy - 10, "width": 20, "height": 20},
        "center": (cx, cy),
        "source": "VLM",
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
    批量场景的快速字符串匹配（零模型调用）。
    优先级: automation_id > 精确 name > 数字映射 > target 在 name 中 > name 在 target 中。
    """
    # 最高优先级: automation_id 精确匹配（语言无关，如 "Send" 匹配到 name="发送" 的元素）
    matched = _match_by_automation_id(elements, target)
    if matched:
        return matched
    # 精确 name 匹配
    for e in elements:
        if e["name"] == target:
            return e
    # 数字映射匹配（处理 "1" ↔ "一" 等跨语言场景）
    if target in _NUMBER_MAPPING:
        mapped = _NUMBER_MAPPING[target]
        for e in elements:
            if e["name"] == mapped:
                return e
    # target 在 name 中
    for e in elements:
        if e["name"] and target in e["name"]:
            return e
    # name 在 target 中
    for e in elements:
        if e["name"] and e["name"] in target:
            return e
    return None


# ══════════════════════════════════════════════════════
# 定位类工具：定位 UI 元素位置（不点击）
# ══════════════════════════════════════════════════════

@tool(description="定位窗口中指定 UI 元素的位置，返回元素名称、类型、坐标等结构化信息，不执行点击。优先通过 UIA + LLM 精确定位，UIA 无法覆盖时自动使用视觉模型兜底。target 为元素描述（如'保存按钮'、'文件菜单'），window 为可选的目标窗口标题。automation_id 为可选 UIA AutomationId，传入后直接确定性匹配，跳过 LLM 语义匹配（零模型调用）。")
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
            return (
                f"❌ 无法定位 '{target}'{aid_hint}。\n"
                f"UIA 未找到匹配元素，视觉模型也无法识别坐标。"
            )
        cx, cy = element["center"]
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
返回格式：每个元素的 name、坐标(cx,cy)、来源(UIA/VLM)。""")
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
            return f"targets 格式错误，需要 JSON 数组字符串，如 '[\"To\",\"Cc\"]'"

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

        for i, item in enumerate(normalized):
            t = item["target"]
            aid = item["automation_id"]

            if elements:
                # automation_id 精确匹配（确定性，零模型调用）
                if aid:
                    m = _match_by_automation_id(elements, aid)
                    if m:
                        matched[i] = m
                        continue
                # 字符串快速匹配（含数字映射）
                m = _simple_match_element(elements, t)
                if m:
                    matched[i] = m
                    continue

            unmatched_indices.append(i)

        # 5. LLM 语义匹配（处理跨语言、同义词等字符串匹配失败的场景）
        if unmatched_indices and elements:
            llm_unmatched = []
            for i in unmatched_indices:
                t = normalized[i]["target"]
                # 用 LLM 从元素列表中语义匹配
                selected_name = await _llm_select_element(elements, t)
                m = _match_element_by_name(elements, selected_name)
                if m:
                    matched[i] = m
                else:
                    llm_unmatched.append(i)
            unmatched_indices = llm_unmatched

        # 6. 仍未匹配的目标用 VLM 批量兜底
        if unmatched_indices:
            tmp_path = _screenshot_path("batch_locate")
            await capture_image(output=tmp_path, window=window)

            unmatched_targets = [normalized[i]["target"] for i in unmatched_indices]
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
                    raw_cx, raw_cy = coords
                    cx = int(raw_cx * scale_x)
                    cy = int(raw_cy * scale_y)
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

@tool(description="【推荐】在指定窗口中找到目标 UI 元素并点击。这是常规左键点击的首选工具，优先通过 UIA + LLM 精确定位，UIA 无法覆盖时自动使用视觉模型兜底。当传入 automation_id 时直接确定性匹配（零模型调用，最快最准）。点击后自动检测是否有新窗口弹出，有则自动激活新窗口。target 为元素描述（如'保存按鈕'、'文件菜单'、'确定'），window 为可选的目标窗口标题，automation_id 为可选 UIA AutomationId 精确匹配。")
async def find_and_click(
    target: str,
    window: Optional[str] = None,
    automation_id: Optional[str] = None,
) -> str:
    """定位元素 + 执行点击，并检测新窗口。"""
    try:
        element = await _locate_element(target, window, automation_id=automation_id)
        if element is None:
            aid_hint = f" (automation_id={automation_id})" if automation_id else ""
            return (
                f"❌ 无法定位 '{target}'{aid_hint}。\n"
                f"UIA 未找到匹配元素，视觉模型也无法识别坐标。"
            )
        cx, cy = element["center"]

        # 点击前快照窗口列表
        before_windows = await _snapshot_windows()

        await click(on=f"{cx},{cy}", window=window)

        aid_info = f" aid={element.get('automation_id', '')}" if element.get('automation_id') else ""
        result = (
            f"✅ [{element['source']}] 成功点击 '{target}'\n"
            f"   匹配元素: name=\"{element['name']}\" type={element['control_type']}{aid_info}\n"
            f"   坐标: ({cx}, {cy})"
        )

        # 点击后检测新窗口
        new_win = await _detect_and_activate_new_window(before_windows, delay=0.8)
        if new_win:
            result += f"\n🔄 检测到新窗口已弹出，已自动激活: \"{new_win}\""

        return result
    except Exception as e:
        return f"find_and_click 失败: {type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════
# 批量类工具：UIA 一次扫描 + 逐个匹配 + VLM 兜底
# ══════════════════════════════════════════════════════

@tool(description="在指定窗口中找到多个 UI 元素并按顺序点击。UIA 只扫描一次，字符串匹配覆盖大部分场景（零模型调用），仅未匹配的目标才触发 VLM 批量兜底。targets 为 JSON 数组字符串，如 '[\"1\",\"2\",\"3\",\"+\",\"=\"]'。window 为可选窗口标题。")
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
            return f"targets 格式错误，需要 JSON 数组字符串，如 '[\"1\",\"2\",\"3\"]'"

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
                selected_name = await _llm_select_element(elements, t)
                m = _match_element_by_name(elements, selected_name)
                if m:
                    matched_results[i] = {**m, "source": "UIA"}
                else:
                    llm_unmatched.append(i)
            unmatched_indices = llm_unmatched

        # 5. 仍未匹配的目标用 VLM 批量兜底
        if unmatched_indices:
            tmp_path = _screenshot_path("batch_click")
            await capture_image(output=tmp_path, window=window)

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
                    raw_cx, raw_cy = coords
                    cx = int(raw_cx * scale_x)
                    cy = int(raw_cy * scale_y)
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

            cx, cy = element["center"]

            # 点击前快照窗口
            before_windows = await _snapshot_windows()

            await click(on=f"{cx},{cy}", window=window)
            results.append(f"  ✅ [{i+1}] '{t}': [{element['source']}] 已点击 ({cx}, {cy})")

            # 检测新窗口
            new_win = await _detect_and_activate_new_window(before_windows, delay=0.5)
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
            f"- ⚠️ 部分符合：[说明看到了什么以及差异]"
        )
        result = await _visual_verify(prompt, window=target_window, screenshot_name="verify")
        return result.strip()

    except Exception as e:
        return f"⚠️ 验证过程出错: {type(e).__name__}: {e}"
