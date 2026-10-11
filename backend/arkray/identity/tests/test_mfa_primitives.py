"""identity.mfa: TOTP against the RFCs' test vectors, the secret's encryption and key
rotation, recovery codes and the QR matrix (ADR-0033)."""

from __future__ import annotations

import base64
import re

import pytest
from cryptography.fernet import InvalidToken

from arkray.identity import mfa

RFC_SEED_SHA1 = b"12345678901234567890"
RFC_SEED_SHA256 = b"12345678901234567890123456789012"
RFC_SEED_SHA512 = b"1234567890" * 6 + b"1234"

# RFC 4226, Appendix D: HOTP values for counters 0..9 (HMAC-SHA1, 6 digits).
RFC_4226 = [
    "755224",
    "287082",
    "359152",
    "969429",
    "338314",
    "254676",
    "287922",
    "162583",
    "399871",
    "520489",
]

# RFC 6238, Appendix B: (time, sha1, sha256, sha512), 8 digits, 30-second steps.
RFC_6238 = [
    (59, "94287082", "46119246", "90693936"),
    (1111111109, "07081804", "68084774", "25091201"),
    (1111111111, "14050471", "67062674", "99943326"),
    (1234567890, "89005924", "91819424", "93441116"),
    (2000000000, "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826"),
]


def b32(raw: bytes) -> str:
    return base64.b32encode(raw).decode().rstrip("=")


@pytest.mark.parametrize(("counter", "expected"), list(enumerate(RFC_4226)))
def test_hotp_matches_rfc_4226(counter, expected):
    assert mfa.hotp(RFC_SEED_SHA1, counter) == expected


@pytest.mark.parametrize(("at", "sha1", "sha256", "sha512"), RFC_6238)
def test_totp_matches_rfc_6238_with_eight_digits(at, sha1, sha256, sha512):
    assert mfa.totp(RFC_SEED_SHA1, at, digits=8) == sha1
    assert mfa.totp(RFC_SEED_SHA256, at, digits=8, digest="sha256") == sha256
    assert mfa.totp(RFC_SEED_SHA512, at, digits=8, digest="sha512") == sha512


@pytest.mark.parametrize(("at", "sha1", "sha256", "sha512"), RFC_6238)
def test_six_digit_codes_are_the_same_truncation(at, sha1, sha256, sha512):
    """The 6-digit code is the same dynamic truncation modulo 10**6: the vectors' last six
    digits. This is what Arkray (and every authenticator app) uses."""
    assert mfa.totp(RFC_SEED_SHA1, at) == sha1[-6:]
    assert mfa.totp(RFC_SEED_SHA256, at, digest="sha256") == sha256[-6:]
    assert mfa.totp(RFC_SEED_SHA512, at, digest="sha512") == sha512[-6:]


@pytest.mark.parametrize(("at", "sha1", "_256", "_512"), RFC_6238)
def test_matching_step_accepts_the_rfc_code_from_a_base32_secret(at, sha1, _256, _512):
    secret = b32(RFC_SEED_SHA1)
    assert mfa.matching_step(secret, sha1[-6:], at=at) == at // 30


class TestMatchingStep:
    SECRET = b32(RFC_SEED_SHA1)
    AT = 1_111_111_111.0

    def code(self, steps: int = 0) -> str:
        return mfa.totp(RFC_SEED_SHA1, self.AT + 30 * steps)

    def test_one_step_of_drift_either_way_is_accepted(self):
        current = int(self.AT // 30)
        for drift in (-1, 0, 1):
            assert mfa.matching_step(self.SECRET, self.code(drift), at=self.AT) == current + drift

    def test_two_steps_away_is_refused(self):
        for drift in (-2, 2):
            assert mfa.matching_step(self.SECRET, self.code(drift), at=self.AT) is None

    def test_the_last_accepted_step_and_earlier_ones_are_refused(self):
        current = int(self.AT // 30)
        assert mfa.matching_step(self.SECRET, self.code(), at=self.AT, after_step=current) is None
        earlier = self.code(-1)
        assert mfa.matching_step(self.SECRET, earlier, at=self.AT, after_step=current - 1) is None
        assert mfa.matching_step(self.SECRET, self.code(1), at=self.AT, after_step=current) == (
            current + 1
        )

    @pytest.mark.parametrize("typed", ["", "12345", "1234567", "abcdef", "١٢٣٤٥٦", "12 34 5x"])
    def test_anything_but_six_digits_is_refused(self, typed):
        assert mfa.matching_step(self.SECRET, typed, at=self.AT) is None

    def test_spaces_and_dashes_are_ignored(self):
        code = self.code()
        spaced = f" {code[:3]} {code[3:]} "
        dashed = f"{code[:3]}-{code[3:]}"
        assert mfa.matching_step(self.SECRET, spaced, at=self.AT) is not None
        assert mfa.matching_step(self.SECRET, dashed, at=self.AT) is not None


def test_secrets_are_160_random_bits_in_base32():
    secrets = {mfa.generate_secret() for _ in range(50)}
    assert len(secrets) == 50
    for secret in secrets:
        assert re.fullmatch(r"[A-Z2-7]{32}", secret)
        assert len(mfa.secret_bytes(secret)) == 20


def test_the_otpauth_uri_is_what_authenticator_apps_scan():
    uri = mfa.otpauth_uri("JBSWY3DPEHPK3PXP", "anita@example.test", "Arkray CRM")
    assert uri.startswith("otpauth://totp/Arkray%20CRM:anita@example.test?")
    for part in ("secret=JBSWY3DPEHPK3PXP", "issuer=Arkray%20CRM", "digits=6", "period=30"):
        assert part in uri
    assert "algorithm=SHA1" in uri


def test_the_qr_code_is_a_square_module_matrix():
    qr = mfa.qr_matrix(mfa.otpauth_uri(mfa.generate_secret(), "a@example.test"))
    assert qr["size"] == len(qr["rows"]) >= 21
    assert (qr["size"] - 17) % 4 == 0  # a QR version's size: 17 + 4v
    for row in qr["rows"]:
        assert len(row) == qr["size"]
        assert set(row) <= {"0", "1"}
    # The three finder patterns: a 7-module dark border top left.
    assert qr["rows"][0][:7] == "1111111"
    assert qr["rows"][0][-7:] == "1111111"
    assert qr["rows"][-1][:7] == "1111111"


class TestEncryption:
    def test_round_trip_and_nothing_readable_at_rest(self):
        secret = mfa.generate_secret()
        token = mfa.encrypt(secret)
        assert token.startswith("gAAAAA")
        assert secret not in token
        assert secret.lower() not in token.lower()
        assert mfa.decrypt(token) == secret

    def test_a_rotated_key_keeps_working_as_a_fallback_and_rotate_moves_it(self, settings):
        settings.SECRET_KEY = "old-" + "a1b2c3d4" * 8
        settings.SECRET_KEY_FALLBACKS = []
        token = mfa.encrypt("JBSWY3DPEHPK3PXP")

        settings.SECRET_KEY = "new-" + "e5f6g7h8" * 8
        with pytest.raises(InvalidToken):
            mfa.decrypt(token)  # the old key is gone: unreadable
        settings.SECRET_KEY_FALLBACKS = ["old-" + "a1b2c3d4" * 8]
        assert mfa.decrypt(token) == "JBSWY3DPEHPK3PXP"
        assert not mfa.under_current_key(token)
        rotated = mfa.rotate(token)
        assert mfa.under_current_key(rotated)

        settings.SECRET_KEY_FALLBACKS = []
        assert mfa.decrypt(rotated) == "JBSWY3DPEHPK3PXP"

    def test_the_key_is_dedicated_not_the_cursor_key(self):
        """A different context than core.keyset's cursors: one can't decrypt the other."""
        from arkray.core import keyset

        token = mfa.encrypt("JBSWY3DPEHPK3PXP")
        with pytest.raises(InvalidToken):
            keyset._cipher(keyset._keys()).decrypt(token.encode())


class TestRecoveryCodes:
    def test_ten_distinct_codes_of_80_bits_in_groups_of_four(self):
        codes = mfa.generate_recovery_codes()
        assert len(codes) == len(set(codes)) == 10
        for code in codes:
            assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{4}(-[0-9A-HJKMNP-TV-Z]{4}){3}", code)
        # 16 characters of a 32-letter alphabet: 80 bits.
        assert len(mfa.RECOVERY_ALPHABET) == 32
        assert len(set(mfa.RECOVERY_ALPHABET)) == 32
        assert not set("ILOU") & set(mfa.RECOVERY_ALPHABET)

    def test_typing_is_forgiving(self):
        code = "ABCD-EFGH-JKMN-PQRS"
        expected = mfa.normalise_recovery_code(code)
        assert expected == "ABCDEFGHJKMNPQRS"
        for typed in ("abcd-efgh-jkmn-pqrs", " abcd efgh jkmn pqrs ", "ABCDEFGHJKMNPQRS"):
            assert mfa.normalise_recovery_code(typed) == expected
        # The look-alikes read as what they look like.
        assert mfa.normalise_recovery_code("0000-1111-OOOO-IIII") == "0000111100001111"
        assert mfa.normalise_recovery_code("llll-0000-0000-0000") == "1111000000000000"

    @pytest.mark.parametrize(
        "typed",
        [
            "",
            "ABCD-EFGH-JKMN",
            "ABCD-EFGH-JKMN-PQRS-T",
            "ABCD-EFGH-JKMN-PQR!",
            "UUUU-UUUU-UUUU-UUUU",
        ],
    )
    def test_what_cant_be_a_code_is_refused(self, typed):
        assert mfa.normalise_recovery_code(typed) is None

    def test_only_sha256_hashes_are_compared(self):
        codes = mfa.generate_recovery_codes()
        hashes = mfa.hash_recovery_codes(codes)
        assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes)
        assert not any(code.replace("-", "") in h for code in codes for h in hashes)
        assert mfa.matching_hash(codes[3].lower(), hashes) == hashes[3]
        assert mfa.matching_hash("0000-0000-0000-0000", hashes) is None
        assert mfa.matching_hash("not a code", hashes) is None
