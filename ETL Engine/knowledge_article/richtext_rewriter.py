"""
Pure HTML rewriter for Salesforce rich-text content.

Applies up to three independent transformations to an HTML string in this order:
  1. rtaImage URLs       Rewritten to point at target Kav (post image rehost).
                          Per `on_missing_image` policy, the entire <img> tag
                          may be replaced with a placeholder when the image
                          has no rewrite mapping (e.g. source binary 404'd).
  2. <a href> record IDs  Source record IDs inside hrefs are mapped via IDMapper.
                          Per `on_missing_record` policy, unmapped hrefs are
                          left intact ("keep_broken_link") or counted ("fail")
                          and reported via stats.
  3. Salesforce hosts     mydomain hosts are replaced with `target_host` if
                          configured, otherwise stripped to relative URLs so
                          the browser resolves them on the target org.

Pure function. No Salesforce calls. Operates entirely on its inputs.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import parse_qs, urlencode

from knowledge_article._richtext_patterns import (
    CLASSIC_RECORD_RE,
    HREF_RE,
    LIGHTNING_RECORD_RE,
    QUERY_ID_RE,
    RTA_IMAGE_RE,
    SF_HOST_RE,
    SHEPHERD_DOWNLOAD_IMAGE_RE,
    SHEPHERD_RENDITION_IMAGE_RE,
)


_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)


class RewriteError(Exception):
    """Raised when a rewrite policy is set to 'fail' and a mapping is missing."""


@dataclass(frozen=True)
class ImageRewrite:
    """
    Replacement coordinates for a previously-uploaded inline image.

    Two kinds, distinguished by `kind`:

      * `kind="rta"` — rtaImage URLs (legacy richTextImageFields).
        `target_eid` / `target_feoid` / `target_refid` are required.
      * `kind="content_version"` — modern Salesforce Files-based images.
        `target_content_version_id` is required. The rewriter substitutes
        the source ContentVersionId in the existing servlet.shepherd URL
        and (optionally) replaces the source host.

    Older callers that passed only `target_eid/feoid/refid` still work —
    `kind` defaults to "rta".
    """
    target_eid: str = ""
    target_feoid: str = ""
    target_refid: str = ""
    kind: str = "rta"
    target_content_version_id: str = ""

    def build_url(self, base_path: str = "/sfc/servlet/rtaImage") -> str:
        """
        Produce a host-relative rtaImage URL for an `rta`-kind rewrite.
        Omits scheme + host so the same body renders on any target sandbox.
        Not meaningful for ContentVersion rewrites — those replace the
        ContentVersion Id in-place in the existing URL.
        """
        query = urlencode({
            "eid": self.target_eid,
            "feoid": self.target_feoid,
            "refid": self.target_refid,
        })
        return f"{base_path}?{query}"


@dataclass
class RewriteContext:
    """Inputs to a single HTML rewrite invocation. Mutate freely between calls."""
    # Map: source_refid -> ImageRewrite for rtaImage-style URLs.
    image_map: dict[str, ImageRewrite] = field(default_factory=dict)
    # Map: source ContentVersionId -> ImageRewrite for Salesforce Files
    # (servlet.shepherd) URLs. Keys are 15- or 18-char ContentVersionIds.
    content_version_image_map: dict[str, ImageRewrite] = field(default_factory=dict)
    # Map: source 15- or 18-char Salesforce ID -> target ID.
    id_map: dict[str, str] = field(default_factory=dict)
    # If set, replace SF mydomain hosts with this host (no scheme).
    # If None, strip scheme+host entirely so URLs become relative.
    target_host: str | None = None
    on_missing_image: str = "keep_broken_link"  # | "replace_with_placeholder" | "fail"
    on_missing_record: str = "keep_broken_link"  # | "fail"  (degrade_to_text deferred)
    placeholder_image_html: str = '<span class="rta-image-missing"></span>'
    # PR 3 staging flag. When True the rewriter leaves <img> tags entirely
    # alone — neither the rtaImage URL nor the SF host inside the tag is
    # touched. PR 4 sets this to False and uses image_map to do real
    # image rehosting alongside host normalization. The flag exists so
    # PR 3 can ship URL/host rewriting for record links without breaking
    # image rendering until image rehosting lands.
    preserve_image_urls: bool = False


@dataclass
class RewriteStats:
    images_rewritten: int = 0
    images_missing: int = 0
    records_rewritten: int = 0
    records_missing: int = 0
    hosts_rewritten: int = 0


# ---------------------------------------------------------------- #
#  Public API                                                       #
# ---------------------------------------------------------------- #

def rewrite(html: str | None, ctx: RewriteContext) -> tuple[str | None, RewriteStats]:
    """
    Rewrite an HTML string per the supplied context.
    Returns (rewritten_html, stats). For None input returns (None, empty stats).
    """
    stats = RewriteStats()
    if html is None:
        return None, stats
    if not html:
        return html, stats

    if not ctx.preserve_image_urls:
        html = _rewrite_images(html, ctx, stats)
    html = _rewrite_hrefs(html, ctx, stats)
    if ctx.preserve_image_urls:
        html = _rewrite_hosts_outside_images(html, ctx, stats)
    else:
        html = _rewrite_hosts(html, ctx, stats)
    return html, stats


# ---------------------------------------------------------------- #
#  Image rewrite                                                    #
# ---------------------------------------------------------------- #

def _rewrite_images(html: str, ctx: RewriteContext, stats: RewriteStats) -> str:
    def replace_img_tag(tag_match: re.Match) -> str:
        tag = tag_match.group(0)

        # 1. rtaImage (richTextImageFields-attached) URLs.
        url_match = RTA_IMAGE_RE.search(tag)
        if url_match:
            return _rewrite_rta_image_tag(tag, url_match, ctx, stats)

        # 2. ContentVersion-style download/rendition URLs.
        cv_match = SHEPHERD_DOWNLOAD_IMAGE_RE.search(tag) or \
            SHEPHERD_RENDITION_IMAGE_RE.search(tag)
        if cv_match:
            return _rewrite_content_version_image_tag(tag, cv_match, ctx, stats)

        # <img> tag with no recognised Salesforce image URL — leave alone.
        return tag

    return _IMG_TAG_RE.sub(replace_img_tag, html)


def _rewrite_rta_image_tag(
    tag: str, url_match: re.Match, ctx: RewriteContext, stats: RewriteStats,
) -> str:
    decoded_query = unescape(url_match.group("query"))
    params = parse_qs(decoded_query, keep_blank_values=False)
    source_refid = (params.get("refid", [""]) or [""])[0]

    rewrite = ctx.image_map.get(source_refid)
    if rewrite is None:
        return _on_missing_image(tag, source_refid, ctx, stats)

    new_url = rewrite.build_url()
    new_tag = tag.replace(url_match.group("full"), new_url, 1)
    stats.images_rewritten += 1
    return new_tag


def _rewrite_content_version_image_tag(
    tag: str, url_match: re.Match, ctx: RewriteContext, stats: RewriteStats,
) -> str:
    full_url = url_match.group("full")

    # Pull out the source ContentVersionId — either the `cvid` path
    # capture (download) or the `versionId` query param (renditionDownload).
    groups = url_match.groupdict()
    source_cvid = groups.get("cvid") or ""
    if not source_cvid:
        decoded_query = unescape(groups.get("query") or "")
        params = parse_qs(decoded_query, keep_blank_values=False)
        source_cvid = (params.get("versionId", [""]) or [""])[0]

    if not source_cvid:
        # Couldn't locate the source ContentVersionId — leave the tag
        # alone (don't risk corrupting the body).
        return tag

    rewrite = ctx.content_version_image_map.get(source_cvid)
    if rewrite is None and len(source_cvid) == 18:
        rewrite = ctx.content_version_image_map.get(source_cvid[:15])
    if rewrite is None and len(source_cvid) == 15:
        for key, value in ctx.content_version_image_map.items():
            if len(key) == 18 and key[:15] == source_cvid:
                rewrite = value
                break

    if rewrite is None:
        return _on_missing_image(tag, source_cvid, ctx, stats)

    target_cvid = rewrite.target_content_version_id
    if not target_cvid:
        return _on_missing_image(tag, source_cvid, ctx, stats)

    new_url = full_url.replace(source_cvid, target_cvid)
    # Strip the source mydomain so the rewritten URL is relative (or use
    # ctx.target_host if explicitly configured). This mirrors the host
    # treatment we apply outside <img> tags.
    new_url = _normalize_host_in_url(new_url, ctx)

    new_tag = tag.replace(full_url, new_url, 1)
    stats.images_rewritten += 1
    return new_tag


def _on_missing_image(
    tag: str, source_id: str, ctx: RewriteContext, stats: RewriteStats,
) -> str:
    stats.images_missing += 1
    policy = ctx.on_missing_image
    if policy == "replace_with_placeholder":
        return ctx.placeholder_image_html
    if policy == "fail":
        raise RewriteError(
            f"No image rewrite mapping for source id '{source_id}'"
        )
    return tag


def _normalize_host_in_url(url: str, ctx: RewriteContext) -> str:
    """Apply the configured target_host policy to a single URL."""
    def replace_host(_match: re.Match) -> str:
        if ctx.target_host:
            return f"https://{ctx.target_host}"
        return ""
    return SF_HOST_RE.sub(replace_host, url)


# ---------------------------------------------------------------- #
#  Href rewrite                                                     #
# ---------------------------------------------------------------- #

def _rewrite_hrefs(html: str, ctx: RewriteContext, stats: RewriteStats) -> str:
    def replace_a_tag(tag_match: re.Match) -> str:
        whole = tag_match.group(0)
        quote = tag_match.group("q")
        original_href = tag_match.group("href")

        new_href, found_id, was_rewritten = _rewrite_href_value(original_href, ctx)
        if found_id is None:
            return whole

        if was_rewritten:
            stats.records_rewritten += 1
        else:
            stats.records_missing += 1
            if ctx.on_missing_record == "fail":
                raise RewriteError(f"No ID mapping for source record '{found_id}'")

        if new_href == original_href:
            return whole

        return whole.replace(
            f"={quote}{original_href}{quote}",
            f"={quote}{new_href}{quote}",
            1,
        )

    return HREF_RE.sub(replace_a_tag, html)


def _rewrite_href_value(href: str, ctx: RewriteContext) -> tuple[str, str | None, bool]:
    """
    Try to substitute any record ID inside `href` using ctx.id_map.
    Operates on the original href (entities preserved); regex patterns are
    tolerant of HTML-encoded ampersands so we never need to decode/re-encode.

    Returns (new_href, source_id_found_or_None, was_rewritten).
    """
    lightning_match = LIGHTNING_RECORD_RE.search(href)
    if lightning_match:
        return _substitute_id(href, lightning_match, ctx)

    query_match = QUERY_ID_RE.search(href)
    if query_match:
        return _substitute_id(href, query_match, ctx)

    classic_match = CLASSIC_RECORD_RE.match(href)
    if classic_match:
        return _substitute_id(href, classic_match, ctx)

    return href, None, False


def _substitute_id(
    href: str, match: re.Match, ctx: RewriteContext
) -> tuple[str, str, bool]:
    source_id = match.group("id")
    target_id = ctx.id_map.get(source_id)
    if target_id is None and len(source_id) == 18:
        # Try the 15-char prefix in case the IDMapper keys in 15-char form.
        target_id = ctx.id_map.get(source_id[:15])
    if target_id is None and len(source_id) == 15:
        # Try matching against any 18-char key whose prefix matches.
        # Cheap linear scan only when the first lookup misses.
        for key, value in ctx.id_map.items():
            if len(key) == 18 and key[:15] == source_id:
                target_id = value
                break

    if target_id is None:
        return href, source_id, False

    new_href = href[: match.start("id")] + target_id + href[match.end("id") :]
    return new_href, source_id, True


# ---------------------------------------------------------------- #
#  Host rewrite                                                     #
# ---------------------------------------------------------------- #

def _rewrite_hosts(html: str, ctx: RewriteContext, stats: RewriteStats) -> str:
    def replace_host(_match: re.Match) -> str:
        stats.hosts_rewritten += 1
        if ctx.target_host:
            return f"https://{ctx.target_host}"
        return ""

    return SF_HOST_RE.sub(replace_host, html)


def _rewrite_hosts_outside_images(
    html: str, ctx: RewriteContext, stats: RewriteStats
) -> str:
    """
    Same as `_rewrite_hosts` but leaves any host that appears inside an
    <img> tag untouched. Used when `preserve_image_urls=True` so PR 3 can
    rewrite hrefs and SF mydomain hosts on regular content without breaking
    inline image references that PR 4 will rehost.
    """
    img_spans = [(m.start(), m.end()) for m in _IMG_TAG_RE.finditer(html)]
    if not img_spans:
        return _rewrite_hosts(html, ctx, stats)

    pieces: list[str] = []
    cursor = 0
    for start, end in img_spans:
        pieces.append(_rewrite_hosts(html[cursor:start], ctx, stats))
        pieces.append(html[start:end])  # leave <img...> untouched
        cursor = end
    pieces.append(_rewrite_hosts(html[cursor:], ctx, stats))
    return "".join(pieces)
