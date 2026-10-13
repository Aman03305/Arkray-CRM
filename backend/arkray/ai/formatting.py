"""How Ask Arkray states figures and dates: formatted by the server from exact values, so
the model only ever repeats them (it is never asked to compute or convert).

- Money: Decimal in, Indian digit grouping out ("₹12,50,000", paise only when present),
  the same rules as the frontend's formatInr. Never float.
- Dates and times: in the organisation's time zone (CRM_TIME_ZONE), so the model never
  converts from UTC ("10:30" means 10:30 where the business is).
- User-written text: links, email addresses, phone numbers, passcodes and identity numbers
  replaced by markers before anything reaches the model (user_text, below).
"""

from __future__ import annotations

import html.entities
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from itertools import pairwise
from typing import Any

from django.conf import settings

from arkray.core.business_time import business_zone

_PAISA = Decimal("0.01")
CURRENCY_SYMBOLS = {"INR": "₹"}


def group_indian(digits: str) -> str:
    """'1250000' -> '12,50,000'; '1234' -> '1,234'."""
    whole = digits.lstrip("0") or "0"
    if len(whole) <= 3:
        return whole
    head, last3 = whole[:-3], whole[-3:]
    pairs: list[str] = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    if head:
        pairs.insert(0, head)
    return ",".join([*pairs, last3])


def money(amount: Decimal) -> dict[str, str]:
    """An amount as the tools state it: the exact decimal string and its display form."""
    if not isinstance(amount, Decimal):
        raise TypeError("Money is Decimal, never float.")
    exact = amount.quantize(_PAISA, rounding=ROUND_HALF_UP)
    whole, _, paise = format(abs(exact), "f").partition(".")
    symbol = CURRENCY_SYMBOLS.get(settings.CRM_CURRENCY, f"{settings.CRM_CURRENCY} ")
    shown = f"{symbol}{group_indian(whole)}" + (f".{paise}" if paise.strip("0") else "")
    return {
        "amount": format(exact, "f"),
        "currency": settings.CRM_CURRENCY,
        "display": f"-{shown}" if exact < 0 else shown,
    }


def percent(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return f"{text}%"


def local(moment: datetime) -> datetime:
    return moment.astimezone(business_zone())


def when(moment: datetime | None) -> dict[str, str] | None:
    """A date and time in the business time zone: ISO (with offset) and a display form."""
    if moment is None:
        return None
    at = local(moment)
    hour = at.strftime("%I").lstrip("0")
    return {
        "iso": at.isoformat(timespec="minutes"),
        "display": f"{at.day} {at.strftime('%b %Y')}, {hour}:{at.strftime('%M %p')}",
    }


def day(value: date | datetime | None) -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        value = local(value).date()
    return {"iso": value.isoformat(), "display": f"{value.day} {value.strftime('%b %Y')}"}


# --- what never leaves the deployment from user-written text ------------------------------------
# docs/rag-architecture.md#privacy-and-data-minimisation. Web links with or without a scheme
# (they carry meeting passcodes and document tokens), email addresses, phone numbers,
# passcodes and other credentials, and identity and card numbers become markers. The record
# itself is a click away. Every decision below is tested both ways (ai/tests/test_masking.py,
# test_masking_adversarial.py).
#
# What the patterns read (adversarial review M5: "Mob.9811155555", "+91.98111.55555" and
# "98111/55555" went out whole): not the stored text but its *view*, the text as a reader
# (or a model) would read it. Every digit in any script as its ASCII digit (Devanagari,
# fullwidth, mathematical), compatibility forms folded (a fullwidth colon, plus or slash, a
# no-break space), every dash as "-", invisible characters dropped (zero-width joiners and
# spaces, soft hyphens, bidi marks, combining marks, variation selectors), and HTML
# character references and %-escapes decoded ("&#57;", "&nbsp;", "%3D"). Each character of
# the view remembers the characters of the text it came from, so what is replaced is the
# text, encoded and invisible characters included.
#
# Phone numbers (privacy audit P3, M5): +country numbers and 0091 in any grouping (groups of
# two digits or more, a trunk "(0)"), Indian mobiles (10 digits from 6-9, optionally after
# 0, 91, 091 or 0091) run together, as 5+5, or in two or three groups joined by "." "/" "_"
# or "-", landlines with their STD code (0 then 10 digits in all: 2+8, 3+7, 4+6), and any
# number of 8 to 15 digits named as one ("Tel 2612 0000", "Phone: 981 115 5555"). Review
# rounds 1 and 2: a mobile in pairs or in four or five joined groups when named ("Mob 98 11
# 15 55 55", "Phone 98.11.15.55.55"; unnamed they are readings, "QC levels
# 60-65-70-75-80"), another country's number after 00 ("001 212 555 0100": a real country
# code as a group of its own, ten digits or more after it), and a country code in brackets
# replaced with its brackets ("(+91) 98111 55555"). A number glued to a word
# ("Mob.9811155555") is a number; one inside a decimal ("12.9811155555"), right after a
# currency ("Rs.9811155555", "INR 9811155555.00") or followed by paise is money; one right
# after a reference label is a reference ("PO 7500012345", "S/N: 6100234567", "Order no.
# 9811155555"; rounds 2 and 3, _REFERENCE_WORD: a label, never a word ending a sentence); a
# landline's STD code never starts with 0 ("001-2026-0042" is an order); "Cell" alone,
# before a count, is a lab's word ("Cell 12345678 per ml"). Kept, deliberately: a bare local
# number without its code ("2612 0000"), and 3+3+4 or 4+3+3 or pairs with spaces and
# nothing naming them ("700 800 9000", "98 11 15 55 55"): eight to ten digits in spaced
# groups are as often quantities.
#
# Passcodes and credentials (M5): the value of a link's credential parameter (?pwd=,
# &token=, #access_token=, ...; also a link's tail cut from its host), a value written after
# a passcode word ("Passcode: 482913", "Password - x7Kp2", "PIN 1234#", "OTP 482913",
# "conference ID: 123 456 789#"), bearer and JSON web tokens, well-known key prefixes, long
# hex strings, and long random-looking strings. A passcode word followed by an ordinary word
# ("Password reset done", "OTP not received") keeps it; "token" and "secret" need a ":" or
# "=" ("token amount 25000"); a postal "PIN 411001" (six digits, no "#") is kept. Review
# round 1 (AI-2): the value on the next line ("Passcode\n482913") or glued to the word
# ("otp482913", four digits or more), a card's CVV, and "pass" and "pw", ordinary words
# unless a ":" or "=" follows them with something no word is ("PW: Kx4Wren9") or a word says
# what they open ("wifi pass Arkray@2026", "Zoom pass: 482913"; "boarding pass 1234" stays).
# Rounds 2 and 3: after "pass"/"pw" four characters or more, as figures alone four digits
# ("PW: 4829"; "QC pass: 3 of 3 levels" stays); a code word needs a digit ("OTP: not
# received", "CVV: n/a"), a CVV three or four; a token word eight characters ("Token: 3
# strips left").
#
# Identity and card numbers: a PAN (five capitals, four digits, a capital; in lower case
# with a holder-type fourth letter, P C H F A T B L J G; spaced only when named as a PAN:
# "BATCH 2026 A" is a batch), an Aadhaar number (12 digits from 2-9, run together or in
# 4+4+4 one to three blanks or a hyphen apart, passing its Verhoeff check digit, unless every
# group is a year or a round figure or a reference word comes before it, or any 12 digits
# named as one, "Adhaar" and "Adhar" included), and a card number (13-19 digits, run
# together or in groups of four up to three blanks apart, passing the Luhn check, unless
# every group is a year or a round figure).
#
# Linear time on any input (final audit RAG-1: one crafted note held a worker for 10 s). Each
# pattern starts only where its run starts (a lookbehind), repeats only bounded or possessive
# pieces, and separates repeated groups by characters the groups cannot contain, so there is
# one way to match; a candidate a check refuses is retried one character later, where the
# lookbehind refuses at once. Blanks after a keyword are possessive (review round 1, AI-1:
# `[ \t]*[:=#.-]?[ \t]*` after "call", "PIN" or "uid" tried every split of a long blank run,
# 3-7 s for one 10,000-character task description inside a routed question's request), a
# run of four blanks or more reads as two spaces, a character expands to at most a few
# (review round 2: U+FDFA, 18 under NFKC, stays one), and within a tool call each text is
# searched once (masking_memo). ai/tests/test_masking_adversarial.py and
# test_masking_cost.py time crafted inputs.
_MARKERS = {
    "link": "[link]",
    "email": "[email]",
    "secret": "[secret]",
    "phone": "[phone]",
    "id": "[id]",
}
# Overlapping finds become one marker: the first kind in this order.
_PRECEDENCE = {kind: rank for rank, kind in enumerate(_MARKERS)}

# --- the view -------------------------------------------------------------------------------------
_ESCAPED = re.compile(
    r"&#[0-9]{1,7};?|&#[xX][0-9a-fA-F]{1,6};?|&[A-Za-z][A-Za-z0-9]{1,31};|%[0-9A-Fa-f]{2}"
)
# A run of four blanks or more reads as two spaces (review round 1, AI-1: no pattern then
# walks a long run). Two, not one (review round 2, AI2-1): one made a figure before the run
# look adjacent to the id after it ("Row 7    2345 6789 0124", a table's columns, read as a
# list of figures), and every pattern that allows blanks between groups of digits allows two
# (review round 3: the PIN's digit groups and a passcode's spaced digits did not). Blanks
# decoded from entities ("&nbsp;" x 4) are a run too: the runs are read after the escapes
# are decoded.
_LONG_BLANK = re.compile(r"[ \t]{4,}+")
_DROPPED = frozenset({"Cf", "Mn", "Me"})  # format characters, combining marks


@lru_cache(maxsize=4096)
def _folded(char: str) -> str:
    """A character as the patterns read it (NFKC, digits in ASCII, invisibles dropped)."""
    if char.isascii():
        return char
    out: list[str] = []
    for c in unicodedata.normalize("NFKC", char):
        category = unicodedata.category(c)
        if category in _DROPPED:
            continue
        if category == "Nd":
            out.append(str(unicodedata.decimal(c)))
        elif category == "Pd":
            out.append("-")
        elif category == "Zs":
            out.append(" ")
        elif category in ("Zl", "Zp"):
            out.append("\n")
        else:
            out.append(c)
    if len(out) > 1 and not any(c.isascii() and not c.isspace() for c in out):
        # Nothing a pattern reads: one character, never 18 (review round 2, AI2-4: U+FDFA,
        # an Arabic ligature, is 18 letters under NFKC; 10,000 of them a 180,000 view).
        return char
    return "".join(out)


def _decoded(token: str) -> str:
    """An HTML character reference or a %-escape as the character it stands for (folded);
    one that stands for nothing printable stays as written."""
    if token.startswith("&#"):
        number = token[2:].rstrip(";")
        code = int(number[1:], 16) if number[0] in "xX" else int(number)
        if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF:
            return _folded(chr(code))
        return token
    if token.startswith("&"):
        named = html.entities.html5.get(token[1:])
        return "".join(map(_folded, named)) if named else token
    code = int(token[1:], 16)
    if code == 0x20:
        return "_"  # a space inside a link must not end the link
    return chr(code) if 0x20 < code < 0x7F else token


@dataclass(frozen=True, slots=True)
class _View:
    text: str
    # For each character of the view, where the characters it came from start and end in
    # the text; None: one for one.
    starts: list[int] | None = None
    ends: list[int] | None = None

    def span(self, start: int, end: int) -> tuple[int, int]:
        if self.starts is None or self.ends is None:
            return start, end
        return self.starts[start], self.ends[end - 1]

    def sources(self, start: int, end: int) -> tuple[list[int], list[int]]:
        """Where the view's characters start..end came from."""
        if self.starts is None or self.ends is None:
            return list(range(start, end)), list(range(start + 1, end + 1))
        return self.starts[start:end], self.ends[start:end]


def _folded_view(text: str) -> _View:
    """Every character folded (_folded): one for one in the usual case, at C speed."""
    if text.isascii():
        return _View(text)
    folded = list(map(_folded, text))
    if all(len(each) == 1 for each in folded):
        return _View("".join(folded))
    starts: list[int] = []
    ends: list[int] = []
    for index, each in enumerate(folded):
        if each:
            starts.extend([index] * len(each))
            ends.extend([index + 1] * len(each))
    return _View("".join(folded), starts, ends)


def _two_blanks(_: str) -> str:
    return "  "


def _rewritten(view: _View, pattern: re.Pattern[str], replace: Callable[[str], str]) -> _View:
    """The view with each match of `pattern` replaced, every character of the replacement
    mapped to all the text the match came from."""
    pieces: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    position = 0
    for match in pattern.finditer(view.text):
        begin, finish = match.span()
        if begin > position:
            pieces.append(view.text[position:begin])
            kept_starts, kept_ends = view.sources(position, begin)
            starts.extend(kept_starts)
            ends.extend(kept_ends)
        replaced = replace(match.group())
        source_start, source_end = view.span(begin, finish)
        pieces.append(replaced)
        starts.extend([source_start] * len(replaced))
        ends.extend([source_end] * len(replaced))
        position = finish
    if position == 0:
        return view
    pieces.append(view.text[position:])
    kept_starts, kept_ends = view.sources(position, len(view.text))
    starts.extend(kept_starts)
    ends.extend(kept_ends)
    return _View("".join(pieces), starts, ends)


def _view(text: str) -> _View:
    """The text as the patterns read it: folded, its escapes decoded, then its long blank
    runs read as two spaces."""
    view = _folded_view(text)
    if "&" in view.text or "%" in view.text:
        view = _rewritten(view, _ESCAPED, _decoded)
    if _LONG_BLANK.search(view.text) is not None:
        view = _rewritten(view, _LONG_BLANK, _two_blanks)
    return view


# --- the patterns (on the view) -------------------------------------------------------------------
_I = re.IGNORECASE
_SEP = r"[ \t\r\n./_-]"  # between a phone number's groups (in the view every dash is "-")
_GAP = _SEP + "{1,3}"  # "98111 - 55555", "98111\n55555"
_JOIN = r"[./_-]"  # a joiner that is not a space
_NOT_IN_A_NUMBER = r"(?<![0-9])(?<![0-9]\.)"
_NUMBER_ENDS = r"(?![0-9])(?!\.[0-9])"
# A currency word before the digits: money, not a phone number.
_NOT_MONEY = r"(?<!\brs)(?<!\brs\.)(?<!\brs )(?<!\brs\. )(?<!\binr)(?<!\binr )(?<!₹)(?<!₹ )"


def _sizes(total: int, parts: int) -> list[tuple[int, ...]]:
    """Every way to write `total` digits as `parts` groups of two digits or more."""
    if parts == 1:
        return [(total,)] if total >= 2 else []
    return [
        (first, *rest) for first in range(2, total) for rest in _sizes(total - first, parts - 1)
    ]


def _mobile_groupings() -> str:
    """The nine digits after a mobile number's first: run together, as 5+5 (any gap), or in
    two or three groups of two digits or more joined by a non-space joiner. Four or five
    groups ("98-11-15-55-55") only when named as a phone (_named_phone): unnamed, they are
    as often readings ("QC levels 60-65-70-75-80", review round 2)."""
    shapes = [r"\d{9}", rf"\d{{4}}{_GAP}\d{{5}}"]
    for parts in (2, 3):
        for first, *rest in _sizes(10, parts):
            shapes.append(rf"\d{{{first - 1}}}" + "".join(rf"{_JOIN}\d{{{n}}}" for n in rest))
    return "|".join(shapes)


_LINK = re.compile(
    # Any scheme (https, ftp, zoommtg, webex ...) and the app links without "//".
    r"(?=[a-z0-9-])(?:(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{1,30}://\S+"
    r"|(?<![a-z0-9])(?:msteams|zoommtg|zoomus|webex|skype):\S+"
    r"|www\.\S+"
    # scheme-less host/path: zoom.us/j/123?pwd=..., meet.google.com/abc-defg-hij
    r"|(?<![a-z0-9.-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}/\S*)",
    _I,
)
_EMAIL = re.compile(r"(?=[\w.+-])(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", _I)
_PARAMETER = re.compile(
    r"[?&;#][ \t]?(?:pwd|passcode|password|passwd|pass|pw|psw|pin|otp|token|access_token"
    r"|refresh_token|id_token|auth|auth_token|authorization|key|apikey|api_key|api-key"
    r"|access_key|secret|secret_key|client_secret|code|sig|signature|session|sessionid"
    r"|session_id|sid|jwt|tk|x-amz-signature|x-amz-credential|x-amz-security-token"
    r"|x-goog-signature|x-goog-credential)[ \t]?=[ \t]?(?P<value>[^\s&#;\"'<>]+)",
    _I,
)
_WRITTEN_SECRET = re.compile(
    r"(?=[acdeghlmoprstvwz])(?<![a-z0-9])"
    # A word that says what "pass" or "pw" opens ("wifi pass", "Zoom pass"): alone they are
    # ordinary words (_written_secret).
    r"(?P<context>(?:wi-?fi|zoom|meeting|login|door|guest|admin|portal|account|webex|teams"
    r"|router|system|e-?mail)[ \t]++)?"
    r"(?P<word>pass[ \t]?codes?|pass[ \t]?words?|passwd|pwd|pswd|pass[ \t]?key|otp"
    r"|access[ \t]code|security[ \t]code|verification[ \t]code|host[ \t]key"
    r"|conference[ \t]id|api[ \t_-]?key|access[ \t_-]?key|secret[ \t_-]?key"
    r"|client[ \t_-]?secret|access[ \t_-]?token|auth[ \t_-]?token|cvv2?|cvc2?|token|secret"
    r"|pass|pw)(?![a-z])"
    # Possessive blanks: one way to read them (review round 1, AI-1).
    r"(?P<sep>[ \t]*+[:=][ \t]*+(?:\r?\n[ \t]*+)?|[ \t]*+-[ \t]*+|[ \t]++is[ \t]++"
    r"|[ \t]*+\r?\n[ \t]*+|[ \t]++|(?=[0-9]))"
    r"(?P<value>\"[^\"\n]{1,64}\"|'[^'\n]{1,64}'|\S{1,128}+(?:[ \t]{1,3}+\d{1,12}+){0,4})",
    _I,
)
_PIN = re.compile(
    r"(?=p)(?<![a-z0-9])pin(?:[ \t]*+(?:no|number)(?![a-z])\.?)?+(?![ \t-]*+codes?(?![a-z]))"
    r"(?![a-z0-9])[ \t]*+(?:[:=#.-][ \t]*+)?"
    r"(?P<value>\d(?:[ \t-]{0,3}+\d){2,15}[ \t]{0,3}+#|\d{4}(?![0-9]))",
    _I,
)
_BEARER = re.compile(r"(?=b)(?<![a-z0-9])bearer[ \t]++(?P<value>[a-z0-9._~+/-]{16,}+=*)", _I)
_JWT = re.compile(
    r"(?=e)(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]*+"
)
_KEY_PREFIX = re.compile(
    r"(?=[Agprsx])(?<![A-Za-z0-9])(?:sk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}+"
    r"|[spr]k_(?:live|test)_[A-Za-z0-9]{16,}+|gh[pousr]_[A-Za-z0-9]{30,}+"
    r"|github_pat_[A-Za-z0-9_]{30,}+|glpat-[A-Za-z0-9_-]{20,}+|xox[abposr]-[A-Za-z0-9-]{10,}+"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])|AIza[A-Za-z0-9_-]{35})"
)
_HEX = re.compile(r"(?=[0-9a-f])(?<![0-9a-z])[0-9a-f]{32,}+(?![0-9a-z])", _I)
_RANDOM = re.compile(
    r"(?=[A-Za-z0-9_+/-])(?<![A-Za-z0-9_+/=-])[A-Za-z0-9_+/-]{40,}+={0,2}(?![A-Za-z0-9_+/=-])"
)
_INTERNATIONAL = re.compile(
    # "(+91) ...", "+91 ...", "0091 ...", or another country's after 00: its code a group of
    # its own and the next group no "00..." again ("0042 0043 0044" is a list of tickets).
    rf"(?=[(+0])(?:\(\+[ \t]?\d{{1,3}}\)|\+[ \t]?\(?\d{{1,3}}\)?|(?<![0-9])0091"
    rf"|(?<![0-9])00[1-9]\d{{0,2}}(?={_SEP}{{1,3}}+(?!00)[0-9(]))"
    # The first group after the code may be one digit ("+33 6 12 34 56 78", "00971 4 123
    # 4567"; review round 3), the others two or more.
    rf"(?:{_SEP}{{0,3}}(?:\(0\)|\(\d{{1,5}}\)|\d++))"
    rf"(?:{_SEP}{{0,3}}(?:\(0\)|\(\d{{2,5}}\)|\d{{2,}}+)){{0,5}}"
)
_MOBILE = re.compile(
    rf"(?=[(0-9]){_NOT_IN_A_NUMBER}{_NOT_MONEY}"
    rf"(?:(?:(?:00|0)?91|\(?0\)?)(?:{_GAP})?)?[6-9](?:{_mobile_groupings()}){_NUMBER_ENDS}",
    _I,
)
_LANDLINE = re.compile(
    rf"(?=[(0]){_NOT_IN_A_NUMBER}\(?0(?=[1-9])(?:\d{{2}}\)?(?:{_GAP})?\d{{4}}|\d{{3}}\)?(?:{_GAP})?\d{{3}}"
    rf"|\d{{4}}\)?(?:{_GAP})?\d{{2}})(?:{_GAP})?\d{{4}}{_NUMBER_ENDS}"
)
_NAMED_PHONE = re.compile(
    r"(?=[cfhlmprtw])(?<![a-z0-9])(?!(?-i:pH)(?![a-z]))"
    r"(?:mob(?:ile)?|cellphone|(?P<cell>cell)|ph(?:one)?|tel(?:ephone|e)?|landline|fax"
    # "(cell) 981 115 5555": a word in brackets names the number after it (review round 3).
    r"|whats[ \t]?app|contact|call|ring|helpline|hotline)(?![a-z])\)?"
    r"(?P<label>(?:[ \t]*+(?:no|nos|num|number)(?![a-z])\.?)?+[ \t]*+(?:[:=#.-]{1,2}+[ \t]*+)?)"
    # Eight digits at least, before the groups are weighed (_named_phone): a bounded look.
    r"(?=\+?[ \t]?\(?[0-9](?:[ \t()./-]{0,3}[0-9]){7})"
    r"(?P<value>\+?[ \t]?\(?\d{1,15}+\)?(?:[ \t./-]{1,3}+\(?\d{1,15}+\)?){0,4})",
    _I,
)
_PAN = re.compile(r"(?=[A-Z])(?<![A-Za-z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Za-z0-9])")
# A PAN's fourth letter says whose it is (P a person, C a company, H, F, A, T, B, L, J, G):
# with it, in lower case ("aaapl1234c") it is a PAN too (review round 1); "lot abcde1234f"
# is not. Spaced ("ABCDE 1234 F") only when named as a PAN (review round 2: "BATCH 2026 A",
# "ASSAY 1000 X").
_TYPED_PAN = re.compile(
    r"(?=[A-Za-z])(?<![A-Za-z0-9])(?i:[a-z]{3}[pchfatbljg][a-z](?P<digits>\d{4})[a-z])"
    r"(?![A-Za-z0-9])"
)
_NAMED_PAN = re.compile(
    r"(?=p)(?<![a-z0-9])pan(?:[ \t]*+(?:no|number|card)(?![a-z])\.?){0,2}+[ \t]*+"
    r"(?:[:=#.-][ \t]*+)?(?P<value>[a-z]{5}[ \t]{0,3}+\d{4}[ \t]{0,3}+[a-z])(?![a-z0-9])",
    _I,
)
# Groups apart by a hyphen or one to three blanks (review round 2, AI2-2: "2345  6789
# 0124"), not necessarily the same (round 3: "2345 6789    0124", two spaces in the view).
_AADHAAR = re.compile(
    r"(?=[2-9])(?<![0-9])(?<![0-9][ \t.-])[2-9]\d{3}(?P<gap>[ \t-]{0,3}+)\d{4}"
    r"(?P<gap2>[ \t-]{0,3}+)\d{4}(?![0-9])(?![ \t.-][0-9])(?!(?P=gap2)[0-9])"
)
_NAMED_AADHAAR = re.compile(
    r"(?=[au])(?<![a-z0-9])(?:aa?dhaa?r|uidai|uid)(?:[ \t]*+(?:no|number|card)(?![a-z])\.?){0,2}+"
    r"[ \t]*+(?:[:=#.-][ \t]*+)?"
    r"(?P<value>\d{4}[ \t-]{0,3}+\d{4}[ \t-]{0,3}+\d{4})(?![0-9])",
    _I,
)
# Where a run of digit groups starts, never after a digit and a separator: a card is not the
# middle of a list of figures (and a list of 4-digit figures is not a card at every group).
# Up to three blanks between groups (review round 2) or a single "." or "-" (_card).
_CARD = re.compile(
    rf"(?=[2-68]){_NOT_IN_A_NUMBER}(?<![0-9][ \t.-])[2-68](?:[ \t.-]{{0,3}}+\d){{12,18}}"
    r"(?![0-9])"
)

_DIGIT_GROUPS = re.compile(r"\d+")


def _groups(text: str) -> list[re.Match[str]]:
    return list(_DIGIT_GROUPS.finditer(text))


def _ordinary_groups(groups: list[str]) -> bool:
    """Every group a year or a round figure: a list of years or quantities, not an id."""
    return all(re.fullmatch(r"(?:19|20)\d\d|\d+00", g) for g in groups)


# ITU country calling codes are prefix-free: 1 or 7, a 2-digit code, or a 3-digit code in a
# zone whose 2-digit prefix is not a code itself.
_COUNTRY_CODES = frozenset(
    {20, 27, *range(30, 35), 36, 39, 40, 41, *range(43, 50), *range(51, 59), *range(60, 67)}
    | {81, 82, 84, 86, *range(90, 96), 98}
)
_COUNTRY_ZONES = frozenset(
    {*range(21, 27), 29, 35, 37, 38, 42, 50, 59, 67, 68, 69, 80, 85, 87, 88, 96, 97, 99}
)


def _country_code(code: str) -> bool:
    if len(code) == 1:
        return code in "17"
    return int(code) in _COUNTRY_CODES if len(code) == 2 else int(code[:2]) in _COUNTRY_ZONES


# A label that makes the figures right after it a reference, not a phone or an Aadhaar
# number (review round 2: "PO 7500012345", "S/N 6100234567", "Ref 0045 1234 5678 9";
# routed answers show masked text). A label, not a word that ends a sentence (review round
# 3: "Thanks a lot. 9811155555 is Dr Shah's direct line." went out whole): an abbreviation
# (ref, inv, acct, txn with their dot; po, utr, awb, a/c and s/n with their slash), or a
# reference word with its "no" or "#" ("Order no. 9811155555", "Invoice #9100234567"),
# then blanks and at most one of ":#=/-", never ",", ";", "!", "?", a full stop or a line
# break. Ordinary words alone ("order", "invoice", "value", "amount") hide nothing: a false
# positive is cheaper than a leak.
_REFERENCE_WORDS = (
    r"ref(?:erence)?|invoice|inv|order|po|ticket|serial|sr|tracking|docket|lot|batch"
    r"|account|acct|a/c|challan|receipt|voucher|txn|transaction|utr|awb|s/n"
)
_REFERENCE_WORD = re.compile(
    r"(?<![a-z/])(?:(?:ref|inv|acct|txn)\.?|po|utr|awb|a/c|s/n"
    rf"|(?:{_REFERENCE_WORDS})\.?[ \t]*+(?:(?:no|nos|num|number)(?![a-z])\.?|#))"
    # \Z, not $: "$" also matches before a final line break ("PO\n9811155555").
    r"[ \t]*+(?:[:#=/-][ \t]*+)?\Z",
    _I,
)
# A currency right before the figures: money ("Rs    9811155555").
_MONEY_WORD = re.compile(r"(?<![a-z])(?:rs\.?|inr|₹)[ \t]*+\Z", _I)


def _after_reference_word(match: re.Match[str], *, or_money: bool = False) -> bool:
    start = match.start()
    window = max(0, start - 24)
    if _REFERENCE_WORD.search(match.string, window, start) is not None:
        return True
    return or_money and _MONEY_WORD.search(match.string, window, start) is not None


def _unnamed_number(match: re.Match[str]) -> tuple[int, int] | None:
    """A mobile or landline number nothing names, unless a reference label or a currency
    comes right before it (review rounds 2 and 3)."""
    return None if _after_reference_word(match, or_money=True) else match.span()


def _international(match: re.Match[str]) -> tuple[int, int] | None:
    """8 to 15 digits (E.164): trailing groups beyond 15 digits are not the number's. After
    00 (an international call prefix, not the number's), another country's number has ten
    digits or more after a real country code, and no reference word before it (review
    rounds 1 and 2: "001 212 555 0100"; "001-2026-0042", "Invoice 0012 3456 7890" and "Ref
    0045 1234 5678 9" are references)."""
    found = match.group()
    if found.startswith("00") and not found.startswith("0091"):
        code = _groups(found)[0].group()[2:]
        if not _country_code(code) or _after_reference_word(match):
            return None
    prefix = 2 if found.startswith("00") else 0
    least = 10 if prefix and not found.startswith("0091") else 8
    total = 0
    end = 0
    for group in _groups(found):
        if total + len(group.group()) > 15 + prefix:
            break
        total += len(group.group())
        end = group.end() + (1 if found[group.end() : group.end() + 1] == ")" else 0)
    return (match.start(), match.start() + end) if total - prefix >= least else None


def _mobile_digits(digits: str) -> bool:
    """An Indian mobile number: 10 digits from 6-9, after 0, 91, 091 or 0091 or not."""
    return re.fullmatch(r"(?:0|91|091|0091)?[6-9]\d{9}", digits) is not None


def _named_phone(match: re.Match[str]) -> tuple[int, int] | None:
    """A number named as a phone: 8 to 15 digits in groups of two or more, at most the first
    of them shorter than three (an STD or country code), or a mobile number in any groups of
    two or more ("Mob 98 11 15 55 55", "Phone 98.11.15.55.55", review rounds 1 and 2; "Call
    12-10-2026" is a date); trailing groups that break the rule are not the number's ("Call
    98111 55555 2026"), and its digits go whole ("Phone 98111555559999", round 2)."""
    value = match.group("value")
    groups = _groups(value)
    # "Cell" names a phone with "no", ":", "#" or "-" after it, or before a mobile number's
    # digits ("Cell: 2612 0000", "Cell 98 11 15 55 55"); "Cell 12345678 per ml" is a count
    # (review rounds 2 and 3).
    plain_cell = match.group("cell") is not None and not re.search(
        r"[:#-]|n", match.group("label"), re.IGNORECASE
    )
    for count in range(len(groups), 0, -1):
        sizes = [len(g.group()) for g in groups[:count]]
        if not 8 <= sum(sizes) <= 15 or min(sizes) < 2:
            continue
        digits = "".join(g.group() for g in groups[:count])
        mobile = _mobile_digits(digits)
        if (any(size < 3 for size in sizes[1:]) or plain_cell) and not mobile:
            continue
        tail = groups[count - 1].end()
        if value[tail : tail + 1] == ")":
            tail += 1
        return match.start("value"), match.start("value") + tail
    return None


_WEAK_WORDS = frozenset({"token", "secret"})
_PLAIN_WORDS = frozenset({"pass", "pw"})
# Words for a code made of digits: no digit, no code ("OTP: not received", "CVV: n/a").
_NUMERIC_WORDS = frozenset(
    {
        "otp",
        "cvv",
        "cvv2",
        "cvc",
        "cvc2",
        "accesscode",
        "securitycode",
        "verificationcode",
        "conferenceid",
        "hostkey",
    }
)
# Words for a token: eight characters or more ("Token: 3 strips left", "Secret: none").
_TOKEN_WORDS = frozenset(
    {
        "token",
        "secret",
        "apikey",
        "accesskey",
        "secretkey",
        "clientsecret",
        "accesstoken",
        "authtoken",
    }
)
_FIGURES = re.compile(r"[\d%/.,:-]+")  # a count, a percentage, a fraction, a reading


def _passes_for_a_passcode(value: str, digits: int, strong: bool, context: bool) -> bool:
    """After "pass" or "pw": four characters or more, not a word, and as figures alone four
    digits or more. "PW: Kx4Wren9", "PW: 4829", "Zoom pass: 482913" (review round 3); not "QC
    pass: 3 of 3 levels", "Calibration pass: L1 L2 L3", "Day pass: 2 visitors", "QC pass:
    100%" (review round 2), "Pass: the demo went well"."""
    if not (strong or context) or len(value) < 4 or value.isalpha():
        return False
    return digits >= 4 if _FIGURES.fullmatch(value) else True


def _written_secret(match: re.Match[str]) -> tuple[int, int] | None:
    """A value after a passcode word. After ":" or "=" it is one; after a space, a line
    break, a dash or "is" only if it holds a digit ("Password - x7Kp2", not "Password
    reset"), glued to the word only with four digits or more ("otp482913"); "token" and
    "secret" only after ":" or "=" ("token amount 25000"); "pass" and "pw" only for a value
    that looks like a passcode (_passes_for_a_passcode), and after a blank only with a word
    saying what they open ("wifi pass Arkray@2026", not "boarding pass 1234"); a code word
    needs a digit (a CVV three or four), a token word eight characters (review round 2)."""
    separator = match.group("sep")
    value = match.group("value").strip("\"'.,;")
    word = re.sub(r"[ \t_-]", "", match.group("word").casefold())
    strong = ":" in separator or "=" in separator
    digits = sum(c.isdigit() for c in value)
    context = match.group("context") is not None
    if word in _PLAIN_WORDS and not _passes_for_a_passcode(value, digits, strong, context):
        return None
    if word in _NUMERIC_WORDS and not digits:
        return None
    if word.startswith(("cvv", "cvc")) and not re.fullmatch(r"\d{3,4}", value):
        return None
    if word in _TOKEN_WORDS and len(value) < 8:
        return None
    if not strong and (word in _WEAK_WORDS or not digits or (not separator and digits < 4)):
        return None
    return match.span("value")


_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _verhoeff(digits: str) -> bool:
    check = 0
    for position, digit in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[position % 8][int(digit)]]
    return check == 0


def _luhn(digits: str) -> bool:
    total = 0
    for position, digit in enumerate(reversed(digits)):
        value = int(digit) * (2 if position % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def _aadhaar(match: re.Match[str]) -> tuple[int, int] | None:
    """12 digits passing the Verhoeff check, not a list of years or round figures, not a
    reference ("Order 2345-6789-0124", review round 2), and not the middle of a list of
    groups the same gap apart ("1  2345  6789  0124")."""
    groups = [g.group() for g in _groups(match.group())]
    if not _verhoeff("".join(groups)) or (len(groups) == 3 and _ordinary_groups(groups)):
        return None
    if not all(_a_gap(g) for g in (match.group("gap"), match.group("gap2"))):
        return None
    gap = match.group("gap")
    before = match.string[max(0, match.start() - len(gap) - 1) : match.start()]
    if (gap and before[1:] == gap and before[:1].isdigit()) or _after_reference_word(match):
        return None
    return match.span()


def _a_gap(gap: str) -> bool:
    """Between two groups of a card or an Aadhaar number: nothing, one separator, or up to
    three blanks."""
    return len(gap) <= 1 or not gap.strip(" \t")


def _card(match: re.Match[str]) -> tuple[int, int] | None:
    """13 to 19 digits run together or in groups of four (4-6-5 and 4-6-4 too) passing the
    Luhn check; trailing groups that break the shape are not the card's."""
    found = match.group()
    groups = _groups(found)
    before = match.string[max(0, match.start() - 4) : match.start()]
    for count in range(len(groups), 0, -1):
        kept = groups[:count]
        gaps = [found[a.end() : b.start()] for a, b in pairwise(kept)]
        if not all(_a_gap(g) for g in gaps):
            continue  # single separators mix, blank runs too (review round 3)
        gap = gaps[0] if gaps else ""
        lead = before[-(len(gap) + 1) :]
        if len(gap) > 1 and lead[1:] == gap and lead[:1].isdigit():
            continue  # the middle of a list of groups the same gap apart
        parts = [g.group() for g in kept]
        digits = "".join(parts)
        sizes = [len(p) for p in parts]
        shaped = (
            count == 1
            or (all(size == 4 for size in sizes[:-1]) and 1 <= sizes[-1] <= 4)
            or sizes in ([4, 6, 5], [4, 6, 4])
        )
        if not 13 <= len(digits) <= 19 or not shaped or not _luhn(digits):
            continue
        if count > 1 and _ordinary_groups(parts):
            continue
        return match.start(), match.start() + groups[count - 1].end()
    return None


def _typed_pan(match: re.Match[str]) -> tuple[int, int] | None:
    """Not when its digits are a year or a round figure ("batch2026a")."""
    return None if _ordinary_groups([match.group("digits")]) else match.span()


def _hex(match: re.Match[str]) -> tuple[int, int] | None:
    found = match.group()
    if not any(c.isdigit() for c in found) or not any(c.isalpha() for c in found):
        return None
    return match.span()


def _random(match: re.Match[str]) -> tuple[int, int] | None:
    """A long string that looks random: capitals, small letters and digits, high entropy,
    and few words in it (a file name or a path is made of words; a token is not)."""
    found = match.group()
    if not (
        any(c.isdigit() for c in found)
        and any(c.isupper() for c in found)
        and any(c.islower() for c in found)
    ):
        return None
    counts = Counter(found)
    entropy = -sum(n / len(found) * math.log2(n / len(found)) for n in counts.values())
    words = sum(len(word) for word in re.findall(r"[a-z]{4,}", found))
    if entropy < 4.0 or words / len(found) >= 0.4:
        return None
    return match.span()


@dataclass(frozen=True, slots=True)
class _Pattern:
    kind: str
    pattern: re.Pattern[str]
    # The part to replace, or None to keep this find; by default the "value" group if the
    # pattern has one, else the whole match.
    check: Callable[[re.Match[str]], tuple[int, int] | None] | None = None
    # Text without this can't hold a find: the pattern isn't run at all (a quick scan).
    hint: re.Pattern[str] | None = None

    def span(self, match: re.Match[str]) -> tuple[int, int] | None:
        if self.check is not None:
            return self.check(match)
        return match.span("value") if "value" in self.pattern.groupindex else match.span()


_hint = re.compile
_DIGITS = _hint(r"[0-9]{2}")
_PATTERNS = (
    _Pattern("link", _LINK, hint=_hint(r"://|www\.|\.[a-z]{2,}/|(?:teams|zoom|webex|skype):", _I)),
    _Pattern("email", _EMAIL, hint=_hint("@")),
    _Pattern("secret", _PARAMETER, hint=_hint("=")),
    _Pattern(
        "secret",
        _WRITTEN_SECRET,
        _written_secret,
        _hint(r"pass|pw|pswd|otp|code|key|token|secret|conference|cvv|cvc", _I),
    ),
    _Pattern("secret", _PIN, hint=_hint("pin", _I)),
    _Pattern("secret", _BEARER, hint=_hint("bearer", _I)),
    _Pattern("secret", _JWT, hint=_hint("eyJ")),
    _Pattern(
        "secret", _KEY_PREFIX, hint=_hint(r"sk-|k_|gh[pousr]_|github|glpat|xox|AKIA|ASIA|AIza")
    ),
    _Pattern("secret", _HEX, _hex, _hint(r"[0-9a-f]{32}", _I)),
    _Pattern("secret", _RANDOM, _random, _hint(r"[A-Za-z0-9_+/-]{40}")),
    _Pattern("phone", _INTERNATIONAL, _international, _hint(r"\+|00")),
    _Pattern("phone", _MOBILE, _unnamed_number, _DIGITS),
    _Pattern("phone", _LANDLINE, _unnamed_number, _DIGITS),
    _Pattern(
        "phone",
        _NAMED_PHONE,
        _named_phone,
        _hint(r"mob|cell|ph|tel|landline|fax|whats|contact|call|ring|helpline|hotline", _I),
    ),
    _Pattern("id", _PAN, hint=_hint(r"[A-Z]{5}[0-9]{4}[A-Z]")),
    _Pattern("id", _TYPED_PAN, _typed_pan, _hint(r"[0-9]{4}")),
    _Pattern("id", _NAMED_PAN, hint=_hint("pan", _I)),
    _Pattern("id", _AADHAAR, _aadhaar, _DIGITS),
    _Pattern("id", _NAMED_AADHAAR, hint=_hint(r"dha|uid", _I)),
    _Pattern("id", _CARD, _card, _DIGITS),
)


# Within a tool call, each text is searched once (review round 1, AI-1: a list row's whole
# text, a document quoted in two passages). Per call, never across calls: nothing is kept.
_MEMO: ContextVar[dict[str, list[tuple[int, int, str]]] | None] = ContextVar(
    "ai_masking_memo", default=None
)


@contextmanager
def masking_memo() -> Iterator[None]:
    """Within it, each text is searched for what to mask at most once (tools.execute)."""
    token = _MEMO.set({})
    try:
        yield
    finally:
        _MEMO.reset(token)


def _spans(text: str) -> list[tuple[int, int, str]]:
    """Where `text` holds something to replace, and what: sorted, never overlapping."""
    memo = _MEMO.get()
    if memo is not None and (known := memo.get(text)) is not None:
        return known
    found = _search(text)
    if memo is not None:
        memo[text] = found
    return found


def _search(text: str) -> list[tuple[int, int, str]]:
    return _find(_view(text))


def _find(view: _View) -> list[tuple[int, int, str]]:
    """What every pattern finds in a view, as spans of the text, merged."""
    found: list[tuple[int, int, str]] = []
    for each in _PATTERNS:
        if each.hint is not None and each.hint.search(view.text) is None:
            continue
        position = 0
        while (match := each.pattern.search(view.text, position)) is not None:
            span = each.span(match)
            if span is not None and span[0] < span[1]:
                found.append((*view.span(*span), each.kind))
            # A refused candidate is retried a character later (the lookbehinds refuse at
            # once inside its run).
            position = (
                max(match.end(), match.start() + 1) if span is not None else match.start() + 1
            )
    merged: list[list[Any]] = []
    for start, end, kind in sorted(found):
        if merged and start < merged[-1][1]:
            last = merged[-1]
            last[1] = max(last[1], end)
            if _PRECEDENCE[kind] < _PRECEDENCE[last[2]]:
                last[2] = kind
        else:
            merged.append([start, end, kind])
    return [(start, end, kind) for start, end, kind in merged]


def _masked(text: str, start: int, end: int) -> str:
    """text[start:end] as it may go to the model, judged on the whole text: a value the
    slice cuts is replaced whole."""
    parts: list[str] = []
    position = start
    for begin, finish, kind in _spans(text):
        if finish <= start:
            continue
        if begin >= end:
            break
        if begin > position:
            parts.append(text[position:begin])
        parts.append(_MARKERS[kind])
        position = max(position, finish)
    if position < end:
        parts.append(text[position:end])
    return "".join(parts)


def user_text(text: str) -> str:
    """User-written text as it may go to the model: links, email addresses, phone numbers,
    passcodes and identity numbers replaced by markers (Phase 9 review: titles, lost
    reasons and scheme-less links went out verbatim). Every user-written string in a tool
    result passes through here."""
    return _masked(text, 0, len(text))


def user_text_slice(text: str, start: int, end: int) -> str:
    """`user_text` of text[start:end], judged on the whole text: a link cut by the slice
    (a chunk boundary in the middle of a URL) is replaced as a whole, never left as a
    passcode-bearing tail."""
    return _masked(text, start, end)


_A_MARKER = re.compile("|".join(re.escape(marker) for marker in _MARKERS.values()))


def clip(text: str, limit: int, *, start: int = 0) -> dict[str, Any]:
    """Bounded user-written text for tool results: `limit` characters (or up to the end of
    a marker the limit falls in: never half a marker), from `start` (a search hit's
    preview), and whether anything was left out. Masked on the WHOLE text, then cut
    (adversarial review M5: a preview cut by the database and masked as a fragment sent
    `456789?pwd=<passcode>`, the tail of a meeting link, and half a phone number)."""
    if start > 0:
        shown, truncated = user_text_slice(text, start, len(text)).strip(), True
    else:
        shown, truncated = user_text(text.strip()), False
    if len(shown) <= limit:
        return {"text": shown, "truncated": truncated}
    cut = limit
    for marker in _A_MARKER.finditer(shown, max(cut - 8, 0), cut + 8):
        if marker.start() < cut < marker.end():
            cut = marker.end()
    return {"text": shown[:cut].rstrip() if start > 0 else shown[:cut], "truncated": True}


def label(text: str | None) -> str | None:
    """A user-written title, name or label for a tool result (None stays None)."""
    return None if text is None else user_text(text)
