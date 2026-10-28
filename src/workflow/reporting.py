"""Readable turn reports for live notebook output and saved-run inspection."""

import json
from pathlib import Path
import sys


ERROR_HINTS = {
    "context_budget_exceeded": "The prepared request exceeded the local context budget before the next model call.",
    "step_limit_exceeded": "The Agent used every allowed model/tool iteration without reaching a final response.",
    "missing_structured_output": "The Agent did not complete a valid submit_output call.",
    "model_error": "The model request failed; inspect the transcript and console log.",
}


def validate_print_limit(max_chars):
    if max_chars is not None and (type(max_chars) is not int or max_chars < 1):
        raise ValueError("max_print_chars must be a positive integer or None for full output")


class ConsoleTee:
    """Mirror console writes to the experiment log and the caller's stream."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def _load(path):
    """Missing/partial artifacts should not hide the rest of a failed-run report."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def print_turn_report(turn, task_dir, *, max_chars=4000, stream=None):
    """Print one persisted Agent turn, including failed and partially saved turns.

    Limit only display text, never the saved artifacts or model context. Typed
    stage output, failure codes and context-budget events are always shown in full.
    Provider reasoning text is not included in the report.
    """
    validate_print_limit(max_chars)
    stream = sys.stdout if stream is None else stream
    directory = Path(task_dir) / f"{turn['turn']:03d}_{turn['agent']}"

    def emit(label, value, *, limited=True):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        if limited and max_chars is not None and len(text) > max_chars:
            remaining = len(text) - max_chars
            text = text[:max_chars] + f"\n... [display truncated: {remaining} more characters; full data: {directory}]"
        print(f"{label}:\n{text}", file=stream, flush=True)

    print(f"\n=== Turn {turn['turn']:03d} | {turn['agent']} | {turn['status']} ===", file=stream, flush=True)
    emit("Turn", {key: turn.get(key) for key in (
        "stage", "attempt", "kind", "reviews_turn", "status", "error", "patient_state_sha256")}, limited=False)
    if turn.get("error"):
        emit("Failure", ERROR_HINTS.get(turn["error"], turn["error"]), limited=False)
    context = _load(directory / "context.json")
    if context is not None:
        emit("Stage context / input", context)

    transcript = _load(directory / "transcript.json")
    if isinstance(transcript, list):
        exchange = 0
        for message in transcript:
            role = message.get("role")
            if role == "user":
                emit("User request", message.get("content"))
            elif role == "assistant":
                exchange += 1
                print(f"\nModel response {exchange}", file=stream, flush=True)
                if message.get("content"):
                    emit("Assistant output", message["content"])
                for call in message.get("tool_calls", []):
                    function = call.get("function", {})
                    arguments = function.get("arguments", "")
                    try:
                        arguments = json.loads(arguments)
                    except (ValueError, TypeError):
                        pass
                    emit(f"Tool call {function.get('name')} [{call.get('id')}]", arguments)
            elif role == "tool":
                content = message.get("content", "")
                try:
                    result = json.loads(content)
                except (ValueError, TypeError):
                    result = None
                if isinstance(result, dict):
                    # Print failures separately so display truncation cannot hide them.
                    emit(f"Tool result [{message.get('tool_call_id')}]", {
                        key: result.get(key) for key in ("tool_name", "status", "error")}, limited=False)
                    if "data" in result:
                        data = result["data"]
                        if isinstance(data, dict):
                            flags = {key: data[key] for key in (
                                "exit_code", "timed_out", "oom_killed", "stdout_truncated", "stderr_truncated",
                                "offset", "next_offset", "total_chars", "complete") if key in data}
                            if flags:
                                emit("Execution / pagination", flags, limited=False)
                        emit("Tool data", result["data"])
                else:
                    emit(f"Tool result [{message.get('tool_call_id')}]", content)
    else:
        emit("Transcript", "Not available; the turn may have failed before the model/tool loop started.", limited=False)

    emit("Structured output", turn.get("output"), limited=False)
    audit = _load(directory / "context_usage.json")
    if isinstance(audit, dict):
        emit("Context policy", audit.get("policy"), limited=False)
        emit("Context budget events", audit.get("events", []), limited=False)
    shell = _load(directory / "shell_execution.json")
    if shell is not None:
        emit("Shell execution audit", shell, limited=False)
    emit("Artifacts (relative to experiment directory)", turn.get("artifacts", {}), limited=False)
    emit("Turn directory", str(directory), limited=False)


def print_workflow_report(result_or_directory, *, max_chars=4000, stream=None):
    """Replay detailed output from an in-memory result or an experiment directory.

    This function only reads saved artifacts; it never calls a model or a tool.
    """
    validate_print_limit(max_chars)
    stream = sys.stdout if stream is None else stream
    if isinstance(result_or_directory, dict):
        result = result_or_directory
        directory = Path(result["task_dir"])
    else:
        directory = Path(result_or_directory).resolve()
        result = _load(directory / "workflow_result.json")
        if result is None:
            # An interrupted run may have turn outputs without a final result.
            result = {"status": "incomplete", "finished": False, "turns": [
                turn for path in sorted(directory.glob("[0-9]*_*/output.json"))
                if isinstance(turn := _load(path), dict)]}
            if not result["turns"]:
                raise ValueError("No saved workflow result or turn outputs found in this directory")
    print(json.dumps({"status": result.get("status"), "finished": result.get("finished"),
                      "task_dir": str(directory), "log": str(directory / "logs/console.log")},
                     ensure_ascii=False, indent=2), file=stream, flush=True)
    for turn in result.get("turns", []):
        print_turn_report(turn, directory, max_chars=max_chars, stream=stream)
