import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from knowledge_article.policy import compile_knowledge_policy


def test_source_knowledge_policy_reuses_full_extension_defaults():
    objects = [{"name": "Knowledge__kav", "fields": ["Id", "Title"]}]
    compiled = compile_knowledge_policy(objects, "salesforce_source")
    extensions = compiled[0]["extensions"]
    assert extensions["scope_expansion"]["enabled"] is True
    assert extensions["image_handling"]["enabled"] is True
    assert extensions["url_rewriting"]["enabled"] is True
    assert extensions["publishing"]["only_if_source_status"] == "Online"
    assert compiled[0]["fields"] == ["Id", "Title", "PublishStatus", "Article_Body__c", "Summary"]
    assert "extensions" not in objects[0]


def test_flat_knowledge_policy_disables_source_dependent_steps():
    compiled = compile_knowledge_policy([{"name": "Knowledge__kav"}], "flat_file")
    extensions = compiled[0]["extensions"]
    assert extensions["scope_expansion"]["enabled"] is False
    assert extensions["image_handling"]["enabled"] is False
    assert extensions["url_rewriting"]["enabled"] is True
    assert extensions["publishing"]["enabled"] is True


def test_explicit_knowledge_policy_is_preserved():
    compiled = compile_knowledge_policy([{
        "name": "Knowledge__kav",
        "extensions": {
            "image_handling": {"enabled": True},
            "publishing": {"enabled": False},
        },
    }], "flat_file")
    assert compiled[0]["extensions"]["image_handling"]["enabled"] is True
    assert compiled[0]["extensions"]["publishing"]["enabled"] is False


def test_non_knowledge_objects_are_unchanged():
    objects = [{"name": "Account", "fields": ["Name"]}]
    assert compile_knowledge_policy(objects, "flat_file") == objects


if __name__ == "__main__":
    import sys
    from tests._runner import run
    sys.exit(run(globals()))
