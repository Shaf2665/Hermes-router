"""Stateless Responses API bridge for local coding clients.

No transcript store, credential handling, tool execution, or cross-turn retries.
Only client-executed function/custom tools are supported. Provider-private
reasoning is never forwarded to another provider.
"""
import copy
import json
import time
import uuid

MAX_BUFFER = 16 * 1024 * 1024


def _string(value, field, empty=False):
    if not isinstance(value, str) or (not empty and not value):
        raise ValueError(f"{field} must be a string")
    return value


def _object(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _arguments(value):
    _string(value, "arguments")
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        raise ValueError("arguments must contain a JSON object") from None
    _object(parsed, "arguments")
    return value


def _content(value, role):
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ValueError("message content must be text or an array")
    blocks = []
    for block in value:
        _object(block, "content block")
        kind = block.get("type")
        if kind in {"input_text", "output_text", "text"}:
            blocks.append({"type": "text", "text": _string(block.get("text"), "text", True)})
        elif kind == "input_image" and role == "user":
            image = {"url": _string(block.get("image_url"), "image_url")}
            if block.get("detail") in {"auto", "low", "high"}:
                image["detail"] = block["detail"]
            blocks.append({"type": "image_url", "image_url": image})
        else:
            raise ValueError("Unsupported content block; use text or user image URLs")
    if all(b["type"] == "text" for b in blocks):
        return "\n".join(b["text"] for b in blocks)
    return blocks


def to_chat(body, default_model):
    """Validate before admission, then return chat payload and custom tool names."""
    _object(body, "request body")
    for field in ("previous_response_id", "conversation", "background", "store"):
        if body.get(field):
            raise ValueError(f"{field} is unsupported; send full input history with store=false")
    if body.get("truncation", "disabled") != "disabled":
        raise ValueError("Automatic truncation is unsupported")
    if type(body.get("stream", False)) is not bool:
        raise ValueError("stream must be a boolean")
    payload = {"model": _string(body.get("model", default_model), "model"),
               "stream": body.get("stream", False), "messages": []}
    if body.get("instructions") is not None:
        payload["messages"].append({"role": "system", "content": _string(body["instructions"], "instructions", True)})
    items = body.get("input")
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    if not isinstance(items, list) or not items:
        raise ValueError("input must be text or a nonempty array")
    calls, results = set(), set()
    for item in items:
        _object(item, "input item")
        kind = item.get("type", "message")
        if kind == "message":
            role = item.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                raise ValueError("Unsupported message role")
            payload["messages"].append({"role": role, "content": _content(item.get("content"), role)})
        elif kind in {"function_call", "custom_tool_call"}:
            call_id = _string(item.get("call_id"), "call_id")
            if call_id in calls:
                raise ValueError("Duplicate call_id")
            calls.add(call_id)
            arguments = (_arguments(item.get("arguments")) if kind == "function_call" else
                         json.dumps({"input": _string(item.get("input"), "input", True)}))
            call = {"id": call_id, "type": "function", "function": {
                "name": _string(item.get("name"), "tool name"), "arguments": arguments}}
            messages = payload["messages"]
            if messages and messages[-1]["role"] == "assistant":
                messages[-1].setdefault("tool_calls", []).append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = _string(item.get("call_id"), "call_id")
            if call_id not in calls or call_id in results:
                raise ValueError("Tool output must match one preceding call_id")
            results.add(call_id)
            payload["messages"].append({"role": "tool", "tool_call_id": call_id,
                                        "content": _content(item.get("output"), "user")})
        elif kind == "reasoning":
            # Opaque encrypted reasoning is provider-bound, not conversation text.
            continue
        else:
            raise ValueError("Unsupported input item; send messages and client tool history")
    if not payload["messages"] or calls != results:
        raise ValueError("Full history must include results for every tool call")
    custom, names, tools = set(), set(), body.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError("tools must be an array")
    translated = []
    for tool in tools:
        _object(tool, "tool")
        if tool.get("type") not in {"function", "custom"}:
            raise ValueError("Only client-executed function and custom tools are supported; disable built-in web search")
        name = _string(tool.get("name"), "tool name")
        if name in names:
            raise ValueError("Tool names must be unique")
        names.add(name)
        function = {"name": name, "description": _string(tool.get("description", ""), "description", True)}
        if tool["type"] == "custom":
            custom.add(name)
            function["parameters"] = {"type": "object", "properties": {"input": {"type": "string"}},
                                      "required": ["input"], "additionalProperties": False}
            function["description"] += "\nProvide the complete raw tool input in the input string."
            fmt = _object(tool.get("format", {"type": "text"}), "custom tool format")
            if fmt.get("type") not in {"text", "grammar"}:
                raise ValueError("Unsupported custom tool format")
            if fmt.get("type") == "grammar":
                function["description"] += "\nRequired input grammar: " + json.dumps(tool["format"], ensure_ascii=False)
        else:
            function["parameters"] = _object(tool.get("parameters", {"type": "object", "properties": {}}), "parameters")
            if "strict" in tool:
                if type(tool["strict"]) is not bool:
                    raise ValueError("strict must be a boolean")
                function["strict"] = tool["strict"]
        translated.append({"type": "function", "function": function})
    if translated:
        payload["tools"] = translated
    choice = body.get("tool_choice")
    if choice is not None:
        if isinstance(choice, str) and choice in {"auto", "none", "required"}:
            payload["tool_choice"] = choice
        elif isinstance(choice, dict) and choice.get("type") in {"function", "custom"} and choice.get("name") in names:
            payload["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        else:
            raise ValueError("Unsupported tool_choice")
    if "parallel_tool_calls" in body:
        if type(body["parallel_tool_calls"]) is not bool:
            raise ValueError("parallel_tool_calls must be a boolean")
        payload["parallel_tool_calls"] = body["parallel_tool_calls"]
    if "max_output_tokens" in body:
        if type(body["max_output_tokens"]) is not int or body["max_output_tokens"] <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        payload["max_tokens"] = body["max_output_tokens"]
    for field in ("temperature", "top_p"):
        if field in body:
            import math
            value = body[field]
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= (2 if field == "temperature" else 1):
                raise ValueError(f"Invalid {field}")
            payload[field] = value
    if "text" in body:
        fmt = _object(_object(body["text"], "text").get("format", {"type": "text"}), "text.format")
        if fmt.get("type") == "json_schema":
            payload["response_format"] = {"type": "json_schema", "json_schema": {k: v for k, v in fmt.items() if k != "type"}}
        elif fmt.get("type") == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif fmt.get("type") != "text":
            raise ValueError("Unsupported text.format")
    return payload, custom


def _chunks(source):
    """Read bounded SSE events, including split UTF-8, CRLF and multi-data lines."""
    pending, data = b"", []
    for chunk in source:
        pending += chunk.encode() if isinstance(chunk, str) else chunk
        if len(pending) + sum(map(len, data)) > MAX_BUFFER:
            raise ValueError("Stream event too large")
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if line.startswith(b"data:"):
                data.append(line[5:].lstrip(b" "))
            elif not line and data:
                raw, data = b"\n".join(data), []
                if raw == b"[DONE]":
                    return
                yield json.loads(raw)
    if pending or data:
        raise ValueError("Incomplete SSE event")


class ResponseBuilder:
    def __init__(self, model, custom=(), names=()):
        self.response = {"id": "resp_" + uuid.uuid4().hex, "object": "response", "created_at": int(time.time()),
                         "status": "in_progress", "model": model, "output": [], "error": None,
                         "incomplete_details": None, "usage": None, "store": False}
        self.custom, self.names = set(custom), set(names)
        self.text_index, self.calls, self.sequence, self.finish = None, {}, 0, None
        self.size = 0

    def event(self, kind, **fields):
        result = {"type": kind, "sequence_number": self.sequence, **copy.deepcopy(fields)}
        self.sequence += 1
        return result

    def start(self):
        yield self.event("response.created", response=self.response)
        yield self.event("response.in_progress", response=self.response)

    def feed(self, chunk):
        if not isinstance(chunk, dict) or chunk.get("error"):
            raise ValueError("Upstream stream failed")
        self.size += len(json.dumps(chunk).encode())
        if self.size > MAX_BUFFER:
            raise ValueError("Response too large")
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            def count(name):
                value = usage.get(name, 0)
                return value if type(value) is int and value >= 0 else 0
            pt, ct = count("prompt_tokens"), count("completion_tokens")
            self.response["usage"] = {"input_tokens": pt, "output_tokens": ct, "total_tokens": pt + ct,
                "input_tokens_details": {"cached_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)},
                "output_tokens_details": {"reasoning_tokens": usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)}}
        choices = chunk.get("choices") or []
        if not choices:
            return
        if len(choices) != 1 or choices[0].get("index", 0) != 0:
            raise ValueError("Expected one response choice")
        choice = choices[0]
        delta = choice.get("delta") or {}
        if self.finish and (delta.get("content") or delta.get("tool_calls")):
            raise ValueError("Content after finish")
        outputs = self.response["output"]
        text = delta.get("content")
        if text:
            _string(text, "text")
            if self.text_index is None:
                self.text_index = len(outputs)
                item = {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "status": "in_progress", "content": []}
                outputs.append(item)
                yield self.event("response.output_item.added", output_index=self.text_index, item=item)
                part = {"type": "output_text", "text": "", "annotations": [], "logprobs": []}
                item["content"].append(part)
                yield self.event("response.content_part.added", item_id=item["id"], output_index=self.text_index, content_index=0, part=part)
            item = outputs[self.text_index]
            item["content"][0]["text"] += text
            yield self.event("response.output_text.delta", item_id=item["id"], output_index=self.text_index, content_index=0, delta=text, logprobs=[])
        for call in delta.get("tool_calls") or []:
            index = call.get("index")
            if type(index) is not int or index < 0 or index > 1024:
                raise ValueError("Invalid tool index")
            state = self.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if call.get("id"):
                if state["id"] and state["id"] != call["id"]:
                    raise ValueError("Changed call ID")
                state["id"] = _string(call["id"], "call_id")
            function = call.get("function") or {}
            # Buffer tools until the upstream turn completes; a broken stream
            # must never expose an executable output_item.done.
            for field in ("name", "arguments"):
                if function.get(field) is not None:
                    state[field] += _string(function[field], field, True)
        if choice.get("finish_reason") is not None:
            self.finish = choice["finish_reason"]

    def complete(self):
        if self.finish not in {"stop", "tool_calls", "length", "content_filter"}:
            raise ValueError("Upstream did not finish")
        if self.calls and self.finish != "tool_calls":
            raise ValueError("Incomplete tool calls")
        # Validate every call before emitting any executable item.
        completed_calls, ids = [], set()
        for call in self.calls.values():
            if not call["id"] or call["id"] in ids or call["name"] not in self.names:
                raise ValueError("Invalid upstream tool call")
            ids.add(call["id"])
            arguments = _arguments(call["arguments"])
            custom = call["name"] in self.custom
            item = {"id": ("ctc_" if custom else "fc_") + uuid.uuid4().hex,
                    "type": "custom_tool_call" if custom else "function_call", "call_id": call["id"],
                    "name": call["name"], "status": "completed"}
            if custom:
                parsed = json.loads(arguments)
                if set(parsed) != {"input"}:
                    raise ValueError("Invalid custom tool input")
                item["input"] = _string(parsed["input"], "input", True)
            else:
                item["arguments"] = arguments
            completed_calls.append(item)
        if self.finish == "tool_calls" and not completed_calls:
            raise ValueError("Missing tool calls")
        outputs = self.response["output"]
        if self.text_index is not None:
            item = outputs[self.text_index]
            item["status"] = "completed" if self.finish in {"stop", "tool_calls"} else "incomplete"
            part = item["content"][0]
            yield self.event("response.output_text.done", item_id=item["id"], output_index=self.text_index, content_index=0, text=part["text"], logprobs=[])
            yield self.event("response.content_part.done", item_id=item["id"], output_index=self.text_index, content_index=0, part=part)
            yield self.event("response.output_item.done", output_index=self.text_index, item=item)
        for item in completed_calls:
            index = len(outputs)
            field = "input" if item["type"] == "custom_tool_call" else "arguments"
            kind = "custom_tool_call_input" if field == "input" else "function_call_arguments"
            yield self.event("response.output_item.added", output_index=index, item={**item, field: "", "status": "in_progress"})
            yield self.event("response." + kind + ".delta", item_id=item["id"], output_index=index, delta=item[field])
            yield self.event("response." + kind + ".done", item_id=item["id"], output_index=index, **{field: item[field]})
            outputs.append(item)
            yield self.event("response.output_item.done", output_index=index, item=item)
        incomplete = self.finish in {"length", "content_filter"}
        self.response["status"] = "incomplete" if incomplete else "completed"
        self.response["incomplete_details"] = {"reason": "max_output_tokens" if self.finish == "length" else "content_filter"} if incomplete else None
        yield self.event("response.incomplete" if incomplete else "response.completed", response=self.response)

    def failed(self):
        self.response.update(status="failed", error={"code": "upstream_error", "message": "Provider response failed or was incomplete. Review the session before retrying."})
        return self.event("response.failed", response=self.response)


def from_chat(data, model, custom=(), names=()):
    builder = ResponseBuilder(model, custom, names)
    choice = data["choices"][0]
    message = choice["message"]
    calls = [{**c, "index": i} for i, c in enumerate(message.get("tool_calls") or [])]
    list(builder.feed({"choices": [{"delta": {"content": message.get("content"), "tool_calls": calls},
                                     "finish_reason": choice.get("finish_reason")}], "usage": data.get("usage")}))
    list(builder.complete())
    return builder.response


def stream(source, model, custom=(), names=()):
    builder = ResponseBuilder(model, custom, names)
    def encode(event):
        return ("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
    try:
        for event in builder.start():
            yield encode(event)
        for chunk in _chunks(source):
            for event in builder.feed(chunk):
                yield encode(event)
        for event in builder.complete():
            yield encode(event)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OSError):
        yield encode(builder.failed())
    finally:
        close = getattr(source, "close", None)
        if close:
            close()
