import unicodedata

import pytest

from arkray.core.text import TextRejected, clean_line, clean_multiline

ZWJ, ZWNJ = chr(0x200D), chr(0x200C)

# Invisible or display-changing characters that are refused everywhere.
REFUSED = [
    chr(0),  # NUL
    chr(7),  # bell
    chr(0x0B),  # vertical tab
    chr(0x1C),  # file separator (Python treats it as whitespace)
    chr(0x1B),  # escape
    chr(0x7F),  # delete
    chr(0x85),  # next line (C1)
    chr(0x9B),  # CSI (C1)
    chr(0x00AD),  # soft hyphen
    chr(0x200B),  # zero-width space
    chr(0x200E),  # left-to-right mark
    chr(0x2060),  # word joiner
    chr(0x202E),  # right-to-left override
    chr(0x2066),  # left-to-right isolate
    chr(0x2028),  # line separator
    chr(0x2029),  # paragraph separator
    chr(0xFEFF),  # byte order mark
    chr(0xE0041),  # tag LATIN CAPITAL LETTER A (prompt smuggling)
    chr(0xE000),  # private use
    chr(0xFFFE),  # noncharacter
    chr(0xFDD0),  # noncharacter
    chr(0xD800),  # lone surrogate
]


class TestCleanLine:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("  Rahul   Sharma ", "Rahul Sharma"),
            ("Tab\tand\nnewline\r\nhere", "Tab and newline here"),
            (f"No{chr(0xA0)}break", "No break"),
            ("", ""),
            ("   ", ""),
        ],
    )
    def test_whitespace_is_collapsed_and_trimmed(self, raw, expected):
        assert clean_line(raw) == expected

    def test_unicode_is_normalised_to_nfc(self):
        decomposed = "Jose" + chr(0x301)  # "e" + combining acute accent
        assert clean_line(decomposed) == "Jos" + chr(0xE9)
        assert unicodedata.is_normalized("NFC", clean_line(decomposed))

    @pytest.mark.parametrize(
        "name",
        [
            "राजेश कुमार",  # Hindi
            "முருகன்",  # Tamil, a single name
            "ശ്രീ" + ZWJ + "ജ",  # Malayalam needs the zero-width joiner
            "क्" + ZWNJ + "ष",  # Devanagari with a zero-width non-joiner
            "李小龍",
            "محمد",
            "O'Brien-Smith",
            "Zoë Ångström",
            "👩" + ZWJ + "💻",  # an emoji ZWJ sequence
        ],
    )
    def test_real_names_in_any_script_are_kept_intact(self, name):
        assert clean_line(name) == unicodedata.normalize("NFC", name)

    @pytest.mark.parametrize("char", REFUSED)
    def test_invisible_and_control_characters_are_refused(self, char):
        with pytest.raises(TextRejected):
            clean_line(f"Evil{char}Name")

    @pytest.mark.parametrize("invisible", [ZWJ, ZWJ + ZWNJ, chr(0x301), " " + ZWJ + " "])
    def test_text_with_nothing_visible_counts_as_empty(self, invisible):
        assert clean_line(invisible) == ""
        assert clean_multiline(invisible) == ""


class TestCleanMultiline:
    def test_line_breaks_are_kept_and_normalised(self):
        assert clean_multiline("  First line  \r\nSecond\rThird\n\n") == "First line\nSecond\nThird"

    def test_tabs_are_allowed(self):
        assert clean_multiline("a\tb") == "a\tb"

    @pytest.mark.parametrize("char", REFUSED)
    def test_other_invisible_characters_are_refused(self, char):
        with pytest.raises(TextRejected):
            clean_multiline(f"text{char}more")
