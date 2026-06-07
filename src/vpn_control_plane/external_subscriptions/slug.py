from __future__ import annotations

import re

from vpn_control_plane.external_subscriptions.parser import ParsedSubscriptionEntry

# Everything that is not a Unicode word char (letters/digits) — including underscores, emoji
# (flags), punctuation and spaces — becomes a separator.
_SLUG_SEPARATORS = re.compile(r"[\W_]+", re.UNICODE)
_UNTITLED_SLUG = "untitled"


def slugify(fragment: str) -> str:
    """Turn an upstream fragment into a slug.

    "🇳🇱 Netherlands, Extra Whitelist Delta" -> "netherlands-extra-whitelist-delta"
    "Амстердам, Нидерланды, Extra"           -> "амстердам-нидерланды-extra"
    Returns "" when nothing usable remains (handled by the caller).
    """
    return _SLUG_SEPARATORS.sub("-", fragment.strip().lower()).strip("-")


def assign_slugs(entries: list[ParsedSubscriptionEntry]) -> list[tuple[str, ParsedSubscriptionEntry]]:
    """Assign a stable, unique slug to each entry.

    Entries are processed in a deterministic order (by uri, then label) so the same set yields
    the same slugs regardless of feed ordering. Collisions get -2, -3, ... suffixes; an empty
    slug (missing fragment) falls back to "untitled" and is suffixed the same way.
    """
    ordered = sorted(entries, key=lambda entry: (entry.uri, entry.label))
    used: set[str] = set()
    slugged: list[tuple[str, ParsedSubscriptionEntry]] = []
    for entry in ordered:
        base = slugify(entry.label) or _UNTITLED_SLUG
        slug = base
        suffix = 1
        while slug in used:
            suffix += 1
            slug = f"{base}-{suffix}"
        used.add(slug)
        slugged.append((slug, entry))
    return slugged
