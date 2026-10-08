"""
Per-object configuration for the Knowledge Article extension.

Read from a template's `extensions.<ObjectName>` section. Defaults
match the agreed-upon plan:
  - scope expansion enabled, depth=3, cap=5000, KA-only by default
  - (no extra SOQL filters in code — use pipeline query_filter for WHERE/LIMIT)
  - missing referenced records: degrade to plain text
  - missing source images: replace with placeholder span
  - upsert behaviour: always overwrite (matches existing pipeline default)
  - manifest verification: off by default (fast steady-state)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


_DEFAULT_RICHTEXT_FIELDS: tuple[str, ...] = ("Article_Body__c", "Summary")


@dataclass(frozen=True)
class ScopeExpansionConfig:
    enabled: bool = True
    max_depth: int = 3
    max_added_records: int = 5_000
    in_scope_objects: tuple[str, ...] = ("Knowledge__kav",)
    respect_seed_filter: bool = True
    on_excluded_reference: str = "degrade_to_text"  # | "keep_broken_link" | "fail"


@dataclass(frozen=True)
class ImageHandlingConfig:
    enabled: bool = True
    on_missing_image: str = "replace_with_placeholder"  # | "keep_broken_link" | "fail"
    placeholder_html: str = '<span class="rta-image-missing"></span>'
    verify_cached_images: bool = False
    manifest_path: str | None = None  # default resolved at runtime under state/


@dataclass(frozen=True)
class UrlRewritingConfig:
    """
    Controls the post_load URL rewrite step (PR 3).
    Independent of image handling — record-link / host rewriting can ship
    on its own, leaving images for PR 4.
    """
    enabled: bool = True
    # What to do when a record link inside an article body has no IDMapper
    # entry — typically because the referenced record was out-of-scope or
    # failed to load. Mirrors the rewriter's `on_missing_record` knob.
    on_missing_record: str = "keep_broken_link"  # | "fail" | "degrade_to_text"


@dataclass(frozen=True)
class PublishingConfig:
    """
    Controls the post_load publish step (PR 5).

    Loaded Knowledge articles land in the target as `Draft`. The publisher
    flips them back to `Online` so they're visible to end users in the
    target org. Enabled by default until product-level config exists; set
    ``enabled: false`` under ``publishing`` to skip the Apex publish step.
    """
    enabled: bool = True
    # Articles per executeAnonymous call. Each statement is ~70 chars and
    # the anonymous Apex limit is ~32 KB, so 200 keeps us well under.
    batch_size: int = 200
    # Only publish articles whose source row had this PublishStatus.
    # Set to None to publish every loaded article regardless of source state.
    only_if_source_status: str | None = "Online"
    # If True, KbManagement.publishArticle is called with flagAsNewVersion=true
    # so the target gets a new version per publish. Default false matches
    # "republish in place" semantics.
    flag_as_new_version: bool = False
    # If True, an executeAnonymous batch failure triggers a per-id retry to
    # localise the specific article(s) that broke. False keeps things fast
    # at the cost of less granular reporting.
    retry_failed_batches_individually: bool = True


@dataclass(frozen=True)
class KnowledgeArticleConfig:
    richtext_fields: tuple[str, ...] = _DEFAULT_RICHTEXT_FIELDS
    upsert_existing: str = "always"  # | "skip_if_present" | "skip_if_unchanged"
    target_host: str | None = None  # if set, replace SF mydomain hosts; else strip to relative
    scope_expansion: ScopeExpansionConfig = field(default_factory=ScopeExpansionConfig)
    url_rewriting: UrlRewritingConfig = field(default_factory=UrlRewritingConfig)
    image_handling: ImageHandlingConfig = field(default_factory=ImageHandlingConfig)
    publishing: PublishingConfig = field(default_factory=PublishingConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "KnowledgeArticleConfig":
        if not data:
            return cls()

        scope_data = data.get("scope_expansion") or {}
        url_data = data.get("url_rewriting") or {}
        image_data = data.get("image_handling") or {}
        publishing_data = data.get("publishing") or {}

        scope = ScopeExpansionConfig(
            enabled=bool(scope_data.get("enabled", True)),
            max_depth=int(scope_data.get("max_depth", 3)),
            max_added_records=int(scope_data.get("max_added_records", 5_000)),
            in_scope_objects=tuple(scope_data.get("in_scope_objects", ("Knowledge__kav",))),
            respect_seed_filter=bool(scope_data.get("respect_seed_filter", True)),
            on_excluded_reference=str(scope_data.get("on_excluded_reference", "degrade_to_text")),
        )

        url_rewriting = UrlRewritingConfig(
            enabled=bool(url_data.get("enabled", True)),
            on_missing_record=str(url_data.get("on_missing_record", "keep_broken_link")),
        )

        image = ImageHandlingConfig(
            enabled=bool(image_data.get("enabled", True)),
            on_missing_image=str(image_data.get("on_missing_image", "replace_with_placeholder")),
            placeholder_html=str(image_data.get("placeholder_html", '<span class="rta-image-missing"></span>')),
            verify_cached_images=bool(image_data.get("verify_cached_images", False)),
            manifest_path=image_data.get("manifest_path"),
        )

        publishing = PublishingConfig(
            enabled=bool(publishing_data.get("enabled", True)),
            batch_size=int(publishing_data.get("batch_size", 200)),
            only_if_source_status=publishing_data.get("only_if_source_status", "Online"),
            flag_as_new_version=bool(publishing_data.get("flag_as_new_version", False)),
            retry_failed_batches_individually=bool(
                publishing_data.get("retry_failed_batches_individually", True)
            ),
        )

        return cls(
            richtext_fields=tuple(data.get("richtext_fields", _DEFAULT_RICHTEXT_FIELDS)),
            upsert_existing=str(data.get("upsert_existing", "always")),
            target_host=data.get("target_host"),
            scope_expansion=scope,
            url_rewriting=url_rewriting,
            image_handling=image,
            publishing=publishing,
        )
