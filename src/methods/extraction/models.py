"""
extraction/models.py
Data structures and Pydantic schemas for extracted business data and personalization briefs.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class SocialProfile(BaseModel):
    platform: str
    url: str
    handle: Optional[str] = None
    bio: Optional[str] = None
    extra_emails: List[str] = Field(default_factory=list)
    extra_phones: List[str] = Field(default_factory=list)


class ContactInfo(BaseModel):
    emails: List[str] = Field(default_factory=list)
    phone_numbers: List[str] = Field(default_factory=list)
    addresses: List[str] = Field(default_factory=list)
    contact_forms: List[str] = Field(default_factory=list)
    socials: List[SocialProfile] = Field(default_factory=list)
    legal_identifiers: Dict[str, str] = Field(
        default_factory=dict,
        description="Tax IDs, VAT, EIN, or Impressum entries if found.",
    )


class BusinessExtractionResult(BaseModel):
    domain: str
    base_url: str
    company_name: Optional[str] = None
    description: Optional[str] = None
    contacts: ContactInfo
    pages_crawled: List[str] = Field(default_factory=list)
    raw_metadata: Dict[str, Any] = Field(default_factory=dict)


class PersonalizationBrief(BaseModel):
    company_name: str
    one_liner: str
    core_value_proposition: str
    target_market: Optional[str] = None
    recent_developments_or_focus: List[str] = Field(default_factory=list)
    personalization_hooks: List[str] = Field(
        default_factory=list,
        description="High-converting conversational opening hooks.",
    )
    suggested_outreach_angle: str
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)