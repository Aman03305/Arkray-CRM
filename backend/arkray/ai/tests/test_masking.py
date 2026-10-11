"""What user-written text may carry to the model (formatting.user_text; privacy audit P3):
phone numbers in the shapes Indian users write them, and a PAN, are replaced; the numbers a
CRM answer is made of (prices, CPTs, quantities, years, dates, times) are never touched."""

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
}


@pytest.mark.parametrize("number", PHONES.values(), ids=PHONES.keys())
@pytest.mark.parametrize(
    "template",
    ["Call her on {}", "{}", "Ring {}, then email.", "Phone: {}.", "({})"],
    ids=["prose", "alone", "comma", "full-stop", "brackets"],
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
