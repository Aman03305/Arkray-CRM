"""The only request parser: JSON, always decoded as UTF-8.

Why not DRF's stock JSONParser: it decodes the body with whatever codec the request's
Content-Type `charset` names, and Django accepts any codec Python knows, including
compression codecs. `Content-Type: application/json; charset=zlib` let a 100 KB body
inflate to 100 MB (and one just under the 2.5 MB upload limit to gigabytes) before any
size check, on every endpoint, signed in or not (Phase 2 review, P1). JSON is UTF-8 by
definition (RFC 8259), so any other charset is refused with 415, and pathologically deep
nesting is a 400 instead of a RecursionError (500).
"""

from __future__ import annotations

import codecs
from collections.abc import Mapping
from typing import Any

from rest_framework.exceptions import ParseError, UnsupportedMediaType
from rest_framework.parsers import JSONParser


class Utf8JSONParser(JSONParser):
    def parse(
        self,
        stream: Any,
        media_type: str | None = None,
        parser_context: Mapping[str, Any] | None = None,
    ) -> Any:
        context = dict(parser_context or {})
        charset = context.get("encoding") or "utf-8"
        try:
            is_utf8 = codecs.lookup(charset).name == "utf-8"
        except LookupError:
            is_utf8 = False
        if not is_utf8:
            raise UnsupportedMediaType(media_type or "", detail="JSON must be encoded as UTF-8.")
        context["encoding"] = "utf-8"
        try:
            return super().parse(stream, media_type, context)
        except RecursionError:
            raise ParseError("JSON is nested too deeply.") from None
