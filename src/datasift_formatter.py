"""Format NoticeData records into DataSift.ai (REISift) upload-ready CSV.

DataSift has 60+ built-in fields that auto-map when CSV headers match exactly.
This module maps our enrichment data to those built-in fields, plus 23 custom
fields in the "SiftStack" custom group for deep prospecting/notice-specific data.

For deceased records, the contact (Owner First/Last + Mailing Address) is set
to the decision maker, not the deceased owner. For living records, the contact
is the property owner.
"""

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from config import OUTPUT_DIR
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# Column order: auto-mapped built-in fields first, then custom fields.
# Headers must match DataSift's exact names for auto-mapping during upload.
DATASIFT_COLUMNS = [
    # ── Core (auto-mapped) ──
    "Property Street Address",
    "Property City",
    "Property State",
    "Property ZIP Code",
    "Owner First Name",
    "Owner Last Name",
    "Owner Type",
    "Company Name",
    "Mailing Street Address",
    "Mailing City",
    "Mailing State",
    "Mailing ZIP Code",
    # ── Phone/Email (Tracerfy skip trace, mapped to DataSift built-in) ──
    "Phone 1",
    "Phone 2",
    "Phone 3",
    "Phone 4",
    "Phone 5",
    "Phone 6",
    "Phone 7",
    "Phone 8",
    "Phone 9",
    "Email 1",
    "Email 2",
    "Email 3",
    "Email 4",
    "Email 5",
    "Tags",
    "Lists",
    "Notes",
    # ── Built-in fields (auto-mapped by DataSift) ──
    "Estimated Value",
    "MSL Status",               # DataSift spells it "MSL" not "MLS"
    "Last Sale Date",
    "Last Sale Price",
    "Equity Percentage",
    "Tax Deliquent Value",      # DataSift typo — "Deliquent" not "Delinquent"
    "Tax Delinquent Year",
    "Tax Auction Date",
    "Foreclosure Date",
    "Probate Open Date",
    "Personal Representative",
    "Parcel ID",
    "Structure Type",
    "Year Built",
    "Living SqFt",
    "Bedrooms",
    "Bathrooms",
    "Lot (Acres)",
    # ── Custom fields (SiftStack group) ──
    "Notice Type",
    "County",
    "Date Added",
    "Owner Deceased",
    "Date of Death",
    "Decedent Name",
    "Decision Maker",
    "DM Relationship",
    "DM Confidence",
    "DM 2 Name",
    "DM 2 Relationship",
    "DM 3 Name",
    "DM 3 Relationship",
    "Obituary URL",
    "Source URL",
    "Notice Screenshot",
    # ── Deep prospecting fields ──
    "DM 1 Status",
    "DM 1 Source",
    "DM 2 Status",
    "DM 3 Status",
    "Heir Count",
    "Heirs Living",
    "Signing Chain Count",
    "Signing Chain Names",
    "DM Confidence Reason",
    "Data Flags",
    # ── Entity research fields ──
    "Entity Type",
    "Entity Contact",
    "Entity Contact Role",
]


def _format_date(iso_date: str) -> str:
    """Convert YYYY-MM-DD to M/D/YYYY."""
    if not iso_date:
        return ""
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return f"{dt.month}/{dt.day}/{dt.year}"
    except ValueError:
        return iso_date


def _heir_count(notice: NoticeData) -> str:
    """Return total heir count from heir_map_json, or empty string."""
    if not notice.heir_map_json:
        return ""
    try:
        return str(len(json.loads(notice.heir_map_json)))
    except (json.JSONDecodeError, TypeError):
        return ""


# Entity suffixes that indicate a business, not a person.
# DataSift marks records incomplete if owner name contains these without a real person.
_ENTITY_SUFFIXES = re.compile(
    r"\b(?:LLC|L\.L\.C|Corp|Corporation|Inc|Incorporated|Trust|LP|LLP|"
    r"LTD|Limited|Co\b|Company|Association|Partners|Partnership|Holdings)\b",
    re.IGNORECASE,
)


def _is_entity_name(name: str) -> bool:
    """Return True if name looks like a business entity, not a person."""
    return bool(_ENTITY_SUFFIXES.search(name))


def _clean_and_split_name(full_name: str) -> tuple[str, str]:
    """Clean a full name for DataSift upload and split into (first, last).

    Handles patterns that cause DataSift "incomplete" records:
    - Court format "LAST, FIRST MIDDLE": "EDWARDS, NICHOLAS" → ("NICHOLAS", "EDWARDS")
    - Joint names with "&" or "AND": "John & Jane Smith" → ("John", "Smith")
    - Entity names (LLC, Trust, etc.): returns ("", "") — entity goes to Notes
    - Special characters: strips &, @, #, % from name parts
    """
    if not full_name:
        return ("", "")

    name = full_name.strip()

    # Entity names → empty (don't put business names in person fields)
    if _is_entity_name(name):
        return ("", "")

    # Court format "LAST, FIRST MIDDLE" → reorder to "FIRST LAST" before further processing
    if "," in name:
        last_part, _, first_part = name.partition(",")
        last = last_part.strip()
        first_words = first_part.strip().split()
        first = first_words[0] if first_words else ""
        if first and last:
            name = f"{first} {last}"

    # Split joint owners on " & " or " AND " — keep first person only
    # "John & Jane Smith" → "John Smith"
    # "John David & Jane Marie Smith" → "John David Smith"
    joint_match = re.split(r"\s+(?:&|AND)\s+", name, maxsplit=1, flags=re.IGNORECASE)
    if len(joint_match) > 1:
        first_person = joint_match[0].strip()
        second_part = joint_match[1].strip()
        # Extract last name from second part (last word(s) after second person's first name)
        second_words = second_part.split()
        if len(second_words) >= 2:
            # "Jane Smith" → last name is "Smith"
            last_name = second_words[-1]
            # Check if first person already has a last name
            first_words = first_person.split()
            if len(first_words) == 1:
                # "John" & "Jane Smith" → "John Smith"
                name = f"{first_person} {last_name}"
            else:
                # "John David" & "Jane Marie Smith" → "John David Smith"
                # But if "John Smith" & "Jane Doe" → keep "John Smith"
                name = first_person
        else:
            # "John & Jane" with no last name → just use first person
            name = first_person

    # Strip remaining special characters that cause incomplete status
    name = re.sub(r"[&@#%]", "", name)
    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        return ("", "")

    parts = name.split()
    if len(parts) == 1:
        return (parts[0], "")
    if len(parts) >= 3:
        # Strip middle initials (single letter + optional period) from between
        # first and last name parts. "Eric J. Yopp" → "Eric Yopp"
        # Keeps multi-char prefixes like "St." in "Richard C. St. Leger"
        middle = parts[1:-1]
        middle = [p for p in middle if not re.match(r"^[A-Za-z]\.?$", p)]
        parts = [parts[0]] + middle + [parts[-1]]
    return (parts[0], " ".join(parts[1:]))


def _split_name(full_name: str) -> tuple[str, str]:
    """Split full name into (first, last). Alias for _clean_and_split_name."""
    return _clean_and_split_name(full_name)


# Map notice_type → DataSift list name for niche sequential marketing.
# DataSift auto-creates lists from CSV if they don't exist yet.
NOTICE_TYPE_TO_LIST = {
    "foreclosure": "Foreclosure",
    "probate": "Probate",
    "tax_sale": "Tax Sale",
    "tax_delinquent": "Tax Delinquent",
    "eviction": "Eviction",
    "code_violation": "Code Violation",
    "divorce": "Divorce",
}


def _build_tags(notice: NoticeData) -> str:
    """Build comma-separated tags string for DataSift upload.

    Tags include:
    - Courthouse Data (all records — for niche sequential filter presets)
    - notice_type (foreclosure, tax_sale, probate, tax_delinquent)
    - county (knox, blount)
    - YYYY-MM date tag
    - deceased/living status
    - DM confidence level (for deceased records)
    - has_auction if auction date is upcoming
    """
    tags = ["Courthouse Data"]

    # Detect pre-probate obituary sources once — used in two places below.
    # These records are ahead of the court filing; they need different tags from
    # regular court-sourced probate records.
    src_lower = (notice.source_url or "").lower()
    is_obit_source = (
        notice.notice_type == "probate"
        and any(x in src_lower for x in (
            "echovita", "funeral_home_direct", "legacy.com",
        ))
    )

    # Data source tag
    if "acclaim" in src_lower:
        tags.append("Acclaim")
    elif "oscn.net" in src_lower:
        tags.append("OSCN")
    elif "column.us" in src_lower or "tulsaworld" in src_lower:
        tags.append("Tulsa World")
    elif "tnpublicnotice" in src_lower:
        tags.append("TN Public Notice")

    # Notice type — pre-probate obituary records get Pre-Probate + obituary instead
    # of the raw "probate" type so they're distinct in DataSift filter presets.
    if notice.notice_type:
        if is_obit_source:
            tags.extend(["Pre-Probate", "obituary"])
        else:
            tags.append(notice.notice_type)

    # County
    if notice.county:
        tags.append(notice.county.lower())

    # Month tag from the notice publication date (the meaningful cohort), falling
    # back to date_added (the run date) when no publication date is available.
    _month_src = notice.date_published or notice.date_added
    if _month_src:
        try:
            dt = datetime.strptime(_month_src, "%Y-%m-%d")
            tags.append(dt.strftime("%Y-%m"))
        except ValueError:
            pass

    # Deceased/living status
    if notice.owner_deceased == "yes":
        tags.append("deceased")
        # DM confidence
        if notice.dm_confidence:
            tags.append(f"{notice.dm_confidence}_confidence")
        # DM relationship — tells callers who they're contacting before they dial
        if notice.decision_maker_relationship:
            rel = notice.decision_maker_relationship.lower().replace(" ", "_")
            tags.append(f"dm_{rel}")
    else:
        tags.append("living")

    # Upcoming auction
    if notice.auction_date:
        try:
            auction_dt = datetime.strptime(notice.auction_date, "%Y-%m-%d")
            if auction_dt >= datetime.now():
                tags.append("has_auction")
        except ValueError:
            pass

    # Tax delinquent flag
    if notice.tax_delinquent_amount:
        try:
            amt = float(notice.tax_delinquent_amount)
            if amt > 0:
                tags.append("tax_delinquent")
        except (ValueError, TypeError):
            pass

    # Deep prospecting tags
    if notice.decision_maker_status == "verified_living":
        tags.append("dm_verified")
    if notice.heir_map_json:
        tags.append("has_heirs")
    elif notice.owner_deceased == "yes" and not is_obit_source:
        # no_heirs means heir research was done and found none.
        # Pre-probate obituary records haven't had heir research run yet —
        # don't tag them no_heirs just because the field is empty.
        tags.append("no_heirs")
    if (notice.owner_deceased == "yes"
            and notice.decision_maker_street
            and notice.decision_maker_street != notice.address):
        tags.append("has_dm_address")

    # Deep Prospecting flag — probate/obituary cases where we either have no
    # heir at all, or the heir we have isn't the spouse (PR, child, sibling,
    # etc.), and we still don't have a mailing address for them. Spouse-only
    # cases are excluded since the spouse is usually still at the property
    # address already on file.
    if notice.notice_type == "probate" and notice.owner_deceased == "yes" and not notice.decision_maker_street:
        rel = (notice.decision_maker_relationship or "").strip().lower()
        if not notice.decision_maker_name or rel != "spouse":
            tags.append("needs_deep_prospecting")

    # Signing chain tags
    if notice.signing_chain_count:
        try:
            sc_count = int(notice.signing_chain_count)
            tags.append(f"signing_chain_{sc_count}")
            # Check if all signing heirs have phone data
            if notice.heir_map_json:
                import json as _json
                try:
                    heirs = _json.loads(notice.heir_map_json)
                    signers = [h for h in heirs
                               if h.get("signing_authority") and h.get("status") != "deceased"]
                    traced = [h for h in signers if h.get("phones")]
                    # DM #1 counts as traced if notice has primary_phone
                    if notice.primary_phone and signers:
                        dm1_name = (notice.decision_maker_name or "").lower()
                        if any(h.get("name", "").lower() == dm1_name for h in signers):
                            traced_names = {h.get("name", "").lower() for h in traced}
                            if dm1_name not in traced_names:
                                traced.append({"name": dm1_name})  # count DM #1
                    if traced and len(traced) >= len(signers):
                        tags.append("signing_chain_complete")
                    elif traced:
                        tags.append("signing_chain_partial")
                except (ValueError, TypeError):
                    pass
        except (ValueError, TypeError):
            pass

    # Entity research tags
    if notice.entity_type:
        tags.append("entity_owned")
        if notice.entity_person_name:
            tags.append("entity_researched")

    # Photo import tag (source_url starts with "photo:")
    if notice.source_url and notice.source_url.startswith("photo:"):
        tags.append("photo_import")

    # Trestle dial tier (set by apply_trestle_tiers_to_notices after scoring)
    dial_tier = getattr(notice, "_dial_tier", "") or ""
    if dial_tier:
        tags.append(dial_tier)

    return ",".join(tags)


def _get_contact_info(notice: NoticeData) -> dict:
    """Determine the contact person and mailing address.

    For deceased owners with a decision maker: contact = DM
    For living owners: contact = property owner
    For entity-owned properties: try tax_owner_name or DM as real person fallback

    Mailing address always falls back to property address to avoid DataSift
    marking records as incomplete.
    """
    if notice.owner_deceased == "yes" and notice.decision_maker_name:
        first, last = _split_name(notice.decision_maker_name)
        # Fall back to property address when DM has no mailing address
        street = notice.decision_maker_street or notice.address
        city = notice.decision_maker_city or notice.city
        state = notice.decision_maker_state or notice.state
        zip_code = notice.decision_maker_zip or notice.zip
        return {
            "first": first,
            "last": last,
            "street": street,
            "city": city,
            "state": state,
            "zip": zip_code,
        }

    # Living owner — try owner_name first
    first, last = _split_name(notice.owner_name)

    # If owner_name was an entity (LLC/Trust), try fallbacks for a real person
    if not first and not last:
        # Try entity research result (signing member, registered agent, etc.)
        if notice.entity_person_name:
            first, last = _split_name(notice.entity_person_name)
        # Try tax API owner name (sometimes has individual behind entity)
        if not first and not last:
            if notice.tax_owner_name and not _is_entity_name(notice.tax_owner_name):
                first, last = _split_name(notice.tax_owner_name)
        # Try decision maker (probate PR, etc.)
        if not first and not last and notice.decision_maker_name:
            first, last = _split_name(notice.decision_maker_name)

    street = notice.owner_street or notice.address
    city = notice.owner_city or notice.city
    state = notice.owner_state or notice.state
    zip_code = notice.owner_zip or notice.zip
    return {
        "first": first,
        "last": last,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
    }


def _build_heir_summary(notice: NoticeData) -> str:
    """Build signing chain + family summary from heir_map_json.

    Two sections:
    1. SIGNING CHAIN — heirs with signing_authority who must sign to sell property.
       Includes phone + address for each.
    2. OTHER FAMILY — everyone else (in-laws, step-children, etc.) in compact format.
    """
    if not notice.heir_map_json:
        return ""

    try:
        heirs = json.loads(notice.heir_map_json)
    except (json.JSONDecodeError, TypeError):
        return ""

    if not heirs:
        return ""

    # Split into signing chain vs others
    signers = [h for h in heirs
                if h.get("signing_authority") and h.get("status") != "deceased"]
    non_signers = [h for h in heirs if not h.get("signing_authority") or h.get("status") == "deceased"]

    lines = []

    # ── Signing chain section ──
    if signers:
        lines.append(f"=== SIGNING CHAIN ({len(signers)} heir{'s' if len(signers) != 1 else ''} must sign) ===")
        for i, h in enumerate(signers, 1):
            name = h.get("name", "?")
            rel = h.get("relationship", "unknown")
            status = h.get("status", "unverified")
            status_label = "ALIVE" if status == "verified_living" else status.upper()

            # Phone info
            phones = h.get("phones", [])
            # DM #1 phones are on flat NoticeData fields, not in heir_map_json
            if not phones and notice.primary_phone:
                dm1_name = (notice.decision_maker_name or "").strip().lower()
                if name.lower() == dm1_name:
                    phones = [notice.primary_phone]

            phone_str = phones[0] if phones else "no phone yet"
            lines.append(f"{i}. {name} ({rel}) — {status_label} — {phone_str}")

            # Address
            street = h.get("street", "")
            if street:
                city = h.get("city", "")
                state = h.get("state", "")
                zip_code = h.get("zip", "")
                addr_parts = [street]
                if city:
                    addr_parts.append(city)
                addr_parts.append(f"{state} {zip_code}".strip())
                lines.append(f"   Mail: {', '.join(addr_parts)}")
    else:
        lines.append("=== NO SIGNING CHAIN IDENTIFIED ===")

    # ── Non-signing family section (compact) ──
    if non_signers:
        entries = []
        for h in non_signers[:6]:
            name = h.get("name", "?")
            rel = h.get("relationship", "")
            status = h.get("status", "unverified")
            tag = "living" if status == "verified_living" else "deceased" if status == "deceased" else status
            entries.append(f"{name} ({rel}) [{tag}]")
        lines.append("")
        lines.append("=== OTHER FAMILY (no signing authority) ===")
        lines.append(", ".join(entries))
        remaining = len(non_signers) - 6
        if remaining > 0:
            lines.append(f"(+{remaining} more)")

    return "\n".join(lines)


def _build_call_prep_section(notice: NoticeData) -> str:
    """Build actionable "what to know before calling" guidance for cold callers.

    Synthesizes confidence/verification/backup-heir signals already on the
    record into a short call script, rather than just restating raw facts
    (which the DECISION MAKERS / SIGNING CHAIN sections already do). Only
    fires for deceased-owner records with a named decision maker — living
    owner records are straightforward enough not to need a script.
    """
    if notice.owner_deceased != "yes" or not notice.decision_maker_name:
        return ""

    lines = []
    name = notice.decision_maker_name
    rel = notice.decision_maker_relationship or "unknown relationship"
    status = notice.decision_maker_status or "unverified"
    confidence = (notice.dm_confidence or "").lower()

    lines.append(f"Primary contact: {name} ({rel})")

    if status == "verified_living":
        lines.append(
            "VERIFIED LIVING — warm lead. Open by referencing the property/estate directly."
        )
    else:
        decedent = notice.decedent_name or "the deceased owner"
        lines.append(
            f"UNVERIFIED contact — confirm you've reached {name} before mentioning "
            f"{decedent}'s passing. If it's the wrong number, ask neutrally whether they "
            "handle the estate rather than revealing details."
        )

    # Backup contacts — known heirs beyond the primary DM
    backups = []
    if notice.decision_maker_2_name:
        backups.append(f"{notice.decision_maker_2_name} ({notice.decision_maker_2_relationship or 'unknown'})")
    if notice.decision_maker_3_name:
        backups.append(f"{notice.decision_maker_3_name} ({notice.decision_maker_3_relationship or 'unknown'})")
    if backups:
        lines.append(f"Known heirs (backup if primary unreachable): {', '.join(backups)}")
    elif not notice.heir_map_json:
        lines.append("No other heirs identified yet — ask on this call if there are additional siblings/heirs.")

    if not notice.decision_maker_street:
        lines.append("Needs Deep Prospecting — no mailing address on file for this contact yet.")

    if confidence:
        reason = f" — {notice.dm_confidence_reason}" if notice.dm_confidence_reason else ""
        lines.append(f"DM confidence: {confidence.upper()}{reason}")

    return "=== CALL PREP ===\n" + "\n".join(lines)


def _build_dm_section(notice: NoticeData) -> str:
    """Build ranked decision maker section with status and address."""
    dms = []

    for i, (name_attr, rel_attr, status_attr) in enumerate([
        ("decision_maker_name", "decision_maker_relationship", "decision_maker_status"),
        ("decision_maker_2_name", "decision_maker_2_relationship", "decision_maker_2_status"),
        ("decision_maker_3_name", "decision_maker_3_relationship", "decision_maker_3_status"),
    ], 1):
        name = getattr(notice, name_attr, "")
        if not name:
            continue
        rel = getattr(notice, rel_attr, "") or "unknown"
        status = getattr(notice, status_attr, "") or "unverified"

        status_label = "VERIFIED LIVING" if status == "verified_living" else status
        line = f"{i}. {name} ({rel}) — {status_label}"

        # Include DM1 mailing address if available
        if i == 1 and notice.decision_maker_street:
            addr_parts = [notice.decision_maker_street]
            if notice.decision_maker_city:
                addr_parts.append(notice.decision_maker_city)
            if notice.decision_maker_state:
                addr_parts.append(notice.decision_maker_state)
            if notice.decision_maker_zip:
                addr_parts[-1] = addr_parts[-1] + " " + notice.decision_maker_zip
            line += f"\n   Mail: {', '.join(addr_parts)}"

        dms.append(line)

    if not dms:
        return ""

    return "=== DECISION MAKERS ===\n" + "\n".join(dms)


def _build_property_section(notice: NoticeData) -> str:
    """Build the property/notice details section for Notes."""
    parts = []

    # Include entity name when owner is LLC/Trust (name stripped from contact fields)
    if notice.owner_name and _is_entity_name(notice.owner_name):
        parts.append(f"Entity: {notice.owner_name}")

    # Include entity research contact if found
    if notice.entity_person_name:
        role = notice.entity_person_role.replace("_", " ").title() if notice.entity_person_role else "Unknown"
        parts.append(f"Entity Contact: {notice.entity_person_name} ({role})")

    if notice.notice_type:
        parts.append(notice.notice_type.replace("_", " ").title())

    if notice.auction_date:
        parts.append(f"Auction: {_format_date(notice.auction_date)}")

    if notice.tax_delinquent_amount:
        tax_str = f"Tax Due: ${notice.tax_delinquent_amount}"
        if notice.tax_delinquent_years:
            tax_str += f" ({notice.tax_delinquent_years} yrs)"
        parts.append(tax_str)

    if notice.source_url:
        parts.append(f"Source: {notice.source_url}")

    # Proof-of-source: link to the screenshot of the actual published notice
    if notice.notice_screenshot_url:
        parts.append(f"Notice Screenshot: {notice.notice_screenshot_url}")

    return " | ".join(parts)


def _build_notes(notice: NoticeData) -> str:
    """Build a structured notes string for DataSift records.

    Deceased records get a multi-section format with heir map and DM summary.
    Living records get a simpler single-section format.
    """
    if notice.owner_deceased == "yes":
        sections = []

        # Section 1: Deceased owner header
        deceased_parts = []
        if notice.decedent_name:
            deceased_parts.append(f"Decedent: {notice.decedent_name}")
        if notice.date_of_death:
            deceased_parts.append(f"Died: {_format_date(notice.date_of_death)}")
        if notice.obituary_url:
            deceased_parts.append(f"Obituary: {notice.obituary_url}")

        confidence_line = ""
        if notice.dm_confidence:
            confidence_line = f"Confidence: {notice.dm_confidence.upper()}"
            if notice.dm_confidence_reason:
                confidence_line += f" — {notice.dm_confidence_reason}"

        if deceased_parts or confidence_line:
            header = "=== DECEASED OWNER ==="
            body = " | ".join(deceased_parts)
            if confidence_line:
                body += f"\n{confidence_line}" if body else confidence_line
            sections.append(f"{header}\n{body}")

        # Section 2: Decision makers
        dm_section = _build_dm_section(notice)
        if dm_section:
            sections.append(dm_section)

        # Section 2b: Call prep — actionable script, not just raw facts
        call_prep_section = _build_call_prep_section(notice)
        if call_prep_section:
            sections.append(call_prep_section)

        # Section 3: Heir map
        heir_section = _build_heir_summary(notice)
        if heir_section:
            sections.append(heir_section)

        # Section 4: Property/notice details
        prop_section = _build_property_section(notice)
        if prop_section:
            sections.append(f"=== PROPERTY ===\n{prop_section}")

        if notice.report_url:
            sections.append(f"=== REPORT ===\n{notice.report_url}")

        return "\n\n".join(sections)

    # Living owner — simple format
    return _build_property_section(notice)


def _build_dm_notes(notice: NoticeData) -> str:
    """Build Notes for CSV 1: deceased owner header + DM breakdown + property.

    For living records, returns the simple property section.
    Used by write_datasift_split_csvs() for the DMs upload.
    """
    if notice.owner_deceased != "yes":
        return _build_property_section(notice)

    sections = []

    # Deceased owner header
    deceased_parts = []
    if notice.decedent_name:
        deceased_parts.append(f"Decedent: {notice.decedent_name}")
    if notice.date_of_death:
        deceased_parts.append(f"Died: {_format_date(notice.date_of_death)}")
    if notice.obituary_url:
        deceased_parts.append(f"Obituary: {notice.obituary_url}")

    confidence_line = ""
    if notice.dm_confidence:
        confidence_line = f"Confidence: {notice.dm_confidence.upper()}"
        if notice.dm_confidence_reason:
            confidence_line += f" — {notice.dm_confidence_reason}"

    if deceased_parts or confidence_line:
        header = "=== DECEASED OWNER ==="
        body = " | ".join(deceased_parts)
        if confidence_line:
            body += f"\n{confidence_line}" if body else confidence_line
        sections.append(f"{header}\n{body}")

    # Decision makers
    dm_section = _build_dm_section(notice)
    if dm_section:
        sections.append(dm_section)

    # Call prep — actionable script, not just raw facts
    call_prep_section = _build_call_prep_section(notice)
    if call_prep_section:
        sections.append(call_prep_section)

    # Property details
    prop_section = _build_property_section(notice)
    if prop_section:
        sections.append(f"=== PROPERTY ===\n{prop_section}")

    return "\n\n".join(sections)


def _build_heir_notes(notice: NoticeData) -> str:
    """Build Notes for CSV 2: full heir map only.

    Used by write_datasift_split_csvs() for the Heirs upload.
    Returns empty string if no heir data.
    """
    return _build_heir_summary(notice)


def _validate_row(row: dict) -> tuple[bool, list[str]]:
    """Check a row dict for DataSift completeness.

    DataSift marks records incomplete when missing owner first/last name,
    mailing address, or property address.

    Returns:
        (is_complete, issues) — True if record will be "clean" in DataSift.
    """
    issues = []
    if not row.get("Owner First Name"):
        issues.append("no_first_name")
    if not row.get("Owner Last Name"):
        issues.append("no_last_name")
    if not row.get("Property Street Address"):
        issues.append("no_property_address")
    if not row.get("Mailing Street Address"):
        issues.append("no_mailing_address")
    return (len(issues) == 0, issues)


def _build_row(notice: NoticeData, notes_override: str | None = None) -> dict:
    """Build a single CSV row dict for a NoticeData record.

    Args:
        notice: The notice to format.
        notes_override: If provided, use this as the Notes value instead of
            calling _build_notes(). Used by write_datasift_split_csvs().

    Returns:
        Dict keyed by DATASIFT_COLUMNS headers.
    """
    contact = _get_contact_info(notice)
    tags = _build_tags(notice)
    list_name = NOTICE_TYPE_TO_LIST.get(notice.notice_type, "")
    notes = notes_override if notes_override is not None else _build_notes(notice)

    # Conditionally map auction_date to the right built-in field
    tax_auction = ""
    foreclosure_date = ""
    probate_open = ""
    if notice.notice_type == "tax_sale":
        tax_auction = _format_date(notice.auction_date)
    elif notice.notice_type == "foreclosure":
        foreclosure_date = _format_date(notice.auction_date)
    elif notice.notice_type == "probate":
        # Probate notices are published when the estate opens — use the
        # publication date, not the run date.
        probate_open = _format_date(notice.date_published or notice.date_added)

    # Personal Representative only for probate notices. Prefer the resolved
    # decision maker (deep prospecting), else the court-named PR that the parser
    # puts in owner_name — so the field is populated even when deep prospecting
    # is off (lean daily run).
    personal_rep = ""
    if notice.notice_type == "probate":
        personal_rep = notice.decision_maker_name or notice.owner_name or ""

    return {
        # ── Core auto-mapped ──
        "Property Street Address": notice.address,
        "Property City": notice.city,
        "Property State": notice.state or "",
        "Property ZIP Code": notice.zip,
        "Owner First Name": contact["first"],
        "Owner Last Name": contact["last"],
        "Owner Type": "Company" if _is_entity_name(notice.owner_name) else "Person",
        "Company Name": notice.owner_name if _is_entity_name(notice.owner_name) else "",
        "Mailing Street Address": contact["street"],
        "Mailing City": contact["city"],
        "Mailing State": contact["state"],
        "Mailing ZIP Code": contact["zip"],
        # ── Phone/Email (Tracerfy → DataSift generic Phone N format) ──
        "Phone 1": notice.primary_phone,
        "Phone 2": notice.mobile_1,
        "Phone 3": notice.mobile_2,
        "Phone 4": notice.mobile_3,
        "Phone 5": notice.mobile_4,
        "Phone 6": notice.mobile_5,
        "Phone 7": notice.landline_1,
        "Phone 8": notice.landline_2,
        "Phone 9": notice.landline_3,
        "Email 1": notice.email_1,
        "Email 2": notice.email_2,
        "Email 3": notice.email_3,
        "Email 4": notice.email_4,
        "Email 5": notice.email_5,
        "Tags": tags,
        "Lists": list_name,
        "Notes": notes,
        # ── Built-in fields ──
        "Estimated Value": notice.estimated_value,
        "MSL Status": notice.mls_status,
        "Last Sale Date": _format_date(notice.mls_last_sold_date),
        "Last Sale Price": notice.mls_last_sold_price,
        "Equity Percentage": notice.equity_percent,
        "Tax Deliquent Value": notice.tax_delinquent_amount,
        "Tax Delinquent Year": notice.tax_delinquent_years,
        "Tax Auction Date": tax_auction,
        "Foreclosure Date": foreclosure_date,
        "Probate Open Date": probate_open,
        "Personal Representative": personal_rep,
        "Parcel ID": notice.parcel_id,
        "Structure Type": notice.property_type,
        "Year Built": notice.year_built,
        "Living SqFt": notice.sqft,
        "Bedrooms": notice.bedrooms,
        "Bathrooms": notice.bathrooms,
        "Lot (Acres)": notice.lot_size,
        # ── Custom fields (SiftStack group) ──
        "Notice Type": notice.notice_type,
        "County": notice.county,
        "Date Added": _format_date(notice.date_added),
        "Owner Deceased": notice.owner_deceased,
        "Date of Death": notice.date_of_death,
        "Decedent Name": notice.decedent_name,
        "Decision Maker": notice.decision_maker_name,
        "DM Relationship": notice.decision_maker_relationship,
        "DM Confidence": notice.dm_confidence,
        "DM 2 Name": notice.decision_maker_2_name,
        "DM 2 Relationship": notice.decision_maker_2_relationship,
        "DM 3 Name": notice.decision_maker_3_name,
        "DM 3 Relationship": notice.decision_maker_3_relationship,
        "Obituary URL": notice.obituary_url,
        "Source URL": notice.source_url,
        "Notice Screenshot": notice.notice_screenshot_url,
        # ── Deep prospecting fields ──
        "DM 1 Status": notice.decision_maker_status,
        "DM 1 Source": notice.decision_maker_source,
        "DM 2 Status": notice.decision_maker_2_status,
        "DM 3 Status": notice.decision_maker_3_status,
        "Heir Count": _heir_count(notice),
        "Heirs Living": notice.heirs_verified_living,
        "Signing Chain Count": notice.signing_chain_count,
        "Signing Chain Names": notice.signing_chain_names,
        "DM Confidence Reason": notice.dm_confidence_reason,
        "Data Flags": notice.missing_data_flags,
        # ── Entity research fields ──
        "Entity Type": notice.entity_type,
        "Entity Contact": notice.entity_person_name,
        "Entity Contact Role": notice.entity_person_role,
    }


_API_CORE_COLUMNS = {
    "Property Street Address", "Property City", "Property State", "Property ZIP Code",
    "Owner First Name", "Owner Last Name", "Owner Type", "Company Name",
    "Mailing Street Address", "Mailing City", "Mailing State", "Mailing ZIP Code",
    "Phone 1", "Phone 2", "Phone 3", "Phone 4", "Phone 5",
    "Phone 6", "Phone 7", "Phone 8", "Phone 9",
    "Email 1", "Email 2", "Email 3", "Email 4", "Email 5",
    "Tags", "Lists", "Notes",
}


def build_api_payload(row: dict) -> dict:
    """Map a DATASIFT_COLUMNS CSV row (as built by _build_row) to the REST API's
    request shapes: address, owner, tags (array), lists, notes, phones, emails,
    and a flat {column_name: value} dict of everything else for
    datasift_api.update_custom_field_values().

    Handles two API requirements the CSV shape itself doesn't need to satisfy
    (confirmed live against the API 2026-08-19, not documented in the
    reference):
      - owner.address is required — falls back to property address, same as
        the CSV's own mailing-address fallback in _get_contact_info().
      - Entity owners must omit first_name/last_name entirely rather than
        send "" — the API 400s on a blank first_name. _build_row() already
        leaves these blank (not omitted) for entities, which the CSV importer
        tolerates but this API will not; this function is where that gets fixed.

    The remaining ~46 non-core columns (the 18 "built-in" fields plus the 16
    SiftStack + 10 deep-prospecting + 3 entity-research custom columns) are
    all routed through the custom-fields mechanism for now, not split into
    "native property field vs. genuine custom field": Phase A's empirical
    check found an existing real property with every candidate native field
    (estimate_value, sqft, bedrooms, ...) already null, so there's no proven
    mapping to a native key yet, and guessing wrong risks a silent drop. A
    custom field write is visible and correctable later; a wrong native-field
    guess is not.
    """
    is_entity = row.get("Owner Type") == "Company"

    phones = [{"number": p} for p in
              (row.get(f"Phone {i}") for i in range(1, 10)) if p and p.strip()]
    emails = [{"email": e} for e in
              (row.get(f"Email {i}") for i in range(1, 6)) if e and e.strip()]

    owner_address = {
        "street": row.get("Mailing Street Address") or row.get("Property Street Address"),
        "city": row.get("Mailing City") or row.get("Property City"),
        "state": row.get("Mailing State") or row.get("Property State"),
        "postal_code": row.get("Mailing ZIP Code") or row.get("Property ZIP Code"),
        "country": "US",
    }
    owner: dict = {"address": owner_address, "phones": phones, "emails": emails}
    if is_entity:
        owner["company"] = row.get("Company Name") or ""
    else:
        owner["first_name"] = row.get("Owner First Name") or ""
        owner["last_name"] = row.get("Owner Last Name") or ""

    # Tags MUST be an array — a comma string creates one tag literally named
    # "Courthouse Data, foreclosure, Knox".
    tags = [t.strip() for t in (row.get("Tags") or "").split(",") if t.strip()]
    # Lists, deliberately, is NOT split. Ty's production uploader
    # (datasift_api_upload.py:152) sends `lists` as a bare string one line
    # after splitting tags into an array — the asymmetry is intentional and
    # matches the OpenAPI spec, which types `lists` as a plain string. Our
    # Lists column only ever holds a single list name (see _build_row), so
    # the multi-list case this could theoretically mangle never arises.

    custom_fields = {
        col: row[col] for col in DATASIFT_COLUMNS
        if col not in _API_CORE_COLUMNS and row.get(col) not in (None, "")
    }

    return {
        "address": {
            "street": row.get("Property Street Address"),
            "city": row.get("Property City"),
            "state": row.get("Property State"),
            "postal_code": row.get("Property ZIP Code"),
            "country": "US",
        },
        "owner": owner,
        "tags": tags,
        "lists": row.get("Lists") or "",
        "notes": row.get("Notes") or "",
        "custom_fields": custom_fields,
    }


"""Petition fields rendered into Notes / Message Board, in display order.

Extended 2026-08-21 at the user's request: the original six were the loan
figures alone, which left out most of what actually drives a decision on a
foreclosure lead. The additions — legal description, lender/plaintiff,
modification history, junior lienholders, owner status — came from reading two
real Tulsa petitions end to end and noting what mattered and wasn't captured.

Grouped so the note reads as sections rather than one long pipe-delimited run.
Any field absent from the row is skipped, so older/plainer rows still work.
"""
_PETITION_SECTIONS: list[tuple[str, list[str]]] = [
    ("CASE", [
        "Date Foreclosure Filed", "Case Number", "Court County",
        "Plaintiff", "Co-Defendants",
    ]),
    ("PROPERTY", [
        "Legal Description", "Plat Number",
    ]),
    ("LOAN", [
        "Date of Mortgage/Note", "Original Lender", "Original Loan Amount",
        "Initial Interest Rate", "Original Monthly Payment",
        "Mortgage Recorded Date", "Mortgage Document Number",
        "Unpaid Principal Balance", "Interest Rate",
        "Date of Default", "Date of Last Payment",
    ]),
    ("MODIFICATIONS", [
        "Loan Modification Count", "Loan Modification History",
    ]),
    ("OWNER", [
        "Owner Status", "Owner Alive",
    ]),
    ("LIENS", [
        "Junior Lienholders",
    ]),
]

#: Flat list, kept for callers that just want to know which columns are
#: petition-derived (e.g. deciding whether a row has petition data at all).
_PETITION_INFO_FIELDS = [f for _, fields in _PETITION_SECTIONS for f in fields]


def _format_petition_notes(rec: dict) -> str:
    """Format the petition-info-extraction skill's extra columns (if present
    in this row) into a single Notes-appendable string. Returns "" if none
    of those columns are present/populated — plain property-template rows
    (no petition data) are unaffected."""

    def _fmt_date(v) -> str:
        if v is None or v == "":
            return ""
        if isinstance(v, datetime):
            return v.strftime("%m/%d/%Y")
        return str(v).strip()

    def _fmt_currency(v) -> str:
        if v is None or v == "":
            return ""
        try:
            return f"${float(v):,.2f}"
        except (TypeError, ValueError):
            return str(v).strip()

    def _fmt_rate(v) -> str:
        if v is None or v == "":
            return ""
        try:
            # Stored as a fraction (0.07125) per the extraction skill's own
            # spec, not already a percent (7.125).
            return f"{float(v) * 100:.3f}%"
        except (TypeError, ValueError):
            return str(v).strip()

    def _fmt_text(v) -> str:
        return "" if v is None else str(v).strip()

    formatters = {
        "Date of Mortgage/Note": _fmt_date,
        "Mortgage Recorded Date": _fmt_date,
        "Date of Default": _fmt_date,
        "Date of Last Payment": _fmt_date,
        "Date Foreclosure Filed": _fmt_date,
        "Original Loan Amount": _fmt_currency,
        "Unpaid Principal Balance": _fmt_currency,
        "Original Monthly Payment": _fmt_currency,
        "Interest Rate": _fmt_rate,
        "Initial Interest Rate": _fmt_rate,
    }

    sections: list[str] = []
    for heading, fields in _PETITION_SECTIONS:
        lines = []
        for field in fields:
            if field not in rec:
                continue
            formatted = formatters.get(field, _fmt_text)(rec.get(field))
            if formatted:
                lines.append(f"  {field}: {formatted}")
        if lines:
            sections.append(heading + "\n" + "\n".join(lines))

    if not sections:
        return ""

    note = "FORECLOSURE PETITION\n" + "\n\n".join(sections)

    # Underwriting flag worth surfacing, not buried in the figures: when the
    # balance owed exceeds the original loan, arrears have been capitalized
    # through repeated modifications and equity may be thin or negative.
    try:
        upb = float(rec.get("Unpaid Principal Balance") or 0)
        orig = float(rec.get("Original Loan Amount") or 0)
        if upb and orig and upb > orig:
            # ASCII only: this string can reach a Windows cp1252 console via
            # logging, where a non-ASCII dash raises UnicodeEncodeError.
            note += (f"\n\nCAUTION: balance owed (${upb:,.2f}) exceeds the original "
                     f"loan (${orig:,.2f}) by ${upb - orig:,.2f} - arrears capitalized "
                     f"through modification. Verify total payoff before underwriting.")
    except (TypeError, ValueError):
        pass

    return note


def build_datasift_csv_from_template(
    template_rows: list[dict],
    trace_results: list[dict],
    *,
    notice_type: str = "foreclosure",
    county: str = "",
    trial_tag: str | None = None,
    output_path: str | Path | None = None,
) -> Path:
    """Build a DataSift-ready CSV from a raw property-template CSV/xlsx
    (Property Street, Property City, Property State, Property Zip, First
    Name, Last Name[, Record Link]) plus Tracerfy trace_contacts() results.

    This is the reusable version of the CSV-building step that was
    previously hand-written inline for every trial run in the 2026-08-13/14
    session — see the skip-and-score-upload CLI mode in main.py.

    Args:
        template_rows: rows from the raw template, each with at least
            "Property Street", "Property City", "Property State",
            "Property Zip", "First Name", "Last Name" (and optionally
            "Record Link" for the source URL).
        trace_results: trace_contacts()'s return value — matched to
            template_rows by (first_name, last_name), case-insensitive.
            Ignored for any row whose "Owner Alive" column is "No" (see
            below), even if a match exists — that owner is never traced.
        (optional per-row) "Owner Alive": "Yes"/"No"/blank. "No" means the
            source document names no living heir/spouse/co-borrower/
            representative for this owner — the record is still built and
            uploaded (tagged "deceased", Owner Deceased=yes, and flagged in
            Notes/Message Board) but is never given phone/email data here,
            and the caller (see skip-and-score-upload in main.py) must also
            exclude it from Tracerfy and DataSift's own skip trace.
        notice_type: e.g. "foreclosure" — drives the Lists column via
            NOTICE_TYPE_TO_LIST and gets tagged directly.
        county: tagged and set in the County column if provided.
        trial_tag: if given, appended to Tags and noted (e.g.
            "Pipeline_Trial_2026-08-14") — use for test/trial runs so
            they're easy to find and distinguish from real leads.
        output_path: where to write the CSV. Defaults to
            output/datasift_ready_{notice_type}_{today}.csv.

    Returns:
        Path to the written CSV.
    """
    from tracerfy_skip_tracer import PHONE_FIELDS, EMAIL_FIELDS

    trace_by_name = {
        (t.get("first_name", "").strip().lower(), t.get("last_name", "").strip().lower()): t
        for t in trace_results
    }

    today = datetime.now().strftime("%m/%d/%Y")
    list_name = NOTICE_TYPE_TO_LIST.get(notice_type, notice_type.title())

    rows = []
    for rec in template_rows:
        street = (rec.get("Property Street") or "").strip()
        city = (rec.get("Property City") or "").strip()
        state = (rec.get("Property State") or "").strip()
        zip_code = str(rec.get("Property Zip") or "").strip()
        first = (rec.get("First Name") or "").strip()
        last = (rec.get("Last Name") or "").strip()
        source_url = (rec.get("Record Link") or "").strip()
        if not (street and last):
            continue

        # "Owner Alive" is an optional column from the petition-info-extraction
        # skill (Yes/No/blank). "No" means the petition names no living
        # heir/spouse/co-borrower/representative for this owner at all —
        # skip tracing a dead person wastes money and a resulting phone
        # number sitting next to their name in DataSift reads as a live,
        # dialable contact, which it isn't. Blank/missing (plain property
        # templates with no petition data) is treated as alive, unchanged
        # from prior behavior.
        owner_alive_raw = str(rec.get("Owner Alive") or "").strip().lower()
        is_deceased_no_contact = owner_alive_raw == "no"

        t = {} if is_deceased_no_contact else trace_by_name.get((first.lower(), last.lower()), {})
        phones = []
        for k in PHONE_FIELDS:
            v = (t.get(k) or "").strip()
            if v and v not in phones:
                phones.append(v)
        phones = phones[:9]
        emails = [(t.get(k) or "").strip() for k in EMAIL_FIELDS]
        emails = [e for e in emails if e][:5]

        mail_street = (t.get("mail_address") or "").strip() or street
        mail_city = (t.get("mail_city") or "").strip() or city
        mail_state = (t.get("mail_state") or "").strip() or state
        mail_zip = (t.get("mail_zip") or "").strip() or zip_code
        is_absentee = bool(mail_street) and mail_street.lower() != street.lower()

        tags = ["Courthouse Data", notice_type]
        if county:
            tags.append(county.lower())
        tags.append(datetime.now().strftime("%Y-%m"))
        tags.append("deceased" if is_deceased_no_contact else "living")
        if is_absentee:
            tags.append("Absentee Owner")
        if trial_tag:
            tags.append(trial_tag)

        notes_parts = []
        if trial_tag:
            notes_parts.append(f"TEST RUN -- {trial_tag}, verify before treating as a live lead.")
        if source_url:
            notes_parts.append(f"Source: {source_url}.")
        if is_deceased_no_contact:
            notes_parts.append(
                "OWNER DECEASED -- the petition names no living heir, spouse, "
                "co-borrower, or estate representative for this owner. NOT "
                "skip traced (would waste spend tracing someone who no longer "
                "exists, and a resulting phone number would misleadingly read "
                "as a live contact). Needs deep prospecting / heir research "
                "before any outreach."
            )
        else:
            notes_parts.append("Skip traced via Tracerfy.")

        # Extra petition-info columns (from petition-info-extraction skill
        # output — Date of Mortgage/Note, Original Loan Amount, Unpaid
        # Principal Balance, Interest Rate, Date of Last Payment, Date
        # Foreclosure Filed) ride along in Notes rather than needing a
        # separate Message Board post: DataSift auto-posts a record's Notes
        # field as a Message Board comment on upload (confirmed 2026-08-14 —
        # no extra automation needed, and it sidesteps a Tagify input that
        # doesn't sync typed text the way a plain textarea would). Only adds
        # this block when the source file actually has these columns, so
        # plain property templates are unaffected.
        petition_note = _format_petition_notes(rec)
        if petition_note:
            notes_parts.append(petition_note)

        row = {c: "" for c in DATASIFT_COLUMNS}
        row.update({
            "Property Street Address": street,
            "Property City": city,
            "Property State": state,
            "Property ZIP Code": zip_code,
            "Owner First Name": first,
            "Owner Last Name": last,
            "Owner Type": "Person",
            "Mailing Street Address": mail_street,
            "Mailing City": mail_city,
            "Mailing State": mail_state,
            "Mailing ZIP Code": mail_zip,
            "Tags": ", ".join(tags),
            "Lists": list_name,
            "Notes": " ".join(notes_parts),
            "Notice Type": notice_type,
            "County": county,
            "Date Added": today,
            "Owner Deceased": "yes" if is_deceased_no_contact else "no",
            "Source URL": source_url,
        })
        for i, p in enumerate(phones, start=1):
            row[f"Phone {i}"] = p
        for i, e in enumerate(emails, start=1):
            row[f"Email {i}"] = e
        rows.append(row)

    if output_path is None:
        output_path = OUTPUT_DIR / f"datasift_ready_{notice_type}_{datetime.now().strftime('%Y-%m-%d')}.csv"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    logger.info("Wrote %d record(s) to %s", len(rows), output_path)
    return output_path


def _slugify_source(source_label: str) -> str:
    """Sanitize a scraper/source name for use in a filename.

    "Acclaim" -> "acclaim", "TN Public Notice" -> "tn_public_notice"
    """
    slug = re.sub(r"[^a-z0-9]+", "_", source_label.strip().lower())
    return slug.strip("_") or "unknown_source"


def write_datasift_csv(
    notices: list[NoticeData],
    filename: str | None = None,
    source_label: str = "",
) -> Path:
    """Write notices to a DataSift-formatted CSV file.

    Args:
        notices: List of enriched NoticeData objects.
        filename: Optional filename override.
        source_label: Scraper/source name (e.g. "acclaim", "oscn",
            "preprobate_obituary") embedded in the auto-generated filename so
            every DataSift-ready CSV is traceable back to where it came from.
            Ignored if filename is provided.

    Returns:
        Path to the written CSV file.
    """
    if filename is None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        slug = _slugify_source(source_label) if source_label else "unknown_source"
        filename = f"datasift_ready_{slug}_{timestamp}.csv"

    output_path = OUTPUT_DIR / filename
    written = 0
    incomplete = 0
    issue_counts: dict[str, int] = {}

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
        writer.writeheader()

        for notice in notices:
            row = _build_row(notice)
            is_complete, issues = _validate_row(row)
            if not is_complete:
                incomplete += 1
                for issue in issues:
                    issue_counts[issue] = issue_counts.get(issue, 0) + 1
                logger.debug("Incomplete record %s: %s", notice.address, issues)
            writer.writerow(row)
            written += 1

    logger.info("Wrote %d records to DataSift CSV: %s", written, output_path)
    if incomplete:
        logger.warning("DataSift completeness: %d/%d clean, %d incomplete (%s)",
                        written - incomplete, written, incomplete,
                        ", ".join(f"{k}={v}" for k, v in issue_counts.items()))
    else:
        logger.info("DataSift completeness: %d/%d clean (100%%)", written, written)
    return output_path


def write_datasift_split_csvs(
    notices: list[NoticeData],
    date_str: str | None = None,
    source_label: str = "",
) -> list[dict]:
    """Generate separate DM and Heir Map CSVs for two-upload Message Board flow.

    CSV 1 ("DMs"): All records. Deceased get DM breakdown as Notes, living get
    property details. Creates/updates all records in DataSift.

    CSV 2 ("Heirs"): Only deceased records with heir data. Notes = full heir map.
    DataSift merges by address, adding a second Message Board comment.

    Args:
        notices: List of enriched NoticeData objects.
        date_str: Optional date string for filenames/list names (default: today).
        source_label: Scraper/source name (e.g. "acclaim", "oscn",
            "preprobate_obituary", "pdf_import") embedded in both filenames so
            every DataSift-ready CSV is traceable back to where it came from,
            e.g. datasift_ready_acclaim_2026-07-10_120000_DMs.csv.

    Returns:
        List of dicts: [{"path": Path, "label": str, "list_name": str}, ...]
        Returns 1 item if no deceased-with-heirs, 2 items otherwise.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    slug = _slugify_source(source_label) if source_label else "unknown_source"
    results = []

    # CSV 1: DMs — all records
    dm_path = OUTPUT_DIR / f"datasift_ready_{slug}_{timestamp}_DMs.csv"
    dm_written = 0
    incomplete = 0
    issue_counts: dict[str, int] = {}
    with open(dm_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
        writer.writeheader()
        for notice in notices:
            row = _build_row(notice, notes_override=_build_dm_notes(notice))
            is_complete, issues = _validate_row(row)
            if not is_complete:
                incomplete += 1
                for issue in issues:
                    issue_counts[issue] = issue_counts.get(issue, 0) + 1
            writer.writerow(row)
            dm_written += 1

    logger.info("DMs CSV: %d records → %s", dm_written, dm_path)
    if incomplete:
        logger.warning("DataSift completeness: %d/%d clean, %d incomplete (%s)",
                        dm_written - incomplete, dm_written, incomplete,
                        ", ".join(f"{k}={v}" for k, v in issue_counts.items()))
    else:
        logger.info("DataSift completeness: %d/%d clean (100%%)", dm_written, dm_written)
    results.append({
        "path": dm_path,
        "label": "DMs",
        "list_name": f"SiftStack {date_str} - DMs",
    })

    # CSV 2: Heirs — only deceased with heir data
    deceased_with_heirs = [
        n for n in notices
        if n.owner_deceased == "yes" and n.heir_map_json
    ]

    if deceased_with_heirs:
        heir_path = OUTPUT_DIR / f"datasift_ready_{slug}_{timestamp}_Heirs.csv"
        heir_written = 0
        with open(heir_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
            writer.writeheader()
            for notice in deceased_with_heirs:
                row = _build_row(notice, notes_override=_build_heir_notes(notice))
                writer.writerow(row)
                heir_written += 1

        logger.info("Heirs CSV: %d records → %s", heir_written, heir_path)
        results.append({
            "path": heir_path,
            "label": "Heirs",
            "list_name": f"SiftStack {date_str} - Heirs",
        })
    else:
        logger.info("No deceased records with heir data — skipping Heirs CSV")

    return results
