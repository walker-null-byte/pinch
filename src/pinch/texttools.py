"""Fallback parser for tool calls that a small local model writes as plain text.

Why: Gemma 4 has its own call syntax,

    <|tool_call>call:edit_file{file_path:<|"|>calc.py<|"|>,old_string:<|"|>a - b<|"|>,new_string:<|"|>a + b<|"|>}<tool_call|>

and an OpenAI-compatible server (LM Studio here) turns that into `tool_calls` only when its own parser is happy. With many tools, long
prompts or several turns the model drops the canonical form, and the server returns the text in `content` with the special tokens
stripped. Format reference: ai.google.dev/gemma/docs/core/prompt-formatting-gemma4; edge cases from vLLM's `vllm/tool_parsers/gemma4_*`.

This parser is deliberately STRICTER than vLLM's. It is used only on the local tier, only when `tool_calls` is empty, and it accepts
a reply only if ALL of these hold; otherwise it returns nothing and the reply stays ordinary text:

  1. The WHOLE reply is one or more calls (plus the model's own special tokens and whitespace). Prose before or after, quoting
     (`>`), code fences, inline backticks and calls in the middle of a sentence are all rejected.
  2. Every call names a tool that was actually offered in this request.
  3. The arguments parse completely: balanced braces, every `<|"|>` string closed, no stray characters, no duplicate keys.
  4. The arguments fit the tool's schema: every required argument present, no unknown argument, scalar types coerced only
     in the safe direction (number to string, numeric string to number, "true"/"false" to bool); anything else is rejected.
  5. At most MAX_CALLS calls per reply.

A parsed call is NOT trusted any more than a native one: the agent turns it into a normal assistant `tool_calls` message and runs
it through the same policy check, approval gate and executor. Strings are taken literally (no escape processing, as the format
defines), and nothing is ever evaluated: the Python-style form is read with `ast` and only literals are accepted.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field

DELIM = '<|"|>'
MAX_CALLS = 4
# Tokens the model or the server may leave around a call. Anything else around it makes the reply "prose".
_OPEN_TOKENS = ("<|tool_call>",)
_CLOSE_TOKENS = ("<tool_call|>", "<turn|>", "<eos>", "<end_of_turn>", "<|tool_response>")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+\.\d+(?:[eE][-+]?\d+)?")


class Reject(Exception):
    """The text is not a well-formed call. The message is a short reason for the session log."""


@dataclass
class TextCall:
    name: str
    arguments: dict
    form: str  # "gemma" | "python" | "json"


@dataclass
class TextParse:
    calls: list[TextCall] = field(default_factory=list)
    reason: str = ""  # why it was rejected (empty when calls were found or the text did not look like a call)
    looked_like_call: bool = False  # the text tried to be a call (so a rejection is worth a log line)


def parse_text_tool_calls(content: str | None, schemas: dict[str, dict]) -> TextParse:
    """`schemas` maps each OFFERED tool name to its JSON-schema `parameters`. Returns the calls, or why not."""
    text = (content or "").strip()
    if not text:
        return TextParse()
    looked = _looks_like_call(text, schemas)
    try:
        calls = _parse(text, schemas)
    except Reject as e:
        return TextParse(reason=str(e), looked_like_call=looked)
    return TextParse(calls=calls, looked_like_call=True)


def _looks_like_call(text: str, schemas: dict) -> bool:
    return ("call:" in text or "<|tool_call>" in text or "<tool_call|>" in text or DELIM in text
            or any(re.search(rf"\b{re.escape(n)}\s*[{{(]", text) for n in schemas) or text.startswith("{") and '"name"' in text)


def _parse(text: str, schemas: dict) -> list[TextCall]:
    if "```" in text or "`" in text:
        raise Reject("code fence or backticks around the call")
    if text.startswith("{") and '"name"' in text.split(DELIM)[0]:
        return [_validate(_parse_json(text), schemas)]
    calls: list[TextCall] = []
    pos, n = 0, len(text)
    while True:
        pos = _skip(text, pos, _OPEN_TOKENS)
        if pos >= n:
            break
        prefixed = text.startswith("call:", pos)
        if prefixed:
            pos += len("call:")
        m = _NAME.match(text, pos)
        if not m:
            raise Reject("prose or an unreadable start where a call should be")
        name, pos = m.group(0), m.end()
        if name not in schemas:
            raise Reject(f"'{name}' is not a tool that was offered")
        if pos < n and text[pos] == "{":
            args, pos = _parse_object(text, pos + 1)
            form = "gemma"
        elif pos < n and text[pos] == "(" and prefixed:
            args, pos = _parse_python_args(text, pos)
            form = "python"
        else:
            raise Reject("no argument list after the tool name")
        calls.append(_validate(TextCall(name, args, form), schemas))
        if len(calls) > MAX_CALLS:
            raise Reject("too many calls in one reply")
        pos = _skip(text, pos, _CLOSE_TOKENS)
    if not calls:
        raise Reject("no call found")
    return calls


def _skip(text: str, pos: int, tokens: tuple[str, ...]) -> int:
    """Skip whitespace and the given special tokens (any number, any order)."""
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        for tok in tokens:
            if text.startswith(tok, pos):
                pos += len(tok)
                break
        else:
            return pos
    return pos


# -- the Gemma value syntax ------------------------------------------------------------------------

def _ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _parse_object(s: str, i: int) -> tuple[dict, int]:
    """Parse `key:value,...}` starting just after `{`. Returns the dict and the index just after the closing `}`."""
    out: dict = {}
    n, first = len(s), True
    while True:
        i = _ws(s, i)
        if i >= n:
            raise Reject("arguments are not closed with }")
        if s[i] == "}":
            return out, i + 1
        if not first:
            if s[i] != ",":
                raise Reject("expected a comma between arguments")
            i = _ws(s, i + 1)
            if i < n and s[i] == "}":  # one trailing comma is harmless
                return out, i + 1
        first = False
        key, i = _parse_key(s, i)
        i = _ws(s, i)
        if i >= n or s[i] != ":":
            raise Reject(f"missing ':' after the key '{key}'")
        value, i = _parse_value(s, _ws(s, i + 1))
        if key in out:
            raise Reject(f"the argument '{key}' appears twice")
        out[key] = value


def _parse_key(s: str, i: int) -> tuple[str, int]:
    if s.startswith(DELIM, i):
        end = s.find(DELIM, i + len(DELIM))
        if end == -1:
            raise Reject("a key string is not closed")
        key = s[i + len(DELIM):end]
        if not _NAME.fullmatch(key):
            raise Reject("an argument name is not a plain identifier")
        return key, end + len(DELIM)
    m = _NAME.match(s, i)
    if not m:
        raise Reject("expected an argument name")
    return m.group(0), m.end()


def _parse_array(s: str, i: int) -> tuple[list, int]:
    out: list = []
    n, first = len(s), True
    while True:
        i = _ws(s, i)
        if i >= n:
            raise Reject("a list is not closed with ]")
        if s[i] == "]":
            return out, i + 1
        if not first:
            if s[i] != ",":
                raise Reject("expected a comma between list items")
            i = _ws(s, i + 1)
            if i < n and s[i] == "]":
                return out, i + 1
        first = False
        value, i = _parse_value(s, i)
        out.append(value)


def _parse_value(s: str, i: int) -> tuple[object, int]:
    if i >= len(s):
        raise Reject("a value is missing")
    if s.startswith(DELIM, i):
        end = s.find(DELIM, i + len(DELIM))
        if end == -1:
            raise Reject("a string is not closed with the string marker")
        return s[i + len(DELIM):end], end + len(DELIM)  # taken literally: newlines, quotes and braces inside are text
    if s[i] == "{":
        return _parse_object(s, i + 1)
    if s[i] == "[":
        return _parse_array(s, i + 1)
    j = i
    while j < len(s) and s[j] not in ",}]":
        j += 1
    token = s[i:j].strip()
    if token == "true":
        return True, j
    if token == "false":
        return False, j
    if token == "null":
        return None, j
    if _INT.fullmatch(token):
        return int(token), j
    if _FLOAT.fullmatch(token):
        return float(token), j
    raise Reject("a value is neither a marked string, a number, true/false/null, a list nor an object")


# -- the other two forms --------------------------------------------------------------------------

def _parse_python_args(s: str, i: int) -> tuple[dict, int]:
    """`call:name(key="value", n=5)`: find the matching `)` (aware of quotes), then read it with `ast`. Only literals are accepted."""
    depth, j, quote = 0, i, ""
    while j < len(s):
        c = s[j]
        if quote:
            if c == "\\":
                j += 1
            elif c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    else:
        raise Reject("the argument list is not closed with )")
    try:
        node = ast.parse("f" + s[i:j + 1], mode="eval").body
    except SyntaxError:
        raise Reject("the argument list is not valid") from None
    if not isinstance(node, ast.Call) or node.args or any(k.arg is None for k in node.keywords):
        raise Reject("only key=value arguments are accepted")
    out: dict = {}
    for kw in node.keywords:
        try:
            value = ast.literal_eval(kw.value)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            raise Reject(f"the value of '{kw.arg}' is not a plain literal") from None
        if kw.arg in out:
            raise Reject(f"the argument '{kw.arg}' appears twice")
        out[kw.arg] = value
    return out, j + 1


def _parse_json(text: str) -> TextCall:
    try:
        data = json.loads(text)
    except ValueError:
        raise Reject("not valid JSON") from None
    if not isinstance(data, dict) or not set(data) <= {"name", "arguments", "parameters"} or not isinstance(data.get("name"), str):
        raise Reject("JSON that is not exactly a name plus arguments")
    args = data.get("arguments", data.get("parameters", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            raise Reject("the arguments are not valid JSON") from None
    if not isinstance(args, dict):
        raise Reject("the arguments are not an object")
    return TextCall(data["name"], args, "json")


# -- checking against the tool's schema ---------------------------------------------------------------

def _validate(call: TextCall, schemas: dict) -> TextCall:
    if call.name not in schemas:
        raise Reject(f"'{call.name}' is not a tool that was offered")
    schema = schemas[call.name] or {}
    props = schema.get("properties", {})
    unknown = sorted(set(call.arguments) - set(props))
    if unknown:
        raise Reject(f"unknown argument(s) for {call.name}: {', '.join(unknown)}")
    missing = [k for k in schema.get("required", []) if k not in call.arguments]
    if missing:
        raise Reject(f"missing required argument(s) for {call.name}: {', '.join(missing)}")
    fixed = {k: _coerce(k, v, props[k].get("type")) for k, v in call.arguments.items()}
    return TextCall(call.name, fixed, call.form)


def _coerce(key: str, value, kind):
    if kind is None or value is None:
        return value
    bad = Reject(f"the argument '{key}' should be a {kind}")
    if kind == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        raise bad
    if kind == "integer":
        if isinstance(value, bool):
            raise bad
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and _INT.fullmatch(value.strip()):
            return int(value)
        raise bad
    if kind == "number":
        if isinstance(value, bool):
            raise bad
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str) and (_INT.fullmatch(value.strip()) or _FLOAT.fullmatch(value.strip())):
            return float(value)
        raise bad
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip() in ("true", "false"):
            return value.strip() == "true"
        raise bad
    if kind == "array":
        if isinstance(value, list):
            return value
        raise bad
    if kind == "object":
        if isinstance(value, dict):
            return value
        raise bad
    return value
