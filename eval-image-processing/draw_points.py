import argparse
import ast
from PIL import Image, ImageDraw, ImageFont

def draw_points_on_image(
    image_path: str,
    points: list,
    output_path: str = "output.png",
    radius: int = 20,
    outline_color: tuple = (255, 255, 255),
    outline_width: int = 3,
    swap_xy: bool = False          # ← 新增：是否交换 x/y
):
    image = Image.open(image_path).convert("RGBA")
    draw = ImageDraw.Draw(image)

    img_width, img_height = image.size
    print(f"\n🖼️  图片尺寸: 宽={img_width}px, 高={img_height}px")
    if swap_xy:
        print(f"🔄  已启用 swap_xy：坐标 (cx, cy) → 交换为 (cy, cx)")

    # 尝试加载字体
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
    except:
        try:
            font = ImageFont.truetype("arial.ttf", 24)
        except:
            font = ImageFont.load_default()

    # 颜色池
    colors = [
        (255,  60,  60),  # 红
        ( 60, 180,  75),  # 绿
        ( 67, 133, 255),  # 蓝
        (255, 165,   0),  # 橙
        (180,  60, 255),  # 紫
        (  0, 210, 210),  # 青
        (255, 220,   0),  # 黄
        (255, 105, 180),  # 粉
    ]

    normalized = _normalize_points(points, swap_xy=swap_xy)

    skipped = 0
    for i, point in enumerate(normalized):
        x     = point["x"]
        y     = point["y"]
        label = point.get("label", str(i + 1))
        c     = colors[i % len(colors)]

        # 边界检查
        if x < 0 or x >= img_width or y < 0 or y >= img_height:
            print(f"  ⚠️  [{i+1}] '{label}': ({x}, {y}) 超出图片边界，已跳过！")
            skipped += 1
            continue

        # 安全裁剪
        left   = max(x - radius, 0)
        top    = max(y - radius, 0)
        right  = min(x + radius, img_width  - 1)
        bottom = min(y + radius, img_height - 1)

        # 绘制填充圆 + 边框
        draw.ellipse(
            [left, top, right, bottom],
            fill=c,
            outline=outline_color,
            width=outline_width
        )

        # 绘制十字准星（黑色描边 + 白色线）
        cross = radius // 2
        for dx, dy, color, w in [
            (0, 0, (0, 0, 0), 3),           # 黑色描边
            (0, 0, outline_color, 1),        # 白色线
        ]:
            draw.line([(max(x - cross, 0), y), (min(x + cross, img_width - 1), y)],  fill=color, width=w)
            draw.line([(x, max(y - cross, 0)), (x, min(y + cross, img_height - 1))], fill=color, width=w)

        # 绘制标签（自动防止超出边界）
        tag   = f"[{i+1}] {label}"
        tag_x = x + radius + 5 if (x + radius + 60) < img_width  else x - radius - 65
        tag_y = y - radius      if (y - radius)      > 0          else y + radius
        draw.text(
            (tag_x, tag_y),
            tag,
            fill=c,
            font=font,
            stroke_width=2,
            stroke_fill=(0, 0, 0)
        )

        print(f"  ✅ [{i+1}] '{label}': ({x}, {y}) 已绘制")

    image.save(output_path)
    print(f"\n🎉 输出图片已保存: {output_path}")
    if skipped > 0:
        print(f"⚠️  共跳过 {skipped} 个超出边界的点")

    try:
        image.show()
    except:
        print("💡 请手动打开输出图片查看结果")

def _normalize_points(points: list, swap_xy: bool = False) -> list:
    """
    统一格式化坐标，支持 swap_xy 交换 x/y。
    """
    normalized = []
    for i, p in enumerate(points):
        if isinstance(p, (tuple, list)) and len(p) == 2:
            x, y = int(p[0]), int(p[1])
        elif isinstance(p, dict):
            if "coordinates" in p:
                x, y = int(p["coordinates"][0]), int(p["coordinates"][1])
            else:
                x, y = int(p["x"]), int(p["y"])
            label = p.get("label", str(i + 1))
        else:
            raise ValueError(f"❌ 不支持的坐标格式: {p}")

        label = p.get("label", str(i + 1)) if isinstance(p, dict) else str(i + 1)

        # ✅ 关键修复：交换 x/y
        if swap_xy:
            x, y = y, x

        normalized.append({"label": label, "x": x, "y": y})
    return normalized

def parse_coords(value: str) -> list:
    try:
        result = ast.literal_eval(value)
        if not isinstance(result, list):
            raise ValueError("输入必须是一个列表")
        return result
    except Exception as e:
        raise argparse.ArgumentTypeError(f"坐标格式解析失败: {e}")

def parse_args():
    parser = argparse.ArgumentParser(description="在图片上动态绘制多个像素坐标圆点")
    parser.add_argument("image_path",           type=str,           help="输入图片路径")
    parser.add_argument("-c", "--coords",       type=parse_coords,  required=True, help="坐标列表")
    parser.add_argument("-o", "--output",       type=str,           default="output.png", help="输出图片路径")
    parser.add_argument("-r", "--radius",       type=int,           default=20,    help="圆的半径（默认: 20）")
    parser.add_argument("--swap-xy",            action="store_true",               help="交换 x/y 坐标（竖屏坐标 → 横屏图片）")
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    print(f"\n📌 共读取到 {len(args.coords)} 个坐标点")
    draw_points_on_image(
        image_path  = args.image_path,
        points      = args.coords,
        output_path = args.output,
        radius      = args.radius,
        swap_xy     = args.swap_xy,
    )