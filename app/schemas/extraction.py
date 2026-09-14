"""Pydantic model describing the strict JSON contract for the LLM extraction response."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# NOTE: "manual" is produced by the manual-entry fallback (app.bot.flow
# .build_manual_result), so it must be a valid value here even though the LLM
# prompt's enum (app.services.extraction.JSON_SCHEMA) does not offer it.
SourceGuess = Literal["2gis", "instagram", "website", "google", "other", "manual", None]


class ExtractionResult(BaseModel):
    """Parsed LLM answer. Missing data must be ``None``, never invented."""

    model_config = ConfigDict(extra="ignore")

    company_name: str | None = None
    category: str | None = None
    city: str | None = None
    address: str | None = None
    phone_raw: str | None = None
    phone_e164: str | None = None
    has_whatsapp: bool | None = None
    whatsapp_number: str | None = None
    email: str | None = None
    instagram: str | None = None
    telegram: str | None = None
    website: str | None = None
    services: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    rating: float | None = None
    reviews_count: int | None = None
    contact_person: str | None = None
    source_guess: SourceGuess = None
    source_url: str | None = None
    uncertain_fields: list[str] = Field(default_factory=list)
    extraction_notes: str | None = None

    def has_contact(self) -> bool:
        return any(
            (
                self.phone_e164,
                self.whatsapp_number,
                self.email,
                self.instagram,
                self.telegram,
                self.website,
            )
        )

    def has_minimum(self) -> bool:
        """At least a name OR one contact is required to add a lead."""
        return bool(self.company_name) or self.has_contact()

    def is_empty(self) -> bool:
        """Almost nothing recognised — treated as garbage input."""
        return not self.company_name and not self.has_contact() and not self.city
