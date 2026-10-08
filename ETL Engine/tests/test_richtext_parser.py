"""
Unit tests for knowledge_article.richtext_parser.

Run any of:
    python tests/test_richtext_parser.py
    python -m pytest tests/test_richtext_parser.py
"""

from __future__ import annotations

from tests._runner import ensure_project_root_on_path
ensure_project_root_on_path()

from knowledge_article.richtext_parser import (
    ImageRef,
    RecordLink,
    is_likely_sf_id,
    scan,
)


# ---------------------------------------------------------------- #
#  Empty / no-match inputs                                          #
# ---------------------------------------------------------------- #

def test_scan_none_returns_empty():
    result = scan(None)
    assert result.images == ()
    assert result.record_links == ()
    assert result.sf_hosts == ()
    assert result.is_empty


def test_scan_empty_string_returns_empty():
    result = scan("")
    assert result.is_empty


def test_scan_plain_text_no_matches():
    result = scan("<p>Hello world. No links here.</p>")
    assert result.is_empty


# ---------------------------------------------------------------- #
#  Inline image extraction                                          #
# ---------------------------------------------------------------- #

def test_scan_extracts_single_rta_image():
    html = (
        '<p>See: <img src="https://my-org.file.force.com/servlet/rtaImage'
        '?eid=ka1B0000000XYZIAA0&feoid=00N1B00000ABCXYZ&refid=0EM1B00000abcDEF" '
        'alt="picture"></p>'
    )
    result = scan(html)
    assert len(result.images) == 1
    img = result.images[0]
    assert img.eid == "ka1B0000000XYZIAA0"
    assert img.feoid == "00N1B00000ABCXYZ"
    assert img.refid == "0EM1B00000abcDEF"
    assert "rtaImage" in img.full_url


def test_scan_image_with_html_entity_ampersand():
    # Salesforce serialises rich text with &amp; in attribute values.
    html = (
        '<img src="https://my-org.file.force.com/servlet/rtaImage'
        '?eid=ka1B0000000XYZIAA0&amp;feoid=00N1B00000ABCXYZ'
        '&amp;refid=0EM1B00000abcDEF">'
    )
    result = scan(html)
    assert len(result.images) == 1
    assert result.images[0].refid == "0EM1B00000abcDEF"


def test_scan_image_param_order_independent():
    html = (
        '<img src="https://my-org.file.force.com/servlet/rtaImage'
        '?refid=0EM1B00000abcDEF&eid=ka1B0000000XYZIAA0&feoid=00N1B00000ABCXYZ">'
    )
    result = scan(html)
    assert len(result.images) == 1
    img = result.images[0]
    assert img.eid == "ka1B0000000XYZIAA0"
    assert img.refid == "0EM1B00000abcDEF"


def test_scan_extracts_multiple_distinct_images():
    html = (
        '<img src="https://x.file.force.com/servlet/rtaImage'
        '?eid=ka1B0000000XYZIAA0&feoid=00N1B00000ABCXYZ&refid=0EM1B00000a">'
        '<img src="https://x.file.force.com/servlet/rtaImage'
        '?eid=ka1B0000000XYZIAA0&feoid=00N1B00000ABCXYZ&refid=0EM1B00000b">'
    )
    result = scan(html)
    assert len(result.images) == 2
    refids = {img.refid for img in result.images}
    assert refids == {"0EM1B00000a", "0EM1B00000b"}


def test_scan_dedupes_repeated_image_references():
    same = (
        'https://x.file.force.com/servlet/rtaImage'
        '?eid=ka1B0000000XYZIAA0&feoid=00N1B00000ABCXYZ&refid=0EM1B00000a'
    )
    html = f'<img src="{same}"><p>text</p><img src="{same}">'
    result = scan(html)
    assert len(result.images) == 1


def test_scan_ignores_non_rta_image_tags():
    html = '<img src="https://example.com/cat.jpg" alt="cat">'
    result = scan(html)
    assert result.images == ()


def test_scan_image_handles_sfc_servlet_path_variant():
    # Salesforce sometimes routes through /sfc/servlet/rtaImage instead of /servlet/rtaImage.
    html = (
        '<img src="https://my-org.file.force.com/sfc/servlet/rtaImage'
        '?eid=ka1B0000000XYZIAA0&feoid=00N1B00000ABCXYZ&refid=0EM1B00000Z">'
    )
    result = scan(html)
    assert len(result.images) == 1
    assert result.images[0].refid == "0EM1B00000Z"


# ---------------------------------------------------------------- #
#  ContentVersion (servlet.shepherd) image extraction               #
# ---------------------------------------------------------------- #

def test_scan_extracts_shepherd_download_image():
    html = (
        '<p>Embedded:'
        '<img src="https://quest-crm--uat.sandbox.my.salesforce.com'
        '/sfc/servlet.shepherd/version/download/068UE000008uUrdYAE"'
        ' alt="diagram"></p>'
    )
    result = scan(html)
    assert len(result.images) == 1
    img = result.images[0]
    assert img.kind == "content_version"
    assert img.content_version_id == "068UE000008uUrdYAE"
    assert img.host == "quest-crm--uat.sandbox.my.salesforce.com"
    assert "servlet.shepherd/version/download" in img.full_url


def test_scan_extracts_shepherd_download_with_query_string():
    html = (
        '<img src="https://my-org.my.salesforce.com'
        '/sfc/servlet.shepherd/version/download/0681700000abcDEFG?asPdf=false'
        '&operationContext=CHATTER">'
    )
    result = scan(html)
    assert len(result.images) == 1
    img = result.images[0]
    assert img.kind == "content_version"
    assert img.content_version_id == "0681700000abcDEFG"


def test_scan_extracts_shepherd_rendition_download_image():
    html = (
        '<img src="https://my-org.my.salesforce.com'
        '/sfc/servlet.shepherd/version/renditionDownload?'
        'rendition=THUMB720BY480&versionId=0681700000abcDEFG">'
    )
    result = scan(html)
    assert len(result.images) == 1
    img = result.images[0]
    assert img.kind == "content_version"
    assert img.content_version_id == "0681700000abcDEFG"


def test_scan_dedupes_repeated_shepherd_images():
    same = (
        'https://my-org.my.salesforce.com'
        '/sfc/servlet.shepherd/version/download/0681700000abcDEFG'
    )
    html = f'<img src="{same}"><p>x</p><img src="{same}">'
    result = scan(html)
    assert len(result.images) == 1


def test_scan_mixes_rta_and_content_version_images():
    html = (
        '<img src="https://my-org.file.force.com/servlet/rtaImage'
        '?eid=ka1B0000000XYZIAA0&feoid=00N1B00000ABCXYZ&refid=0EM1B00000a">'
        '<img src="https://my-org.my.salesforce.com'
        '/sfc/servlet.shepherd/version/download/0681700000abcDEFG">'
    )
    result = scan(html)
    assert len(result.images) == 2
    kinds = {img.kind for img in result.images}
    assert kinds == {"rta", "content_version"}


# ---------------------------------------------------------------- #
#  Record-link extraction                                           #
# ---------------------------------------------------------------- #

def test_scan_extracts_lightning_record_link():
    html = (
        '<a href="https://my-org.lightning.force.com/lightning/r/Knowledge__kav/'
        'ka1B0000000XYZIAA0/view">Related KA</a>'
    )
    result = scan(html)
    assert len(result.record_links) == 1
    link = result.record_links[0]
    assert link.record_id == "ka1B0000000XYZIAA0"
    assert link.link_kind == "lightning"
    assert link.object_name == "Knowledge__kav"


def test_scan_extracts_lightning_link_without_view_suffix():
    html = (
        '<a href="https://x.lightning.force.com/lightning/r/Account/'
        '001B0000003ABCDEFG/edit">Acc</a>'
    )
    result = scan(html)
    assert len(result.record_links) == 1
    assert result.record_links[0].object_name == "Account"


def test_scan_extracts_query_style_record_link():
    html = '<a href="/apex/MyPage?id=001B0000003ABCDEFG">Old style</a>'
    result = scan(html)
    assert len(result.record_links) == 1
    link = result.record_links[0]
    assert link.link_kind == "query"
    assert link.record_id == "001B0000003ABCDEFG"


def test_scan_extracts_query_style_with_amp_entity():
    html = '<a href="/apex/MyPage?foo=1&amp;id=001B0000003ABCDEFG">x</a>'
    result = scan(html)
    assert len(result.record_links) == 1
    assert result.record_links[0].record_id == "001B0000003ABCDEFG"


def test_scan_extracts_classic_record_link():
    html = '<a href="https://na123.salesforce.com/001B0000003ABCDEFG">Classic</a>'
    result = scan(html)
    assert len(result.record_links) == 1
    assert result.record_links[0].link_kind == "classic"
    assert result.record_links[0].record_id == "001B0000003ABCDEFG"


def test_scan_ignores_external_and_mailto_links():
    html = (
        '<a href="https://google.com">External</a>'
        '<a href="mailto:foo@bar.com">Email</a>'
        '<a href="/articles/Knowledge/some-url-name">UrlName-based</a>'
        '<a href="#anchor">Anchor</a>'
    )
    result = scan(html)
    assert result.record_links == ()


def test_scan_dedupes_repeated_record_links():
    html = (
        '<a href="https://x.lightning.force.com/lightning/r/Account/001B0000003ABCDEFG/view">a</a>'
        '<a href="https://x.lightning.force.com/lightning/r/Account/001B0000003ABCDEFG/view">b</a>'
    )
    result = scan(html)
    assert len(result.record_links) == 1


# ---------------------------------------------------------------- #
#  Salesforce host extraction                                       #
# ---------------------------------------------------------------- #

def test_scan_extracts_sf_hosts_uniquely():
    html = (
        '<img src="https://my-org.file.force.com/servlet/rtaImage'
        '?eid=ka1B0000000XYZIAA0&feoid=A&refid=B">'
        '<a href="https://my-org.lightning.force.com/lightning/r/Account/001B0000003ABCDEFG/view">x</a>'
        '<a href="https://my-org.lightning.force.com/lightning/r/Case/500B0000003ABCDEFG/view">y</a>'
    )
    result = scan(html)
    hosts = set(result.sf_hosts)
    assert "my-org.file.force.com" in hosts
    assert "my-org.lightning.force.com" in hosts
    # uniqued
    assert len(result.sf_hosts) == len(hosts)


def test_scan_ignores_non_sf_hosts():
    html = '<a href="https://example.com/foo">x</a>'
    result = scan(html)
    assert result.sf_hosts == ()


# ---------------------------------------------------------------- #
#  is_likely_sf_id                                                  #
# ---------------------------------------------------------------- #

def test_is_likely_sf_id_15_char():
    assert is_likely_sf_id("001B0000003ABCD")


def test_is_likely_sf_id_18_char():
    assert is_likely_sf_id("001B0000003ABCDEFG")


def test_is_likely_sf_id_rejects_short_id():
    assert not is_likely_sf_id("001B0000003")


def test_is_likely_sf_id_rejects_empty_and_non_string():
    assert not is_likely_sf_id("")
    assert not is_likely_sf_id(None)


def test_is_likely_sf_id_rejects_obvious_words():
    assert not is_likely_sf_id("hello world!!")


# ---------------------------------------------------------------- #
#  Combined / realistic body                                        #
# ---------------------------------------------------------------- #

def test_scan_realistic_kav_body():
    html = """
    <p>For details see
       <a href="https://my-org.lightning.force.com/lightning/r/Knowledge__kav/ka1B0000000XYZIAA0/view">this article</a>.
    </p>
    <p>Diagram:
       <img src="https://my-org.file.force.com/servlet/rtaImage?eid=ka1B0000000ZZZIAA0&amp;feoid=00N1B00000ABCXYZ&amp;refid=0EM1B00000img1" alt="diagram">
    </p>
    <p>Plain link: <a href="https://example.com">elsewhere</a></p>
    """
    result = scan(html)
    assert len(result.images) == 1
    assert result.images[0].refid == "0EM1B00000img1"
    assert len(result.record_links) == 1
    assert result.record_links[0].record_id == "ka1B0000000XYZIAA0"
    assert "my-org.lightning.force.com" in result.sf_hosts
    assert "my-org.file.force.com" in result.sf_hosts


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
