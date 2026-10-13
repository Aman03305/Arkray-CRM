import time
import unicodedata

import pytest

from arkray.core.text import (
    TextRejected,
    clean_line,
    clean_multiline,
    clean_search_query,
    search_needles,
    without_refused,
)

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


class TestSearchInput:
    """One set of search-input rules for the Leads list and global search (Phase 7)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("  Rahul   Sharma ", "Rahul Sharma"),
            ("Jose" + chr(0x301), "Jos" + chr(0xE9)),  # NFD typed, NFC searched
            ("a b Rahul", "a b Rahul"),  # short words are kept in the text, skipped as terms
            ("", ""),
        ],
    )
    def test_queries_are_cleaned_like_stored_text(self, raw, expected):
        assert clean_search_query(raw) == expected

    @pytest.mark.parametrize("raw", ["a", "a b c", "x " * 20])
    def test_a_query_of_only_one_letter_words_is_refused(self, raw):
        with pytest.raises(ValueError, match="at least 2 characters"):
            clean_search_query(raw)

    @pytest.mark.parametrize("char", REFUSED)
    def test_invisible_and_control_characters_are_refused(self, char):
        with pytest.raises(TextRejected):
            clean_search_query(f"Rahul{char}Sharma")

    def test_short_words_are_skipped_before_the_five_word_cap(self):
        """Phase 7 fix: "a b c d e Rahul" used to search the first five words, all of them too
        short, so nothing at all (the Leads list then showed every lead)."""
        assert search_needles("a b c d e Rahul") == ["Rahul"]
        assert search_needles("one two three four five six") == [
            "one",
            "two",
            "three",
            "four",
            "five",
        ]

    def test_needles_keep_joiners_and_scripts(self):
        assert search_needles("क्" + ZWNJ + "ष राहुल") == ["क्" + ZWNJ + "ष", "राहुल"]


# --- blank-looking characters (adversarial review of 73ff1e3, L12, R164; review round 1) -----
# Characters that render blank or as a look-alike for a space yet are not caught by the
# category rule above: Hangul fillers (Lo), the Braille blank and other "blank" symbols (So),
# the combining grapheme joiner and Khmer inherent vowels (Mn), and the unassigned holes in
# the Unicode tag block (Cn). A name made of these renders empty or can be dodged past an
# exact-match check, so each is refused like any other invisible character.
BLANK_LOOKALIKES = [
    chr(0x2800),  # BRAILLE PATTERN BLANK (So)
    chr(0x3164),  # HANGUL FILLER (Lo)
    chr(0xFFA0),  # HALFWIDTH HANGUL FILLER (Lo)
    chr(0x115F),  # HANGUL CHOSEONG FILLER (Lo)
    chr(0x1160),  # HANGUL JUNGSEONG FILLER (Lo)
    chr(0x1D159),  # MUSICAL SYMBOL NULL NOTEHEAD (So)
    chr(0xFFFC),  # OBJECT REPLACEMENT CHARACTER (So)
    chr(0x034F),  # COMBINING GRAPHEME JOINER (Mn)
    chr(0x17B4),  # KHMER VOWEL INHERENT AQ (Mn, invisible)
    chr(0x17B5),  # KHMER VOWEL INHERENT AA (Mn, invisible)
    chr(0x180E),  # MONGOLIAN VOWEL SEPARATOR (Cf, already refused: kept here for cover)
    chr(0xE0000),  # an unassigned hole in the tag block (Cn)
    chr(0xE0020),  # TAG SPACE (Cf)
    chr(0xE007F),  # CANCEL TAG (Cf)
]


# Unicode DerivedCoreProperties Default_Ignorable_Code_Point (15.1), every code point.
_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
DEFAULT_IGNORABLE = [
    code
    for low, high in _IGNORABLE_RANGES
    for code in range(low, high + 1)
    if code not in (0x200C, 0x200D)  # ZWNJ, ZWJ: Indic scripts need them
]
# Names that must keep passing (review round 1's list): joiners, accents, scripts, emoji
# sequences, ideographic variation sequences, a Mongolian letter with its free variation selector.
LEGITIMATE = {
    "hindi_zwj": "क्" + chr(0x200D) + "ष",
    "devanagari_zwnj": "क्" + chr(0x200C) + "ष",
    "malayalam_zwj": "ശ്രീ" + chr(0x200D) + "ജ",
    "malayalam_zwnj": "ന്" + chr(0x200C),
    "accented": "José Müller",
    "accented_decomposed": "Jose" + chr(0x301),
    "cjk": "山田太郎",
    "cjk_ivs": chr(0x845B) + chr(0xE0100),
    "cjk_compatibility_variant": chr(0x6F22) + chr(0xFE00),
    "arabic": "محمد",
    "thai": "สมชาย",
    "korean": "홍길동",
    "emoji_vs16": chr(0x2764) + chr(0xFE0F),
    "symbol_vs16": chr(0x260E) + chr(0xFE0F),
    "symbol_vs15": chr(0x260E) + chr(0xFE0E),
    "thumbs_vs16": chr(0x1F44D) + chr(0xFE0F),
    "information_vs16": chr(0x2139) + chr(0xFE0F),
    "double_exclamation_vs16": chr(0x203C) + chr(0xFE0F),
    "emoji_zwj": chr(0x1F468) + chr(0x200D) + chr(0x1F469) + chr(0x200D) + chr(0x1F467),
    "emoji_zwj_vs": chr(0x1F3F3) + chr(0xFE0F) + chr(0x200D) + chr(0x1F308),
    "technologist": chr(0x1F469) + chr(0x200D) + chr(0x1F4BB),
    "keycap": "1" + chr(0xFE0F) + chr(0x20E3),
    "keycap_hash": "#" + chr(0xFE0F) + chr(0x20E3),
    "flag": chr(0x1F1EE) + chr(0x1F1F3),
    "skin_tone": chr(0x1F44D) + chr(0x1F3FD),
    "mongolian_fvs": chr(0x182D) + chr(0x180B),  # MONGOLIAN LETTER GA + FVS1
    "mongolian_fvs4": chr(0x1820) + chr(0x180F),
}


class TestBlankLookalikes:
    @pytest.mark.parametrize("char", BLANK_LOOKALIKES)
    def test_blank_characters_are_refused(self, char):
        with pytest.raises(TextRejected):
            clean_line(f"City{char}Lab")
        with pytest.raises(TextRejected):
            clean_multiline(f"City{char}Lab")

    @pytest.mark.parametrize("char", BLANK_LOOKALIKES)
    def test_a_name_of_only_a_blank_character_is_refused(self, char):
        with pytest.raises(TextRejected):
            clean_line(char)

    def test_a_leading_variation_selector_is_refused(self):
        # A standalone variation selector (no base character to style) renders blank.
        with pytest.raises(TextRejected):
            clean_line(chr(0xFE0F) + "Lab")
        with pytest.raises(TextRejected):
            clean_line("City " + chr(0xFE0F))  # after a space: nothing to apply to

    @pytest.mark.parametrize("label", sorted(LEGITIMATE))
    def test_legitimate_joiners_and_selectors_are_kept(self, label):
        name = LEGITIMATE[label]
        assert clean_line(name) == unicodedata.normalize("NFC", name)

    @pytest.mark.parametrize("code", DEFAULT_IGNORABLE, ids=hex)
    def test_every_default_ignorable_after_a_letter_is_refused(self, code):
        """Review round 1, PARSERS-5: every Default_Ignorable_Code_Point (Unicode
        DerivedCoreProperties) after a base letter, ZWJ and ZWNJ apart. A variation selector or a
        Mongolian free variation selector after a Latin letter styles nothing and renders blank."""
        with pytest.raises(TextRejected):
            clean_line("Ab" + chr(code) + "c")

    @pytest.mark.parametrize(
        "name",
        [
            "Rahul" + chr(0xFE0F),  # renders exactly like "Rahul": an exact-match dodge
            "City" + chr(0xFE0F) + "Lab",
            "Ravi" + chr(0xFE0E),
            "राहुल" + chr(0xFE0F),  # after an Indic letter
            "Ab" + chr(0xE0100),  # an ideographic selector after a Latin letter
            chr(0x260E) + chr(0xE0100),  # an ideographic selector after a symbol
            "Ab" + chr(0x180B),  # a Mongolian selector after a Latin letter
            chr(0x5C71) + chr(0x180B),  # ... after a CJK ideograph
            chr(0x1F44D) + chr(0xFE0F) + chr(0xFE0F),  # a selector after a selector
        ],
    )
    def test_a_selector_after_a_base_that_cannot_carry_it_is_refused(self, name):
        with pytest.raises(TextRejected):
            clean_line(name)


# --- review round 2 (PARSERS2-8) --------------------------------------------------------------
# The review's 48 legitimate names (every script, joiner and emoji sequence it tried), and what it
# found still passing: blank characters outside the default-ignorable list, and variation
# selectors after a base that has no such variant (an exact-match dodge).
REVIEW_LEGITIMATE = {
    "accented": "Jos\u00e9 M\u00fcller",
    "accented_decomposed": "Jose\u0301",
    "arabic": "\u0645\u062d\u0645\u062f",
    "arabic_tatweel": "\u0645\u0640\u062d\u0645\u062f",
    "arrow": "\u2194\ufe0f",
    "bangbang": "\u203c\ufe0f",
    "bengali_ya_phala_zwj": "\u09b0\u200d\u09cd\u09af\u09be\u09ac",
    "bengali_zwnj": "\u09b0\u200c\u09cd\u09af",
    "circled_m": "\u24c2\ufe0f",
    "cjk": "\u5c71\u7530\u592a\u90ce",
    "cjk_ext_b_ivs": "\U00020000\U000e0100",
    "cjk_ivs": "\u845b\U000e0100",
    "cjk_svs_fe00": "\u8279\ufe00",
    "company_symbols": "AT&T / Johnson & Johnson + Co. \u20b9100",
    "emoji_text_vs15": "\u00a9\ufe0e",
    "emoji_vs16": "\u2764\ufe0f",
    "eye_bubble": "\U0001f441\ufe0f\u200d\U0001f5e8\ufe0f",
    "family_zwj": "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",
    "flag_in": "\U0001f1ee\U0001f1f3",
    "gujarati": "\u0a97\u0ac1\u0a9c\u0ab0\u0abe\u0aa4\u0ac0",
    "gurmukhi": "\u0a2a\u0a70\u0a1c\u0a3e\u0a2c\u0a40",
    "handshake": "\U0001faf1\U0001f3fc\u200d\U0001faf2\U0001f3ff",
    "heart_fire": "\u2764\ufe0f\u200d\U0001f525",
    "hebrew_niqqud": "\u05e9\u05b8\u05c1\u05dc\u05d5\u05b9\u05dd",
    "hindi_name": "\u0936\u094d\u0930\u0940 \u0930\u093e\u092e",
    "hindi_zwj": "\u0915\u094d\u200d\u0937",
    "info": "\u2139\ufe0f",
    "japanese_kana": "\u3084\u307e\u3060",
    "kannada": "\u0c95\u0ca8\u0ccd\u0ca8\u0ca1",
    "keycap_1": "1\ufe0f\u20e3",
    "keycap_hash": "#\ufe0f\u20e3",
    "keycap_star": "*\ufe0f\u20e3",
    "korean": "\ud64d\uae38\ub3d9",
    "malayalam_chillu_zwj": "\u0d28\u0d4d\u200d",
    "malayalam_zwnj": "\u0d28\u0d4d\u200c",
    "marathi_eyelash_ra": "\u0930\u094d\u200d\u092f",
    "mongolian_fvs": "\u182d\u180b",
    "odia": "\u0b13\u0b21\u0b3c\u0b3f\u0b06",
    "persian_zwnj": "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",
    "pirate_flag": "\U0001f3f4\u200d\u2620\ufe0f",
    "rainbow_flag": "\U0001f3f3\ufe0f\u200d\U0001f308",
    "sinhala_zwj": "\u0dc1\u0dca\u200d\u0dbb\u0dd3",
    "skin_tone": "\U0001f44d\U0001f3fd",
    "skin_tone_v": "\u270c\U0001f3fd",
    "tamil": "\u0ba4\u0bae\u0bbf\u0bb4\u0bcd",
    "technologist": "\U0001f9d1\U0001f3fd\u200d\U0001f4bb",
    "telugu": "\u0c24\u0c46\u0c32\u0c41\u0c17\u0c41",
    "thai": "\u0e2a\u0e21\u0e0a\u0e32\u0e22",
}
REVIEW_BLANKS = {
    "egyptian full blank U+13441": "\U00013441",
    "egyptian half blank U+13442": "\U00013442",
    "khitan filler U+16FE4": "\U00016fe4",
}
REVIEW_DODGES = {
    "A+ Labs with FE0F after +": "A+\ufe0f Labs",
    "Apollo 24|7 with FE01 after |": "Apollo 24|\ufe017",
    "Arabic name + RLM": "\u0645\u062d\u0645\u062f\u200f",
    "CJK + unregistered FE0F": "\u5c71\ufe0f\u7530",
    "CJK + unregistered IVS E01EF": "\u5c71\U000e01ef\u7530",
    "Lab1 + FE0F (digit base)": "Lab1\ufe0f",
    "Rahul + Egyptian full blank": "Rahul\U00013441",
    "Rahul + FE0F": "Rahul\ufe0f",
    "Rahul + khitan filler": "Rahul\U00016fe4",
    "price with FE00 after rupee sign": "\u20b9\ufe00100",
}


class TestReviewRound2:
    @pytest.mark.parametrize("label", sorted(REVIEW_LEGITIMATE))
    def test_legitimate_names_pass(self, label):
        name = REVIEW_LEGITIMATE[label]
        assert clean_line(name) == " ".join(unicodedata.normalize("NFC", name).split())

    @pytest.mark.parametrize("label", sorted(REVIEW_BLANKS))
    def test_blanks_are_refused_alone_and_after_a_letter(self, label):
        for value in (REVIEW_BLANKS[label], "Rahul" + REVIEW_BLANKS[label]):
            with pytest.raises(TextRejected):
                clean_line(value)

    @pytest.mark.parametrize("label", sorted(REVIEW_DODGES))
    def test_selectors_and_blanks_that_dodge_an_exact_match_are_refused(self, label):
        with pytest.raises(TextRejected):
            clean_line(REVIEW_DODGES[label])

    @pytest.mark.parametrize(
        "name",
        [
            "1" + chr(0xFE0F),  # a keycap base without its keycap
            "#" + chr(0xFE0E),
            chr(0x5C71) + chr(0xFE03),  # past the CJK compatibility variants
            chr(0x5C71) + chr(0xE0140),  # past the ideographic selectors in use
            "Lab" + chr(0x2229) + chr(0xFE01),  # a math symbol's variant is FE00 only
        ],
    )
    def test_more_selectors_that_select_nothing(self, name):
        with pytest.raises(TextRejected):
            clean_line(name)


# Review round 3 (PARSERS3-4): selectors after a base that has no sequence for them, blanks the
# lists missed, unassigned code points.
VS15, VS16 = chr(0xFE0E), chr(0xFE0F)
ROUND3_REFUSED = {
    "arrow-vs16": "Rahul " + chr(0x2192) + VS16,  # RIGHTWARDS ARROW: no sequence
    "sum-vs16": "Lab " + chr(0x2211) + VS16,
    "infinity-vs15": "Lab " + chr(0x221E) + VS15,
    "black-star-vs16": "Star " + chr(0x2605) + VS16,  # U+2B50 has one, U+2605 not
    "check-mark-vs16": "Check " + chr(0x2713) + VS16,  # U+2714 has one, U+2713 not
    "emoji-default-face-vs16": "Emoji " + chr(0x1F600) + VS16,
    "playing-card-vs16": "Card " + chr(0x1F0A1) + VS16,
    "cjk-one-vs1": chr(0x4E00) + chr(0xFE00),  # no compatibility ideograph for it
    "cjk-field-vs3": chr(0x5C71) + chr(0x7530) + chr(0xFE02),
    "cjk-second-variant-missing": chr(0x8C48) + chr(0xFE01),  # U+8C48 has VS1 only
    "half-fill-space": "Rahul" + chr(0x303F),
    "tifinagh-joiner-after-latin": "Rahul" + chr(0x2D7F) + "Sharma",
    "tifinagh-joiner-at-the-end": chr(0x2D4E) + chr(0x2D7F),
    "unassigned-greek": "Rahul" + chr(0x0378),
    "unassigned-plane-3": "Rahul" + chr(0x3134F),
}
ROUND3_LEGITIMATE = {
    "red-heart": chr(0x2764) + VS16,
    "left-right-arrow": chr(0x2194) + VS16,
    "black-rightwards-arrow": chr(0x27A1) + VS16,
    "star": chr(0x2B50) + VS16,
    "heavy-check-mark": chr(0x2714) + VS16,
    "keycap-one": "1" + VS16 + chr(0x20E3),
    "flag-india": chr(0x1F1EE) + chr(0x1F1F3),
    "rainbow-flag": chr(0x1F3F3) + VS16 + chr(0x200D) + chr(0x1F308),
    "copyright": "Arkray" + chr(0x00A9) + VS16,
    "trade-mark": "Arkray" + chr(0x2122) + VS16,
    "thumbs-up-vs16": "Done " + chr(0x1F44D) + VS16,  # emoji-default, but listed
    "watch-vs15": chr(0x231A) + VS15,
    "cjk-compatibility-variant": chr(0x8C48) + chr(0xFE00),
    "cjk-second-variant": chr(0x6F22) + chr(0xFE01),  # U+FA9A
    "japanese-ivs": chr(0x845B) + chr(0xE0100),
    "mongolian-fvs1": chr(0x1820) + chr(0x180B),
    "tifinagh-joined": chr(0x2D4E) + chr(0x2D7F) + chr(0x2D4F),
    "newer-emoji": "Tired " + chr(0x1FAE9),  # Unicode 16, unknown to this Python: kept
}


class TestReviewRound3:
    @pytest.mark.parametrize("label", sorted(ROUND3_REFUSED))
    def test_refused(self, label):
        with pytest.raises(TextRejected):
            clean_line(ROUND3_REFUSED[label])

    @pytest.mark.parametrize("label", sorted(ROUND3_LEGITIMATE))
    def test_legitimate(self, label):
        name = ROUND3_LEGITIMATE[label]
        assert clean_line(name) == unicodedata.normalize("NFC", name)


class TestReviewRound4:
    """PARSERS4-2: cleaning stays linear in the value's length whatever it holds: the Tifinagh
    joiner's check copied the rest of the value for every joiner, so 170,000 letter + joiner
    pairs (0.97 MiB, within the proxy's 1 MiB JSON body) cost 1.5-1.6 s against 0.1 s for
    plain letters, before any length check. The control is plain letters of the same length,
    timed in the same process (coverage tracing, a slower or loaded CPU slow both alike), so
    the test needs no absolute bound; at 250,000 pairs the copy costs 3 s and more, the pairs
    1.1-1.6 times the plain letters without it."""

    LETTER, JOINER = chr(0x2D30), chr(0x2D7F)  # TIFINAGH LETTER YA, CONSONANT JOINER

    @staticmethod
    def cpu(clean, value: str) -> float:
        """The fastest of two runs: a moment's load on the machine doesn't count."""
        spent = []
        for _ in range(2):
            started = time.process_time()
            clean(value)
            spent.append(time.process_time() - started)
        return min(spent)

    @pytest.mark.parametrize("clean", [clean_line, clean_multiline, without_refused])
    def test_joiner_pairs_cost_what_plain_letters_cost(self, clean):
        pairs = (self.LETTER + self.JOINER) * 250_000 + self.LETTER
        plain = self.LETTER * len(pairs)
        control = self.cpu(clean, plain)
        spent = self.cpu(clean, pairs)
        assert spent < 3 * control + 0.05, (spent, control)
