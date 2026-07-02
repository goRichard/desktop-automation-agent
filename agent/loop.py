"""
Agent Loop 核心：多轮工具调用循环
User Input → Context Assembly → LLM Call → Tool Dispatch → Observation → Loop / Final Response
所有消息自动写入 SQLite
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Optional

from config import get_settings
from llm import TokenUsage, capture_token_usage, get_llm_client
from memory import (
    MessageRole,
    create_session,
    save_message,
    update_session_title,
)
from runtime import (
    EventBus,
    RunCancelled,
    RunController,
    RunStatus,
    desktop_execution_lock,
    get_runtime_persistence,
)
from tools import get_all_schemas

from . import context as ctx
from . import tool_dispatcher
from .planner import TaskPlan, TaskStep, TaskStatus


_PLAN_OBSERVATION_TOOLS = frozenset({
    "analyze_image",
    "analyze_screen",
    "browser_get_state",
    "browser_screenshot",
    "capture_image",
    "list_apps",
    "list_elements",
    "list_screens",
    "list_windows",
    "path_exists",
    "sleep",
})
_MAX_POLICY_CORRECTIONS = 1


class AgentLoop:
    """
    Agent 主循环
    每次 run() 调用对应一个 Agent 思考-执行-观察的完整过程
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        event_bus: Optional[EventBus] = None,
    ):
        self.settings = get_settings()
        self.llm = get_llm_client()

        # 创建或复用会话
        if session_id is None:
            session = create_session()
            self.session_id = session.id
        else:
            self.session_id = session_id

        self._turn_count = 0
        self._plan: Optional[TaskPlan] = None  # P1-3: 计划状态追踪
        self._pending_token_usage: list[TokenUsage] = []
        self.event_bus = event_bus or EventBus()
        self.current_run: Optional[RunController] = None

    async def generate_skill_plan(
        self,
        user_input: str,
        skill_content: str,
    ) -> str:
        """
        Plan-First: 根据 Skill 步骤 + 用户输入生成具体化执行计划。
        返回编号步骤列表文本（供用户确认）。
        """
        available_tools = ", ".join(
            schema["function"]["name"] for schema in get_all_schemas()
        )
        prompt = (
            f"你是任务规划专家。根据以下 Skill 执行规范和用户请求，生成一份具体可执行的步骤计划。\n\n"
            f"## Skill 执行规范\n{skill_content}\n\n"
            f"## 用户请求\n{user_input}\n\n"
            f"## 可用工具名\n{available_tools}\n\n"
            f"要求：\n"
            f"1. 将 Skill 步骤具体化，将用户提供的参数（如收件人、主题、附件路径等）填入对应位置\n"
            f"2. 每个步骤必须明确指定使用的工具名，且只能使用上面列出的准确名称\n"
            f"3. 步骤描述格式：`步骤描述（工具名）`，例如：`批量定位 To/Subject/Body 控件（batch_locate_elements）`\n"
            f"4. 仅返回编号列表，不要标题、不要 markdown 标记，格式如下：\n"
            f"1. 启动 Outlook 并新建邮件（app_launch + hotkey Ctrl+N）\n"
            f"2. 定位新邮件窗口（list_windows）\n"
            f"3. 批量定位邮件编辑区控件（batch_locate_elements）\n"
            f"4. 批量填写收件人、主题、正文（run_actions）"
        )

        messages = [
            {"role": "system", "content": "你是任务规划助手，只返回编号步骤列表。"},
            {"role": "user", "content": prompt},
        ]

        with capture_token_usage(self._pending_token_usage.append):
            response = await self.llm.chat(messages)
        return (response.content or "").strip()

    async def run(
        self,
        user_input: str,
        on_token: Optional[Any] = None,
        on_tool_call: Optional[Any] = None,
        on_tool_result: Optional[Any] = None,
        confirmed_plan: Optional[str] = None,
    ) -> str:
        """
        执行一轮对话（含多步工具调用）。
        是 run_stream() 的便捷包装：收集流式 token 后返回完整文本。

        参数：
            user_input: 用户输入的文字
            on_token: 回调函数，收到流式 token 时调用 on_token(token: str)
            on_tool_call: 回调函数，发起工具调用时调用 on_tool_call(name, args)
            on_tool_result: 回调函数，工具返回结果时调用 on_tool_result(name, result)
            confirmed_plan: Plan-First 模式下用户已确认的执行计划

        返回：
            最终的文本回复内容
        """
        full_response = ""
        async for token in self.run_stream(
            user_input=user_input,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
            confirmed_plan=confirmed_plan,
            on_token=on_token,
        ):
            full_response += token
        return full_response

    async def run_stream(
        self,
        user_input: str,
        on_tool_call: Optional[Any] = None,
        on_tool_result: Optional[Any] = None,
        confirmed_plan: Optional[str] = None,
        on_token: Optional[Any] = None,
        run_id: Optional[str] = None,
        run_controller: Optional[RunController] = None,
    ) -> AsyncGenerator[str, None]:
        """创建受控 Run，串行占用桌面并转发执行输出。"""
        controller = run_controller or RunController(
            session_id=self.session_id,
            user_input=user_input,
            event_bus=self.event_bus,
            run_id=run_id,
            persistence=get_runtime_persistence(),
        )
        if controller.state.session_id != self.session_id:
            raise ValueError("RunController session does not match AgentLoop session")
        self.current_run = controller
        await controller.initialize()
        pending_usage = self._pending_token_usage
        self._pending_token_usage = []
        for usage in pending_usage:
            await controller.record_model_usage(usage)

        try:
            async with desktop_execution_lock.hold(controller):
                await controller.checkpoint()
                await controller.transition(RunStatus.PREPARING)
                await controller.checkpoint()
                await controller.transition(RunStatus.RUNNING)
                await controller.checkpoint()
                with capture_token_usage(controller.record_model_usage):
                    async for token in self._execute_stream(
                        user_input=user_input,
                        on_tool_call=on_tool_call,
                        on_tool_result=on_tool_result,
                        confirmed_plan=confirmed_plan,
                        on_token=on_token,
                        controller=controller,
                    ):
                        yield token
        except RunCancelled as error:
            message = f"执行已取消：{error}"
            controller.state.output += message
            await controller.emit("run.output", {"delta": message})
            if controller.persistence:
                await controller.persistence.save_run(controller.state)
            yield message
        except Exception as error:
            await controller.fail(f"{type(error).__name__}: {error}")
            raise
        finally:
            if not controller.state.is_terminal:
                await controller.cancel("Run consumer disconnected before completion")

    async def _execute_stream(
        self,
        user_input: str,
        on_tool_call: Optional[Any] = None,
        on_tool_result: Optional[Any] = None,
        confirmed_plan: Optional[str] = None,
        on_token: Optional[Any] = None,
        controller: Optional[RunController] = None,
        allowed_tool_names: Optional[set[str]] = None,
        finalize_run: bool = True,
    ) -> AsyncGenerator[str, None]:
        """
        流式版本：先用非流式检测工具调用（多轮），最终回复用流式输出。
        confirmed_plan: Plan-First 模式下用户已确认的执行计划，传入后使用建立上下文的约束注入。
        yield str token
        """
        if controller is None:
            raise RuntimeError("RunController is required")
        if self._turn_count == 0:
            title = user_input[:20] + ("..." if len(user_input) > 20 else "")
            update_session_title(self.session_id, title)
        self._turn_count += 1

        # P1-3: 解析计划文本为 TaskPlan，注入上下文
        if confirmed_plan:
            self._plan = self._parse_plan(confirmed_plan)
            ctx.set_current_plan(self._plan)
            messages = ctx.assemble_with_confirmed_plan(
                user_input, self.session_id, confirmed_plan
            )
        else:
            self._plan = None
            ctx.clear_current_plan()
            messages = await ctx.assemble(user_input, self.session_id)

        # 上下文必须在保存本轮消息前组装，否则数据库历史和下面追加的 user message
        # 会包含同一条输入两次。
        save_message(self.session_id, role=MessageRole.user, content=user_input)
        tools = get_all_schemas()
        if allowed_tool_names:
            tools = [
                schema for schema in tools
                if schema["function"]["name"] in allowed_tool_names
            ]

        policy_corrections: dict[str, int] = {}
        incomplete_plan_corrections: dict[int, int] = {}

        # ── 任务执行阶段 ───────────────────────────────────
        for iteration in range(self.settings.max_iterations):
            await controller.checkpoint()
            iteration_tools = self._tools_for_current_plan_step(tools)
            try:
                response = await self.llm.chat(messages, tools=iteration_tools)
            except Exception as e:
                if _is_context_overflow(e):
                    overflow_message = _context_overflow_msg()
                    controller.state.output += f"\n{overflow_message}"
                    await controller.emit(
                        "run.output", {"delta": f"\n{overflow_message}"}
                    )
                    await controller.fail(overflow_message)
                    yield f"\n{overflow_message}"
                    save_message(self.session_id, role=MessageRole.assistant, content=_context_overflow_msg())
                    return
                raise

            # 如果在模型请求期间收到取消，不能保存一个缺少 tool result 的
            # assistant tool_calls 消息，否则后续历史将不符合 OpenAI 协议。
            await controller.checkpoint()

            if response.has_tool_calls:
                # 工具调用阶段（非流式）
                save_message(
                    self.session_id,
                    role=MessageRole.assistant,
                    content=response.content,
                    tool_calls=[
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                        for tc in response.tool_calls
                    ],
                )
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            },
                        }
                        for tc in response.tool_calls
                    ],
                }
                messages.append(assistant_msg)

                runtime_step = await controller.start_step(
                    name=self._runtime_step_name(response.tool_calls),
                    tool_names=[tool_call.name for tool_call in response.tool_calls],
                )

                for tc in response.tool_calls:
                    await controller.emit(
                        "step.tool_called",
                        {
                            "stepId": runtime_step.id,
                            "tool": tc.name,
                            "arguments": tc.arguments,
                        },
                    )
                    if on_tool_call:
                        await _maybe_await(on_tool_call(tc.name, tc.arguments))

                unauthorized_tools = (
                    [
                        tool_call.name for tool_call in response.tool_calls
                        if tool_call.name not in allowed_tool_names
                    ]
                    if allowed_tool_names is not None
                    else []
                )
                tool_policy_error = (
                    f"Skill Agent step does not allow tools: {unauthorized_tools}"
                    if unauthorized_tools
                    else None
                )
                plan_policy_error = self._begin_plan_step(response.tool_calls)
                policy_error = tool_policy_error or plan_policy_error
                if policy_error:
                    tool_results = tool_dispatcher.rejected(
                        response.tool_calls, policy_error
                    )
                else:
                    tool_results = await tool_dispatcher.execute(response.tool_calls)

                # ── 步骤验证：用多模态模型检查操作是否成功 ──
                if (
                    confirmed_plan
                    and not policy_error
                    and all(result.get("success", True) for result in tool_results)
                    and self._should_verify(response.tool_calls)
                ):
                    verification = await self._verify_step(response.tool_calls, confirmed_plan)
                    if verification:
                        _append_verification(tool_results, verification)
                        if on_tool_result:
                            await _maybe_await(on_tool_result("verify_action_result", verification))

                for tr in tool_results:
                    save_message(
                        self.session_id,
                        role=MessageRole.tool,
                        content=tr["content"],
                        tool_call_id=tr["tool_call_id"],
                        tool_name=tr["name"],
                    )
                    if on_tool_result:
                        await _maybe_await(on_tool_result(tr["name"], tr["content"]))
                    await controller.emit(
                        "step.tool_result",
                        {
                            "stepId": runtime_step.id,
                            "tool": tr["name"],
                            "success": tr.get("success", True),
                            "content": tr["content"],
                            "error": tr.get("error"),
                        },
                    )

                messages.extend(tool_dispatcher.to_openai_messages(tool_results))

                if policy_error:
                    policy_key = self._policy_scope_key()
                    correction_count = policy_corrections.get(policy_key, 0) + 1
                    policy_corrections[policy_key] = correction_count
                    if correction_count <= _MAX_POLICY_CORRECTIONS:
                        await controller.finish_step(
                            runtime_step,
                            success=False,
                            error=policy_error,
                        )
                        messages.append({
                            "role": "system",
                            "content": self._policy_correction_message(
                                policy_error,
                                iteration_tools,
                            ),
                        })
                        continue

                # P1-3: 标记当前步骤进度
                if self._plan:
                    plan_failure = self._advance_plan(response.tool_calls, tool_results)
                    if plan_failure:
                        await controller.finish_step(
                            runtime_step,
                            success=False,
                            error=plan_failure,
                        )
                        message = f"计划执行已停止：{plan_failure}"
                        controller.state.output += message
                        await controller.emit("run.output", {"delta": message})
                        await controller.fail(plan_failure)
                        save_message(
                            self.session_id,
                            role=MessageRole.assistant,
                            content=message,
                        )
                        yield message
                        return

                if policy_error:
                    await controller.finish_step(
                        runtime_step,
                        success=False,
                        error=policy_error,
                    )
                    message = f"工具策略校验失败：{policy_error}"
                    controller.state.output += message
                    await controller.emit("run.output", {"delta": message})
                    await controller.fail(policy_error)
                    save_message(
                        self.session_id,
                        role=MessageRole.assistant,
                        content=message,
                    )
                    yield message
                    return

                step_success = all(
                    result.get("success", True) for result in tool_results
                )
                step_summary = tool_results[-1]["content"][:500] if tool_results else ""
                await controller.finish_step(
                    runtime_step,
                    success=step_success,
                    result=step_summary if step_success else None,
                    error=None if step_success else step_summary,
                )

                continue

            else:
                incomplete_step = self._incomplete_plan_step()
                if incomplete_step is not None:
                    correction_count = incomplete_plan_corrections.get(incomplete_step.id, 0) + 1
                    incomplete_plan_corrections[incomplete_step.id] = correction_count
                    if correction_count <= _MAX_POLICY_CORRECTIONS:
                        messages.append({
                            "role": "assistant",
                            "content": response.content or "",
                        })
                        messages.append({
                            "role": "system",
                            "content": (
                                f"计划步骤 {incomplete_step.id} 尚未完成。"
                                f"必须完成工具 {incomplete_step.expected_tools} 后才能返回最终答复。"
                                "请从当前提供的工具中继续执行，不要只回复文字。"
                            ),
                        })
                        continue

                    error = (
                        f"步骤 {incomplete_step.id} 未完成要求的工具 "
                        f"{incomplete_step.expected_tools}，模型提前结束执行"
                    )
                    self._plan.mark_failed(incomplete_step.id, error)
                    await controller.fail(error)
                    message = f"计划执行已停止：{error}"
                    controller.state.output += message
                    await controller.emit("run.output", {"delta": message})
                    save_message(
                        self.session_id,
                        role=MessageRole.assistant,
                        content=message,
                    )
                    yield message
                    return

                # 已通过上面的非流式请求获得最终内容。直接输出该响应，
                # 避免为了“流式展示”再次请求模型并重复消耗 Token。
                full_response = response.content or ""
                await controller.checkpoint()
                controller.state.output += full_response
                await controller.emit("run.output", {"delta": full_response})
                if on_token:
                    await _maybe_await(on_token(full_response))
                if full_response:
                    yield full_response

                save_message(
                    self.session_id,
                    role=MessageRole.assistant,
                    content=full_response,
                )
                if finalize_run:
                    await controller.succeed()
                return

        message = f"已达到最大迭代次数 {self.settings.max_iterations}"
        controller.state.output += f"\n[{message}]"
        await controller.emit("run.output", {"delta": f"\n[{message}]"})
        await controller.fail(message)
        yield f"\n[{message}]"

    async def execute_instruction(
        self,
        instruction: str,
        controller: RunController,
        allowed_tool_names: Optional[set[str]] = None,
    ) -> str:
        """Execute one Agent-backed Skill step inside an existing controlled Run."""
        self.current_run = controller
        output = ""
        with capture_token_usage(controller.record_model_usage):
            async for token in self._execute_stream(
                user_input=instruction,
                controller=controller,
                allowed_tool_names=allowed_tool_names,
                finalize_run=False,
            ):
                output += token
        if controller.state.status == RunStatus.FAILED:
            raise RuntimeError(controller.state.error or "Agent Skill step failed")
        return output

    async def pause_current_run(self) -> None:
        if self.current_run is None:
            raise RuntimeError("当前没有可暂停的 Run")
        await self.current_run.pause()

    async def resume_current_run(self) -> None:
        if self.current_run is None:
            raise RuntimeError("当前没有可继续的 Run")
        await self.current_run.resume()

    async def cancel_current_run(self, reason: str = "Cancelled by user") -> None:
        if self.current_run is None:
            raise RuntimeError("当前没有可取消的 Run")
        await self.current_run.cancel(reason)

    # ── 计划管理 ──

    @staticmethod
    def _parse_plan(plan_text: str) -> Optional[TaskPlan]:
        """
        P1-3: 将编号计划文本解析为 TaskPlan 对象。
        """
        import re

        available_tools = {
            schema["function"]["name"]
            for schema in get_all_schemas()
        }
        steps = []
        for line in plan_text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(\d+)[.、]\s*(.+)$", line)
            if m:
                step_id = int(m.group(1))
                desc = m.group(2).strip()
                expected_tools = sorted(
                    (
                        tool_name for tool_name in available_tools
                        if re.search(rf"(?<![\w]){re.escape(tool_name)}(?![\w])", desc)
                    ),
                    key=desc.find,
                )
                steps.append(TaskStep(
                    id=step_id,
                    description=desc,
                    expected_tools=expected_tools,
                ))
        if not steps:
            return None
        return TaskPlan(goal=plan_text[:80], steps=steps)

    def _begin_plan_step(self, tool_calls: list) -> Optional[str]:
        """标记当前步骤开始，并拒绝计划未授权的工具。"""
        if not self._plan or not self._plan.steps:
            return None

        step = self._plan.current_step
        if step is None:
            step = self._plan.advance_to_next()
        if step is None:
            return "计划已完成，但模型仍尝试调用工具"
        if step.status == TaskStatus.PENDING:
            self._plan.mark_running(step.id)
        if step.status == TaskStatus.FAILED:
            return f"步骤 {step.id} 已失败，不能继续执行"

        if not step.expected_tools:
            return f"步骤 {step.id} 未声明有效工具，无法安全执行"

        allowed_tools = self._allowed_tools_for_plan_step(step)
        unexpected = [
            tool_call.name for tool_call in tool_calls
            if tool_call.name not in allowed_tools
        ]
        if unexpected:
            return (
                f"步骤 {step.id} 要求工具 {step.expected_tools}，"
                f"仅额外允许只读观察工具 {sorted(allowed_tools - set(step.expected_tools))}，"
                f"但模型请求了 {unexpected}"
            )
        return None

    def _tools_for_current_plan_step(self, tools: list[dict]) -> list[dict]:
        """只向模型暴露当前计划步骤可以调用的工具。"""
        if not self._plan or not self._plan.steps:
            return tools
        step = self._plan.current_step
        if step is None:
            step = self._plan.advance_to_next()
        if step is None:
            return []
        allowed_tools = self._allowed_tools_for_plan_step(step)
        return [
            schema for schema in tools
            if schema["function"]["name"] in allowed_tools
        ]

    @staticmethod
    def _allowed_tools_for_plan_step(step: TaskStep) -> set[str]:
        """必需执行工具加无副作用观察工具；观察工具不计入步骤完成条件。"""
        return set(step.expected_tools) | set(_PLAN_OBSERVATION_TOOLS)

    def _policy_scope_key(self) -> str:
        if self._plan and self._plan.current_step:
            return f"plan:{self._plan.current_step.id}"
        return "skill-agent"

    @staticmethod
    def _policy_correction_message(error: str, allowed_schemas: list[dict]) -> str:
        allowed = [
            schema["function"]["name"]
            for schema in allowed_schemas
        ]
        return (
            f"上一次工具调用因策略越界而未执行：{error}。"
            f"这是一次纠正机会。只能调用当前允许的工具：{allowed}。"
            "不要重复被拒绝的调用。"
        )

    def _incomplete_plan_step(self) -> Optional[TaskStep]:
        if not self._plan or self._plan.is_complete:
            return None
        step = self._plan.current_step
        if step and step.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            return step
        return self._plan.advance_to_next()

    def _runtime_step_name(self, tool_calls: list) -> str:
        """为 Runtime Step 生成稳定、用户可读的名称。"""
        if self._plan and self._plan.current_step:
            step = self._plan.current_step
            return f"计划步骤 {step.id}: {step.description}"
        tool_names = ", ".join(tool_call.name for tool_call in tool_calls)
        return f"执行工具: {tool_names}"

    def _advance_plan(
        self,
        tool_calls: list,
        tool_results: list[dict],
    ) -> Optional[str]:
        """
        根据工具调用结果更新当前步骤。失败时返回停止原因。
        """
        if not self._plan or not self._plan.steps:
            return None

        # 找当前待执行步骤
        step = self._plan.current_step
        if step is None:
            step = self._plan.advance_to_next()
        if step is None:
            return None

        # 检查是否有工具执行出错
        errors = [tr["content"] for tr in tool_results if not tr.get("success", True)]

        tool_names = [tc.name for tc in tool_calls]
        tool_names_str = ", ".join(tool_names)

        if errors:
            error = "; ".join(errors)
            self._plan.mark_failed(step.id, error=error)
            return f"步骤 {step.id} 失败：{error}"

        self._plan.record_completed_tools(step.id, tool_names)
        all_expected_done = (
            not step.expected_tools
            or all(name in step.completed_tools for name in step.expected_tools)
        )
        if step.status in (TaskStatus.PENDING, TaskStatus.RUNNING) and all_expected_done:
            summary = tool_results[-1]["content"][:100] if tool_results else ""
            self._plan.mark_done(
                step.id,
                result=summary,
                tool_used=tool_names_str,
            )
            self._plan.advance_to_next()

        return None

    # ── 步骤验证辅助方法 ──

    # 不需要视觉验证的工具：纯查询、后台静默执行、不改变 UI 状态的操作
    _NO_VERIFY_TOOLS = {
        # 窗口/系统查询
        "list_windows", "path_exists", "list_dir",
        # 等待/时间控制
        "sleep",
        # 剪贴板操作
        "get_clipboard", "set_clipboard",
        # 后台命令（静默执行，屏幕不会有可见变化）
        "run_command",
        # 截图（本身不改变 UI）
        "capture_image",
        # 内容读取类
        "analyze_screen", "analyze_image",
        # 计划生成
        "create_plan",
    }

    def _should_verify(self, tool_calls: list) -> bool:
        """判断是否需要执行步骤验证"""
        if not tool_calls:
            return False
        # 如果所有工具都是纯查询类，不需要验证
        for tc in tool_calls:
            if tc.name not in self._NO_VERIFY_TOOLS:
                return True
        return False

    async def _verify_step(self, tool_calls: list, confirmed_plan: str) -> Optional[str]:
        """
        用多模态模型验证当前步骤是否成功。
        返回验证结果文本，如果不需要验证返回 None。
        """
        from tools.vision import verify_action_result

        # 从计划中提取当前步骤的预期效果
        try:
            # 构建验证提示
            tool_summary = "; ".join(f"{tc.name}({json.dumps(tc.arguments, ensure_ascii=False)[:80]})" for tc in tool_calls)

            # 从工具参数中提取目标窗口名（用于验证截图前的窗口激活）
            target_window = None
            is_launch_op = False
            for tc in tool_calls:
                args = tc.arguments or {}
                # 对于 app_launch，name 是进程名（如 outlook.exe），不是窗口标题
                # 需要先等待窗口出现，再用 list_windows 找到实际窗口
                if tc.name == "app_launch":
                    is_launch_op = True
                    proc_name = args.get("name", "").replace(".exe", "")
                    # 等待窗口出现
                    await asyncio.sleep(3.0)
                    try:
                        # 用 list_windows 找到匹配进程名的窗口
                        # 注：app_launch 工具已完成此发现并返回 window_title
                        # 此处为验证截图提供窗口激活备用
                        from tools.winpeekaboo import list_windows as list_wins
                        raw = await list_wins(filter=proc_name)
                        wins = json.loads(raw)
                        if wins:
                            # 取第一个匹配的窗口标题
                            target_window = wins[0].get("title") or wins[0].get("text")
                    except Exception:
                        pass
                    break
                # 其他操作：从 window / title / name 参数提取窗口
                for key in ("window", "title", "name"):
                    if key in args and isinstance(args[key], str):
                        target_window = args[key]
                        break
                if target_window:
                    break

            # app_switch 类操作也需要较长等待
            if not is_launch_op:
                is_launch_op = any(tc.name == "app_switch" for tc in tool_calls)
            wait_seconds = 3.0 if is_launch_op else 1.0

            # 用 LLM 生成验证描述
            prompt = (
                f"根据以下执行计划和刚执行的工具调用，简要描述预期的屏幕状态（一句话）。\n\n"
                f"注意：请描述可直接在屏幕上观察到的 UI 状态，而非内部命令返回值。\n\n"
                f"## 执行计划\n{confirmed_plan}\n\n"
                f"## 刚执行的工具调用\n{tool_summary}\n\n"
                f"只返回预期效果的描述，不要其他内容。"
            )
            messages = [
                {"role": "system", "content": "你是任务验证助手，只返回可见 UI 状态的描述。"},
                {"role": "user", "content": prompt},
            ]
            response = await self.llm.chat(messages)
            expected = (response.content or "").strip()

            if not expected:
                return None

            # 用多模态模型验证（传入目标窗口，验证前会自动激活）
            result = await verify_action_result(expected, window=target_window, wait_seconds=wait_seconds)
            return f"[屏幕观察] {result}"

        except Exception as e:
            return f"[屏幕观察] ⚠️ 观察过程出错: {type(e).__name__}: {e}"


def _append_verification(tool_results: list[dict], verification: str) -> None:
    """
    将验证结果追加到最后一个工具结果的 content 末尾。

    OpenAI API 要求 role= tool 的消息必须关联真实的 tool_call_id，
    因此无法作为独立消息插入。追加到最后一个工具结果是符合
    API 协议的唯一方式。
    """
    if not tool_results:
        return
    last_content = tool_results[-1]["content"]
    if last_content:
        tool_results[-1]["content"] = last_content + "\n\n" + verification
    else:
        tool_results[-1]["content"] = verification

    if "⚠️" in verification or "❌" in verification:
        tool_results[-1]["success"] = False
        tool_results[-1]["error"] = verification


async def _maybe_await(coro_or_none):
    """兼容普通函数和协程回调"""
    import asyncio
    if asyncio.iscoroutine(coro_or_none):
        await coro_or_none


# ══════════════════════════════════════════════════════
# 上下文溢出检测（LLM 报错 → 优雅停止）
# ══════════════════════════════════════════════════════

_CONTEXT_OVERFLOW_KEYWORDS = [
    "context length",
    "maximum context length",
    "context_length_exceeded",
    "reduce your prompt",
    "too many tokens",
    "maximum number of tokens",
    "reduce the number of tokens",
    "input is too long",
    "prompt is too long",
]


def _is_context_overflow(error: Exception) -> bool:
    """判断是否为模型上下文溢出异常"""
    # openai 库对 context overflow 返回 400 或 413，具体取决于服务端
    status_code = getattr(error, "status_code", None)
    if status_code == 413:
        return True
    # 仅当 status_code 为 400 时才检查关键词，避免误判其他错误
    if status_code != 400:
        return False
    msg = str(error).lower()
    return any(kw in msg for kw in _CONTEXT_OVERFLOW_KEYWORDS)


def _context_overflow_msg() -> str:
    return "⚠️ 已超出模型上下文窗口限制，本次任务停止。建议发起新会话（/new）继续。"
