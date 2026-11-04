"""The day the agents work in: the clock, the event writer, the tools, and how an agent is asked to do something.

Mock: the agent's tool calls and words are fixed templates, usage is 0. Live: the four coordinating agents are
AgentScope ReActAgents on Claude; they choose which tools to call and write their own bubble text. Either way the
same tool code runs and the same events are written, so the stage cannot tell the modes apart except by `mode`.

Usage is read from each Messages API response (wrapped below, because AgentScope's own usage object drops cache
tokens) and attached to the event that call produced: a tool event for the call that asked for the tool, the say
event for the call that wrote the answer. So cost_total in summary.json is the sum of the event lines.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError

from sim import prices
from sim.schema import TEXT_MAX, Emitter, Usage, at
from sim.tools import Tools
from sim.world import World, generate


class BudgetStop(RuntimeError):
    """MAX_RUN_USD reached: stop the day in order (events so far are kept and validated)."""


class Reply(BaseModel):
    """What a coordinating agent hands back after its tool calls."""
    text: str = Field(description=f"Câu hiện trong bong bóng, tiếng Việt, tối đa {TEXT_MAX} ký tự, có số cụ thể "
                                  "(số đơn, số tiền, tên hàng). Không chào hỏi.")
    messages: list[dict] = Field(default_factory=list,
                                 description="Chỉ khi được yêu cầu: danh sách {to, text} gửi từng người, mỗi text "
                                             f"tối đa {TEXT_MAX} ký tự.")


# Parameters of each tool, for the JSON schema the Claude agents see. Descriptions are the tools' docstrings.
TOOL_PARAMS: dict[str, dict] = {
    "list_baskets": {},
    "stock": {"supplier": {"type": "string", "description": "mã nhà cung cấp ncc1..ncc5"}},
    "make_picklist": {},
    "apply_substitution": {"sku": {"type": "string", "description": "mã hàng thiếu"},
                           "alt": {"type": "string", "description": "mã hàng thay"},
                           "count": {"type": "integer", "description": "số phần cần thay"}},
    "plan_route": {"stops": {"type": "array", "items": {"type": "string"},
                             "description": "các điểm theo thứ tự, ví dụ [\"loc_kho\", \"loc_duong\", \"loc_tram\"]"}},
    "laundry_pickups": {},
    "check_lot": {"supplier": {"type": "string", "description": "mã nhà cung cấp"},
                  "item": {"type": "string", "description": "mã lô: rau, thit, tom, ca, trung, chao_hau"}},
    "order_of": {"household": {"type": "string", "description": "mã hộ ho01..ho50"}},
    "return_policy": {},
    "completed_orders": {},
    "fee_split": {"amount_vnd": {"type": "integer", "description": "tiền hàng đã bán, đồng"}},
    "settlement": {},
}


def _money(v: int) -> str:
    return f"{v:,.0f} đ".replace(",", ".")


# One line of input and output for the tool bubble on stage and the log panel.
ARG_WORDS = {"supplier": "nhà cung cấp", "household": "hộ", "item": "lô", "stops": "điểm dừng", "sku": "hàng thiếu",
             "alt": "hàng thay", "count": "số phần", "amount_vnd": "tiền hàng"}


CODE_WORDS = {"loc_kho": "kho", "loc_duong": "đường", "loc_tram": "Trạm Z", "loc_ncc": "nhà cung cấp",
              "rau": "rau", "thit": "thịt", "tom": "tôm", "ca": "cá", "trung": "trứng", "chao_hau": "cháo hàu",
              "cai_ngot": "cải ngọt", "rau_muong": "rau muống"}


def _word(v: Any) -> str:
    if isinstance(v, list):
        return " → ".join(_word(x) for x in v)
    return CODE_WORDS.get(str(v), str(v))


def summarize(name: str, args: dict, out: dict) -> tuple[str, str]:
    arg = ", ".join(f"{ARG_WORDS.get(k, k)} {_word(v)}" for k, v in args.items()) or "cả ngày"
    s = {
        "list_baskets": lambda: f"{out['baskets']} Giỏ, {_money(out['total_vnd'])}",
        "stock": lambda: f"{out['name']}: " + ", ".join(f"{g['name']} {g['stock']}" for g in out["goods"]),
        "make_picklist": lambda: f"{out['items']} mặt hàng cho {out['orders']} đơn; thiếu: "
                                 + (", ".join(f"{x['name']} {x['missing']}" for x in out["short"]) or "không"),
        "plan_route": lambda: " → ".join(f"{leg['to']} {leg['arrive']}" for leg in out["legs"]),
        "laundry_pickups": lambda: f"{out['count']} hộ gửi giặt: {', '.join(out['households'])}",
        "check_lot": lambda: f"{out['label']}: {out['result']}" + (f" ({'; '.join(out['reasons'])})"
                                                                   if out["reasons"] else ""),
        "order_of": lambda: (f"{out['order']}: " + ", ".join(li["name"] for li in out["lines"])
                             + f", {_money(out['total_vnd'])}") if out.get("order") else "không có đơn",
        "return_policy": lambda: out["policy"][:80],
        "completed_orders": lambda: f"{out['orders']} đơn, {_money(out['gmv_vnd'])}",
        "fee_split": lambda: f"phí {_money(out['fee_vnd'])}: trạm {_money(out['tram'])}, kho {_money(out['kho_giao'])}, "
                             f"giới thiệu {_money(out['gioi_thieu'])}, Chợ Z {_money(out['cho_z'])}",
        "settlement": lambda: f"{len(out['rows'])} dòng, phí {_money(out['totals']['fee_vnd'])}",
        "apply_substitution": lambda: f"thay {out.get('name', out['sku'])} bằng {out.get('alt_name', out['alt'])} ở "
                                      f"{len(out['orders'])} đơn",
    }.get(name, lambda: json.dumps(out, ensure_ascii=False)[:80])
    return arg[:120], s()[:160]


@dataclass
class Asked:
    """The result of asking an agent: its words and every tool output it read, by tool name."""
    text: str
    messages: list[dict] = field(default_factory=list)
    outputs: dict[str, list[dict]] = field(default_factory=dict)
    usage: Usage | None = None

    def out(self, name: str) -> dict:
        return self.outputs[name][-1]


class Day:
    STEP_S = 30          # simulated seconds between two events unless a scene says otherwise

    def __init__(self, out: Path, mode: str, seed: int, usd_vnd: int, max_run_usd: float, claude_model: str):
        self.out, self.mode, self.seed = out, mode, seed
        out.mkdir(parents=True, exist_ok=True)
        self.world: World = generate(seed)
        self.tools = Tools(self.world)
        self.em = Emitter(out / "events.jsonl", mode, usd_vnd)
        self.usd_vnd, self.max_run_usd, self.claude_model = usd_vnd, max_run_usd, claude_model
        self.t = at("20:00", -1)
        self.calls: list[dict] = []          # every model call (Claude and Meta), for summary.json
        self.pending: list[dict] = []        # Claude calls not yet attached to an event
        self.errors = 0
        self._log = (out / "log.jsonl").open("w", encoding="utf-8", newline="\n")
        self._claude = None
        self._asking: dict[str, list] = {}
        self.listing = ""                    # the listing the dang_ban scene is working on (sample key)

    # ── clock and events ───────────────────────────────────────────────────────────────────────────────
    def at(self, hhmm: str, day: int = 0) -> None:
        self.t = max(self.t, at(hhmm, day))

    def emit(self, type: str, step: float | None = None, **fields: Any) -> dict:
        self.t += self.STEP_S if step is None else step
        if self.listing and type not in ("scene", "metric", "move"):
            # every line about one listing carries its key, so the stage can follow it from words to verdict
            fields["detail"] = {**(fields.get("detail") or {}), "listing": self.listing}
        return self.em.emit(self.t, type, **fields)

    def add(self, **amounts: float) -> None:
        self.em.add(**amounts)

    def metric(self) -> dict:
        return self.em.metric(self.t)

    def log(self, record: dict) -> None:
        self._log.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._log.flush()

    def close(self) -> None:
        self.em.close()
        self._log.close()

    @property
    def spent_usd(self) -> float:
        return sum(c["cost_usd"] for c in self.calls)

    # ── usage ──────────────────────────────────────────────────────────────────────────────────────────
    def mock_usage(self) -> Usage:
        return Usage(provider="claude", model="mock", endpoint="mock")

    def take_usage(self) -> Usage | None:
        """All Claude calls since the last event that carried usage, summed into one usage block."""
        if self.mode == "mock":
            return self.mock_usage()
        calls, self.pending = self.pending, []
        if not calls:
            return None
        return Usage(provider="claude", model=calls[0]["model"], endpoint="messages",
                     input_tokens=sum(c["input_tokens"] for c in calls),
                     output_tokens=sum(c["output_tokens"] for c in calls),
                     cached_tokens=sum(c["cached_tokens"] for c in calls),
                     cache_write_tokens=sum(c["cache_write_tokens"] for c in calls),
                     ms=sum(c["ms"] for c in calls), cost_usd=round(sum(c["cost_usd"] for c in calls), 6))

    def record_claude(self, agent: str, response: Any, ms: int) -> None:
        u = response.usage
        rec = {"provider": "claude", "agent": agent, "model": response.model or self.claude_model,
               "endpoint": "messages", "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
               "cached_tokens": u.cache_read_input_tokens or 0, "cache_write_tokens": u.cache_creation_input_tokens or 0,
               "images": 0, "audio_seconds": 0.0, "ms": ms}
        rec["cost_usd"] = round(prices.claude_cost(self.claude_model, u.input_tokens, u.output_tokens,
                                                   rec["cached_tokens"], rec["cache_write_tokens"]), 6)
        self.calls.append(rec)
        self.pending.append(rec)
        self.log({"at": time.time(), **rec, "stop_reason": response.stop_reason,
                  "content": [b.model_dump() for b in response.content]})

    # ── tools ──────────────────────────────────────────────────────────────────────────────────────────
    def call(self, agent: str, name: str, **args: Any) -> dict:
        """Run a tool for an agent and write the tool event (usage: the model call that asked for it)."""
        try:
            out = getattr(self.tools, name)(**args)
            ok = True
        except Exception as exc:          # a bad argument from the model is the model's error, shown as such
            out, ok = {"error": f"{type(exc).__name__}: {exc}"}, False
        inp, outp = summarize(name, args, out) if ok else (str(args)[:120], out["error"][:160])
        self.emit("tool", from_=agent, tool={"name": name, "input_summary": inp, "output_summary": outp, "ok": ok},
                  usage=self.take_usage(), detail={"args": args, "output": out})
        for record in self._asking.values():
            record.append((name, out))
        return out

    # ── asking an agent ────────────────────────────────────────────────────────────────────────────────
    async def ask(self, agent: str, instruction: str, tools: list[str], mock_calls: list[tuple[str, dict]],
                  mock_text: Callable[[dict[str, list[dict]]], str],
                  mock_messages: Callable[[dict[str, list[dict]]], list[dict]] | None = None,
                  required: tuple[str, ...] = (), listeners: tuple[str, ...] = (),
                  check: Callable[[Reply, dict[str, list[dict]]], str] | None = None) -> Asked:
        """Ask an agent to do a task with some tools. Returns what it said and every tool output it read.

        Mock runs `mock_calls` in order and words the result with `mock_text`. Live hands the instruction to the
        agent's ReActAgent (inside a MsgHub with `listeners`, who hear the answer), checks that it called the
        `required` tools, that its text fits the bubble and passes `check` (numbers that must match the tools); one
        retry, then an error event.
        """
        self._asking[agent] = calls = []
        try:
            if self.mode == "mock":
                for name, args in mock_calls:
                    self.call(agent, name, **args)
                outputs = _by_name(calls)
                return Asked(mock_text(outputs), (mock_messages or (lambda o: []))(outputs), outputs,
                             self.take_usage())
            hub = await self.claude()
            reply = await hub.ask(agent, instruction, tools, listeners)
            missing = [r for r in required if r not in {n for n, _ in calls}]
            problem = _problem(reply, missing) or (check(reply, _by_name(calls)) if check and reply else "")
            if problem:
                reply = await hub.ask(agent, f"Chưa đạt: {problem}. Làm lại cho đúng.", tools, listeners)
                missing = [r for r in required if r not in {n for n, _ in calls}]
                problem = _problem(reply, missing) or (check(reply, _by_name(calls)) if check and reply else "")
            if problem:
                self.errors += 1
                self.emit("error", from_=agent, text=f"Agent trả lời chưa đạt: {problem}"[:TEXT_MAX],
                          detail={"reply": reply.model_dump() if reply else None})
                for name in missing:          # the work still has to be done: run the tool, labelled as code
                    args = dict(next((a for n, a in mock_calls if n == name), {}))
                    self.call(agent, name, **args)
                outputs = _by_name(calls)
                reply = Reply(text=_fit(reply.text if reply else "") or mock_text(outputs),
                              messages=(mock_messages or (lambda o: []))(outputs) if check else
                              (reply.messages if reply else []))
            return Asked(reply.text, reply.messages, _by_name(calls), self.take_usage())
        finally:
            self._asking.pop(agent, None)

    async def claude(self):
        if self._claude is None:
            from sim.agents.claude import ClaudeHub
            self._claude = ClaudeHub(self)
        return self._claude


def _by_name(calls: list[tuple[str, dict]]) -> dict[str, list[dict]]:
    outputs: dict[str, list[dict]] = {}
    for name, out in calls:
        outputs.setdefault(name, []).append(out)
    return outputs


def _fit(text: str) -> str:
    return text if len(text) <= TEXT_MAX else ""


def _problem(reply: Reply | None, missing: list[str]) -> str:
    if reply is None:
        return "không có câu trả lời có cấu trúc"
    if missing:
        return "chưa gọi công cụ " + ", ".join(missing)
    long = [m for m in [reply.text] + [m.get("text", "") for m in reply.messages] if len(m) > TEXT_MAX]
    if long:
        return f"câu dài quá {TEXT_MAX} ký tự: {long[0][:40]}..."
    if not any(ch.isdigit() for ch in reply.text):
        return "câu chưa có số cụ thể"
    return ""


def env_settings() -> dict:
    return {"usd_vnd": int(os.environ.get("USD_VND", prices.USD_VND_DEFAULT)),
            "max_run_usd": float(os.environ.get("MAX_RUN_USD", "40")),
            "claude_model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")}


__all__ = ["Asked", "BudgetStop", "Day", "Reply", "TOOL_PARAMS", "ValidationError", "env_settings", "summarize"]
