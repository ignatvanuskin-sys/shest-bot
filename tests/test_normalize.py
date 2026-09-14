"""Normalization unit tests (dirty / boundary input formats)."""
from __future__ import annotations

from app.services.normalize import (
    normalize_city,
    normalize_company_name,
    normalize_phone,
    normalize_social_handle,
    normalize_website,
    phone_last_digits,
    website_key,
)


def test_normalize_phone_kz_e164():
    assert normalize_phone("+7 700 123 45 67") == "+77001234567"
    assert normalize_phone("8 700 123 45 67") == "+77001234567"
    assert normalize_phone("87001234567") == "+77001234567"
    assert normalize_phone("7001234567") == "+77001234567"
    assert normalize_phone("+7 (701) 765-43-21") == "+77017654321"


def test_normalize_phone_invalid():
    assert normalize_phone("garbage") is None
    assert normalize_phone("") is None
    assert normalize_phone(None) is None
    assert normalize_phone("123") is None


def test_phone_last_digits():
    assert phone_last_digits("+77001234567", 7) == "1234567"
    assert phone_last_digits("77001234567", 10) == "7001234567"
    assert phone_last_digits(None, 7) is None


def test_normalize_website_storage():
    assert normalize_website("https://www.Example.com/About?utm_source=x&id=5#sec") == (
        "https://example.com/About?id=5"
    )
    assert normalize_website("http://example.com") == "http://example.com"
    assert normalize_website(None) is None


def test_website_key_dedup():
    assert website_key("HTTPS://www.Example.com/About/") == "example.com/about"
    assert website_key("https://example.com/?utm=1&a=2") == "example.com"
    assert website_key("example.com") == "example.com"
    assert website_key(None) is None


def test_normalize_social_handle():
    assert normalize_social_handle("@AliMotors") == "alimotors"
    assert normalize_social_handle("https://instagram.com/AliMotors/") == "alimotors"
    assert normalize_social_handle("t.me/AliMotors") == "alimotors"
    assert normalize_social_handle("instagram.com/ali.motors") == "ali.motors"
    assert normalize_social_handle(None) is None


def test_normalize_company_name():
    assert normalize_company_name("ТОО «Ali Motors»") == "ali motors"
    assert normalize_company_name("ИП Иванов") == "иванов"
    assert normalize_company_name("  Ali   Motors LLC  ") == "ali motors"


def test_normalize_city():
    assert normalize_city(" Караганда ") == "караганда"
    assert normalize_city(None) is None
