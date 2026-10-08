"""
Salesforce key-prefix utilities.

Every Salesforce object has a 3-character `keyPrefix` (e.g. ``001`` for
Account, ``ka0`` for Knowledge__ka, ``kA0`` for Knowledge__kav). The first
three characters of any record ID identify which object the record
belongs to.

Used by scope expansion to classify incoming `RecordLink.record_id`
values when the URL does not carry an object name (classic and
query-style links).

These helpers are pure aside from one Salesforce describe-global call
on first use; results are cached on the connector by SFConnector itself.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connector import SFConnector


def build_keyprefix_index(connector: "SFConnector") -> dict[str, str]:
    """
    Return a dict mapping 3-character Salesforce key prefixes to the
    canonical object API name.

    Notes:
      * `describe_global` results are cached on the connector, so calling
        this function multiple times in a single run is cheap.
      * Some prefixes can in theory be shared (e.g. between custom
        metadata types). First-write-wins; this is fine for our use
        because we only ever care about *standard* and *custom object*
        prefixes that Salesforce guarantees unique.
      * Sobjects without a key prefix (custom metadata, abstract objects
        like Name, etc.) are skipped.
    """
    index: dict[str, str] = {}
    for sobject in connector.describe_global():
        prefix = sobject.get("keyPrefix")
        api_name = sobject.get("name")
        if not prefix or not api_name:
            continue
        index.setdefault(prefix, api_name)
    return index


def object_for_id(record_id: str | None, prefix_index: dict[str, str]) -> str | None:
    """
    Resolve a Salesforce record ID to its object API name using a
    pre-built `prefix_index`. Returns None for unknown / malformed input.
    """
    if not record_id or len(record_id) < 3:
        return None
    return prefix_index.get(record_id[:3])
