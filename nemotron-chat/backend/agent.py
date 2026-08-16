"""
Agent loop: drives the model through a ReAct-style tool-calling loop.

Read-only tools execute immediately. write_file/edit_file are diffed and
paused for user approval — the generator yields an 'awaiting_approval'
event and stops; a follow-up call resumes exactly where it left off.
"""
import json
import uuid
from dataclasses import dataclass, field

import tools as T

MAX_MODEL_CALLS_PER_TURN = 20


@dataclass
class LoopState:
    tool_calls: list          # [{id, name, arguments(dict)}]
    index: int = 0
    results: list = field(default_factory=list)   # [{tool_call_id, content}]
    assistant_message: dict = None
    pending_diff: dict = None  # set while waiting on an approval


@dataclass
class Session:
    id: str
    workspace: str
    messages: list = field(default_factory=list)
    loop: LoopState = None
    model_calls_this_turn: int = 0


SESSIONS: dict[str, Session] = {}


def create_session(workspace: str) -> Session:
    sid = str(uuid.uuid4())
    session = Session(id=sid, workspace=workspace, messages=[{"role": "system", "content": T.AGENT_SYSTEM_PROMPT}])
    SESSIONS[sid] = session
    return session


def get_session(session_id: str) -> Session | None:
    return SESSIONS.get(session_id)


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _execute_read_tool(session: Session, name: str, args: dict) -> str:
    try:
        if name == "list_directory":
            return T.list_directory(session.workspace, args.get("path", "."))
        if name == "read_file":
            return T.read_file(session.workspace, args["path"], args.get("start_line"), args.get("end_line"))
        if name == "search_code":
            return T.search_code(session.workspace, args["pattern"], args.get("path", "."))
    except T.UnsafePathError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error running {name}: {e}"
    return f"Error: unknown tool '{name}'"


def _propose_write_tool(session: Session, name: str, args: dict) -> dict:
    """Returns {ok, diff, ...} without touching disk."""
    if name == "write_file":
        return T.propose_write_file(session.workspace, args["path"], args["content"])
    if name == "edit_file":
        return T.propose_edit_file(session.workspace, args["path"], args["old_string"], args["new_string"])
    return {"ok": False, "error": f"unknown write tool '{name}'"}


def run_loop(client, model: str, session: Session):
    """Generator yielding SSE-formatted strings. Call again after approve() to resume."""
    session.model_calls_this_turn = 0

    while True:
        if session.loop is None:
            if session.model_calls_this_turn >= MAX_MODEL_CALLS_PER_TURN:
                yield sse({"type": "error", "message": "Hit the per-turn tool-call limit — ask a follow-up to continue."})
                return
            session.model_calls_this_turn += 1

            yield from _call_model(client, model, session)
            if session.loop is None:
                # model returned plain text, no tool calls — turn is over
                yield sse({"type": "done"})
                return

        # Process tool calls in this loop state, in order, starting at .index
        while session.loop.index < len(session.loop.tool_calls):
            tc = session.loop.tool_calls[session.loop.index]
            name, args = tc["name"], tc["arguments"]

            if name in T.READ_ONLY_TOOLS:
                yield sse({"type": "tool_call", "name": name, "args": args})
                result = _execute_read_tool(session, name, args)
                session.loop.results.append({"tool_call_id": tc["id"], "content": result})
                yield sse({"type": "tool_result", "name": name, "content": result[:3000]})
                session.loop.index += 1
                continue

            if name in T.WRITE_TOOLS:
                proposal = _propose_write_tool(session, name, args)
                if not proposal.get("ok"):
                    # invalid proposal — surface as an error result, no approval needed
                    err = proposal.get("error", "invalid request")
                    yield sse({"type": "tool_call", "name": name, "args": args})
                    yield sse({"type": "tool_result", "name": name, "content": f"Error: {err}"})
                    session.loop.results.append({"tool_call_id": tc["id"], "content": f"Error: {err}"})
                    session.loop.index += 1
                    continue

                # valid — needs user approval before it's applied
                session.loop.pending_diff = proposal
                yield sse({
                    "type": "pending_approval",
                    "tool_call_id": tc["id"],
                    "name": name,
                    "path": args.get("path"),
                    "diff": proposal["diff"],
                })
                yield sse({"type": "awaiting_approval"})
                return  # stop here; resume() picks this back up

            # unknown tool name
            err = f"Unknown tool '{name}'"
            session.loop.results.append({"tool_call_id": tc["id"], "content": err})
            yield sse({"type": "tool_result", "name": name, "content": err})
            session.loop.index += 1

        # all tool calls in this turn are resolved — fold them into history
        session.messages.append(session.loop.assistant_message)
        for r in session.loop.results:
            session.messages.append({"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["content"]})
        session.loop = None
        # loop back around to call the model again with the new context


def resume_after_approval(client, model: str, session: Session, tool_call_id: str, approved: bool):
    """Apply/skip the pending write, record the result, and continue the loop."""
    if session.loop is None or session.loop.pending_diff is None:
        yield sse({"type": "error", "message": "No pending approval for this session."})
        return

    tc = session.loop.tool_calls[session.loop.index]
    if tc["id"] != tool_call_id:
        yield sse({"type": "error", "message": "Approval does not match the pending tool call."})
        return

    proposal = session.loop.pending_diff
    if approved:
        if tc["name"] == "write_file":
            result = T.apply_write_file(proposal["target"], tc["arguments"]["content"])
        else:  # edit_file
            result = T.apply_edit_file(proposal["target"], proposal["new_text"])
        yield sse({"type": "tool_result", "name": tc["name"], "content": result})
    else:
        result = "User rejected this change. Do not repeat the same edit unless explicitly asked to."
        yield sse({"type": "tool_result", "name": tc["name"], "content": result})

    session.loop.results.append({"tool_call_id": tc["id"], "content": result})
    session.loop.pending_diff = None
    session.loop.index += 1

    yield from run_loop(client, model, session)


def _call_model(client, model: str, session: Session):
    """Streams one model turn; sets session.loop if the model wants to call tools."""
    tool_calls_acc: dict[int, dict] = {}
    full_text = ""
    finish_reason = None

    try:
        stream = client.chat.completions.create(
            model=model,
            messages=session.messages,
            tools=T.TOOL_SPECS,
            tool_choice="auto",
            temperature=0.4,
            max_tokens=4096,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            if getattr(delta, "content", None):
                full_text += delta.content
                yield sse({"type": "token", "content": delta.content})

            if getattr(delta, "tool_calls", None):
                for tcd in delta.tool_calls:
                    entry = tool_calls_acc.setdefault(tcd.index, {"id": None, "name": None, "arguments": ""})
                    if tcd.id:
                        entry["id"] = tcd.id
                    if tcd.function:
                        if tcd.function.name:
                            entry["name"] = tcd.function.name
                        if tcd.function.arguments:
                            entry["arguments"] += tcd.function.arguments

            if choice.finish_reason:
                finish_reason = choice.finish_reason

    except Exception as e:
        yield sse({"type": "error", "message": str(e)})
        return

    if finish_reason == "tool_calls" and tool_calls_acc:
        ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
        parsed = []
        raw_tool_calls_for_message = []
        for e in ordered:
            try:
                args = json.loads(e["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            parsed.append({"id": e["id"], "name": e["name"], "arguments": args})
            raw_tool_calls_for_message.append({
                "id": e["id"],
                "type": "function",
                "function": {"name": e["name"], "arguments": e["arguments"] or "{}"},
            })

        assistant_message = {
            "role": "assistant",
            "content": full_text or None,
            "tool_calls": raw_tool_calls_for_message,
        }
        session.loop = LoopState(tool_calls=parsed, assistant_message=assistant_message)
    else:
        session.messages.append({"role": "assistant", "content": full_text})
