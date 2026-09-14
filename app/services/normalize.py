"""Normalization helpers used both before storing and before dedup comparison."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import phonenumbers

DEFAULT_REGION = "KZ"

# Query parameters removed from stored URLs (tracking noise).
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "yclid", "igshid", "ref", "ref_src",
    "source", "spm", "scm", "mc_cid", "mc_eid",
}

# Legal suffixes stripped from company names for fuzzy comparison.
LEGAL_SUFFIXES = {
    "тоо", "тoo", "llc", "llp", "ltd", "inc", "corp", "co", "company",
    "limited", "ип", "ао", "жшс", "gmbh", "kg", "ой", "oü", "s.r.o", "sp",
    "zoo", "груп", "group",
}


def digits_only(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\D", "", str(value))


def normalize_phone(raw: str | None) -> str | None:
    """Normalize a phone number to E.164 (default region KZ). Returns None if invalid."""
    if not raw:
        return None
    digits = digits_only(raw)
    if not digits:
        return None
    # Handle RU/KZ local forms: leading "8" as country prefix, or 10-digit national number.
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits.startswith("7"):
        digits = "7" + digits
    try:
        parsed = phonenumbers.parse("+" + digits, DEFAULT_REGION)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def phone_digits(raw: str | None) -> str | None:
    """All digits of a phone (for suffix comparison)."""
    if not raw:
        return None
    digits = digits_only(raw)
    return digits or None


def phone_last_digits(raw: str | None, n: int) -> str | None:
    """Last ``n`` digits of a phone number, or None if absent."""
    digits = phone_digits(raw)
    if not digits:
        return None
    return digits[-n:] if len(digits) >= n else digits


def strip_tracking(url: str) -> str:
    """Remove tracking query params, keeping the rest of the URL intact."""
    parsed = urlparse(url if "://" in url else "https://" + url)
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(query), ""))


def normalize_website(raw: str | None) -> str | None:
    """Normalized website for storage: lowercase scheme, no www, no tracking params."""
    if not raw:
        return None
    value = str(raw).strip()
    if not value:
        return None
    value = strip_tracking(value)
    parsed = urlparse(value if "://" in value else "https://" + value)
    netloc = re.sub(r"^www\.", "", parsed.netloc, flags=re.IGNORECASE).lower()
    path = parsed.path.rstrip("/")
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
         if k.lower() not in TRACKING_PARAMS]
    )
    result = urlunparse((parsed.scheme.lower(), netloc, path, "", query, ""))
    return result or None


def website_key(raw: str | None) -> str | None:
    """Dedup key for a website: no protocol, no www, no query, no trailing slash."""
    if not raw:
        return None
    value = str(raw).strip()
    if not value:
        return None
    parsed = urlparse(value if "://" in value else "https://" + value)
    host = re.sub(r"^www\.", "", parsed.netloc, flags=re.IGNORECASE).lower()
    path = parsed.path.rstrip("/")
    key = (host + path).lower()
    return key or None


def normalize_social_handle(raw: str | None) -> str | None:
    """Instagram/Telegram handle: lowercase, no ``@``, no domain/path noise."""
    if not raw:
        return None
    value = str(raw).strip()
    if not value:
        return None
    # If it looks like a URL, take the first path segment as the handle.
    if "/" in value or "instagram.com" in value or "t.me" in value:
        parsed = urlparse(value if "://" in value else "https://" + value)
        path = parsed.path.strip("/")
        value = path.split("/")[0] if path else value
    value = value.lstrip("@").split("?")[0].split("#")[0].strip("/").strip()
    value = value.lower()
    return value or None


def normalize_company_name(raw: str | None) -> str:
    """Company name key: lowercase, legal suffixes removed, whitespace collapsed."""
    if not raw:
        return ""
    value = str(raw).strip().lower()
    value = value.strip("\"'«»").strip()
    value = re.sub(r"\s+", " ", value)
    tokens = value.split(" ")
    while tokens and tokens[0] in LEGAL_SUFFIXES:
        tokens.pop(0)
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    value = " ".join(tokens).strip()
    value = re.sub(r"[«»\"',.;:!?]+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_city(raw: str | None) -> str | None:
    if not raw:
        return None
    value = str(raw).strip().lower()
    value = re.sub(r"\s+", " ", value).strip(" ,.")
    return value or None
