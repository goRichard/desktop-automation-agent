"""
Agent Loop 核心：多轮工具调用循环
User Input → Context Assembly → LLM Call → Tool Dispatch → Observation → Loop / Final Response
所有消息自动写入 SQLite
"""
from __future__ import annotations

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
from runtime.observation import (
    RuntimeObservation,
    active_window_label,
    collect_runtime_observation,
    observation_summary,
)
from tools import get_all_schemas, get_tool_metadata

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
    "inspect_elements",
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
            f"3. `user.confirm` 等 Skill 控制动作不是工具，不得出现在计划中；不要添加用户确认步骤\n"
            f"4. 步骤描述格式：`步骤描述（工具名）`，例如：`批量定位 To/Subject/Body 控件（batch_locate_elements）`\n"
            f"5. 仅返回编号列表，不要标题、不要 markdown 标记，格式如下：\n"
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
        allow_tool_confirmation: bool = False,
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
                        allow_tool_confirmation=allow_tool_confirmation,
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
        allow_tool_confirmation: bool = False,
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
        policy_mode = _tool_policy_mode(controller)
        tools = _filter_schemas_for_tool_policy_mode(get_all_schemas(), policy_mode)
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
            observation_for_context = await collect_runtime_observation()
            request_messages = _messages_with_runtime_situation(
                messages,
                controller.state.execution_memory,
                observation_for_context,
                self._plan,
                iteration_tools,
            )
            try:
                response = await self.llm.chat(request_messages, tools=iteration_tools)
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

                observation_before = await collect_runtime_observation()

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
                mode_policy_error = _tool_mode_policy_error(
                    response.tool_calls,
                    policy_mode,
                )
                plan_policy_error = self._begin_plan_step(response.tool_calls)
                policy_error = tool_policy_error or mode_policy_error or plan_policy_error
                if not policy_error:
                    policy_error = await _tool_confirmation_policy_error(
                        controller,
                        response.tool_calls,
                        policy_mode,
                        allow_tool_confirmation=allow_tool_confirmation,
                    )
                if policy_error:
                    tool_results = tool_dispatcher.rejected(
                        response.tool_calls, policy_error
                    )
                else:
                    tool_results = await tool_dispatcher.execute(response.tool_calls)

                observation_after = (
                    observation_before
                    if policy_error
                    else await collect_runtime_observation()
                )

                # ── 分层验证：仅在检查点/窗口切换/高风险/最终步骤使用视觉模型 ──
                verification = None
                if (
                    confirmed_plan
                    and not policy_error
                    and all(result.get("success", True) for result in tool_results)
                ):
                    verification_reason = self._verification_reason(
                        response.tool_calls,
                        tool_results,
                    )
                    if verification_reason:
                        verification = await self._verify_step(
                            response.tool_calls,
                            tool_results,
                            verification_reason,
                            controller.state.execution_memory,
                        )
                        _append_verification(tool_results, verification)
                        if on_tool_result:
                            await _maybe_await(on_tool_result("verify_action_result", verification))

                await self._record_execution_memory(
                    controller,
                    response.tool_calls,
                    tool_results,
                    verification,
                    observation_before=observation_before,
                    observation_after=observation_after,
                    compliance_status=(
                        "rejected_tool_not_allowed" if policy_error else "compliant"
                    ),
                    compliance_reason=policy_error,
                )

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
                allow_tool_confirmation=controller.state.execution_mode != "unattended",
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
        "list_windows", "path_exists", "list_dir", "read_file", "write_file",
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

    def _has_visible_action(self, tool_calls: list) -> bool:
        if not tool_calls:
            return False
        return any(tc.name not in self._NO_VERIFY_TOOLS for tc in tool_calls)

    def _verification_reason(
        self,
        tool_calls: list,
        tool_results: list[dict],
    ) -> Optional[str]:
        """Return why a costly visual verification is required, or None to skip it."""
        if not self._has_visible_action(tool_calls):
            return None

        config = getattr(getattr(self, "settings", None), "verification", {}) or {}
        mode = str(config.get("mode", "checkpoint")).lower()
        if mode == "off":
            return None
        if mode == "all":
            return "all_actions"

        if config.get("verifyWindowTransitions", True) and (
            _reported_new_window(tool_results)
            or any(
                tc.name in {
                    "app_launch",
                    "app_switch",
                    "outlook_launch_classic",
                    "outlook_open_compose",
                }
                for tc in tool_calls
            )
        ):
            return "window_transition"

        if config.get("verifyHighRiskActions", True) and _is_high_risk_action(
            tool_calls,
        ):
            return "high_risk"

        if not self._plan_step_will_complete(tool_calls):
            return None

        if self._plan and self._plan.current_step:
            index = self._plan.current_step_index
            if config.get("verifyFinalStep", True) and index == len(self._plan.steps) - 1:
                return "final_step"
            interval = max(1, int(config.get("checkpointInterval", 3) or 3))
            if (index + 1) % interval == 0:
                return "periodic_checkpoint"
        return None

    def _plan_step_will_complete(self, tool_calls: list) -> bool:
        if not self._plan or not self._plan.current_step:
            return False
        step = self._plan.current_step
        completed = set(step.completed_tools) | {tool_call.name for tool_call in tool_calls}
        return bool(step.expected_tools) and all(
            tool_name in completed for tool_name in step.expected_tools
        )

    async def _verify_step(
        self,
        tool_calls: list,
        tool_results: list[dict],
        reason: str,
        execution_memory: list[dict],
    ) -> Optional[str]:
        """
        用多模态模型验证当前步骤是否成功。
        返回验证结果文本，如果不需要验证返回 None。
        """
        from tools.vision import verify_action_result

        # 从计划中提取当前步骤的预期效果
        try:
            tool_summary = "; ".join(
                f"{tc.name}({json.dumps(tc.arguments, ensure_ascii=False)[:120]})"
                for tc in tool_calls
            )
            step_description = (
                self._plan.current_step.description
                if self._plan and self._plan.current_step
                else "确认刚执行的操作已经产生预期的可见界面变化"
            )
            expected = (
                f"当前计划步骤（仅作为上下文，可能尚未完成）：{step_description}\n"
                f"刚执行的工具：{tool_summary}\n"
                f"验证原因：{reason}\n"
                f"此前动作记录：\n{_execution_memory_summary(execution_memory)}\n"
                "请只判断刚执行的工具是否产生了合理的直接可见效果。"
                "不要要求当前步骤中尚未调用的其他工具已经完成，也不要检查后续计划步骤。"
                "此前动作是执行记录；如果相关区域当前不可见，不要据此断言内容缺失。"
            )
            target_window = _verification_target_window(tool_calls, tool_results)
            wait_seconds = _verification_wait_seconds(tool_calls)

            result = await verify_action_result(
                expected,
                window=target_window,
                wait_seconds=wait_seconds,
            )
            screenshot_target = target_window or "当前前台窗口/全屏"
            return (
                f"[屏幕观察] {result}\n"
                f"[验证触发] {reason}\n"
                f"[验证截图目标] {screenshot_target}"
            )

        except Exception as e:
            return f"[屏幕观察] ⚠️ 观察过程出错: {type(e).__name__}: {e}"

    async def _record_execution_memory(
        self,
        controller: RunController,
        tool_calls: list,
        tool_results: list[dict],
        verification: Optional[str],
        *,
        observation_before: Optional[RuntimeObservation] = None,
        observation_after: Optional[RuntimeObservation] = None,
        compliance_status: str = "compliant",
        compliance_reason: Optional[str] = None,
    ) -> None:
        step = self._plan.current_step if self._plan else None
        result_by_id = {
            result.get("tool_call_id"): result for result in tool_results
        }
        for tool_call in tool_calls:
            result = result_by_id.get(tool_call.id, {})
            entry = {
                "sequence": len(controller.state.execution_memory) + 1,
                "planStepId": step.id if step else None,
                "planStep": step.description if step else None,
                "tool": tool_call.name,
                "arguments": _sanitize_action_value(tool_call.arguments),
                "success": bool(result.get("success", True)),
                "result": str(result.get("content") or "")[:300],
                "verification": verification,
                "activeWindowBefore": active_window_label(observation_before),
                "activeWindowAfter": active_window_label(observation_after),
                "planCompliance": {
                    "status": compliance_status,
                    "reason": compliance_reason,
                    "expectedTools": step.expected_tools if step else [],
                },
            }
            await controller.record_execution_action(entry)


_WINDOW_TRANSITION_TOOLS = {
    "app_launch",
    "app_quit",
    "browser_click",
    "browser_close",
    "browser_go_back",
    "browser_navigate",
    "click",
    "find_and_click",
    "find_and_click_batch",
    "hotkey",
    "outlook_launch_classic",
    "outlook_open_compose",
    "outlook_resolve_compose",
    "outlook_send_message",
    "press_key",
    "run_actions",
    "window_close",
    "window_minimize",
}

_WINDOW_RESULT_TARGET_TOOLS = {
    "outlook_launch_classic",
    "outlook_open_compose",
    "outlook_resolve_compose",
}


def _verification_target_window(tool_calls: list, tool_results: list[dict]) -> Optional[str]:
    """Use only the latest transition result; never reactivate a stale window."""
    import re

    patterns = (
        r"window_title:\s*([^\r\n]+)",
        r"检测到新窗口[^\"“]*[\"“]([^\"”]+)[\"”]",
    )
    result_by_id = {
        result.get("tool_call_id"): result
        for result in tool_results
        if result.get("tool_call_id")
    }
    for index in range(len(tool_calls) - 1, -1, -1):
        tool_call = tool_calls[index]
        if tool_call.name not in _WINDOW_TRANSITION_TOOLS:
            continue
        result = result_by_id.get(tool_call.id)
        if result is None and index < len(tool_results):
            result = tool_results[index]
        if tool_call.name in _WINDOW_RESULT_TARGET_TOOLS:
            structured_title = _structured_result_window_title(result or {})
            if structured_title:
                return structured_title
        content = str((result or {}).get("content") or "")
        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                return match.group(1).strip()
        # The latest transition may have changed foreground focus even when the
        # tool cannot report a title (for example Ctrl+N). In that case capture
        # the current foreground desktop and do not reuse an earlier title.
        return None

    for tool_call in reversed(tool_calls):
        arguments = tool_call.arguments or {}
        for key in ("window", "title"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if tool_call.name == "app_switch":
            value = arguments.get("name")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _structured_result_window_title(result: dict) -> Optional[str]:
    data = result.get("data")
    if not isinstance(data, dict):
        content = result.get("content")
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                data = parsed.get("data")
    if not isinstance(data, dict):
        return None
    value = data.get("windowTitle") or data.get("window_title")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _verification_wait_seconds(tool_calls: list) -> float:
    names = {tool_call.name for tool_call in tool_calls}
    if "app_launch" in names:
        return 2.0
    if "app_switch" in names or names & _WINDOW_TRANSITION_TOOLS:
        return 1.2
    return 0.5


def _reported_new_window(tool_results: list[dict]) -> bool:
    return any(
        "检测到新窗口" in str(result.get("content") or "")
        or "window_title:" in str(result.get("content") or "")
        or (
            isinstance(result.get("data"), dict)
            and result["data"].get("action") == "open_compose"
            and bool(_structured_result_window_title(result))
        )
        for result in tool_results
    )


_HIGH_RISK_ACTION_TERMS = (
    "send",
    "submit",
    "delete",
    "remove",
    "confirm",
    "publish",
    "save",
    "发送",
    "提交",
    "删除",
    "移除",
    "确认",
    "发布",
    "保存",
    "付款",
)


def _is_high_risk_action(tool_calls: list) -> bool:
    for tool_call in tool_calls:
        raw_arguments = tool_call.arguments or {}
        action_target = " ".join(
            str(raw_arguments.get(key) or "")
            for key in ("target", "on", "name", "description", "button")
        ).lower()
        if any(term in action_target for term in _HIGH_RISK_ACTION_TERMS):
            return True
        if tool_call.name == "hotkey":
            keys = str(raw_arguments.get("keys") or "").lower().replace(" ", "")
            if keys in {"alt+s", "ctrl+enter"}:
                return True
    return False


def _sanitize_action_value(value: Any, key: str = "") -> Any:
    normalized_key = key.lower().replace("-", "_")
    if any(term in normalized_key for term in ("password", "secret", "token", "api_key")):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(child_key): _sanitize_action_value(child_value, str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_action_value(item, key) for item in value[:20]]
    if isinstance(value, str) and len(value) > 120:
        return value[:120] + "…"
    return value


def _execution_memory_summary(execution_memory: list[dict], limit: int = 12) -> str:
    if not execution_memory:
        return "- 无"
    lines = []
    for entry in execution_memory[-limit:]:
        status = "成功" if entry.get("success") else "失败"
        arguments = json.dumps(
            entry.get("arguments", {}),
            ensure_ascii=False,
            default=str,
        )
        lines.append(
            f"- #{entry.get('sequence')} 步骤 {entry.get('planStepId')}: "
            f"{entry.get('tool')}({arguments[:180]}) -> {status}"
        )
        active_after = entry.get("activeWindowAfter")
        compliance = entry.get("planCompliance")
        if active_after or compliance:
            compliance_status = (
                compliance.get("status")
                if isinstance(compliance, dict)
                else compliance
            )
            suffix = []
            if active_after:
                suffix.append(f"前台={active_after}")
            if compliance_status:
                suffix.append(f"计划一致性={compliance_status}")
            if suffix:
                lines[-1] += "；" + "；".join(suffix)
    return "\n".join(lines)


def _tool_policy_mode(controller: RunController) -> str:
    """Map Run metadata to the mode names used by tool registry metadata."""
    if controller.state.execution_mode:
        return str(controller.state.execution_mode)
    return "agent"


def _filter_schemas_for_tool_policy_mode(
    schemas: list[dict[str, Any]],
    mode: str,
) -> list[dict[str, Any]]:
    return [
        schema for schema in schemas
        if _tool_allowed_in_mode(schema["function"]["name"], mode)
    ]


def _tool_allowed_in_mode(tool_name: str, mode: str) -> bool:
    metadata = get_tool_metadata(tool_name)
    if metadata is None:
        return True
    allowed_modes = metadata.get("allowedModes") or []
    return mode in allowed_modes


def _tool_mode_policy_error(tool_calls: list, mode: str) -> Optional[str]:
    blocked = [
        tool_call.name for tool_call in tool_calls
        if not _tool_allowed_in_mode(tool_call.name, mode)
    ]
    if not blocked:
        return None
    return f"Tool calls are not allowed in {mode} mode: {blocked}"


async def _tool_confirmation_policy_error(
    controller: RunController,
    tool_calls: list,
    mode: str,
    *,
    allow_tool_confirmation: bool,
) -> Optional[str]:
    required = []
    for tool_call in tool_calls:
        metadata = get_tool_metadata(tool_call.name)
        if metadata and metadata.get("requiresConfirmation"):
            required.append({
                "name": tool_call.name,
                "risk": metadata.get("risk"),
                "sideEffect": metadata.get("sideEffect"),
                "arguments": _sanitize_action_value(tool_call.arguments),
            })
    if not required:
        return None

    names = [item["name"] for item in required]
    if not allow_tool_confirmation:
        return f"Tool calls require explicit user confirmation: {names}"

    approved = await controller.request_confirmation({
        "type": "tool_policy",
        "reason": "requires_confirmation",
        "mode": mode,
        "tools": required,
    })
    if not approved:
        return f"User rejected tool calls requiring confirmation: {names}"
    return None


def _messages_with_execution_memory(
    messages: list[dict[str, Any]],
    execution_memory: list[dict],
) -> list[dict[str, Any]]:
    if not execution_memory:
        return messages
    return _messages_with_runtime_situation(
        messages,
        execution_memory,
        observation=None,
        plan=None,
        allowed_tool_schemas=None,
    )


def _messages_with_runtime_situation(
    messages: list[dict[str, Any]],
    execution_memory: list[dict],
    observation: Optional[RuntimeObservation],
    plan: Optional[TaskPlan],
    allowed_tool_schemas: Optional[list[dict]],
) -> list[dict[str, Any]]:
    memory_text = _execution_memory_summary(execution_memory)
    plan_text = _plan_situation_summary(plan, allowed_tool_schemas)
    memory_prompt = (
        "## Runtime Situation（由 Runtime 记录）\n"
        f"{plan_text}\n\n"
        f"{observation_summary(observation)}\n\n"
        "最近动作（当前 Run 的执行记忆）：\n"
        f"{memory_text}\n"
        "后续操作必须基于这些执行事实；不要重复成功的点击或输入，失败项可用于恢复判断。"
        "如果当前步骤有 allowed tools，必须严格调用这些工具；不要自行替换为计划外工具。"
    )
    request_messages = [dict(message) for message in messages]
    if request_messages and request_messages[0].get("role") == "system":
        request_messages[0]["content"] = (
            str(request_messages[0].get("content") or "")
            + "\n\n"
            + memory_prompt
        )
    else:
        request_messages.insert(0, {"role": "system", "content": memory_prompt})
    return request_messages


def _plan_situation_summary(
    plan: Optional[TaskPlan],
    allowed_tool_schemas: Optional[list[dict]],
) -> str:
    if not plan:
        return "当前计划步骤：未启用确认计划"
    step = plan.current_step
    if step is None:
        return "当前计划步骤：计划已完成"
    allowed = [
        schema["function"]["name"]
        for schema in (allowed_tool_schemas or [])
    ]
    return (
        "当前计划步骤：\n"
        f"- step-{step.id}: {step.description}\n"
        f"- status: {step.status.value}\n"
        f"- expected tools: {step.expected_tools}\n"
        f"- completed tools: {step.completed_tools}\n"
        f"- allowed tools now: {allowed}"
    )


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

    if "❌" in verification:
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
