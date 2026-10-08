"""Unit tests for Transformer email mask helpers."""

import polars as pl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.transformer import Transformer


def _make_transformer():
    t = Transformer.__new__(Transformer)
    t.mask_emails = True
    t.remove_email_mask = False
    return t


def test_simple_email_masked():
    t = _make_transformer()
    df = pl.DataFrame({"Email": ["foo@bar.com", "no-email", None]})
    result = t._mask_emails(df, "Test")
    assert result["Email"][0] == "foo@bar.com.Invalid", f"Got: {result['Email'][0]}"
    assert result["Email"][1] == "no-email"
    assert result["Email"][2] is None


def test_multiple_emails_in_text():
    t = _make_transformer()
    df = pl.DataFrame({"Desc": ["Contact alice@x.org and bob@y.co.uk"]})
    result = t._mask_emails(df, "Test")
    val = result["Desc"][0]
    assert "alice@x.org.Invalid" in val, f"Got: {val}"
    assert "bob@y.co.uk.Invalid" in val, f"Got: {val}"


def test_existing_invalid_suffix_not_duplicated():
    t = _make_transformer()
    df = pl.DataFrame({"Email": ["foo@bar.com.Invalid", "bar@baz.org"]})
    result = t._mask_emails(df, "Test")
    assert result["Email"][0] == "foo@bar.com.Invalid"
    assert result["Email"][1] == "bar@baz.org.Invalid"


def test_non_string_columns_untouched():
    t = _make_transformer()
    df = pl.DataFrame({"num": [1, 2], "email": ["a@b.com", "c@d.org"]})
    result = t._mask_emails(df, "Test")
    assert result["num"].to_list() == [1, 2]
    assert result["email"][0] == "a@b.com.Invalid"


def test_empty_dataframe():
    t = _make_transformer()
    df = pl.DataFrame({"Email": pl.Series([], dtype=pl.Utf8)})
    result = t._mask_emails(df, "Test")
    assert len(result) == 0


def test_remove_email_mask():
    t = _make_transformer()
    df = pl.DataFrame({"Email": ["foo@bar.com.Invalid", "plain text", None]})
    result = t._remove_email_mask(df, "Test")
    assert result["Email"][0] == "foo@bar.com"
    assert result["Email"][1] == "plain text"
    assert result["Email"][2] is None


def test_remove_email_mask_from_embedded_text():
    t = _make_transformer()
    df = pl.DataFrame({"Desc": ["Reach foo@bar.com.Invalid or baz@qux.org.Invalid today"]})
    result = t._remove_email_mask(df, "Test")
    val = result["Desc"][0]
    assert "foo@bar.com.Invalid" not in val
    assert "baz@qux.org.Invalid" not in val
    assert "foo@bar.com" in val
    assert "baz@qux.org" in val


if __name__ == "__main__":
    test_simple_email_masked()
    test_multiple_emails_in_text()
    test_existing_invalid_suffix_not_duplicated()
    test_non_string_columns_untouched()
    test_empty_dataframe()
    test_remove_email_mask()
    test_remove_email_mask_from_embedded_text()
    print("All tests passed!")
