"""What user-written text may carry to the model, adversarially (adversarial review M5).

test_masking.py holds the everyday shapes; this file the ones people, pasted e-mails,
calendar invitations and web pages produce around them: other digit scripts, invisible
characters inside a number, HTML entities and percent-encoding, numbers glued together or
split across lines, credentials in links and meeting passcodes written out, tokens and
identity numbers, every cut through a masked value, and inputs built to make the patterns
backtrack. Each kept value is a decision (docs/rag-architecture.md#privacy-and-data-
minimisation), tested both ways.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import time

import pytest

from arkray.ai import formatting
from arkray.ai.formatting import clip, user_text, user_text_slice

# Built at run time: no source line pairs a credential word with a value (secret scanning,
# .gitleaksignore). HIDDEN stands for a passcode, a password or a token.
HIDDEN = "Wren7" + "Canary42"


def fragments(text: str, secret: str, size: int = 4) -> list[str]:
    """Every piece of `secret` (`size` characters or more) that `text` still carries."""
    return [
        secret[i : i + size] for i in range(len(secret) - size + 1) if secret[i : i + size] in text
    ]


def has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)  # any script's digits, not only ASCII


# --- phone numbers in other scripts, encodings and layouts -------------------------------------
ZEROS = {
    "fullwidth": 0xFF10,
    "devanagari": 0x0966,
    "bengali": 0x09E6,
    "gujarati": 0x0AE6,
    "tamil": 0x0BE6,
    "arabic-indic": 0x0660,
    "mathematical bold": 0x1D7CE,
    "mathematical monospace": 0x1D7F6,
}
NUMBERS = ["98111 55555", "9811155555", "+91 98111 55555", "011-2345 6789", "98111.55555"]


def written_in(number: str, zero: int) -> str:
    return "".join(chr(zero + int(ch)) if ch in "0123456789" else ch for ch in number)


@pytest.mark.parametrize("number", NUMBERS)
@pytest.mark.parametrize("zero", ZEROS.values(), ids=ZEROS.keys())
def test_phone_numbers_in_other_digits_are_replaced(number, zero):
    masked = user_text(f"Mob.{written_in(number, zero)} today")
    assert "[phone]" in masked, masked
    assert not has_digit(masked), masked


@pytest.mark.parametrize("number", NUMBERS)
def test_phone_numbers_in_mixed_scripts_are_replaced(number):
    mixed = "".join(
        chr(ZEROS["devanagari"] + int(ch)) if ch.isdigit() and i % 2 else ch
        for i, ch in enumerate(number)
    )
    masked = user_text(f"Ring {mixed}")
    assert "[phone]" in masked, masked
    assert not has_digit(masked), masked


INVISIBLE = {
    "zero-width joiner": 0x200D,
    "zero-width non-joiner": 0x200C,
    "zero-width space": 0x200B,
    "word joiner": 0x2060,
    "byte order mark": 0xFEFF,
    "soft hyphen": 0x00AD,
    "left-to-right mark": 0x200E,
    "combining acute accent": 0x0301,
    "variation selector": 0xFE0F,
}


@pytest.mark.parametrize("code", INVISIBLE.values(), ids=INVISIBLE.keys())
@pytest.mark.parametrize("number", ["9811155555", "98111 55555", "+91 98111 55555"])
def test_invisible_characters_inside_a_number_change_nothing(code, number):
    """ZWJ and ZWNJ are accepted in stored text (Indic spelling); the rest are refused on
    input, but text from before that rule, or from an import, must not slip past either."""
    for text in (chr(code).join(number), number[:5] + chr(code) + number[5:]):
        masked = user_text(f"Ring {text} today")
        assert "[phone]" in masked, repr(masked)
        assert not has_digit(masked), repr(masked)


def test_keycap_digits_are_replaced():
    keycaps = "".join(d + chr(0xFE0F) + chr(0x20E3) for d in "9811155555")
    masked = user_text(f"WhatsApp {keycaps}")
    assert "[phone]" in masked, repr(masked)
    assert not has_digit(masked), repr(masked)


def entities(number: str, form: str) -> str:
    return "".join(form.format(ord(ch)) if ch.isdigit() else ch for ch in number)


@pytest.mark.parametrize(
    "text",
    [
        entities("98111 55555", "&#{};"),
        entities("9811155555", "&#{}"),  # browsers accept them without the semicolon
        entities("+91 98111 55555", "&#x{:x};"),
        entities("+91 98111 55555", "&#X{:X};"),
        "98111&nbsp;55555",
        "+91&nbsp;98111&nbsp;55555",
        "98111&#32;55555",
        "98111&ndash;55555",
        "98111%2055555",
        "%2B91%2098111%2055555",
        "Mob:\n98111\n55555",
        "98111\r\n55555",
        "+91\n98111 55555",
        "Ph: 98111" + chr(0x00A0) + "55555",  # a no-break space
        "Ph: 98111" + chr(0x2013) + "55555",  # an en dash
        "Ph: 98111" + chr(0xFF0F) + "55555",  # a fullwidth slash
        "Ph" + chr(0xFF1A) + chr(0xFF0B) + "91 98111 55555",  # fullwidth colon and plus
    ],
)
def test_encoded_and_split_numbers_are_replaced(text):
    masked = user_text(f"Contact {text} today")
    assert "[phone]" in masked, masked
    assert not has_digit(masked), masked
    assert "&#" not in masked, masked
    assert "%2" not in masked, masked


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("9811155555/9811155556", 2),
        ("9811155555,9811155556", 2),
        ("9811155555;9811155556", 2),
        ("9811155555 9811155556", 2),
        ("98111 55555/98111 55556", 2),
        ("Mob.98111.55555/Ph.98111.55556", 2),
        ("98111-55555, 98111-55556 and 011-2345 6789", 3),
    ],
)
def test_numbers_side_by_side_are_each_replaced(text, count):
    masked = user_text(text)
    assert not has_digit(masked), masked
    assert masked.count("[phone]") == count, masked


def test_a_number_repeated_thousands_of_times_is_replaced_every_time():
    masked = user_text("Mob.98111.55555 " * 1000)
    assert masked.count("[phone]") == 1000
    assert not has_digit(masked)


@pytest.mark.parametrize(
    "text",
    [
        "Tel 2612 0000",  # a local number, with what it is
        "Phone: 981 115 5555",  # 3+3+4 with spaces, with what it is
        "Call 2612-0000 today",
        "mobile no. 98 1115 5555",
        "Landline (22) 2612 0000",
        "Fax: 26120000",
    ],
)
def test_a_number_named_as_a_phone_is_replaced_in_any_grouping(text):
    masked = user_text(text)
    assert "[phone]" in masked, masked
    assert not has_digit(masked), masked


# --- credentials in links ----------------------------------------------------------------------
PARAMETERS = (
    "pwd",
    "passcode",
    "password",
    "pass",
    "token",
    "access_token",
    "key",
    "apikey",
    "api_key",
    "code",
    "sig",
    "signature",
    "secret",
    "auth",
    "session",
    "jwt",
)
PARAMETER_SHAPES = {
    "a link's tail without its host": "j/88123456789?{name}={value} see you",
    "alone": "use ?{name}={value} below",
    "after another parameter": "a=1&{name}={value}&b=2",
    "semicolon": "x;{name}={value}",
    "fragment": "callback#{name}={value}",
    "upper case": "?{upper}={value}",
    "encoded equals sign": "?{name}%3D{value}",
    "encoded name": "?{encoded}={value}",
    "HTML-escaped ampersand": "a=1&amp;{name}={value}",
    "numeric-escaped ampersand": "a=1&#38;{name}={value}",
}


@pytest.mark.parametrize("shape", PARAMETER_SHAPES.values(), ids=PARAMETER_SHAPES.keys())
@pytest.mark.parametrize("name", PARAMETERS)
def test_credentials_in_query_parameters_are_replaced(name, shape):
    text = shape.format(
        name=name,
        upper=name.upper(),
        encoded="".join(f"%{ord(ch):02X}" for ch in name),
        value=HIDDEN,
    )
    masked = user_text(text)
    assert fragments(masked, HIDDEN) == [], masked
    assert "[secret]" in masked or "[link]" in masked, masked


@pytest.mark.parametrize(
    "text",
    [
        "https://admin:{v}@intranet.example/report",
        "ftp://admin:{v}@files.example",
        "admin:{v}@intranet.example",
        "//admin:{v}@intranet.example/x",
        "zoommtg://zoom.us/join?confno=88123456789&pwd={v}",
        "msteams:/l/meetup-join/19%3ameeting_{v}",
        "https://teams.microsoft.com/l/meetup-join/19%3ameeting_{v}%40thread.v2/0",
        "HTTPS://US02WEB.ZOOM.US/J/88123456789?PWD={v}",
        "us02web.zoom.us/j/88123456789?pwd={v}",
        "meet.google.com/{v}",
    ],
)
def test_links_with_credentials_are_replaced_whole(text):
    masked = user_text(f"Join {text.format(v=HIDDEN)} today")
    assert fragments(masked, HIDDEN) == [], masked


# --- meeting passcodes and other credentials written out ---------------------------------------
PASSCODES = {
    "zoom": "Passcode: {v}",
    "no space": "Passcode:{v}",
    "equals": "PASSCODE = {v}",
    "dash": "Password - {v}",
    "is": "the passcode is {v}",
    "meeting password": "Meeting password: {v}",
    "teams": "Microsoft Teams meeting. Passcode: {v}",
    "webex": "Webex meeting password: {v}",
    "pass code": "Pass code: {v}",
    "pwd": "pwd: {v}",
    "pwd equals": "Zoom pwd={v}",
    "quoted": 'Password: "{q}"',
    "one-time password": "OTP {v}",
    "host key": "Host key: {v}",
    "bearer": "Authorization: Bearer {t}",
    "api key": "API key: {t}",
    "client secret": "client secret = {t}",
}


@pytest.mark.parametrize("text", PASSCODES.values(), ids=PASSCODES.keys())
def test_passcodes_written_out_are_replaced(text):
    token = (HIDDEN * 3)[:32]
    masked = user_text(text.format(v=HIDDEN, q="Wren 7 Canary", t=token) + " thanks")
    assert "[secret]" in masked, masked
    assert fragments(masked, HIDDEN) == [], masked
    assert "Wren 7" not in masked, masked
    assert masked.endswith(" thanks"), masked  # only the value goes


@pytest.mark.parametrize(
    "text",
    [
        "PIN 1234#",
        "Google Meet: +1 234-567-8901 PIN: 123 456 789#",
        "Phone conference ID: 123 456 789#",
        "Access code: 2511 123 4567",
        "Passcode: 482 913",
        "passcode 482913",
        "ATM PIN 4826",
        "Meeting PIN: 7781",
    ],
)
def test_numeric_passcodes_and_pins_are_replaced(text):
    masked = user_text(text)
    assert not has_digit(masked), masked
    assert "[secret]" in masked, masked


def _jwt() -> str:
    def part(value: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    signature = base64.urlsafe_b64encode(hashlib.sha256(b"x").digest()).rstrip(b"=").decode()
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part({'sub': 'asha'})}.{signature}"


TOKENS = {
    "json web token": _jwt(),
    "sha-256 in hex": hashlib.sha256(b"arkray").hexdigest(),
    "md5 in hex": hashlib.md5(b"arkray", usedforsecurity=False).hexdigest(),
    "base64 of random bytes": base64.b64encode(hashlib.sha512(b"arkray").digest()).decode(),
    "base64url of random bytes": base64.urlsafe_b64encode(
        hashlib.sha384(b"arkray").digest()
    ).decode(),
    "an api key prefix": "sk-" + "ant-" + "a1B2c3D4" * 4,
    "a github token prefix": "gh" + "p_" + "a1B2c3D4e5" * 4,
    "a cloud key prefix": "AK" + "IA" + "Z7Q2" * 4,
    "a chat token prefix": "xo" + "xb-" + "1234567890-" + "a1B2c3D4e5" * 2,
}


@pytest.mark.parametrize("token", TOKENS.values(), ids=TOKENS.keys())
def test_tokens_and_long_secrets_are_replaced(token):
    masked = user_text(f"Use {token} for the export")
    assert "[secret]" in masked, masked
    assert fragments(masked, token, size=8) == [], masked


# --- identity and card numbers -----------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Aadhaar 2345 6789 0124",
        "2345 6789 0124",  # passes the Aadhaar checksum (Verhoeff)
        "234567890124",
        "UID: 4991-1866-5246",
        "Aadhaar no: 1234 5678 9012",  # named as one: replaced, checksum or not
        "aadhar 5678-1234-5678",
        "Card 4111 1111 1111 1111 exp soon",  # passes the Luhn check
        "5500-0000-0000-0004",
        "378282246310005",
        "6011111111111117",
        "PAN ABCDE1234F",
    ],
)
def test_identity_and_card_numbers_are_replaced(text):
    masked = user_text(text)
    assert "[id]" in masked, masked
    assert not has_digit(masked), masked


# --- what stays --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Order 2345 6789 0123",  # fails the Aadhaar checksum, not named as one
        "FY 2023 2024 2025",  # passes the checksum, but every group is a year
        "ref 4111 1111 1111 1112",  # fails the Luhn check
        "lead 3f2a9c1e-8b7d-4e6f-9a0b-1c2d3e4f5a6b",  # a UUID is no secret
        "PIN 411001; pin code 560001; Pincode: 110001",
        "pH 7.4 6.8 7.0 7.2",
        "Password reset done; the password policy is strict",
        "token amount 25000, token advance paid",
        "Secret sauce: service quality",
        "OTP not received, access code pending",
        "INR 9811155555.00 tender",
        "Rs.9811155555 quoted",
        "ratio 12.9811155555",
        "₹ 98,11,155 and 1,00,000",
        "7/10/2026-8/10/2026",
        "S/N 6100-2345-678",
        "report_Adams_8380_V-lite_comparison_2026_final.pdf",
        "e.g. apollo.example; 3.5/5; 12.5 units",
    ],
)
def test_ordinary_values_are_kept(text):
    assert user_text(text) == text


# --- every cut through a masked value ------------------------------------------------------------
SENSITIVE = {
    "phone": ("Ring ", "98111 55555", " today", "9811155555"),
    "dotted phone": ("Mob.", "98111.55555", " today", "9811155555"),
    "international": ("call ", "+91 (0) 98111 55555", " now", "9811155555"),
    "landline": ("office ", "(022) 2345-6789", " later", "02223456789"),
    "link": ("join ", f"https://us02web.zoom.us/j/88123456789?pwd={HIDDEN}", " soon", HIDDEN),
    "link tail": ("join ", f"j/88123456789?pwd={HIDDEN}", " soon", HIDDEN),
    "passcode": ("Zoom ", f"Passcode: {HIDDEN}", " ok", HIDDEN),
    "email": ("mail ", "dr.mehta@apollo.example", " ok", "mehta@apollo"),
    "pan": ("PAN ", "ABCDE1234F", " ok", "ABCDE1234F"),
    "card": ("card ", "4111 1111 1111 1111", " ok", "4111111111111111"),
}


def leaked(part: str, core: str) -> list[str]:
    squeezed = re.sub(r"[\s./()+-]", "", part)
    return fragments(squeezed, core, size=3) if core.isdigit() else fragments(part, core)


@pytest.mark.parametrize(("before", "value", "after", "core"), SENSITIVE.values(), ids=SENSITIVE)
def test_a_cut_anywhere_through_a_masked_value_leaves_no_piece_of_it(before, value, after, core):
    text = "x" * 40 + " " + before + value + after + " " + "y" * 40
    begin = text.index(value)
    for cut in range(begin - 1, begin + len(value) + 2):
        head = user_text_slice(text, 0, cut)
        tail = user_text_slice(text, cut, len(text))
        for part in (head, tail):
            assert leaked(part, core) == [], (cut, part)
        # A preview cut there: from the start, or starting there (a search hit's preview).
        for shown in (clip(text, cut)["text"], clip(text, 60, start=cut)["text"]):
            assert leaked(shown, core) == [], (cut, shown)
            assert not re.search(r"\[[a-z]*$", shown), (cut, shown)  # never half a marker


def test_a_preview_reports_what_it_left_out():
    text = "Ring 98111 55555 about the analyser upgrade and the reagent contract"
    assert clip(text, 200) == {"text": user_text(text), "truncated": False}
    assert clip(text, 10)["truncated"] is True
    assert clip(text, 10)["text"] == "Ring [phone]"  # the marker is never cut in half
    assert clip(text, 30, start=11)["truncated"] is True
    assert clip(text, 30, start=11)["text"].startswith("[phone]")


# --- linear time on any input --------------------------------------------------------------------
ADVERSARIAL = {
    "plus and digit": "+1 " * 3400,
    "plus and pairs": "+91 " + "12 " * 3300,
    "dotted digits": "9." * 5000,
    "hyphenated digits": "9-" * 5000,
    "slashed pairs": "98/" * 3400,
    "spaced pairs": "98 " * 3400,
    "spaced digits": "9 " * 5000,
    "bracketed zeros": "(0) " * 2500,
    "phone keywords": "Tel " * 2500,
    "phone keywords and digits": "Ph 12 " * 1700,
    "passcode keywords": "Passcode " * 1100,
    "passcode keywords and colons": "Passcode: " * 1000,
    "pin keywords": "PIN 1 " * 1700,
    "query parameters": "?pwd=" * 2000,
    "ampersands": "&" * 10000,
    "entities": "&#57;" * 2000,
    "unfinished entities": "&#5" * 3400,
    "named-entity lookalikes": "&nbsp" * 2000,
    "percent runs": "%39" * 3400,
    "percent signs": "%" * 10000,
    "devanagari digits": chr(0x096F) * 10000,
    "fullwidth digits": chr(0xFF19) * 10000,
    "joiners between digits": (chr(0x200D) + "9") * 5000,
    "hex": "ab" * 5000,
    "hex words": "abcdef0123456789 " * 600,
    "base64 alphabet": "aB3" * 3400,
    "jwt lookalikes": "eyJ." * 2500,
    "bearer words": "Bearer " * 1500,
    "card candidates": "4111 " * 2000,
    "aadhaar candidates": "2345 " * 2000,
    "scheme lookalikes": "a:" * 5000,
    "scheme characters": "ab+.-" * 2000,
    "at signs": "a@" * 5000,
    "pan lookalikes": "ABCDE1234" * 1100,
    # Review round 2 (AI2-4): characters NFKC expands many times over.
    "expanding ligatures": chr(0xFDFA) * 10_000,
    "expanding unit squares": chr(0x33AF) * 10_000,
    "ligatures and digits": (chr(0xFDFA) + "9") * 5_000,
    "spaced capitals and figures": "BATCH 2026 A " * 770,
    "double-spaced card groups": "4111  " * 1_700,
    "double-spaced aadhaar groups": "2345  " * 1_700,
}
# Review round 1, AI-1: a word the patterns key on, then a long blank run and no value. The
# optional separators after the word (`[ \t]*[:=#.-]?[ \t]*`) backtracked over every split of
# the run: 3-7 s for one 10,000-character task description, inside a routed question's web
# request. Every keyword, after every kind of blank (the view folds them to spaces).
KEYWORDS = (
    "phone",
    "Ph",
    "phone no",
    "mobile no.",
    "Mob",
    "cell",
    "tel",
    "telephone",
    "landline",
    "fax",
    "WhatsApp",
    "contact",
    "call",
    "ring",
    "helpline",
    "hotline",
    "PIN",
    "pin no",
    "aadhaar",
    "aadhar",
    "adhaar",
    "uid",
    "aadhaar card no",
    "PAN",
    "pan no",
    "passcode",
    "password",
    "pwd",
    "pass",
    "wifi pass",
    "pw",
    "otp",
    "token",
    "secret",
    "api key",
    "access code",
    "conference id",
    "host key",
    "cvv",
    "bearer",
)
BLANKS = {
    "spaces": " ",
    "tabs": "\t",
    "no-break spaces": chr(0x00A0),
    "ideographic spaces": chr(0x3000),
    "spaces and tabs": " \t",
}
for _keyword in KEYWORDS:
    for _name, _blank in BLANKS.items():
        ADVERSARIAL[f"{_keyword} then {_name}"] = _keyword + (_blank * 9_990)[:9_990] + "x"


@pytest.mark.parametrize("text", ADVERSARIAL.values(), ids=ADVERSARIAL.keys())
def test_masking_stays_linear_on_crafted_text(text):
    assert len(text) >= 9_000
    started = time.perf_counter()
    user_text(text)
    user_text_slice(text, 0, len(text) // 2)
    clip(text, 240, start=len(text) // 3)
    assert time.perf_counter() - started < 0.5


BLANK_RUNS = {name: text for name, text in ADVERSARIAL.items() if " then " in name}


@pytest.mark.parametrize("text", BLANK_RUNS.values(), ids=BLANK_RUNS.keys())
def test_a_keyword_then_a_long_blank_run_costs_what_ordinary_text_costs(text):
    """Review round 1, AI-1 (3-7 s for one such text before): within a small factor of
    ordinary text of the same length, measured side by side (whatever else the machine
    is doing)."""
    ordinary = ("x " * len(text))[: len(text)]
    started = time.perf_counter()
    user_text(ordinary)
    baseline = time.perf_counter() - started
    started = time.perf_counter()
    user_text(text)
    crafted = time.perf_counter() - started
    assert crafted < 0.05 + 10 * baseline, (crafted, baseline)


def test_every_pattern_is_linear_on_blank_runs_without_the_view():
    """The patterns themselves, not only the view that reads a long blank run as one space:
    every keyword pattern's blanks are possessive (review round 1, AI-1). Runs of three
    blanks between keywords too (the view leaves those)."""
    texts = [kw + " " * 9_990 + "x" for kw in KEYWORDS]
    texts += [kw + "\t" * 9_990 for kw in ("call", "PIN", "uid", "passcode", "bearer", "pan")]
    texts += [("phone   " * 1_250)[:9_990] + "x", ("PIN no   " * 1_100)[:9_990]]
    slowest = (0.0, "")
    for text in texts:
        started = time.perf_counter()
        formatting._find(formatting._View(text))
        slowest = max(slowest, (time.perf_counter() - started, repr(text[:12])))
    assert slowest[0] < 0.25, slowest


FUZZ_PIECES = (
    " ",
    "\t",
    chr(0x00A0),
    chr(0x3000),
    ":",
    "-",
    ".",
    "#",
    "=",
    "\n",
    chr(0x200D),
    " no",
    " number",
    " card",
    "(",
    "+",
    " is",
)
FUZZ_TAILS = ("x", "", "\n", "98111", "1" * 20, "#", "ABCDE", ":", '"')


def test_keyword_separator_runs_stay_linear_fuzz():
    """Review round 1, AI-1: any keyword, any mix of separators repeated to any length,
    any ending; seeded, so a failure reproduces."""
    rng = random.Random(20261012)
    slowest = (0.0, "")
    for _ in range(300):
        keyword = rng.choice(KEYWORDS)
        keyword = keyword.upper() if rng.random() < 0.2 else keyword
        unit = "".join(rng.choice(FUZZ_PIECES) for _ in range(rng.randrange(1, 4)))
        length = rng.choice((1, 3, 40, 1_000, 9_000))
        text = keyword + (unit * length)[:length] + rng.choice(FUZZ_TAILS)
        started = time.perf_counter()
        user_text(text)
        elapsed = time.perf_counter() - started
        slowest = max(slowest, (elapsed, repr(text[:40])))
    assert slowest[0] < 0.25, slowest


def test_blank_runs_and_encodings_keep_the_offset_map_exact():
    """Long blank runs are read as one space (review round 1): the text around a masked
    number is still exactly the text, and no piece of the number survives."""
    rng = random.Random(7)
    blanks = (" ", "\t", chr(0x00A0), chr(0x3000))
    words = ("Dr", "Mehta", "said", "the", "analyser", "&amp;", "50%", "re:", "x")
    for _ in range(400):

        def filler() -> str:
            return "".join(
                rng.choice(words) + rng.choice(blanks) * rng.randrange(1, 12)
                for _ in range(rng.randrange(0, 5))
            )

        digits = [
            chr(ZEROS["devanagari"] + int(d)) if rng.random() < 0.3 else d for d in "9811155555"
        ]
        gap = rng.choice(blanks) * rng.randrange(1, 9)
        number = "".join(digits[:5]) + gap + "".join(digits[5:])
        before, after = "Ring " + filler(), " " + filler() + "today"
        masked = user_text(before + number + after)
        assert masked == before + "[phone]" + after, repr(masked)
        # Review round 2 (AI2-1): an Aadhaar or a card number after a figure and a run of two
        # blanks or more (a table's columns) is no "list of figures": masked, whatever the run.
        figure = rng.choice(("Row 7", "S.No 12", "1", "Qty 2"))
        value = rng.choice(("2345 6789 0124", "234567890124", "4111 1111 1111 1111"))
        run = rng.choice(blanks) * rng.randrange(2, 13)
        column = figure + run
        masked = user_text(column + value + " today")
        assert masked == column + "[id] today", repr(masked)


# --- review round 1, AI-2: the shapes still unmasked ---------------------------------------------
@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Zoom pass: 482913", "4829"),
        ("PW: Kx4Wren9", "Kx4W"),
        ("wifi pass Arkray@2026", "Arkray@"),
        ("Wi-Fi pass: Arkray@2026", "Arkray@"),
        ("Passcode\n482913", "4829"),
        ("Passcode :\n482913", "4829"),
        ("passcode482913", "4829"),
        ("CVV: 123", "123"),
        ("card 4111 1111 1111 1111 exp soon, cvv 123", "123"),
    ],
)
def test_round_one_passcodes_are_replaced(text, secret):
    masked = user_text(text)
    assert "[secret]" in masked, masked
    assert secret not in masked, masked


@pytest.mark.parametrize(
    "text",
    [
        "Mob 98 11 15 55 55",
        "call 98 11 15 55 55",
        "Mob 98-11-15-55-55",  # four or five joined groups need a phone word (round 2)
        "Mob 98.11.15.55.55",
        "Ph: 981-11-555-55",
        "Phone 98/11/15/55/55",
        "001 212 555 0100",
        "0044 20 7946 0958",
        "00971 50 123 4567",
    ],
)
def test_round_one_phone_numbers_are_replaced(text):
    masked = user_text(text)
    assert "[phone]" in masked, masked
    assert not has_digit(masked), masked


def test_a_country_code_in_brackets_is_replaced_with_its_brackets():
    assert user_text("(+91) 9811155555") == "[phone]"
    assert user_text("call (+91 98111 55555) now") == "call ([phone]) now"


@pytest.mark.parametrize(
    "text",
    [
        "2345-6789-0124",  # passes the Verhoeff check
        "Adhaar 2345 6789 0123",
        "Adhar: 2345-6789-0123",
        "aadhaar card no 1234 5678 9012",
        "aaapl1234c",  # a lower-case PAN (an individual's: P in fourth place)
        "pan abcde1234f",
        "PAN: ABCDE 1234 F",
        "pan no. abcde 1234 f",
        "PAN AAAPL 1234 C",  # spaced capitals need the name (round 2: "BATCH 2026 A")
        "4111.1111.1111.1111",
    ],
)
def test_round_one_identity_and_card_numbers_are_replaced(text):
    masked = user_text(text)
    assert "[id]" in masked, masked
    assert not has_digit(masked), masked


@pytest.mark.parametrize(
    "text",
    [
        "pass 2 units to the lab",
        "boarding pass 1234",
        "Pass: the demo went well",
        "I'll pass on the message",
        "pw protected",
        "CVV not shared",
        "98 11 15 55 55",  # spaced pairs nothing names: a list of figures
        "98.11.15.55",
        "Ticket 0042 0043 0044",
        "Invoice 001/2026/0042, 001-2026-0042",
        "order 2345-6789-0123",  # fails the Verhoeff check
        "years 2023-2024-2025",
        "lot abcde1234f",  # not a PAN: D is no holder type
        "batch 2026 A, ADAMS 8180 V, trial 2026 a",
        "pan India launch 2026; pan card pending",
        "Release 2.10.15.55",
    ],
)
def test_round_one_ordinary_text_is_kept(text):
    assert user_text(text) == text


# --- review round 2 ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Rahul    2345 6789 0124    9811155555    Pune",
        "1    4111 1111 1111 1111",
        "S.No 12\t\t\t\t4111111111111111 exp 12/27",
        "Row 7" + chr(0x2009) * 4 + "234567890124",
        "Row 7" + "&nbsp;" * 4 + "234567890124",
    ],
)
def test_a_wide_gap_after_a_figure_still_masks_ids_and_cards(text):
    """AI2-1: a run of blanks read as one space made the figure before it adjacent (a list
    of figures), so the id after it went out; three blanks were masked, four were not."""
    masked = user_text(text)
    assert "[id]" in masked, masked
    for number in ("2345", "6789", "0124", "4111", "1111"):
        assert number not in masked, masked


@pytest.mark.parametrize(
    "text",
    [
        "Aadhaar no: 2345  6789  0124",
        "Aadhaar: 2345\t\t6789\t\t0124",
        "Aadhaar: 2345" + chr(0x00A0) * 2 + "6789" + chr(0x00A0) * 2 + "0124",
        "Aadhaar: 2345&nbsp;&nbsp;6789&nbsp;&nbsp;0124",
        "2345   6789   0124",
        "4111  1111  1111  1111",
        "Card 4111\t\t1111\t\t1111\t\t1111",
        "PAN: ABCDE  1234  F",
        "pan abcde   1234   f",
    ],
)
def test_ids_with_two_or_three_blanks_between_groups_are_replaced(text):
    """AI2-2: phones with these gaps were masked, ids and cards were not."""
    masked = user_text(text)
    assert "[id]" in masked, masked
    assert not has_digit(masked), masked


@pytest.mark.parametrize(
    "text",
    [
        "98111&nbsp;&nbsp;&nbsp;&nbsp;55555",
        "98111&#32;&#32;&#32;&#32;55555",
        "+91&nbsp;&nbsp;&nbsp;&nbsp;98111 55555",
        "Phone 98111555559999",
    ],
)
def test_encoded_blank_runs_and_long_runs_are_replaced_whole(text):
    """AI2-2: blanks decoded from entities are a run like any other; AI2-4: a named number's
    digits go as one (no "[phone]9")."""
    masked = user_text(text)
    assert "[phone]" in masked, masked
    assert not has_digit(masked), masked


@pytest.mark.parametrize(
    "text",
    [
        "BATCH 2026 A received.",
        "MATCH 2026 B scheduled.",
        "CHECK 2026 A done.",
        "ASSAY 1000 X kit.",
        "ROUTE 1000 A for the van.",
        "QC pass: 3 of 3 levels.",
        "Calibration pass: L1 L2 L3.",
        "Day pass: 2 visitors.",
        "Pass: 2 of 3 controls.",
        "QC pass: 100%",
        "QC levels 60-65-70-75-80 mg/dL.",
        "Temperatures 65-70-75-80-85 C logged.",
        "Readings 90.12.34.56.78 (log).",
        "Units: 98-11-15-55-55 per region.",
        "Invoice 0012 3456 7890 sent.",
        "Ref 0045 1234 5678 9 attached.",
        "CVV: n/a",
        "CVV: 2 attempts left",
        "Label 0012 3456 7890",  # no country 12
        "OTP: not received.",
        "Access code: shared by IT later.",
        "Token: 3 strips left.",
        "Secret: none.",
        # Pre-round-1 over-masking routed answers showed: references and amounts after
        # their word, "Cell" in a lab's sense.
        "PO 7500012345 released.",
        "AWB 7894561230 (Blue Dart) dispatched.",
        "S/N 6100234567 installed at Ruby Hall.",
        "Analyser S/N: 7012345678",
        "Serial No 8380-1234-56 installed.",
        "Rs    9811155555",
        "Cell 12345678 per ml.",
    ],
)
def test_round_two_ordinary_text_is_kept(text):
    """AI2-3: routed answers and retrieval snippets show masked text, so over-masking is
    visible today; each of these was masked after round 1."""
    assert user_text(text) == text


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Zoom pass: 482913", "4829"),
        ("Door pass: 4826", "4826"),
        ("PW: Kx4Wren9", "Kx4W"),
        ("CVV: 123", "123"),
        ("OTP: 482913", "4829"),
        ("Access code: 2511 123 4567", "2511"),
        ("Password: reset123", "reset1"),
        ("Token: " + "a1B2c3D4e5", "a1B2"),
        ("0045 3312 3456 for the Copenhagen office", "3312"),
        ("Call Bill 98111 55555", "98111"),
        ("Cellphone: 2612 0000", "2612"),
        ("cell no 2612 0000", "2612"),
        ("so 98111 55555 is his", "98111"),
    ],
)
def test_round_two_still_replaced(text, secret):
    """The tightened rules keep what they are for."""
    masked = user_text(text)
    assert secret not in masked, masked


def test_no_character_expands_without_bound_in_the_view():
    """AI2-4: U+FDFA became 18 characters under NFKC (10,000 characters, a 180,000-character
    view). A character whose expansion carries no ASCII stays one character; the longest
    expansion left is short."""
    folded = getattr(formatting._folded, "__wrapped__", formatting._folded)
    longest = max(
        len(folded(chr(code))) for code in range(0x80, 0x10000) if not 0xD800 <= code <= 0xDFFF
    )
    assert longest <= 8, longest
    text = chr(0xFDFA) * 10_000
    assert len(formatting._view(text).text) == len(text)


# --- review round 3 ------------------------------------------------------------------------------
# AI3-1: a reference or amount word hid the number after it even across a sentence or clause
# boundary ("Thanks a lot. 9811155555 is Dr Shah's direct line."). Only a label right before
# the figure counts now: an abbreviation or a word with its "no" or "#", then blanks and at
# most one of ":#=/-"; a dot only straight after ref, inv, acct, txn or "no".
SENTENCE_WORDS = (
    "ref",
    "Ref",
    "inv",
    "PO",
    "order",
    "invoice",
    "ticket",
    "serial",
    "tracking",
    "docket",
    "lot",
    "batch",
    "account",
    "a/c",
    "AC",
    "s/n",
    "SN",
    "acct",
    "challan",
    "receipt",
    "voucher",
    "txn",
    "transaction",
    "utr",
    "awb",
    "value",
    "amount",
    "price",
    "total",
    "budget",
    "worth",
    "cost",
    "Rs",
    "INR",
)
BOUNDARIES = {
    "full stop": ". ",
    "comma": ", ",
    "semicolon": "; ",
    "full stop and line break": ".\n",
    "exclamation and line break": "!\n",
    "line break": "\n",
    "question mark": "? ",
}
# "Ref. 9100234567", "Rs. 98,11,155": these abbreviations' dot is their own.
OWN_DOT = frozenset({"ref", "Ref", "inv", "acct", "txn", "Rs"})
NUMBER_SHAPES = (
    "9811155555",
    "98111 55555",
    "09811155555",
    "020-2612-0000",
    "0044 20 7946 0958",
    "2345 6789 0124",
)
SENTENCE_CASES = [
    pytest.param(word, boundary, id=f"{word}-{name}")
    for word in SENTENCE_WORDS
    for name, boundary in BOUNDARIES.items()
    if not (boundary == ". " and word in OWN_DOT)
]


@pytest.mark.parametrize(("word", "boundary"), SENTENCE_CASES)
def test_a_word_ending_a_sentence_never_hides_the_next_number(word, boundary):
    for number in NUMBER_SHAPES:
        masked = user_text(f"Discussed the {word}{boundary}{number} is Rahul's line.")
        assert not has_digit(masked), masked


@pytest.mark.parametrize(
    "text",
    [
        "Thanks a lot. 9811155555 is Dr Shah's direct line.",
        "Asked for the PO. 9811155555 is the purchase head.",
        "Discussed price. 98111 55555 for follow-up.",
        "Need the order. 020-2612-0000 is the lab.",
        "UK distributor discussed the order. 0044 20 7946 0958 is their desk.",
        "KYC done for the account. 2345 6789 0124 is the holder's.",
        "Collected documents for the order: 2345 6789 0124",
        "Lab landline after the order: 020 2612 0000",
        "AC 9811155555",
        "SN 9811155555",
        "lot - 9811155555",
    ],
)
def test_the_reviewers_sentences_are_masked(text):
    masked = user_text(text)
    assert not has_digit(masked), masked


@pytest.mark.parametrize(
    "text",
    [
        "PO 7500012345",
        "PO-9100023456 approved.",
        "PO: 7500012345",
        "Ref. 9100234567",
        "Ref no. 9811155555",
        "Analyser S/N: 7012345678",
        "S/N 6100234567 installed at Ruby Hall.",
        "AWB 7894561230 (Blue Dart) dispatched.",
        "Serial No 8380-1234-56 installed.",
        "Invoice #9100234567",
        "Order no. 9811155555",
        "A/c 9811155555",
        "Txn: 9811155555",
        "Rs    9811155555",
    ],
)
def test_a_reference_label_keeps_its_figures(text):
    """What round 2 kept and a label still says: a reference, not a phone."""
    assert user_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "Deal value 9500000000.",
        "Invoice 9100234567 raised.",
        "Tracking 8901234567 via DTDC.",
        "Order 2345-6789-0124 confirmed.",
        "Invoice 2345 6789 0124 raised.",
    ],
)
def test_ordinary_words_no_longer_hide_figures(text):
    """Accepted over-masking (review round 3: a false positive is cheaper than a leak): an
    ordinary word ("order", "invoice", "value") followed by a figure in a phone's or an
    Aadhaar's shape, with no "no", "#" or ":" making it a label."""
    masked = user_text(text)
    assert "[phone]" in masked or "[id]" in masked, masked


@pytest.mark.parametrize(
    "text",
    [
        "4111 1111-1111 1111",
        "4111.1111 1111 1111",
        "4111 1111\t1111 1111",
        "Card: 4111 1111 1111\t1111",
        "4111 1111    1111 1111",
        "2345 6789    0124",
    ],
)
def test_cards_and_ids_with_mixed_single_gaps_are_replaced(text):
    """AI3-2: one gap throughout was required; single separators mix (and a long run, two
    spaces in the view, among single ones)."""
    masked = user_text(text)
    assert "[id]" in masked, masked
    assert not has_digit(masked), masked


@pytest.mark.parametrize(
    "text",
    [
        "PIN 123    456#",
        "PIN: 123  456  789#",
        "Conference ID:    123    456    789#",
        "Passcode: 482  913",
    ],
)
def test_spaced_numeric_passcodes_are_replaced_whole(text):
    masked = user_text(text)
    assert "[secret]" in masked, masked
    assert not has_digit(masked), masked


@pytest.mark.parametrize(
    "text",
    [
        "Cell: 981 115 5555",
        "Cell: 2612 0000",
        "Cell: 98 111 55555",
        "Cell 98 11 15 55 55",
        "Cell- 9811 155 555",
        "cell#2612 0000",
    ],
)
def test_cell_with_a_separator_or_a_mobile_is_a_phone_word(text):
    """AI3-3; "Cell 12345678 per ml" (a lab's cell count) stays (round 2)."""
    masked = user_text(text)
    assert "[phone]" in masked, masked
    assert not has_digit(masked), masked


@pytest.mark.parametrize(
    "text",
    [
        "0033 6 12 34 56 78",
        "+33 6 12 34 56 78",
        "00971 4 123 4567",
        "Dr Shah (cell) 981 115 5555",
        "Dr Shah (mob) 2612 0000",
    ],
)
def test_round_three_numbers_the_probes_found_are_replaced(text):
    """A one-digit first group after a country code; a phone word in brackets."""
    masked = user_text(text)
    assert "[phone]" in masked, masked
    assert not has_digit(masked), masked


@pytest.mark.parametrize("text", ["+10.5 12.5 13.0 14.2", "+5 6 apples", "(cell) 12345678 per ml"])
def test_round_three_figures_are_kept(text):
    assert user_text(text) == text


@pytest.mark.parametrize("text", ["PW: 4829", "pass: 4829", "Pass = 4829", "Door pass 4829"])
def test_four_digits_after_pw_or_pass_are_a_passcode(text):
    """AI3-4; "QC pass: 100%", "Pass: 2 of 3 controls" stay (round 2)."""
    masked = user_text(text)
    assert "[secret]" in masked, masked
    assert not has_digit(masked), masked


def test_a_long_mixed_note_is_masked_in_bounded_time():
    """Ten times the longest note: every kind of value, repeated."""
    piece = (
        f"Mob.98111.55555 Passcode: {HIDDEN} zoom.us/j/1?pwd={HIDDEN} &#57;&#56; "
        "dr.mehta@apollo.example ABCDE1234F 4111 1111 1111 1111 ₹ 1,00,000 pH 7.4 "
    )
    text = (piece * (100_000 // len(piece) + 1))[:100_000]
    started = time.perf_counter()
    masked = user_text(text)
    assert time.perf_counter() - started < 2.0
    assert fragments(masked, HIDDEN) == []
    assert "98111" not in masked
