"""
Unit tests for knowledge_article.richtext_rewriter.

Run any of:
    python tests/test_richtext_rewriter.py
    python -m pytest tests/test_richtext_rewriter.py
"""

from __future__ import annotations

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from knowledge_article.richtext_rewriter import (
    ImageRewrite,
    RewriteContext,
    RewriteError,
    rewrite,
)


# ---------------------------------------------------------------- #
#  Empty / no-op inputs                                             #
# ---------------------------------------------------------------- #

def test_rewrite_none_returns_none():
    out, stats = rewrite(None, RewriteContext())
    assert out is None
    assert stats.images_rewritten == 0
    assert stats.records_rewritten == 0


def test_rewrite_empty_string_unchanged():
    out, stats = rewrite("", RewriteContext())
    assert out == ""


def test_rewrite_plain_html_unchanged():
    html = "<p>Nothing to see here.</p>"
    out, stats = rewrite(html, RewriteContext())
    assert out == html
    assert stats.images_rewritten == 0
    assert stats.records_rewritten == 0


# ---------------------------------------------------------------- #
#  Image rewrite                                                    #
# ---------------------------------------------------------------- #

def test_rewrite_image_with_known_mapping():
    html = (
        '<p><img src="https://x.file.force.com/servlet/rtaImage'
        '?eid=ka1B0000000XYZIAA0&feoid=00N1B00000ABCXYZ&refid=SOURCE_REFID01"></p>'
    )
    ctx = RewriteContext(
        image_map={
            "SOURCE_REFID01": ImageRewrite(
                target_eid="ka1T0000000ABCIAA0",
                target_feoid="00N1T00000DEFXYZ",
                target_refid="TARGET_REFID01",
            )
        }
    )
    out, stats = rewrite(html, ctx)
    assert "TARGET_REFID01" in out
    assert "SOURCE_REFID01" not in out
    assert "ka1T0000000ABCIAA0" in out
    assert stats.images_rewritten == 1
    assert stats.images_missing == 0


def test_rewrite_image_handles_amp_entity_in_src():
    html = (
        '<img src="https://x.file.force.com/servlet/rtaImage'
        '?eid=ka1B0000000XYZIAA0&amp;feoid=00N1B00000ABCXYZ'
        '&amp;refid=ENT_REFID">'
    )
    ctx = RewriteContext(
        image_map={
            "ENT_REFID": ImageRewrite(
                target_eid="ka1T",
                target_feoid="00NT",
                target_refid="NEW_REFID",
            )
        }
    )
    out, stats = rewrite(html, ctx)
    assert "NEW_REFID" in out
    assert "ENT_REFID" not in out
    assert stats.images_rewritten == 1


def test_rewrite_image_missing_keep_broken_link_default():
    # Default policy keeps the original image tag intact; the source SF host
    # is still stripped by the (independent) host-rewrite phase, but the
    # rtaImage params (eid/feoid/refid) are preserved so rerunning with a
    # populated image_map will rewrite cleanly.
    html = (
        '<img src="https://x.file.force.com/servlet/rtaImage'
        '?eid=A&feoid=B&refid=NO_MAPPING">'
    )
    out, stats = rewrite(html, RewriteContext())
    assert "<img" in out
    assert "rta-image-missing" not in out
    assert "NO_MAPPING" in out
    assert "refid=NO_MAPPING" in out
    assert stats.images_missing == 1
    assert stats.images_rewritten == 0


def test_rewrite_image_missing_replace_with_placeholder():
    html = (
        '<p>before<img src="https://x.file.force.com/servlet/rtaImage'
        '?eid=A&feoid=B&refid=NOPE">after</p>'
    )
    ctx = RewriteContext(on_missing_image="replace_with_placeholder")
    out, stats = rewrite(html, ctx)
    assert "<img" not in out
    assert "rta-image-missing" in out
    assert "before" in out and "after" in out
    assert stats.images_missing == 1


def test_rewrite_image_missing_fail_raises():
    html = (
        '<img src="https://x.file.force.com/servlet/rtaImage'
        '?eid=A&feoid=B&refid=NOPE">'
    )
    ctx = RewriteContext(on_missing_image="fail")
    raised = False
    try:
        rewrite(html, ctx)
    except RewriteError:
        raised = True
    assert raised, "Expected RewriteError when on_missing_image='fail'"


def test_rewrite_image_preserves_other_attributes():
    html = (
        '<img alt="Diagram" class="rta-image" '
        'src="https://x.file.force.com/servlet/rtaImage'
        '?eid=A&feoid=B&refid=R" data-extra="ok">'
    )
    ctx = RewriteContext(
        image_map={
            "R": ImageRewrite(target_eid="E", target_feoid="F", target_refid="NEW"),
        }
    )
    out, _ = rewrite(html, ctx)
    assert 'alt="Diagram"' in out
    assert 'class="rta-image"' in out
    assert 'data-extra="ok"' in out
    assert "NEW" in out


# ---------------------------------------------------------------- #
#  Record-link rewrite                                              #
# ---------------------------------------------------------------- #

def test_rewrite_lightning_record_link():
    html = (
        '<a href="https://my-org.lightning.force.com/lightning/r/Knowledge__kav/'
        'ka1B0000000XYZIAA0/view">KA</a>'
    )
    ctx = RewriteContext(
        id_map={"ka1B0000000XYZIAA0": "ka1T0000000ABCIAA0"},
    )
    out, stats = rewrite(html, ctx)
    assert "ka1T0000000ABCIAA0" in out
    assert "ka1B0000000XYZIAA0" not in out
    assert stats.records_rewritten == 1


def test_rewrite_record_link_missing_keep_broken_link_default():
    html = (
        '<a href="https://x.lightning.force.com/lightning/r/Account/'
        '001B0000003ABCDEFG/view">A</a>'
    )
    out, stats = rewrite(html, RewriteContext())
    # ID not in map; the lightning host is stripped (default), but the ID stays.
    assert "001B0000003ABCDEFG" in out
    assert stats.records_missing == 1


def test_rewrite_record_link_query_style():
    html = '<a href="/apex/MyPage?id=001B0000003ABCDEFG">old</a>'
    ctx = RewriteContext(id_map={"001B0000003ABCDEFG": "001T0000003ZZZWXYZ"})
    out, stats = rewrite(html, ctx)
    assert "001T0000003ZZZWXYZ" in out
    assert stats.records_rewritten == 1


def test_rewrite_record_link_query_style_amp_entity():
    html = '<a href="/apex/MyPage?foo=1&amp;id=001B0000003ABCDEFG">x</a>'
    ctx = RewriteContext(id_map={"001B0000003ABCDEFG": "001T_____TARGET___"})
    out, stats = rewrite(html, ctx)
    assert "001T_____TARGET___" in out
    assert stats.records_rewritten == 1


def test_rewrite_record_link_classic():
    html = '<a href="https://na123.salesforce.com/001B0000003ABCDEFG">classic</a>'
    ctx = RewriteContext(id_map={"001B0000003ABCDEFG": "001T_____TARGET___"})
    out, stats = rewrite(html, ctx)
    assert "001T_____TARGET___" in out
    assert stats.records_rewritten == 1


def test_rewrite_15_char_id_resolves_via_18_char_map_entry():
    # Pipeline IDMapper typically keys by 18-char IDs. A href may carry the
    # 15-char form. The rewriter should still resolve using the 15-char prefix.
    html = (
        '<a href="https://x.lightning.force.com/lightning/r/Account/'
        '001B0000003ABCD/view">A</a>'
    )
    ctx = RewriteContext(
        id_map={"001B0000003ABCDEFG": "001T0000003ZZZWXYZ"},
    )
    out, stats = rewrite(html, ctx)
    assert stats.records_rewritten == 1
    assert "001T0000003ZZZWXYZ" in out


def test_rewrite_18_char_id_resolves_via_15_char_map_entry():
    html = (
        '<a href="https://x.lightning.force.com/lightning/r/Account/'
        '001B0000003ABCDEFG/view">A</a>'
    )
    ctx = RewriteContext(
        id_map={"001B0000003ABCD": "001T0000003ZZZWXYZ"},
    )
    out, stats = rewrite(html, ctx)
    assert stats.records_rewritten == 1
    assert "001T0000003ZZZWXYZ" in out


def test_rewrite_record_link_missing_fail_raises():
    html = (
        '<a href="https://x.lightning.force.com/lightning/r/Account/'
        '001B0000003ABCDEFG/view">A</a>'
    )
    ctx = RewriteContext(on_missing_record="fail")
    raised = False
    try:
        rewrite(html, ctx)
    except RewriteError:
        raised = True
    assert raised


# ---------------------------------------------------------------- #
#  Host rewrite                                                     #
# ---------------------------------------------------------------- #

def test_rewrite_strips_sf_host_to_relative_by_default():
    html = (
        '<a href="https://my-org.lightning.force.com/lightning/r/Account/'
        '001B0000003ABCDEFG/view">A</a>'
    )
    ctx = RewriteContext(id_map={"001B0000003ABCDEFG": "001T_____TARGET___"})
    out, _ = rewrite(html, ctx)
    assert "https://my-org.lightning.force.com" not in out
    assert "/lightning/r/Account/001T_____TARGET___/view" in out


def test_rewrite_replaces_sf_host_with_target_host():
    html = (
        '<a href="https://src.lightning.force.com/lightning/r/Account/'
        '001B0000003ABCDEFG/view">A</a>'
    )
    ctx = RewriteContext(
        id_map={"001B0000003ABCDEFG": "001T_____TARGET___"},
        target_host="tgt.lightning.force.com",
    )
    out, stats = rewrite(html, ctx)
    assert "https://tgt.lightning.force.com" in out
    assert "src.lightning.force.com" not in out
    assert stats.hosts_rewritten >= 1


def test_rewrite_does_not_touch_non_sf_hosts():
    html = '<a href="https://example.com/foo">x</a>'
    out, stats = rewrite(html, RewriteContext())
    assert out == html
    assert stats.hosts_rewritten == 0


# ---------------------------------------------------------------- #
#  Combined / realistic                                             #
# ---------------------------------------------------------------- #

def test_rewrite_realistic_kav_body_full_pass():
    html = (
        '<p>See: <a href="https://src.lightning.force.com/lightning/r/Knowledge__kav/'
        'ka1B0000000XYZIAA0/view">related</a></p>'
        '<p><img src="https://src.file.force.com/servlet/rtaImage'
        '?eid=ka1B0000000ZZZIAA0&amp;feoid=00N1B00000ABCXYZ'
        '&amp;refid=IMG_REFID_1" alt="diag"></p>'
    )
    ctx = RewriteContext(
        id_map={"ka1B0000000XYZIAA0": "ka1T0000000ABCIAA0"},
        image_map={
            "IMG_REFID_1": ImageRewrite(
                target_eid="ka1T0000000ZZZIAA0",
                target_feoid="00N1T00000DEFXYZ",
                target_refid="NEW_IMG",
            )
        },
        target_host="tgt.lightning.force.com",
    )
    out, stats = rewrite(html, ctx)
    # Record ID rewritten
    assert "ka1T0000000ABCIAA0" in out
    assert "ka1B0000000XYZIAA0" not in out
    # Image refid rewritten
    assert "NEW_IMG" in out
    assert "IMG_REFID_1" not in out
    # Source host replaced with target host (for the record link)
    assert "tgt.lightning.force.com" in out
    assert "src.lightning.force.com" not in out
    # The image URL was rewritten to relative form (no host) — good
    assert "src.file.force.com" not in out
    assert stats.records_rewritten == 1
    assert stats.images_rewritten == 1


# ---------------------------------------------------------------- #
#  preserve_image_urls (PR 3 staging flag)                          #
# ---------------------------------------------------------------- #

def test_preserve_image_urls_skips_image_rewrite_with_known_mapping():
    """
    Even though image_map has a real mapping, the rewriter must leave the
    <img> tag entirely alone when preserve_image_urls=True. PR 4 disables
    the flag to actually rehost.
    """
    html = (
        '<p><img src="https://x.file.force.com/servlet/rtaImage'
        '?eid=ka1B0000000XYZIAA0&feoid=00N1B00000ABCXYZ&refid=SOURCE_REFID01"></p>'
    )
    ctx = RewriteContext(
        image_map={
            "SOURCE_REFID01": ImageRewrite(
                target_eid="ka1T0000000ABCIAA0",
                target_feoid="00N1T00000DEFXYZ",
                target_refid="TARGET_REFID01",
            )
        },
        preserve_image_urls=True,
    )
    out, stats = rewrite(html, ctx)
    assert out == html
    assert stats.images_rewritten == 0
    assert stats.images_missing == 0


def test_preserve_image_urls_does_not_strip_host_inside_img_tag():
    """
    Default host rewriting strips SF mydomain hosts. With preserve_image_urls=True
    the rewriter must NOT strip the host from URLs inside <img> tags so PR 4
    can still find and rehost them later.
    """
    html = (
        '<p><img src="https://my-org.file.force.com/servlet/rtaImage'
        '?eid=ka1B0000000XYZIAA0&refid=R1"></p>'
    )
    ctx = RewriteContext(preserve_image_urls=True)
    out, stats = rewrite(html, ctx)
    assert "my-org.file.force.com" in out
    assert stats.hosts_rewritten == 0


def test_preserve_image_urls_still_rewrites_record_links_and_outside_hosts():
    """
    The flag must only protect <img> tags. Plain <a href> record links
    and Salesforce hosts elsewhere in the body still get rewritten.
    """
    html = (
        '<p><a href="https://src.lightning.force.com/lightning/r/Knowledge__kav/'
        'ka1B0000000XYZIAA0/view">link</a></p>'
        '<p><img src="https://src.file.force.com/servlet/rtaImage'
        '?eid=ka1B0000000XYZIAA0&refid=R1"></p>'
    )
    ctx = RewriteContext(
        id_map={"ka1B0000000XYZIAA0": "ka1T0000000ABCIAA0"},
        target_host="tgt.lightning.force.com",
        preserve_image_urls=True,
    )
    out, stats = rewrite(html, ctx)
    # Record link rewritten + outside host replaced.
    assert "ka1T0000000ABCIAA0" in out
    assert "tgt.lightning.force.com" in out
    # Image URL host fully preserved.
    assert "src.file.force.com" in out
    assert "src.lightning.force.com" not in out
    assert stats.records_rewritten == 1
    # One host substitution (the <a href>); the image host is left alone.
    assert stats.hosts_rewritten == 1


def test_preserve_image_urls_handles_multiple_images_and_segments():
    """
    Sanity-check the segment-stitching code path with several <img> tags
    interleaved with rewriteable hosts.
    """
    html = (
        '<a href="https://src.lightning.force.com/x">A</a>'
        '<img src="https://src.file.force.com/servlet/rtaImage?refid=R1">'
        '<a href="https://src.lightning.force.com/y">B</a>'
        '<img src="https://src.file.force.com/servlet/rtaImage?refid=R2">'
    )
    ctx = RewriteContext(
        target_host="tgt.lightning.force.com",
        preserve_image_urls=True,
    )
    out, _ = rewrite(html, ctx)
    # Both <a> hosts rewritten to target.
    assert out.count("tgt.lightning.force.com") == 2
    # Both <img> hosts preserved.
    assert out.count("src.file.force.com") == 2


def test_preserve_image_urls_false_default_unchanged():
    """
    Default (preserve_image_urls=False) keeps the existing PR 1/PR 2 behaviour:
    image URLs without a mapping fall through `on_missing_image`.
    """
    html = '<p><img src="https://x.file.force.com/servlet/rtaImage?refid=R1"></p>'
    ctx = RewriteContext()  # preserve_image_urls defaults to False
    out, stats = rewrite(html, ctx)
    # No image_map means no rewrite, and default policy keeps the broken link.
    assert "<img" in out
    # Host gets stripped because preserve_image_urls is False.
    assert "x.file.force.com" not in out


# ---------------------------------------------------------------- #
#  ContentVersion (servlet.shepherd) image rewrite                  #
# ---------------------------------------------------------------- #

def test_rewrite_content_version_image_replaces_id_and_strips_host():
    html = (
        '<p><img src="https://src-org.my.salesforce.com'
        '/sfc/servlet.shepherd/version/download/068SOURCE000000001"></p>'
    )
    ctx = RewriteContext(
        content_version_image_map={
            "068SOURCE000000001": ImageRewrite(
                kind="content_version",
                target_content_version_id="068TARGET000000001",
            )
        }
    )
    out, stats = rewrite(html, ctx)
    assert "068TARGET000000001" in out
    assert "068SOURCE000000001" not in out
    assert "src-org.my.salesforce.com" not in out  # host stripped to relative
    assert stats.images_rewritten == 1
    assert stats.images_missing == 0


def test_rewrite_content_version_image_replaces_host_when_target_set():
    html = (
        '<img src="https://src-org.my.salesforce.com'
        '/sfc/servlet.shepherd/version/download/068SOURCE000000002">'
    )
    ctx = RewriteContext(
        content_version_image_map={
            "068SOURCE000000002": ImageRewrite(
                kind="content_version",
                target_content_version_id="068TARGET000000002",
            )
        },
        target_host="tgt-org.my.salesforce.com",
    )
    out, _ = rewrite(html, ctx)
    assert "https://tgt-org.my.salesforce.com" in out
    assert "src-org.my.salesforce.com" not in out
    assert "068TARGET000000002" in out


def test_rewrite_content_version_image_with_query_string_preserved():
    html = (
        '<img src="https://src-org.my.salesforce.com'
        '/sfc/servlet.shepherd/version/download/068SOURCE000000003?asPdf=false">'
    )
    ctx = RewriteContext(
        content_version_image_map={
            "068SOURCE000000003": ImageRewrite(
                kind="content_version",
                target_content_version_id="068TARGET000000003",
            )
        }
    )
    out, _ = rewrite(html, ctx)
    assert "068TARGET000000003?asPdf=false" in out


def test_rewrite_content_version_rendition_url():
    html = (
        '<img src="https://src-org.my.salesforce.com'
        '/sfc/servlet.shepherd/version/renditionDownload?'
        'rendition=THUMB720BY480&versionId=068SOURCE000000004">'
    )
    ctx = RewriteContext(
        content_version_image_map={
            "068SOURCE000000004": ImageRewrite(
                kind="content_version",
                target_content_version_id="068TARGET000000004",
            )
        }
    )
    out, stats = rewrite(html, ctx)
    assert "068TARGET000000004" in out
    assert "068SOURCE000000004" not in out
    assert stats.images_rewritten == 1


def test_rewrite_content_version_image_missing_keep_broken_link():
    html = (
        '<img src="https://src-org.my.salesforce.com'
        '/sfc/servlet.shepherd/version/download/068NOMATCH00000005">'
    )
    out, stats = rewrite(html, RewriteContext())
    # Default policy keeps the original tag, but the host gets stripped by
    # the host-rewrite phase because preserve_image_urls is False here.
    assert "<img" in out
    assert "068NOMATCH00000005" in out
    assert stats.images_missing == 1


def test_rewrite_content_version_image_missing_replace_with_placeholder():
    html = (
        '<p>before<img src="https://src-org.my.salesforce.com'
        '/sfc/servlet.shepherd/version/download/068NOMATCH00000006">after</p>'
    )
    ctx = RewriteContext(on_missing_image="replace_with_placeholder")
    out, stats = rewrite(html, ctx)
    assert "<img" not in out
    assert "rta-image-missing" in out
    assert "before" in out and "after" in out
    assert stats.images_missing == 1


def test_rewrite_mixed_rta_and_content_version_images():
    """Both URL families coexist in a single body and rewrite cleanly."""
    html = (
        '<img src="https://src-org.file.force.com/servlet/rtaImage'
        '?eid=ka1S0000000XYZIAA0&feoid=00N1S00000ABCXYZ&refid=RTA_REFID_1">'
        '<img src="https://src-org.my.salesforce.com'
        '/sfc/servlet.shepherd/version/download/068SOURCE000000007">'
    )
    ctx = RewriteContext(
        image_map={
            "RTA_REFID_1": ImageRewrite(
                target_eid="ka1T0000000ABCIAA0",
                target_feoid="00N1T00000DEFXYZ",
                target_refid="RTA_REFID_NEW",
            )
        },
        content_version_image_map={
            "068SOURCE000000007": ImageRewrite(
                kind="content_version",
                target_content_version_id="068TARGET000000007",
            )
        },
    )
    out, stats = rewrite(html, ctx)
    assert "RTA_REFID_NEW" in out
    assert "068TARGET000000007" in out
    assert stats.images_rewritten == 2


def test_preserve_image_urls_skips_content_version_image_too():
    """When preserve_image_urls=True the rewriter must leave shepherd URLs alone."""
    html = (
        '<img src="https://src-org.my.salesforce.com'
        '/sfc/servlet.shepherd/version/download/068SOURCE000000008">'
    )
    ctx = RewriteContext(
        content_version_image_map={
            "068SOURCE000000008": ImageRewrite(
                kind="content_version",
                target_content_version_id="068TARGET000000008",
            )
        },
        preserve_image_urls=True,
    )
    out, stats = rewrite(html, ctx)
    assert out == html
    assert stats.images_rewritten == 0


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
