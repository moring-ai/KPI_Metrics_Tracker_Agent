"""Joining blog authors to team members.

The blog's JSON-LD carries each author's LinkedIn profile URL in
`author.sameAs`, and that handle is the same key the members file uses. So the
join is an exact string comparison and there is no fuzzy name matching, no
LLM, and no nondeterminism in a number the CTO reads.

The one catch is that the URLs are not written consistently -- four of the five
authors' URLs end in a slash and one does not -- so everything goes through
normalise() before it is compared or stored.
"""

from __future__ import annotations

import re

_PROFILE_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)

# A post permalink embeds its author's handle before the underscore:
#   linkedin.com/posts/ashvath-narayanan_some-slug-activity-7054...-iH3M
_POST_AUTHOR_RE = re.compile(r"linkedin\.com/posts/([^_/?#]+)_", re.IGNORECASE)


def normalise(handle: str | None) -> str | None:
    """Reduce anything that identifies a person to one canonical handle.

    Accepts a bare handle or a full profile URL, with or without a trailing
    slash, protocol, www, or ?trk= tracking parameters.

        >>> normalise("https://www.linkedin.com/in/Ashvath-Narayanan/")
        'ashvath-narayanan'
        >>> normalise("ashvath-narayanan")
        'ashvath-narayanan'
    """
    if not handle:
        return None

    text = handle.strip()
    if not text:
        return None

    match = _PROFILE_RE.search(text)
    if match:
        text = match.group(1)
    elif "/" in text or "linkedin.com" in text.lower():
        # URL-shaped but not a profile URL. Returning the whole URL as a
        # "handle" would silently poison every join, so refuse instead.
        return None

    text = text.split("?")[0].split("#")[0].strip("/").lower()
    # A LinkedIn vanity handle never contains whitespace. Anything that does
    # is a typo or a display name in the wrong field, and accepting it would
    # create a join key that silently matches nothing.
    if not text or any(character.isspace() for character in text):
        return None
    return text


def handle_from_post_url(url: str | None) -> str | None:
    """Whose post is this? Read the author handle out of the permalink.

    Post permalinks carry the author handle themselves, which means we can
    attribute a post without trusting a vendor-specific author field.
    """
    match = _POST_AUTHOR_RE.search(url or "")
    return normalise(match.group(1)) if match else None


def blog_key(value: str | None) -> str | None:
    """Canonical form of a blog-author identity.

    The Moring blog identifies an author three different ways -- a display
    name ("Balaji Nagaraj"), an author-page path ("/authors/balaji-nagaraj"),
    and a LinkedIn URL. The first two collapse onto the same string once you
    lowercase and hyphenate, which lets one field in members.json match either.

        >>> blog_key("Balaji Nagaraj")
        'balaji-nagaraj'
        >>> blog_key("/authors/balaji-nagaraj")
        'balaji-nagaraj'
    """
    if not value:
        return None
    text = value.strip().lower()
    text = re.sub(r"^/?authors?/", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or None


def first_linkedin_url(same_as: object) -> str | None:
    """Pick the LinkedIn URL out of a JSON-LD `author.sameAs` value.

    sameAs is a list whose second element is an empty string on every post we
    have seen, so indexing [0] blindly is fragile and iterating it naively
    yields junk. It is also occasionally a bare string rather than a list.
    """
    candidates = [same_as] if isinstance(same_as, str) else list(same_as or [])
    for candidate in candidates:
        if isinstance(candidate, str) and "linkedin.com/in/" in candidate.lower():
            return candidate
    return None
