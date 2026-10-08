"""
Pure HTML scanner for Salesforce rich-text content.

Extracts:
  - Inline image refs (rtaImage URLs with eid/feoid/refid params)
  - Record-style hyperlinks (Lightning, Classic, query-string IDs)
  - Salesforce mydomain host references found anywhere in the HTML

Pure functions only. No Salesforce calls. No state. No I/O.
Designed to be deterministic, regex-based, and tolerant of HTML-encoded
entities in attribute values (&amp; inside hrefs and image URLs).
"""

from __future__ import annotations
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs

from knowledge_article._richtext_patterns import (
    CLASSIC_RECORD_RE,
    HREF_RE,
    LIGHTNING_RECORD_RE,
    QUERY_ID_RE,
    RTA_IMAGE_RE,
    SF_HOST_RE,
    SF_ID_FULL_RE,
    SHEPHERD_DOWNLOAD_IMAGE_RE,
    SHEPHERD_RENDITION_IMAGE_RE,
)


# ---------------------------------------------------------------- #
#  Data classes                                                     #
# ---------------------------------------------------------------- #

@dataclass(frozen=True)
class ImageRef:
    """
    A single inline rich-text image reference parsed from HTML.

    Salesforce supports two embedding mechanisms inside rich-text fields:

      * `kind="rta"`        rtaImage URL (richTextImageFields-attached image).
                            Carries `eid` (article id), `feoid` (field id),
                            `refid` (per-upload id).
      * `kind="content_version"`  servlet.shepherd download URL pointing at a
                            ContentVersion record (Salesforce File). Carries
                            `content_version_id`. The host is also captured
                            so the rewriter can swap the source mydomain.

    Older code only knew about `rta`, so that remains the default for
    positional construction. New consumers should branch on `kind`.
    """
    eid: str = ""              # source Kav Id (rta only)
    feoid: str = ""            # field-related encoded id (rta only)
    refid: str = ""            # the image refid (rta only)
    full_url: str = ""         # original URL as found in the HTML
    kind: str = "rta"          # "rta" | "content_version"
    content_version_id: str = ""  # source ContentVersion 18-char Id (cv only)
    host: str = ""             # source Salesforce host (no scheme), if known


@dataclass(frozen=True)
class RecordLink:
    """A single hyperlink whose URL contains a Salesforce record ID."""
    href: str          # original href attribute value (entities preserved)
    record_id: str     # extracted source SF ID (15- or 18-char form)
    link_kind: str     # "lightning" | "classic" | "query"
    object_name: str | None  # parsed object name for lightning links, else None


@dataclass(frozen=True)
class RichTextScanResult:
    images: tuple[ImageRef, ...]
    record_links: tuple[RecordLink, ...]
    sf_hosts: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.images or self.record_links or self.sf_hosts)


# ---------------------------------------------------------------- #
#  Public API                                                       #
# ---------------------------------------------------------------- #

def scan(html: str | None) -> RichTextScanResult:
    """
    Scan a single HTML string and return all rich-text references found.
    Returns empty tuples for None / empty input.
    """
    if not html:
        return RichTextScanResult(images=(), record_links=(), sf_hosts=())

    return RichTextScanResult(
        images=tuple(_extract_image_refs(html)),
        record_links=tuple(_extract_record_links(html)),
        sf_hosts=tuple(_extract_sf_hosts(html)),
    )


def is_likely_sf_id(value: str | None) -> bool:
    """True if `value` looks like a 15- or 18-char Salesforce record ID."""
    return bool(value) and bool(SF_ID_FULL_RE.match(value))


# ---------------------------------------------------------------- #
#  Extractors                                                       #
# ---------------------------------------------------------------- #

def _extract_image_refs(html: str) -> list[ImageRef]:
    refs: list[ImageRef] = []
    seen_rta: set[tuple[str, str, str]] = set()
    seen_cv: set[tuple[str, str]] = set()  # (full_url, content_version_id)

    # rtaImage (richTextImageFields-attached) URLs.
    for match in RTA_IMAGE_RE.finditer(html):
        full_url = match.group("full")
        decoded_query = unescape(match.group("query"))
        params = parse_qs(decoded_query, keep_blank_values=False)
        eid = _first_param(params, "eid")
        feoid = _first_param(params, "feoid")
        refid = _first_param(params, "refid")
        if not (eid and refid):
            continue
        key = (eid, feoid, refid)
        if key in seen_rta:
            continue
        seen_rta.add(key)
        refs.append(ImageRef(
            eid=eid, feoid=feoid, refid=refid,
            full_url=full_url, kind="rta",
        ))

    # ContentVersion-style: /sfc/servlet.shepherd/version/download/<cvid>
    for match in SHEPHERD_DOWNLOAD_IMAGE_RE.finditer(html):
        full_url = match.group("full")
        host = (match.group("host") or "").lower()
        cvid = match.group("cvid")
        if not cvid:
            continue
        key = (full_url, cvid)
        if key in seen_cv:
            continue
        seen_cv.add(key)
        refs.append(ImageRef(
            full_url=full_url,
            kind="content_version",
            content_version_id=cvid,
            host=host,
        ))

    # ContentVersion-style: /sfc/servlet.shepherd/version/renditionDownload?versionId=<cvid>
    for match in SHEPHERD_RENDITION_IMAGE_RE.finditer(html):
        full_url = match.group("full")
        host = (match.group("host") or "").lower()
        decoded_query = unescape(match.group("query"))
        params = parse_qs(decoded_query, keep_blank_values=False)
        cvid = _first_param(params, "versionId")
        if not cvid:
            continue
        key = (full_url, cvid)
        if key in seen_cv:
            continue
        seen_cv.add(key)
        refs.append(ImageRef(
            full_url=full_url,
            kind="content_version",
            content_version_id=cvid,
            host=host,
        ))
    return refs


def _first_param(params: dict[str, list[str]], name: str) -> str:
    values = params.get(name) or []
    return values[0] if values else ""


def _extract_record_links(html: str) -> list[RecordLink]:
    links: list[RecordLink] = []
    seen: set[tuple[str, str]] = set()
    for match in HREF_RE.finditer(html):
        # Decode entity-encoded ampersands so URL parsing patterns match
        # both '&' and '&amp;' forms uniformly.
        href = unescape(match.group("href")).strip()
        if not href:
            continue

        link = _classify_href(href)
        if link is None:
            continue

        key = (link.record_id, link.link_kind)
        if key in seen:
            continue
        seen.add(key)
        links.append(link)
    return links


def _classify_href(href: str) -> RecordLink | None:
    lightning_match = LIGHTNING_RECORD_RE.search(href)
    if lightning_match:
        return RecordLink(
            href=href,
            record_id=lightning_match.group("id"),
            link_kind="lightning",
            object_name=lightning_match.group("object"),
        )

    query_match = QUERY_ID_RE.search(href)
    if query_match:
        return RecordLink(
            href=href,
            record_id=query_match.group("id"),
            link_kind="query",
            object_name=None,
        )

    classic_match = CLASSIC_RECORD_RE.match(href)
    if classic_match:
        return RecordLink(
            href=href,
            record_id=classic_match.group("id"),
            link_kind="classic",
            object_name=None,
        )

    return None


def _extract_sf_hosts(html: str) -> list[str]:
    seen: set[str] = set()
    hosts: list[str] = []
    for match in SF_HOST_RE.finditer(html):
        host = match.group("host").lower()
        if host in seen:
            continue
        seen.add(host)
        hosts.append(host)
    return hosts
