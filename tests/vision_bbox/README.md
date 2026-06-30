# Vision BBox Precision Test

测试多模态模型在 Windows 桌面截图上的 UI 元素定位精度。

## 目的

评估多模态模型返回的 bbox / click_point 与 UIA (pywinauto) ground truth 之间的差距，为后续 **UIA fallback 到视觉定位** 的设计提供数据支撑。

## 指标

| 指标 | 含义 |
|------|------|
| **Center Distance** | 视觉返回的 click_point 与 UIA 中心点的像素距离 |
| **BBox IoU** | 视觉 bbox 与 UIA bbox 的交并比 |
| **Click Hit Rate** | 视觉 click_point 是否落在 UIA bbox 内（1=命中） |
| **Element Match Rate** | 视觉识别出的元素能成功与 UIA 元素匹配的比例 |

## 使用

```bash
# 从项目根目录运行
cd c:\Users\z00490ns\Code\desktop-agent

# 测试指定窗口（默认记事本）
python -m tests.vision_bbox.test_bbox_precision --window "记事本"

# 测试当前活动窗口
python -m tests.vision_bbox.test_bbox_precision --active-window

# 保存截图和详细结果到文件
python -m tests.vision_bbox.test_bbox_precision --window "记事本" --save-dir ./tests/vision_bbox/output

# 多次测试取平均
python -m tests.vision_bbox.test_bbox_precision --window "记事本" --runs 3
```

## 依赖

- 项目本身的依赖（openai, pydantic-settings, yaml）
- winpeekaboo 包（已安装在项目中）
