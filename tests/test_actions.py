from __future__ import annotations

import pytest

from tools.actions import run_actions


@pytest.mark.asyncio
async def test_run_actions_rejects_invalid_payload() -> None:
    with pytest.raises(ValueError, match="JSON 数组"):
        await run_actions("not-json")


@pytest.mark.asyncio
async def test_run_actions_fails_when_a_child_action_is_unsupported() -> None:
    with pytest.raises(RuntimeError, match="子动作失败"):
        await run_actions('[{"tool":"unsupported","args":{}}]')
