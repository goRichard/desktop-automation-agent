"""
多模态模型 BBox 精度测试

流程:
1. 用 winpeekaboo 截图 + 获取 UIA 元素（ground truth）
2. 将截图发给多模态模型，要求返回结构化 JSON（元素名 + bbox + click_point）
3. 将视觉结果与 UIA ground truth 匹配并计算精度指标

用法:
    python -m tests.vision_bbox.test_bbox_precision --window "记事本"
    python -m tests.vision_bbox.test_bbox_precision --active-window
    python -m tests.vision_bbox.test_bbox_precision --window "记事本" --save-dir ./tests/vision_bbox/output
"""
from __future__ import annotations

import os
# Windows 控制台 UTF-8 输出，避免 UnicodeEncodeError
os.environ.setdefault("PYTHONUTF8", "1")

import argparse
import base64
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ─── 把项目根目录加到 sys.path ───────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ═══════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════

@dataclass
class UIAGroundTruth:
    """UIA 元素 ground truth"""
    name: str
    control_type: str
    automation_id: str
    bbox: tuple[int, int, int, int]  # (x, y, width, height)
    center: tuple[int, int]  # (cx, cy)


@dataclass
class VisionElement:
    """多模态模型识别的元素"""
    name: str
    element_type: str
    bbox: tuple[int, int, int, int]  # (x1, y1, x2, y2) — 模型返回的格式
    click_point: tuple[int, int]  # (cx, cy)
    raw_response: str = ""


@dataclass
class MatchResult:
    """一个元素匹配结果"""
    uia_element: UIAGroundTruth
    vision_element: VisionElement
    center_distance: float = 0.0
    bbox_iou: float = 0.0
    click_hit: bool = False  # click_point 是否在 UIA bbox 内
    name_match: bool = False  # 名称是否匹配


@dataclass
class TestReport:
    """一次测试的完整报告"""
    window_title: str
    timestamp: str
    uia_element_count: int = 0
    vision_element_count: int = 0
    matched_count: int = 0
    matches: list[MatchResult] = field(default_factory=list)

    # 聚合指标
    avg_center_distance: float = 0.0
    median_center_distance: float = 0.0
    avg_iou: float = 0.0
    click_hit_rate: float = 0.0
    name_match_rate: float = 0.0

    # 误差分布
    distance_under_10px: float = 0.0  # 中心距离 < 10px 的比例
    distance_under_20px: float = 0.0
    distance_under_50px: float = 0.0


# ═══════════════════════════════════════════════════════════
# WinPeekaboo 交互
# ═══════════════════════════════════════════════════════════

def _run_wpb(*args: str) -> str:
    """执行 winpeekaboo CLI 命令"""
    cmd = [sys.executable, "-m", "winpeekaboo"] + list(args)
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        err_msg = result.stderr.strip() if result.stderr else "unknown error"
        raise RuntimeError(f"winpeekaboo error (rc={result.returncode}): {err_msg}")
    if result.stderr:
        # winpeekaboo 的日志走 stderr，不影响 stdout 的 JSON
        for line in result.stderr.strip().splitlines():
            print(f"  [wpb-log] {line}")
    return result.stdout.strip()


def capture_screenshot(output_path: str, window: Optional[str] = None) -> str:
    """截图并保存到文件"""
    args = ["image", "--output", output_path]
    if window:
        args += ["--window", window]
    _run_wpb(*args)
    return output_path


def get_uia_elements(window: str) -> list[UIAGroundTruth]:
    """获取窗口的 UIA 元素列表作为 ground truth"""
    output = _run_wpb("list", "elements", "--window", window, "--json")
    if not output:
        print("[WARN] winpeekaboo list elements 返回为空")
        return []

    # 尝试从输出中提取 JSON（可能前面有日志输出）
    json_str = output
    if "{" in output:
        json_str = output[output.index("{"):]
        # 如果返回的是列表，找到第一个 [
        if "[" in output and output.index("[") < output.index("{"):
            json_str = output[output.index("["):]

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"[WARN] 无法解析 JSON: {e}")
        print(f"       原始输出前 200 字符: {output[:200]}")
        return []
    elements = []

    for item in data:
        bounds = item.get("bounds", {})
        x = bounds.get("x", 0)
        y = bounds.get("y", 0)
        w = bounds.get("width", 0)
        h = bounds.get("height", 0)

        # 过滤掉没有面积或不可见的元素
        if w <= 0 or h <= 0:
            continue
        if not item.get("is_visible", True):
            continue

        # 过滤掉面积过小的元素（< 5x5 像素）
        if w < 5 and h < 5:
            continue

        cx = x + w // 2
        cy = y + h // 2

        elements.append(UIAGroundTruth(
            name=item.get("name") or "",
            control_type=item.get("control_type", "Unknown"),
            automation_id=item.get("automation_id") or "",
            bbox=(x, y, w, h),
            center=(cx, cy),
        ))

    return elements


# ═══════════════════════════════════════════════════════════
# 多模态模型调用
# ═══════════════════════════════════════════════════════════

VISION_PROMPT = """\
你是一个 Windows 桌面 UI 分析专家。请仔细观察这张桌面截图，识别出所有可交互的 UI 元素。

对每个元素，请返回以下 JSON 格式的信息：
- name: 元素的名称或文本（如按钮上的文字、输入框的标签等）
- element_type: 元素类型（Button/Edit/Text/MenuItem/CheckBox/RadioButton/ComboBox/ListItem/Tab/Toolbar/Pane/StatusBar/Icon/Link 等）
- bbox: 元素的边界框，格式为 [x1, y1, x2, y2]（左上角和右下角的像素坐标）
- click_point: 建议点击的中心坐标，格式为 [cx, cy]

请严格按以下 JSON 格式返回，不要包含任何其他文字说明：
```json
{
  "elements": [
    {
      "name": "元素名称",
      "element_type": "Button",
      "bbox": [x1, y1, x2, y2],
      "click_point": [cx, cy]
    }
  ],
  "screen_description": "对当前屏幕内容的简要描述"
}
```

注意事项：
1. 坐标必须基于截图的像素尺寸，左上角为 (0, 0)
2. bbox 必须准确框住元素的可见区域
3. click_point 应该是元素的可点击区域中心
4. 尽可能识别所有可见的可交互元素（按钮、输入框、菜单项、链接等）
5. 对于文本标签，如果它看起来是可交互的（如链接），也请包含
"""


async def call_vision_model(image_path: str) -> list[VisionElement]:
    """调用多模态模型分析截图，返回结构化的元素列表"""
    from openai import AsyncOpenAI
    from config import get_settings

    settings = get_settings()
    vision_cfg = settings.vision

    client = AsyncOpenAI(
        base_url=vision_cfg.get("api_base") or None,
        api_key=vision_cfg.get("api_key") or "not-needed",
    )

    # 读取图片并编码
    image_bytes = Path(image_path).read_bytes()
    b64 = base64.b64encode(image_bytes).encode().decode()
    suffix = Path(image_path).suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(suffix, "image/png")

    response = await client.chat.completions.create(
        model=vision_cfg["model"],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }
        ],
        temperature=0.1,  # 低温度以获得更确定的结果
        max_tokens=4096,
    )

    raw_text = response.choices[0].message.content or ""
    return _parse_vision_response(raw_text)


def _parse_vision_response(raw_text: str) -> list[VisionElement]:
    """解析多模态模型的返回，提取元素列表"""
    # 尝试从 markdown 代码块中提取 JSON
    json_text = raw_text

    # 处理 ```json ... ``` 包裹
    if "```json" in json_text:
        json_text = json_text.split("```json", 1)[1]
        json_text = json_text.split("```", 1)[0]
    elif "```" in json_text:
        json_text = json_text.split("```", 1)[1]
        json_text = json_text.split("```", 1)[0]

    json_text = json_text.strip()

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        print(f"[WARN] 无法解析模型返回为 JSON，原始文本:\n{raw_text[:500]}")
        return []

    elements = []
    for item in data.get("elements", []):
        bbox = item.get("bbox", [0, 0, 0, 0])
        click_point = item.get("click_point", [0, 0])

        if len(bbox) != 4 or len(click_point) != 2:
            continue

        elements.append(VisionElement(
            name=item.get("name", ""),
            element_type=item.get("element_type", "Unknown"),
            bbox=(int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])),
            click_point=(int(click_point[0]), int(click_point[1])),
            raw_response=raw_text,
        ))

    return elements


# ═══════════════════════════════════════════════════════════
# 匹配与指标计算
# ═══════════════════════════════════════════════════════════

def _compute_iou(box_a: tuple[int, int, int, int], box_b: tuple[int, int, int, int]) -> float:
    """
    计算 IoU。
    box_a: (x, y, w, h) — UIA 格式
    box_b: (x1, y1, x2, y2) — 视觉模型格式
    """
    # 统一为 (x1, y1, x2, y2)
    ax1, ay1 = box_a[0], box_a[1]
    ax2, ay2 = box_a[0] + box_a[2], box_a[1] + box_a[3]
    bx1, by1 = box_b[0], box_b[1]
    bx2, by2 = box_b[2], box_b[3]

    # 交集
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0

    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    union = area_a + area_b - inter

    return inter / union if union > 0 else 0.0


def _point_in_box(point: tuple[int, int], box: tuple[int, int, int, int]) -> bool:
    """检查点是否在 (x, y, w, h) bbox 内"""
    px, py = point
    bx, by, bw, bh = box
    return bx <= px <= bx + bw and by <= py <= by + bh


def _names_similar(name_a: str, name_b: str) -> bool:
    """简单的名称相似度判断"""
    a = name_a.strip().lower()
    b = name_b.strip().lower()
    if not a or not b:
        return False
    # 完全匹配
    if a == b:
        return True
    # 一个包含另一个
    if a in b or b in a:
        return True
    # 去掉常见后缀后比较
    for suffix in ["按钮", "button", "框", "输入", "edit", "text", "菜单", "menu"]:
        a_clean = a.replace(suffix, "")
        b_clean = b.replace(suffix, "")
        if a_clean and b_clean and (a_clean in b_clean or b_clean in a_clean):
            return True
    return False


def match_elements(
    uia_elements: list[UIAGroundTruth],
    vision_elements: list[VisionElement],
) -> list[MatchResult]:
    """
    将视觉识别的元素与 UIA ground truth 进行匹配。
    策略: 基于空间距离 + 名称相似度的贪心匹配。
    """
    matches: list[MatchResult] = []
    used_vision: set[int] = set()

    for uia in uia_elements:
        best_idx = -1
        best_score = -1.0

        for vi, vision in enumerate(vision_elements):
            if vi in used_vision:
                continue

            # 计算中心点距离
            dx = vision.click_point[0] - uia.center[0]
            dy = vision.click_point[1] - uia.center[1]
            distance = math.sqrt(dx * dx + dy * dy)

            # 距离超过 200px 基本不可能匹配
            if distance > 200:
                continue

            # 名称加分
            name_bonus = 50.0 if _names_similar(uia.name, vision.name) else 0.0

            # 综合得分: 名称加分 - 距离（越近越好）
            score = name_bonus - distance

            if score > best_score:
                best_score = score
                best_idx = vi

        if best_idx >= 0 and best_score > -150:  # 阈值：距离 200px 以内 + 名称加分
            used_vision.add(best_idx)
            vision = vision_elements[best_idx]

            # 计算指标
            dx = vision.click_point[0] - uia.center[0]
            dy = vision.click_point[1] - uia.center[1]
            center_distance = math.sqrt(dx * dx + dy * dy)
            bbox_iou = _compute_iou(uia.bbox, vision.bbox)
            click_hit = _point_in_box(vision.click_point, uia.bbox)
            name_match = _names_similar(uia.name, vision.name)

            matches.append(MatchResult(
                uia_element=uia,
                vision_element=vision,
                center_distance=center_distance,
                bbox_iou=bbox_iou,
                click_hit=click_hit,
                name_match=name_match,
            ))

    return matches


def compute_report(
    window_title: str,
    uia_elements: list[UIAGroundTruth],
    vision_elements: list[VisionElement],
    matches: list[MatchResult],
) -> TestReport:
    """计算聚合指标并生成报告"""
    report = TestReport(
        window_title=window_title,
        timestamp=datetime.now().isoformat(),
        uia_element_count=len(uia_elements),
        vision_element_count=len(vision_elements),
        matched_count=len(matches),
        matches=matches,
    )

    if not matches:
        return report

    distances = sorted([m.center_distance for m in matches])
    ious = [m.bbox_iou for m in matches]
    click_hits = [m.click_hit for m in matches]
    name_matches = [m.name_match for m in matches]

    report.avg_center_distance = sum(distances) / len(distances)
    report.median_center_distance = distances[len(distances) // 2]
    report.avg_iou = sum(ious) / len(ious)
    report.click_hit_rate = sum(click_hits) / len(click_hits)
    report.name_match_rate = sum(name_matches) / len(name_matches)

    report.distance_under_10px = sum(1 for d in distances if d < 10) / len(distances)
    report.distance_under_20px = sum(1 for d in distances if d < 20) / len(distances)
    report.distance_under_50px = sum(1 for d in distances if d < 50) / len(distances)

    return report


# ═══════════════════════════════════════════════════════════
# 报告输出
# ═══════════════════════════════════════════════════════════

def print_report(report: TestReport) -> None:
    """打印测试报告到终端"""
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()

    # 总体指标
    summary = (
        f"[bold]窗口:[/bold] {report.window_title}\n"
        f"[bold]时间:[/bold] {report.timestamp}\n"
        f"[bold]UIA 元素数:[/bold] {report.uia_element_count}\n"
        f"[bold]视觉识别元素数:[/bold] {report.vision_element_count}\n"
        f"[bold]匹配元素数:[/bold] {report.matched_count}\n\n"
        f"[bold cyan]── 核心指标 ──[/bold cyan]\n"
        f"[bold]平均中心距离:[/bold] {report.avg_center_distance:.1f}px\n"
        f"[bold]中位中心距离:[/bold] {report.median_center_distance:.1f}px\n"
        f"[bold]平均 IoU:[/bold] {report.avg_iou:.3f}\n"
        f"[bold]点击命中率:[/bold] {report.click_hit_rate:.1%}\n"
        f"[bold]名称匹配率:[/bold] {report.name_match_rate:.1%}\n\n"
        f"[bold cyan]── 距离分布 ──[/bold cyan]\n"
        f"[bold]< 10px:[/bold] {report.distance_under_10px:.1%}\n"
        f"[bold]< 20px:[/bold] {report.distance_under_20px:.1%}\n"
        f"[bold]< 50px:[/bold] {report.distance_under_50px:.1%}"
    )
    console.print(Panel(summary, title="🧪 Vision BBox Precision Report", border_style="blue"))

    # 逐元素详情
    if report.matches:
        table = Table(title="逐元素匹配详情", show_lines=True)
        table.add_column("UIA 名称", style="cyan", max_width=20)
        table.add_column("UIA 类型", style="green", max_width=10)
        table.add_column("视觉名称", style="yellow", max_width=20)
        table.add_column("视觉类型", style="magenta", max_width=10)
        table.add_column("中心距离(px)", style="red", justify="right")
        table.add_column("IoU", style="blue", justify="right")
        table.add_column("点击命中", style="green")
        table.add_column("名称匹配", style="green")

        for m in report.matches:
            uia_name = m.uia_element.name[:18] + ".." if len(m.uia_element.name) > 20 else m.uia_element.name
            vis_name = m.vision_element.name[:18] + ".." if len(m.vision_element.name) > 20 else m.vision_element.name

            click_icon = "✅" if m.click_hit else "❌"
            name_icon = "✅" if m.name_match else "❌"

            table.add_row(
                uia_name or "-",
                m.uia_element.control_type,
                vis_name or "-",
                m.vision_element.element_type,
                f"{m.center_distance:.1f}",
                f"{m.bbox_iou:.3f}",
                click_icon,
                name_icon,
            )

        console.print(table)

    # 精度评估
    console.print()
    if report.click_hit_rate >= 0.9 and report.avg_center_distance < 20:
        console.print("[bold green]✅ 结论: 多模态模型的定位精度可以满足 fallback 需求[/bold green]")
    elif report.click_hit_rate >= 0.7:
        console.print("[bold yellow]⚠️ 结论: 精度尚可，但需要增加点击容错策略（如点击后验证）[/bold yellow]")
    else:
        console.print("[bold red]❌ 结论: 精度不足，不适合直接用于 fallback，需要更精细的 prompt 或更强模型[/bold red]")


def save_report(report: TestReport, save_dir: Path, screenshot_path: Optional[str] = None) -> None:
    """保存详细报告到文件"""
    save_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = save_dir / f"report_{timestamp}.json"

    # 序列化
    data = {
        "window_title": report.window_title,
        "timestamp": report.timestamp,
        "uia_element_count": report.uia_element_count,
        "vision_element_count": report.vision_element_count,
        "matched_count": report.matched_count,
        "metrics": {
            "avg_center_distance": round(report.avg_center_distance, 2),
            "median_center_distance": round(report.median_center_distance, 2),
            "avg_iou": round(report.avg_iou, 4),
            "click_hit_rate": round(report.click_hit_rate, 4),
            "name_match_rate": round(report.name_match_rate, 4),
            "distance_under_10px": round(report.distance_under_10px, 4),
            "distance_under_20px": round(report.distance_under_20px, 4),
            "distance_under_50px": round(report.distance_under_50px, 4),
        },
        "matches": [
            {
                "uia": {
                    "name": m.uia_element.name,
                    "control_type": m.uia_element.control_type,
                    "bbox": list(m.uia_element.bbox),
                    "center": list(m.uia_element.center),
                },
                "vision": {
                    "name": m.vision_element.name,
                    "element_type": m.vision_element.element_type,
                    "bbox": list(m.vision_element.bbox),
                    "click_point": list(m.vision_element.click_point),
                },
                "center_distance": round(m.center_distance, 2),
                "bbox_iou": round(m.bbox_iou, 4),
                "click_hit": m.click_hit,
                "name_match": m.name_match,
            }
            for m in report.matches
        ],
    }

    if screenshot_path:
        data["screenshot_file"] = screenshot_path

    report_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📄 报告已保存: {report_file}")


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════

async def run_test(
    window: str,
    save_dir: Optional[Path] = None,
) -> TestReport:
    """
    执行一次完整的精度测试。

    Args:
        window: 目标窗口标题
        save_dir: 可选，保存截图和报告的目录
    """
    import tempfile

    print(f"\n🔍 开始测试窗口: {window}")
    print("=" * 60)

    # 1. 截图
    tmp_dir = save_dir or Path(tempfile.mkdtemp(prefix="vision_bbox_"))
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    screenshot_path = str(tmp_dir / "screenshot.png")
    print(f"📸 截图中... → {screenshot_path}")
    capture_screenshot(screenshot_path, window=window)
    print("✅ 截图完成")

    # 2. 获取 UIA ground truth
    print("🔍 获取 UIA 元素 (ground truth)...")
    uia_elements = get_uia_elements(window)
    print(f"   找到 {len(uia_elements)} 个可见 UIA 元素")

    if not uia_elements:
        print("⚠️ 没有找到 UIA 元素，尝试获取所有元素（包括不可见的）...")
        # 这种情况下可能 UIA 无法连接，直接跳过
        return TestReport(
            window_title=window,
            timestamp=datetime.now().isoformat(),
        )

    # 打印 UIA 元素摘要
    for elem in uia_elements[:10]:
        name = elem.name[:25] + ".." if len(elem.name) > 25 else elem.name
        print(f"   - {elem.control_type:12s} | {name or '-':28s} | center=({elem.center[0]}, {elem.center[1]})")
    if len(uia_elements) > 10:
        print(f"   ... 还有 {len(uia_elements) - 10} 个元素")

    # 3. 调用多模态模型
    print("\n🤖 调用多模态模型分析截图...")
    vision_elements = await call_vision_model(screenshot_path)
    print(f"   识别到 {len(vision_elements)} 个元素")

    for elem in vision_elements[:10]:
        name = elem.name[:25] + ".." if len(elem.name) > 25 else elem.name
        print(f"   - {elem.element_type:12s} | {name or '-':28s} | click=({elem.click_point[0]}, {elem.click_point[1]})")
    if len(vision_elements) > 10:
        print(f"   ... 还有 {len(vision_elements) - 10} 个元素")

    # 4. 匹配与计算指标
    print("\n📊 匹配元素并计算指标...")
    matches = match_elements(uia_elements, vision_elements)
    report = compute_report(window, uia_elements, vision_elements, matches)

    # 5. 输出报告
    print()
    print_report(report)

    # 6. 保存
    if save_dir:
        save_report(report, save_dir, screenshot_path=screenshot_path)

    return report


async def run_multi_test(
    window: str,
    runs: int = 3,
    save_dir: Optional[Path] = None,
) -> None:
    """多次测试取平均"""
    reports: list[TestReport] = []

    for i in range(runs):
        print(f"\n{'=' * 60}")
        print(f"🔄 第 {i + 1}/{runs} 轮测试")
        print(f"{'=' * 60}")

        report = await run_test(window, save_dir=save_dir)
        reports.append(report)

        if i < runs - 1:
            print("\n⏳ 等待 2 秒后进行下一轮...")
            time.sleep(2)

    # 汇总
    if len(reports) > 1:
        print("\n" + "=" * 60)
        print("📈 多轮测试汇总")
        print("=" * 60)

        avg_distance = sum(r.avg_center_distance for r in reports) / len(reports)
        avg_iou = sum(r.avg_iou for r in reports) / len(reports)
        avg_hit_rate = sum(r.click_hit_rate for r in reports) / len(reports)

        print(f"  平均中心距离: {avg_distance:.1f}px")
        print(f"  平均 IoU: {avg_iou:.3f}")
        print(f"  平均点击命中率: {avg_hit_rate:.1%}")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="多模态模型 BBox 精度测试")
    parser.add_argument("--window", type=str, help="目标窗口标题（部分匹配）")
    parser.add_argument("--active-window", action="store_true", help="测试当前活动窗口")
    parser.add_argument("--save-dir", type=str, help="保存截图和报告的目录")
    parser.add_argument("--runs", type=int, default=1, help="测试轮次（默认 1）")

    args = parser.parse_args()

    if not args.window and not args.active_window:
        parser.error("请指定 --window 或 --active-window")

    # 确定窗口
    window = args.window
    if args.active_window:
        # 获取当前活动窗口标题
        output = _run_wpb("list", "windows", "--json")
        windows = json.loads(output)
        # 找到第一个可见的非空标题窗口
        for w in windows:
            if w.get("is_visible") and w.get("title"):
                window = w["title"]
                break
        if not window:
            print("❌ 无法确定当前活动窗口")
            sys.exit(1)
        print(f"🎯 当前活动窗口: {window}")

    save_dir = Path(args.save_dir) if args.save_dir else None

    import asyncio
    if args.runs > 1:
        asyncio.run(run_multi_test(window, runs=args.runs, save_dir=save_dir))
    else:
        asyncio.run(run_test(window, save_dir=save_dir))


if __name__ == "__main__":
    main()
