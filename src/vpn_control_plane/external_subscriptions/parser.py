from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from urllib.parse import unquote


@dataclass(frozen=True)
class ParsedSubscriptionEntry:
    """A single share link from an upstream subscription feed.

    ``uri`` has the ``#label`` fragment stripped so the control plane can attach its own
    friendly label later; ``label`` is the decoded fragment used for matching.
    """

    uri: str
    label: str


def parse_subscription_body(body: str) -> list[ParsedSubscriptionEntry]:
    """Parse a subscription response into entries.

    Feeds are served either as plain newline-separated links or as a single base64 blob of
    the same. We detect the encoding by looking for a scheme; if none is present we attempt a
    base64 decode before giving up.
    """
    text = _maybe_base64_decode(body)
    entries: list[ParsedSubscriptionEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if "://" not in line:
            continue
        uri, separator, fragment = line.partition("#")
        uri = uri.strip()
        if not uri:
            continue
        label = unquote(fragment).strip() if separator else ""
        entries.append(ParsedSubscriptionEntry(uri=uri, label=label))
    return entries


def _maybe_base64_decode(body: str) -> str:
    text = body.strip()
    if not text or "://" in text:
        return text
    compact = "".join(text.split())
    padded = compact + "=" * (-len(compact) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return text
    return decoded if "://" in decoded else text
