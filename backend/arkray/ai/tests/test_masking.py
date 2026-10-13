"""What user-written text may carry to the model (formatting.user_text; privacy audit P3):
phone numbers in the shapes Indian users write them, and a PAN, are replaced; the numbers a
CRM answer is made of (prices, CPTs, quantities, years, dates, times) are never touched.
Adversarial review M5 added the shapes that went out whole (a dot, slash or underscore
between the groups, a number glued to a word, a country code without its plus) and the
kept values the wider patterns must still leave alone; test_masking_adversarial.py has the
encodings, passcodes, tokens and identity numbers."""

from __future__ import annotations

import time

import pytest

from arkray.ai.formatting import user_text, user_text_slice

PHONES = {
    "landline, STD 2+8 spaced": "020 2612 0000",
    "landline, STD hyphenated": "020-26120000",
    "landline, run together": "02026120000",
    "landline, STD in brackets": "(020) 2612 0000",
    "landline, STD 3+7": "0124 4567890",
    "landline, STD 3+3+4": "(0124) 456-7890",
    "landline, STD 4+6": "01652 234567",
    "mobile, trunk 0": "09811155555",
    "mobile, trunk 0 and 5+5": "098111 55555",
    "mobile, trunk 0 spaced": "0 98111 55555",
    "mobile, 5+5": "98111 55555",
    "mobile, hyphen": "98111-55555",
    "mobile, run together": "9811155555",
    "international": "+91 98111 55555",
    "international, brackets": "+91 (20) 2612 0000",
    # Adversarial review M5: the shapes that went out whole (a dot, slash or underscore
    # between the groups, a country code without its plus, a trunk 0 in brackets).
    "mobile, dots": "98111.55555",
    "mobile, slash": "98111/55555",
    "mobile, underscore": "98111_55555",
    "mobile, spaced hyphen": "98111 - 55555",
    "mobile, two spaces": "98111  55555",
    "mobile, line break": "98111\n55555",
    "mobile, 4+3+3 hyphens": "9811-155-555",
    "mobile, 3+3+4 hyphens": "981-115-5555",
    "mobile, 3+3+4 dots": "981.115.5555",
    "mobile, 91 spaced": "91 98111 55555",
    "mobile, 91 run together": "919811155555",
    "mobile, 091 hyphenated": "091-98111-55555",
    "mobile, 0091 spaced": "0091 98111 55555",
    "international, dots": "+91.98111.55555",
    "international, hyphens": "+91-98111-55555",
    "international, trunk 0 in brackets": "+91 (0) 98111 55555",
    "international, run together": "+919811155555",
    "international, space after the plus": "+ 91 98111 55555",
    "international, landline": "+91 11 2345 6789",
    "landline, STD 2+4+4": "011-2345 6789",
    "landline, STD in brackets 2+4-4": "(022) 2345-6789",
    "landline, STD 3+3+4 spaced": "0124 234 5678",
    "landline, dots": "011.2345.6789",
    "landline, slash": "011/23456789",
}


@pytest.mark.parametrize("number", PHONES.values(), ids=PHONES.keys())
@pytest.mark.parametrize(
    "template",
    [
        "Call her on {}",
        "{}",
        "Ring {}, then email.",
        "Phone: {}.",
        "({})",
        # Glued to a word or to punctuation (M5: "Mob.9811155555" went out whole).
        "Mob.{}",
        "Ph:{}",
        "Tel-{}",
        "Mob{}",
        "Ph.{}.",
        "call:{};",
        "[{}]",
        '"{}"',
        "WhatsApp-{}!",
    ],
    ids=[
        "prose",
        "alone",
        "comma",
        "full-stop",
        "brackets",
        "mob-dot",
        "ph-colon",
        "tel-hyphen",
        "glued",
        "ph-dot-dot",
        "call-colon-semicolon",
        "square-brackets",
        "quotes",
        "whatsapp-hyphen",
    ],
)
def test_phone_numbers_are_replaced(number, template):
    masked = user_text(template.format(number))
    assert "[phone]" in masked, masked
    assert sum(ch.isdigit() for ch in masked) == 0, masked


@pytest.mark.parametrize(
    "text",
    ["Her PAN is ABCDE1234F", "PAN: AAAPL1234C.", "pan ABCPE1234F,"],
    ids=["sample", "individual", "lower-case-word"],
)
def test_a_pan_is_replaced(text):
    masked = user_text(text)
    assert "[id]" in masked, masked
    assert sum(ch.isdigit() for ch in masked) == 0, masked


KEPT = {
    "cpt": "Agreed CPT Rs 4 per test, expected Rs 18.50 per test",
    "amounts": "Value ₹12,50,000; PO 7,77,77,777; Rs. 5,00,000; INR 950000; 2 crore",
    "big amounts": "1250000 12500000 125000000 and 1500000000",
    "quantities": "300 tests a day, qty 05, 0.5 ml, 1,500 units, 12.5 units",
    "dates": "2026-10-04, 11/10/2026, 11-10-2026, FY 2025-26, 3 Oct 2026",
    "times": "10:30, 09:45 AM, 18:00-19:30",
    "years and lists": "Years 2026 2027 2028; volumes 1000 2000 3000 4000; 700 800 9000",
    "pin and refs": "PIN 411001; INV-2026-0042; ticket 0042; 3.5/5",
    "instruments": "Adams 8380 V-lite, Adams 8180 V, Adams 8180 T, PCBA with Printer",
    "gstin": "GSTIN 27ABCDE1234F1Z5 (an organisation's registration)",
    "lower-case code": "lot abcde1234f",
    "local number without its code": "extension 2612 0000",
    # Adversarial review M5: the shapes the wider phone patterns must still leave alone
    # (each decision in docs/rag-architecture.md#privacy-and-data-minimisation).
    "rupee grouping": "₹ 98,11,155 and 1,00,000 and Rs 1,00,00,000",
    "an amount with paise": "INR 9811155555.00 tender",
    "a currency word before ten digits": "Rs.9811155555 and Rs 9811155555 and ₹9811155555",
    "a decimal fraction": "ratio 12.9811155555",
    "pH readings": "pH 7.4 6.8 7.0 7.2, pH 6.5",
    "postal PIN codes": "PIN 411001, PIN code 560001, pincode: 110001, Pin-411001",
    "date ranges": "7/10/2026-8/10/2026, 06/10/2026 - 09/10/2026, 2026.10.12, 12.10.26",
    "times and durations": "10:30-11:00, 9.30 am, 18:00 hrs, 1.5 h",
    "percentages": "HbA1c 6.5%, growth +12.5%, +5 units, +12.50 ml",
    "instrument and serial numbers": "AX-4030, PU-4010, model 8180-T, S/N 6100-2345-678",
    "invoice and order ids": "Invoice INV/2026/0042, order 2026-0915, PO 4500012345",
    "lot and batch": "Lot 2026A1234, batch B-0915-77, SN98111",
    "fiscal years": "FY 2023 2024 2025, years 2023-2024-2025",
    "a twelve-digit order id failing the Aadhaar checksum": "order 2345 6789 0123",
    "a card-like number failing the Luhn check": "ref 4111 1111 1111 1112",
    "secret-sounding words": (
        "Password reset done; the password policy is strict; token amount 25000; "
        "token advance paid; secret sauce; OTP not received; access code pending; "
        "Passcode will be shared"
    ),
    "a file name": "report_Adams_8380_V-lite_comparison_2026_final.pdf",
    "an ordinary long word": "Pneumonoultramicroscopicsilicovolcanoconiosis tests",
}


@pytest.mark.parametrize("text", KEPT.values(), ids=KEPT.keys())
def test_ordinary_numbers_are_kept(text):
    assert user_text(text) == text


def test_a_number_cut_by_a_chunk_boundary_is_replaced_whole():
    text = "x" * 990 + " ring 020 2612 0000 tomorrow"
    assert user_text_slice(text, 1000, len(text)).startswith("[phone]")
    assert user_text_slice(text, 0, 1005).endswith("[phone]")


@pytest.mark.parametrize(
    "text",
    ["0-" * 5000, "0 " * 5000, "(0" * 5000, "0" * 10000, "ABCDE" * 4000, "98111 " * 3000],
    ids=["zero-dash", "zero-space", "bracket-zero", "zeros", "capitals", "mobile-halves"],
)
def test_masking_stays_linear(text):
    started = time.perf_counter()
    user_text(text)
    user_text_slice(text, 0, len(text) // 2)
    assert time.perf_counter() - started < 0.5
