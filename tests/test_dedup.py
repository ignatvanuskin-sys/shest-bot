"""Deduplication level tests (strong auto-merge vs medium ask vs none)."""
from __future__ import annotations

from app.services.dedup import LeadFingerprint, classify_match


def fp(**kw) -> LeadFingerprint:
    return LeadFingerprint(**kw)


def test_strong_exact_phone():
    match = classify_match(fp(phone_digits="77001234567"), fp(phone_digits="77001234567"))
    assert match.level == "strong"
    assert "телефон" in match.reason


def test_strong_last9_phone():
    # Same last 10 digits (country code present vs absent).
    match = classify_match(fp(phone_digits="77001234567"), fp(phone_digits="7001234567"))
    assert match.level == "strong"


def test_strong_website():
    match = classify_match(fp(website="example.com"), fp(website="example.com"))
    assert match.level == "strong"
    assert "сайт" in match.reason


def test_strong_instagram():
    match = classify_match(fp(instagram="alimotors"), fp(instagram="alimotors"))
    assert match.level == "strong"
    assert "Instagram" in match.reason


def test_strong_telegram():
    match = classify_match(fp(telegram="alimotors"), fp(telegram="alimotors"))
    assert match.level == "strong"
    assert "Telegram" in match.reason


def test_medium_fuzzy_name_same_city():
    match = classify_match(
        fp(name_key="автосервис али", city="караганда"),
        fp(name_key="али автосервис", city="караганда"),
    )
    assert match.level == "medium"
    assert match.score >= 85


def test_medium_name_different_city_is_none():
    match = classify_match(
        fp(name_key="али автосервис", city="караганда"),
        fp(name_key="али автосервис", city="алматы"),
    )
    assert match.level == "none"


def test_medium_last7_phone():
    # Same last 7 digits but different full number.
    match = classify_match(fp(phone_digits="77001234567"), fp(phone_digits="77101234567"))
    assert match.level == "medium"
    assert "7 цифр" in match.reason


def test_none_different_companies():
    match = classify_match(
        fp(name_key="али мотос", city="караганда"),
        fp(name_key="стоматология люкс", city="алматы"),
    )
    assert match.level == "none"


def test_none_empty_fingerprint():
    match = classify_match(fp(), fp())
    assert match.level == "none"
