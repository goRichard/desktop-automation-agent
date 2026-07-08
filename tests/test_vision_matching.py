from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tools import vision
from tools.registry import list_tools
from tools.uia import UIAResponseError, normalize_element_records, parse_element_records


def _element(
    key: str,
    name: str,
    control_type: str,
    *,
    automation_id: str = "",
    x: int = 0,
) -> dict:
    return {
        "element_key": key,
        "name": name,
        "control_type": control_type,
        "automation_id": automation_id,
        "bounds": {"x": x, "y": 10, "width": 100, "height": 30},
        "center": (x + 50, 25),
        "is_visible": True,
        "is_enabled": True,
    }


@pytest.fixture(autouse=True)
def reset_uia_cache() -> None:
    vision.invalidate_uia_cache()
    yield
    vision.invalidate_uia_cache()


def test_raw_list_elements_is_not_agent_facing() -> None:
    assert "list_elements" not in list_tools()
    assert "inspect_elements" in list_tools()


def test_uia_normalization_handles_control_type_and_bounds() -> None:
    elements = normalize_element_records(json.dumps([{
        "name": "Body",
        "controlType": "ControlType.Document",
        "automationId": "BodyEditor",
        "bounds": [10, 20, 210, 120],
    }]))

    assert elements[0]["element_key"] == "E0001"
    assert elements[0]["control_type"] == "Document"
    assert elements[0]["automation_id"] == "BodyEditor"
    assert elements[0]["bounds"] == {
        "x": 10,
        "y": 20,
        "width": 200,
        "height": 100,
    }
    assert elements[0]["center"] == (110, 70)


def test_uia_parser_accepts_bom_and_log_prefix() -> None:
    elements = parse_element_records(
        '\ufeffWinPeekaboo INFO: scanning window "Message"\n'
        '[{"name":"Send","control_type":"Button"}]\n'
    )

    assert elements[0]["name"] == "Send"


def test_uia_parser_reports_empty_output_explicitly() -> None:
    with pytest.raises(UIAResponseError, match="empty output"):
        parse_element_records("  \r\n")


@pytest.mark.asyncio
async def test_interactive_scan_uses_cli_and_keeps_outlook_document(monkeypatch) -> None:
    activations = []

    async def fake_activate(title):
        activations.append(title)
        return "ok"

    async def fake_list_elements(window):
        return json.dumps([
            {
                "name": "Body",
                "control_type": "Document",
                "bounds": {"x": 0, "y": 0, "width": 400, "height": 300},
            },
            {
                "name": "Disabled",
                "control_type": "Button",
                "is_enabled": False,
                "bounds": {"x": 0, "y": 0, "width": 100, "height": 30},
            },
        ])

    monkeypatch.setattr(vision, "window_activate", fake_activate)
    monkeypatch.setattr(vision, "list_elements", fake_list_elements)

    elements = await vision._get_interactive_elements("Untitled - Message")

    assert activations == ["Untitled - Message"]
    assert [element["name"] for element in elements] == ["Body"]


@pytest.mark.asyncio
async def test_compact_inspection_limits_fields_and_reports_metrics(monkeypatch) -> None:
    async def fake_activate(title):
        return "ok"

    async def fake_list_elements(window):
        return json.dumps([
            {
                "name": f"Button {index}",
                "control_type": "Button",
                "automation_id": f"button-{index}",
                "bounds": {"x": index * 10, "y": 10, "width": 50, "height": 20},
                "extra_large_property": "x" * 500,
            }
            for index in range(40)
        ])

    monkeypatch.setattr(vision, "window_activate", fake_activate)
    monkeypatch.setattr(vision, "list_elements", fake_list_elements)

    result = await vision.inspect_elements("Window", limit=5)

    assert "raw=40" in result
    assert "interactive=40" in result
    assert "returned=5" in result
    assert "omitted=35" in result
    assert result.count("\n[") == 5
    assert "bounds" not in result
    assert "extra_large_property" not in result


@pytest.mark.asyncio
async def test_interactive_scan_uses_short_cache_and_can_invalidate(monkeypatch) -> None:
    scans = 0

    async def fake_activate(title):
        return "ok"

    async def fake_list_elements(window):
        nonlocal scans
        scans += 1
        return json.dumps([{
            "name": "Send",
            "control_type": "Button",
            "bounds": {"x": 0, "y": 0, "width": 100, "height": 30},
        }])

    monkeypatch.setattr(vision, "window_activate", fake_activate)
    monkeypatch.setattr(vision, "list_elements", fake_list_elements)

    await vision._get_interactive_elements("Window")
    await vision._get_interactive_elements("Window")
    assert scans == 1

    vision.invalidate_uia_cache("Window")
    await vision._get_interactive_elements("Window")
    assert scans == 2


def test_deterministic_match_is_normalized_and_prefers_specific_name() -> None:
    elements = [
        _element("E0001", "Email", "Button"),
        _element("E0002", "New Email", "Button", x=120),
    ]

    matched = vision._simple_match_element(elements, "NEW email button")

    assert matched["element_key"] == "E0002"


def test_deterministic_match_rejects_ambiguous_duplicate_names() -> None:
    elements = [
        _element("E0001", "Send", "Button"),
        _element("E0002", "Send", "Button", x=120),
    ]

    assert vision._simple_match_element(elements, "Send") is None


def test_automation_id_rejects_duplicate_matches() -> None:
    elements = [
        _element("E0001", "First", "Button", automation_id="Send"),
        _element("E0002", "Second", "Button", automation_id="send", x=120),
    ]

    assert vision._match_by_automation_id(elements, "SEND") is None


@pytest.mark.asyncio
async def test_semantic_match_returns_unique_element_key(monkeypatch) -> None:
    elements = [
        _element("E0001", "", "Edit"),
        _element("E0002", "", "Edit", x=120),
    ]

    class FakeClient:
        async def chat(self, _):
            return SimpleNamespace(content="E0002")

    monkeypatch.setattr(vision, "get_llm_client", lambda: FakeClient())

    matched = await vision._llm_select_element(elements, "recipient input")

    assert matched["element_key"] == "E0002"


@pytest.mark.asyncio
async def test_semantic_match_sends_at_most_twelve_candidates(monkeypatch) -> None:
    elements = [
        _element(f"E{index:04d}", f"Candidate {index}", "Button", x=index * 10)
        for index in range(1, 31)
    ]
    captured = {}

    class FakeClient:
        async def chat(self, messages):
            captured["prompt"] = messages[-1]["content"]
            return SimpleNamespace(content="NOT_FOUND")

    monkeypatch.setattr(vision, "get_llm_client", lambda: FakeClient())

    assert await vision._llm_select_element(elements, "unmatched action") is None
    candidate_lines = [
        line for line in captured["prompt"].splitlines() if line.startswith("- [")
    ]
    assert len(candidate_lines) == 12


@pytest.mark.asyncio
async def test_explicit_automation_id_never_uses_visual_fallback(monkeypatch) -> None:
    class ForbiddenClient:
        def __getattr__(self, name):
            raise AssertionError(f"Vision fallback must not be called: {name}")

    monkeypatch.setattr(vision, "get_llm_client", lambda: ForbiddenClient())

    matched = await vision._locate_element(
        target="Send",
        window="Untitled - Message",
        elements=[_element("E0001", "Send", "Button", automation_id="Other")],
        automation_id="ExpectedSend",
    )

    assert matched is None


def test_invalid_center_outside_bounds_is_rejected() -> None:
    element = _element("E0001", "Send", "Button")
    element["center"] = (500, 500)

    assert vision._validated_click_point(element) is None


@pytest.mark.asyncio
async def test_find_and_click_refuses_invalid_coordinates(monkeypatch) -> None:
    element = _element("E0001", "Send", "Button")
    element["center"] = (500, 500)

    async def fake_locate(*_, **__):
        return {**element, "source": "UIA"}

    async def forbidden_click(**_):
        raise AssertionError("Invalid coordinates must not be clicked")

    monkeypatch.setattr(vision, "_locate_element", fake_locate)
    monkeypatch.setattr(vision, "click", forbidden_click)

    result = await vision.find_and_click("Send", window="Message")

    assert "坐标无效" in result


@pytest.mark.asyncio
async def test_find_and_click_can_skip_new_window_polling(monkeypatch) -> None:
    element = _element("E0001", "File name", "Edit")
    clicks = []

    async def fake_locate(*_, **__):
        return {**element, "source": "UIA"}

    async def fake_click(**kwargs):
        clicks.append(kwargs)
        return "ok"

    async def forbidden_snapshot():
        raise AssertionError("Window snapshot must be skipped")

    monkeypatch.setattr(vision, "_locate_element", fake_locate)
    monkeypatch.setattr(vision, "click", fake_click)
    monkeypatch.setattr(vision, "_snapshot_windows", forbidden_snapshot)

    result = await vision.find_and_click(
        "File name",
        window="Insert File",
        detect_new_window=False,
    )

    assert "成功点击" in result
    assert clicks == [{"on": "50,25", "window": "Insert File"}]


@pytest.mark.asyncio
async def test_new_window_detection_preserves_source_window_process(
    monkeypatch,
) -> None:
    captured = {}

    async def no_sleep(_):
        return None

    def fake_wait(before, source_window, timeout_seconds):
        captured.update({
            "before": before,
            "source_window": source_window,
            "timeout_seconds": timeout_seconds,
        })
        return "Untitled - Message"

    async def fake_activate(title):
        captured["activated"] = title
        return "ok"

    monkeypatch.setattr(vision.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(vision, "wait_for_new_window", fake_wait)
    monkeypatch.setattr(vision, "window_activate", fake_activate)

    title = await vision._detect_and_activate_new_window(
        {"1": {"title": "Inbox - Outlook"}},
        source_window="Inbox - Outlook",
    )

    assert title == "Untitled - Message"
    assert captured["source_window"] == "Inbox - Outlook"
    assert captured["activated"] == "Untitled - Message"


@pytest.mark.asyncio
async def test_batch_automation_id_miss_does_not_use_model(monkeypatch) -> None:
    async def fake_elements(_):
        return [_element("E0001", "Send", "Button", automation_id="Other")]

    async def forbidden_capture(**_):
        raise AssertionError("Strict automation_id miss must not use Vision")

    monkeypatch.setattr(vision, "_get_interactive_elements", fake_elements)
    monkeypatch.setattr(vision, "capture_image", forbidden_capture)

    result = await vision.batch_locate_elements(
        json.dumps([{
            "target": "Send",
            "automation_id": "ExpectedSend",
        }]),
        window="Message",
    )

    assert "未找到" in result


@pytest.mark.asyncio
async def test_coordinate_capture_uses_full_desktop_after_activation(
    monkeypatch,
) -> None:
    calls = []

    async def fake_activate(title):
        calls.append(("activate", title))
        return "ok"

    async def fake_capture(output, window=None):
        calls.append(("capture", output, window))
        return output

    async def no_sleep(_):
        return None

    monkeypatch.setattr(vision, "window_activate", fake_activate)
    monkeypatch.setattr(vision, "capture_image", fake_capture)
    monkeypatch.setattr(vision.asyncio, "sleep", no_sleep)

    path = await vision._capture_for_coordinates("Message", "coordinate-test")

    assert calls[0] == ("activate", "Message")
    assert calls[1] == ("capture", path, None)


def test_scaled_vision_point_must_remain_inside_screenshot(tmp_path) -> None:
    from PIL import Image

    image_path = tmp_path / "screen.png"
    Image.new("RGB", (200, 100)).save(image_path)

    assert vision._scale_vision_point(
        str(image_path),
        (50, 25),
        2.0,
        2.0,
    ) == (100, 50)
    assert vision._scale_vision_point(
        str(image_path),
        (150, 25),
        2.0,
        2.0,
    ) is None
