import json
import logging

from arkray.core.context import ExecutionContext, bind_context, reset_context
from arkray.core.logging import ConsoleFormatter, ContextFilter, JsonFormatter

# U+2028 LINE SEPARATOR, U+2029 PARAGRAPH SEPARATOR, U+0085 NEXT LINE
LINE_SEPARATORS = "".join(map(chr, (0x2028, 0x2029, 0x85)))


def make_record(msg: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord("arkray.test", logging.INFO, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_output_is_one_json_line_even_with_injected_newlines():
    line = JsonFormatter().format(make_record('evil\n{"level":"CRITICAL"}'))
    assert "\n" not in line
    assert json.loads(line)["msg"] == 'evil\n{"level":"CRITICAL"}'


def test_unicode_line_separators_are_escaped_in_json():
    """U+2028/U+2029/U+0085 split lines in some log shippers."""
    message = f"path/{LINE_SEPARATORS}injected"
    line = JsonFormatter().format(make_record(message))
    assert not any(char in line for char in LINE_SEPARATORS)
    assert json.loads(line)["msg"] == message


def test_console_format_keeps_one_event_per_line():
    line = ConsoleFormatter().format(make_record("evil\nINFO fake: entry", path="/a\r\nb"))
    assert "\n" not in line
    assert "\r" not in line


def test_context_identifiers_are_attached():
    token = bind_context(ExecutionContext(correlation_id="req-abcdef12", user_id="u-1"))
    try:
        record = make_record("hello")
        ContextFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))
    finally:
        reset_context(token)
    assert payload["correlation_id"] == "req-abcdef12"
    assert payload["user_id"] == "u-1"


def test_request_objects_are_never_serialised():
    """Django attaches the request (with query string) to error records; we drop it."""
    payload = json.loads(
        JsonFormatter().format(make_record("boom", request=object(), status_code=500))
    )
    assert "request" not in payload
    assert payload["status_code"] == 500
