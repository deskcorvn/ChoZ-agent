"""The coordinating agents as AgentScope ReActAgents on Claude, with metered usage and the day's tools.

AgentScope 1.0.21 imports `streamablehttp_client` from mcp, which mcp 2.x (pinned by the repo) renamed to
`streamable_http_client`; the alias below is the only compatibility shim. MCP itself is not used here.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import mcp.client.streamable_http as _mcp_http

if not hasattr(_mcp_http, "streamablehttp_client"):
    _mcp_http.streamablehttp_client = _mcp_http.streamable_http_client

from agentscope.agent import ReActAgent  # noqa: E402
from agentscope.formatter import AnthropicMultiAgentFormatter  # noqa: E402
from agentscope.message import Msg, TextBlock  # noqa: E402
from agentscope.model import AnthropicChatModel  # noqa: E402
from agentscope.pipeline import MsgHub  # noqa: E402
from agentscope.tool import Toolkit, ToolResponse  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from sim.agents.base import TOOL_PARAMS, BudgetStop, Reply  # noqa: E402
from sim.agents.prompts import AGENTS, system_prompt  # noqa: E402


class ClaudeHub:
    def __init__(self, day: Any):
        self.day = day
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (live mode needs it; see sim/README.md)")
        self.agents: dict[str, ReActAgent] = {}

    def _model(self, code: str) -> AnthropicChatModel:
        model = AnthropicChatModel(model_name=self.day.claude_model, api_key=self.key, stream=False, max_tokens=1024)
        create = model.client.messages.create
        day = self.day

        async def metered(**kwargs: Any):
            if day.spent_usd >= day.max_run_usd:
                raise BudgetStop(f"MAX_RUN_USD {day.max_run_usd} reached (${day.spent_usd:.4f} spent)")
            # AgentScope forces the structured-output tool with tool_choice "any"/"tool"; the 5.5 models refuse forced
            # tool use, so ask with "auto" (the system prompt says to finish with generate_response; ReActAgent
            # reminds the model if it does not).
            choice = kwargs.get("tool_choice")
            if isinstance(choice, dict) and choice.get("type") in ("any", "tool"):
                kwargs["tool_choice"] = {"type": "auto"}
            t0 = time.perf_counter()
            response = await create(**kwargs)
            day.record_claude(code, response, round((time.perf_counter() - t0) * 1000))
            return response

        model.client.messages.create = metered
        return model

    def _toolkit(self, code: str) -> Toolkit:
        kit = Toolkit()
        for name in AGENTS[code]["tools"]:
            kit.register_tool_function(self._tool(code, name), json_schema={
                "type": "function",
                "function": {"name": name, "description": (getattr(self.day.tools, name).__doc__ or name).strip(),
                             "parameters": {"type": "object", "properties": TOOL_PARAMS[name],
                                            "required": list(TOOL_PARAMS[name])}}})
        return kit

    def _tool(self, code: str, name: str):
        day = self.day

        async def run(**kwargs: Any) -> ToolResponse:
            out = day.call(code, name, **kwargs)
            return ToolResponse(content=[TextBlock(type="text", text=json.dumps(out, ensure_ascii=False))])

        run.__name__ = name
        return run

    def agent(self, code: str) -> ReActAgent:
        if code not in self.agents:
            agent = ReActAgent(name=code, sys_prompt=system_prompt(code), model=self._model(code),
                               formatter=AnthropicMultiAgentFormatter(), toolkit=self._toolkit(code), max_iters=8,
                               enable_rewrite_query=False)
            agent.set_console_output_enabled(False)
            self.agents[code] = agent
        return self.agents[code]

    async def ask(self, code: str, instruction: str, tools: list[str], listeners: tuple[str, ...]) -> Reply | None:
        agent = self.agent(code)
        msg = Msg("dieu_phoi", instruction + ("\nCông cụ nên dùng: " + ", ".join(tools) if tools else ""), "user")
        if listeners:
            async with MsgHub(participants=[agent, *(self.agent(x) for x in listeners)]):
                reply = await agent(msg, structured_model=Reply)
        else:
            reply = await agent(msg, structured_model=Reply)
        try:
            return Reply.model_validate(reply.metadata or {})
        except ValidationError:
            return None
