"""
Internal regex patterns shared between richtext_parser and richtext_rewriter.

These patterns are tolerant of Salesforce rich-text quirks:
  - HTML-entity-encoded ampersands inside attribute values (&amp;)
  - 15- or 18-character Salesforce IDs (case-sensitive 15-char form, with
    optional uppercase 3-char checksum suffix for the 18-char form)
  - Lightning, Classic, and query-string-based record URL shapes
  - Salesforce-hosted domains across all the *.force.com / salesforce.com
    sub-realms that appear in rich text content

Internal — do not import from outside knowledge_article/.
"""

import re

# ---------------------------------------------------------------- #
#  Salesforce ID                                                    #
# ---------------------------------------------------------------- #

# 15-char body (case sensitive), optional 18-char checksum suffix
# (the 3-char checksum is restricted to uppercase A-Z and digits 0-5,
#  but we accept any uppercase alphanum digit for tolerance).
_SF_ID_BODY = r"[a-zA-Z0-9]{15}"
_SF_ID_CHECKSUM = r"[A-Z0-9]{3}"
SF_ID_RAW = rf"{_SF_ID_BODY}(?:{_SF_ID_CHECKSUM})?"

SF_ID_FULL_RE = re.compile(rf"^(?:{SF_ID_RAW})$")


# ---------------------------------------------------------------- #
#  Inline rich-text image (rtaImage)                                #
# ---------------------------------------------------------------- #
#
# Examples seen in the wild:
#   https://my-org.file.force.com/servlet/rtaImage?eid=ka1...&feoid=00N...&refid=0EM...
#   https://my-org.file.force.com/sfc/servlet/rtaImage?...
#   ...and the same URLs with &amp; instead of &.
#
# We capture the entire URL (`full`) and the query string (`query`).
RTA_IMAGE_RE = re.compile(
    r'(?P<full>https?://[^\s"\'<>]+?/(?:[a-zA-Z]+/)*?rtaImage\?(?P<query>[^"\'\s<>]+))',
    re.IGNORECASE,
)


# ---------------------------------------------------------------- #
#  Inline ContentVersion-based images (servlet.shepherd)            #
# ---------------------------------------------------------------- #
#
# Modern Lightning rich text editors embed images by uploading them
# as Salesforce Files (ContentVersion records) and referencing the
# version's download URL. We see two common shapes:
#
#   https://<host>/sfc/servlet.shepherd/version/download/<ContentVersionId>[?...]
#   https://<host>/sfc/servlet.shepherd/version/renditionDownload?rendition=...&versionId=<ContentVersionId>
#
# Both forms point at a ContentVersion 18-char Id (key prefix `068`).
# We capture the whole URL (`full`), the originating Salesforce host
# (`host`, no scheme), and the ContentVersion Id (`cvid`).
SHEPHERD_DOWNLOAD_IMAGE_RE = re.compile(
    r'(?P<full>https?://(?P<host>[^/\s"\'<>]+)'
    r'/sfc/servlet\.shepherd/version/download/'
    r'(?P<cvid>[a-zA-Z0-9]{15,18})'
    r'(?:[?&][^"\'\s<>]*)?)',
    re.IGNORECASE,
)

SHEPHERD_RENDITION_IMAGE_RE = re.compile(
    r'(?P<full>https?://(?P<host>[^/\s"\'<>]+)'
    r'/sfc/servlet\.shepherd/version/renditionDownload\?'
    r'(?P<query>[^"\'\s<>]+))',
    re.IGNORECASE,
)


# ---------------------------------------------------------------- #
#  href attribute                                                   #
# ---------------------------------------------------------------- #
#
# Match opening <a ...> tags and capture the href value verbatim.
# We use DOTALL so attribute values with line breaks still match.
HREF_RE = re.compile(
    r"<a\b[^>]*?\bhref\s*=\s*(?P<q>[\"'])(?P<href>.*?)(?P=q)",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------- #
#  Record-URL shapes (matched against the href value)               #
# ---------------------------------------------------------------- #

# Lightning: /lightning/r/<Object>/<Id>[/view|/edit|/related]
LIGHTNING_RECORD_RE = re.compile(
    rf"/lightning/r/(?P<object>[A-Za-z_][A-Za-z0-9_]*)/(?P<id>{SF_ID_RAW})(?:/[a-zA-Z]+)?",
    re.IGNORECASE,
)

# Classic absolute: https://<host>/<Id>[...]
CLASSIC_RECORD_RE = re.compile(
    rf"^https?://[^/\s]+/(?P<id>{SF_ID_RAW})(?:[/?#].*)?$",
    re.IGNORECASE,
)

# Query-string id (handles &amp; entity form too):
#   ?id=<Id>   &id=<Id>   &amp;id=<Id>
QUERY_ID_RE = re.compile(
    rf"[?&](?:amp;)?id=(?P<id>{SF_ID_RAW})",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- #
#  Salesforce-hosted domains                                        #
# ---------------------------------------------------------------- #
#
# Captures scheme + host so the rewriter can replace or strip the
# whole prefix in one substitution.
SF_HOST_RE = re.compile(
    r"https?://(?P<host>[a-z0-9.-]+\."
    r"(?:lightning\.force\.com"
    r"|my\.salesforce\.com"
    r"|file\.force\.com"
    r"|content\.force\.com"
    r"|visual\.force\.com"
    r"|salesforce\.com))",
    re.IGNORECASE,
)
