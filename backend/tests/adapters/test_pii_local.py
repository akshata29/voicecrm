"""LocalPiiDetector: identifier PII (cards, bank, government ids, credentials, contact) in English and Spanish."""
from __future__ import annotations

import re
from itertools import pairwise

import pytest

from voicecrm.adapters.pii.local import LocalPiiDetector, aba_valid, iban_valid, luhn_valid
from voicecrm.ports.pii import PII_CATEGORIES, PiiPort

DETECTOR = LocalPiiDetector()


def spans(text: str, language: str = "en"):
    return DETECTOR.detect(text, language=language)


def found(text: str, category: str | None = None) -> list[str]:
    """Texts of the spans (optionally of one category)."""
    return [s.text for s in spans(text) if category is None or s.category == category]


def categories(text: str) -> set[str]:
    return {s.category for s in spans(text)}


# ---- contract ---------------------------------------------------------------------------------------------------

def test_implements_the_port_and_returns_valid_spans():
    assert isinstance(DETECTOR, PiiPort) and DETECTOR.name == "local"
    text = "My card number is 4111 1111 1111 1111, email jane.doe@example.com."
    for s in spans(text):
        assert s.category in PII_CATEGORIES and s.source == "local" and 0 < s.confidence <= 1
        assert text[s.start:s.end] == s.text


async def test_detect_many_is_aligned_with_inputs_and_offsets_are_per_text():
    texts = ["Nothing sensitive here.", "", "   ", "Write to jane.doe@example.com today.",
             "My SSN is 123-45-6789."]
    out = await DETECTOR.detect_many(texts, language="en")
    assert len(out) == len(texts)
    assert out[0] == out[1] == out[2] == []
    (email,) = out[3]
    assert (email.category, email.text) == ("personal_contact", "jane.doe@example.com")
    assert texts[3][email.start:email.end] == email.text and email.start == 9
    (ssn,) = out[4]
    assert (ssn.category, ssn.text, ssn.start) == ("government_id", "123-45-6789", 10)


async def test_language_codes_are_accepted_and_both_lexicons_always_apply():
    text = "my card is cuatro uno uno uno uno uno uno uno uno uno uno uno uno uno uno uno"
    for language in ("en", "es", "es-US", "en-US", "fr"):
        (out,) = await DETECTOR.detect_many([text], language=language)
        assert [s.category for s in out] == ["payment_card"]


def test_checksum_helpers():
    assert luhn_valid("4111111111111111") and not luhn_valid("4111111111111112") and not luhn_valid("41a1")
    assert aba_valid("111000025") and not aba_valid("111000026") and not aba_valid("12345")
    assert iban_valid("GB82 WEST 1234 5698 7654 32") and iban_valid("DE89370400440532013000")
    assert not iban_valid("GB82 WEST 1234 5698 7654 33") and not iban_valid("XX00")


# ---- payment cards ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,number", [
    ("My card number is 4111 1111 1111 1111 please.", "4111 1111 1111 1111"),
    ("card 4111-1111-1111-1111", "4111-1111-1111-1111"),
    ("4111111111111111", "4111111111111111"),
    ("it is 5555 5555 5555 4444.", "5555 5555 5555 4444"),
    ("Amex 3782 822463 10005 thanks", "3782 822463 10005"),
    ("Discover 6011 1111 1111 1117", "6011 1111 1111 1117"),
    ("tarjeta 4111 1111 1111 1111 gracias", "4111 1111 1111 1111"),
    ("4111, 1111, 1111, 1111", "4111, 1111, 1111, 1111"),
])
def test_typed_card_numbers(text, number):
    assert found(text, "payment_card") == [number]


@pytest.mark.parametrize("text,number", [
    ("my card is four one one one one one one one one one one one one one one one",
     "four one one one one one one one one one one one one one one one"),
    ("four one one one, one one one one, one one one one, one one one one",
     "four one one one, one one one one, one one one one, one one one one"),
    ("mi tarjeta es cuatro uno uno uno uno uno uno uno uno uno uno uno uno uno uno uno",
     "cuatro uno uno uno uno uno uno uno uno uno uno uno uno uno uno uno"),
    ("4 1 1 1 one one one one 1111 1111", "4 1 1 1 one one one one 1111 1111"),                       # typed + spoken mix
    ("four double one one one one one one one one one one one one one one",
     "four double one one one one one one one one one one one one one one"),
])
def test_spoken_card_numbers(text, number):
    assert found(text, "payment_card") == [number]


def test_luhn_failures_are_flagged_only_when_card_shaped_or_cued():
    assert found("my number is 4111 1111 1111 1112 sorry", "payment_card") == ["4111 1111 1111 1112"]    # Visa-shaped
    low = spans("my credit card number is 8111 1111 1111 1113")                  # Luhn fails, no issuer, but cued
    assert [(s.category, s.confidence) for s in low] == [("payment_card", 0.6)]
    assert spans("the order id is 1234 5678 9012 3456") == []                    # no issuer, no cue, no Luhn
    assert spans("tracking 8111 1111 1111 1113") == []


def test_card_inside_a_longer_run_is_isolated_and_security_data_is_flagged():
    text = "4111 1111 1111 1111 12 26"
    assert found(text) == ["4111 1111 1111 1111"]
    text = "Card 5555 5555 5555 4444, cvv 123, expires 12/26."
    assert found(text) == ["5555 5555 5555 4444", "123", "12/26"]
    assert found("security code is one two three") == ["one two three"]
    assert spans("she expires 12/26 on the lease") == []                       # no card cue: not card data


async def test_card_number_split_across_adjacent_segments_is_linked():
    texts = ["Sure, my card number is 4111 1111", "mm-hmm", "1111 1111, got it?"]
    out = await DETECTOR.detect_many(texts, language="en")
    assert [s.text for s in out[0]] == []                                        # not adjacent to the second half
    texts = ["Sure, my card number is 4111 1111", "1111 1111, got it?"]
    out = await DETECTOR.detect_many(texts, language="en")
    assert [(s.category, s.text) for s in out[0]] == [("payment_card", "4111 1111")]
    assert [(s.category, s.text) for s in out[1]] == [("payment_card", "1111 1111")]
    assert out[1][0].start == 0
    texts = ["I own 1234 5678", "9012 3456 shares"]                               # joins to a non-card, non-Luhn number
    assert all(not part for part in await DETECTOR.detect_many(texts, language="en"))


# ---- bank accounts ----------------------------------------------------------------------------------------------

def test_iban_requires_a_valid_checksum():
    assert found("IBAN GB82 WEST 1234 5698 7654 32 please", "bank_account") == ["GB82 WEST 1234 5698 7654 32"]
    assert found("DE89370400440532013000", "bank_account") == ["DE89370400440532013000"]
    assert found("ES91 2100 0418 4502 0005 1332", "bank_account") == ["ES91 2100 0418 4502 0005 1332"]
    assert spans("GB82 WEST 1234 5698 7654 33") == []


def test_routing_and_account_numbers_need_a_cue():
    text = "routing number 111000025 and account number 12345678"
    assert [(s.category, s.text, s.confidence) for s in spans(text)] == [
        ("bank_account", "111000025", 0.9), ("bank_account", "12345678", 0.75)]
    assert [(s.text, s.confidence) for s in spans("the routing number is 111000026")] == [("111000026", 0.7)]
    assert found("sort code 20 00 00 and account 12345678", "bank_account") == ["20 00 00", "12345678"]
    assert found("mi número de cuenta es 00123456789012", "bank_account") == ["00123456789012"]
    assert found("account number is one two three four five six seven eight", "bank_account") != []
    assert spans("we sent 12345678 units") == []
    assert spans("my account manager is Bob, reach him on extension 4412") == []
    assert spans("the account balance is 123456 dollars") == []
    assert spans("our account is 1,250,000 dollars in total") == []              # a comma-grouped amount
    assert found("my account number is 1250000", "bank_account") == ["1250000"]


# ---- government ids ---------------------------------------------------------------------------------------------

def test_us_ssn_formats_and_validity():
    assert found("My SSN is 123-45-6789 ok", "government_id") == ["123-45-6789"]
    assert [(s.text, s.confidence) for s in spans("social 123 45 6789")] == [("123 45 6789", 0.9)]    # cue raises it
    assert found("123 45 6789 was on the form", "government_id") == ["123 45 6789"]
    assert found("my social is 123456789", "government_id") == ["123456789"]
    assert found("social security number one two three four five six seven eight nine", "government_id") != []
    for invalid in ("000-12-3456", "666-12-3456", "912-12-3456", "123-00-4567", "123-45-0000"):
        assert spans(f"number {invalid}") == [], invalid


def test_uk_national_insurance_number():
    assert found("NI number AB 12 34 56 C", "government_id") == ["AB 12 34 56 C"]
    assert found("AB123456C", "government_id") == ["AB123456C"]
    for invalid in ("BG 12 34 56 C", "QQ 12 34 56 C", "AB 12 34 56 E"):
        assert spans(invalid) == [], invalid


def test_spanish_dni_and_nie_use_the_check_letter():
    assert found("Mi DNI es 12345678Z", "government_id") == ["12345678Z"]
    assert found("NIE X1234567L", "government_id") == ["X1234567L"]
    assert spans("DNI 12345678A") == []                                         # wrong check letter
    assert spans("NIE X1234567A") == []


def test_passport_and_drivers_licence_need_a_cue_and_a_digit():
    assert found("my passport number is X1234567", "government_id") == ["X1234567"]
    assert found("pasaporte PAB123456", "government_id") == ["PAB123456"]
    assert found("driver's license number D1234-5678-9012", "government_id") == ["D1234-5678-9012"]
    assert spans("passport office opens at nine") == []


# ---- credentials ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,secret", [
    ("my password is Tr0ub4dor&3 and then we move on", "Tr0ub4dor&3"),
    ("The password is hunter2.", "hunter2"),
    ("mi contraseña es azul caballo bateria grapa, gracias", "azul caballo bateria grapa"),
    ("la clave es x7y8z9", "x7y8z9"),
    ("PIN: 1234", "1234"),
    ("my pin is four seven two nine", "four seven two nine"),
    ("passcode is 482913 thanks", "482913"),
    ("password: \"s3cret!\"", "s3cret!"),
])
def test_credentials(text, secret):
    assert found(text, "credentials") == [secret]


def test_talking_about_passwords_is_not_a_credential():
    for text in ("the password is required for login", "your password is expired, please reset it",
                 "our password policy is strict", "Use a PIN for the terminal when travelling"):
        assert "credentials" not in categories(text), text


# ---- personal contact -------------------------------------------------------------------------------------------

def test_email_typed_and_spoken():
    assert found("email jane.doe@example.com now", "personal_contact") == ["jane.doe@example.com"]
    assert found("john dot smith at gmail dot com is my email", "personal_contact") == ["john dot smith at gmail dot com"]
    assert found("mi correo es maria arroba empresa punto es", "personal_contact") == ["maria arroba empresa punto es"]
    assert found("my email is john at gmail dot com thanks", "personal_contact") == ["john at gmail dot com"]
    assert spans("I was looking at amazon dot com for that") == []


@pytest.mark.parametrize("text,number", [
    ("call me at (206) 555-0100 tomorrow", "(206) 555-0100"),
    ("My cell is 206.555.0100", "206.555.0100"),
    ("reach me on 206-555-0100", "206-555-0100"),
    ("or +1 206 555 0100", "+1 206 555 0100"),
    ("on +44 20 7946 0958 please", "+44 20 7946 0958"),
    ("it is 020 7946 0958", "020 7946 0958"),
    ("llámame al 612 34 56 78", "612 34 56 78"),
    ("mi móvil es 55 1234 5678", "55 1234 5678"),
])
def test_typed_phone_numbers(text, number):
    assert found(text, "personal_contact") == [number]


def test_spoken_phone_numbers():
    spoken = "five five five one two three four five six seven"
    assert found(f"my phone is {spoken}", "personal_contact") == [spoken]
    assert [s.confidence for s in spans(f"my phone is {spoken}")] == [0.85]
    assert [s.confidence for s in spans(spoken)] == [0.55]                       # shape only, no cue
    assert spans("he said 5551234567 is the order number") == []


def test_street_addresses_and_po_boxes():
    assert found("we live at 123 Main Street, Seattle, WA 98101 near the park", "personal_contact") == [
        "123 Main Street, Seattle, WA 98101"]
    assert found("send it to 42 Wallaby Way, Sydney", "personal_contact") == ["42 Wallaby Way"]
    assert found("one two three Main Street", "personal_contact") == ["one two three Main Street"]
    assert found("PO Box 4421", "personal_contact") == ["PO Box 4421"]
    assert found("vivo en Calle Mayor 12 y trabajo en Avenida de la Constitución, 24", "personal_contact") == [
        "Calle Mayor 12", "Avenida de la Constitución, 24"]
    assert spans("Dr. Lee joined at 3 PM Dr. Smith will follow") == []


# ---- precision --------------------------------------------------------------------------------------------------

BUSINESS_TEXT = [
    "Contoso Equity Fund class C has an expense ratio of 1.05 percent and a 5 percent load.",
    "We discussed 3 funds, 10 million dollars and a 12 month horizon.",
    "AUM of $1,250,000,000 and 12.5% growth over 10 years, 2026-11-10.",
    "The R6 class launched in 2019 and the A class in 1996; minimum is 1,000,000.",
    "Call at 3 15 pm tomorrow; the fund has 5 8 12 holdings.",
    "one two three four five",
    "Seahawks won 31 to 24 on 11/10/2026, great game.",
    "Quiero saber más sobre el fondo de renta variable clase C y los gastos del 1,5 por ciento.",
    "I'll send the fee schedule by Friday and follow up on the 15th.",
    "between 31 2026 2027 results",
    "my daughter is 12 and she plays on a team of 11 girls with 2 coaches",
]


@pytest.mark.parametrize("text", BUSINESS_TEXT)
def test_ordinary_business_text_has_no_spans(text):
    assert spans(text) == []


def test_health_and_family_semantics_are_not_this_detectors_job():
    assert spans("She has stage two cancer and her husband is recovering from surgery.") == []


# ---- overlap handling -------------------------------------------------------------------------------------------

def test_spans_never_overlap_and_are_sorted():
    text = ("Card 4111 1111 1111 1111 exp 12/26 cvv 123, ssn 123-45-6789, email a.b@example.com, "
            "call (206) 555-0100, password is Tr0ub4dor, 12 Oak Street, IBAN GB82 WEST 1234 5698 7654 32.")
    out = spans(text)
    assert [s.start for s in out] == sorted(s.start for s in out)
    assert all(a.end <= b.start for a, b in pairwise(out))
    assert {s.category for s in out} == {"payment_card", "government_id", "personal_contact", "credentials",
                                         "bank_account"}


def test_phone_inside_an_email_or_card_is_not_double_reported():
    out = spans("card 4111 1111 1111 1111 and 206-555-0100")
    assert [(s.category, s.text) for s in out] == [("payment_card", "4111 1111 1111 1111"),
                                                   ("personal_contact", "206-555-0100")]


def test_every_sample_returns_text_equal_to_its_slice():
    for text in BUSINESS_TEXT + ["My SSN is 123-45-6789", "mi contraseña es abc123, gracias"]:
        for s in spans(text):
            assert text[s.start:s.end] == s.text
            assert re.search(r"\S", s.text)
