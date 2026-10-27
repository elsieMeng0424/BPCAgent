"""Responses transport for the existing BasicAgent function-tool protocol."""

from copy import deepcopy

from .exceptions import ConfigException, LLMException
from .messages import LLMResponse, ToolCall


def invoke_responses(client, messages, options):
    options = deepcopy(options)
    options.pop("stream", None)
    for key in ("max_tokens", "max_completion_tokens"):
        if key in options:
            options["max_output_tokens"] = options.pop(key)
    if "reasoning_effort" in options:
        options["reasoning"] = {"effort": options.pop("reasoning_effort")}
    if "tools" in options:
        options["tools"] = [{"type": "function", **t["function"]} for t in options["tools"]]
    choice = options.get("tool_choice")
    if isinstance(choice, dict) and "function" in choice:
        options["tool_choice"] = {"type": "function", "name": choice["function"]["name"]}
    if any(key in options for key in ("input", "previous_response_id", "conversation", "instructions")):
        raise ConfigException("Responses input and history are owned by the Agent")
    inputs = []
    for message in messages:
        if message.get("response_items") is not None:
            inputs.extend(deepcopy(message["response_items"]))
        elif message["role"] == "tool":
            inputs.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                           "output": message["content"]})
        elif message.get("tool_calls"):
            if message.get("content"):
                inputs.append({"role": "assistant", "content": message["content"]})
            inputs.extend({"type": "function_call", "call_id": c["id"],
                           "name": c["function"]["name"], "arguments": c["function"]["arguments"]}
                          for c in message["tool_calls"])
        else:
            inputs.append({"role": message["role"], "content": message["content"]})
    options["store"] = False
    try:
        response = client.responses.create(input=inputs, **options)
        if response.status != "completed":
            raise LLMException(f"Responses API did not finish normally: {response.status}")
        items = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
        calls, text = [], []
        for item in items:
            if item["type"] == "function_call":
                calls.append(ToolCall(id=item["call_id"], name=item["name"], arguments=item["arguments"]))
            elif item["type"] == "message":
                for content in item["content"]:
                    if content["type"] == "refusal":
                        raise LLMException("The model refused the request")
                    if content["type"] == "output_text":
                        text.append(content["text"])
            elif item["type"] != "reasoning":
                raise LLMException("Unsupported Responses output item type")
        return LLMResponse(content="".join(text) or None, tool_calls=calls, response_items=items,
                           finish_reason="tool_calls" if calls else "stop")
    except LLMException:
        raise
    except Exception as exc:
        raise LLMException(f"Responses API call failed ({type(exc).__name__})") from exc
