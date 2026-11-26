"""Excel-driven recipient logic: parsing, validation, global de-duplication, TO-over-CC priority."""
import logging
import pytest
from src import email_sender as es
from src.config import load_settings
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent


def plan(rows):
    return es.collect_recipients([(f"r{i}", a, b) for i, (a, b) in enumerate(rows)])


def test_spec_example_owners_to_developers_cc():
    p = plan([("owner1@x.io, owner2@x.io", "dev1@x.io;dev2@x.io"), ("owner3@x.io", "dev3@x.io dev4@x.io")])
    assert p.to == ["owner1@x.io", "owner2@x.io", "owner3@x.io"]
    assert p.cc == ["dev1@x.io", "dev2@x.io", "dev3@x.io", "dev4@x.io"]
    assert p.duplicates_removed == 0 and p.invalid == [] and p.repositories == 2


@pytest.mark.parametrize("cell", ["a@x.io,b@x.io", "a@x.io;b@x.io", "a@x.io b@x.io", "a@x.io\nb@x.io", " a@x.io ;; , b@x.io ", "Ann <a@x.io>, Bob <b@x.io>"])
def test_separators(cell):
    assert es.parse_addresses(cell) == (["a@x.io", "b@x.io"], [])


def test_case_and_whitespace_insensitive_global_dedupe():
    p = plan([("  Owner@X.io ", "Dev@x.io"), ("owner@x.IO;OWNER@x.io", "DEV@X.IO, dev@x.io")])
    assert p.to == ["owner@x.io"] and p.cc == ["dev@x.io"]
    assert p.duplicates_removed == 4          # 6 valid tokens - 2 unique


def test_to_wins_over_cc_and_no_address_twice():
    p = plan([("boss@x.io", "boss@x.io, dev@x.io"), ("o2@x.io", "BOSS@x.io")])
    assert "boss@x.io" in p.to and "boss@x.io" not in p.cc and p.cc == ["dev@x.io"]
    assert len(set(p.to + p.cc)) == len(p.to + p.cc)
    assert p.duplicates_removed == 2


def test_cc_only_address_in_later_row_also_moves_to_to():
    p = plan([("", "x@x.io"), ("x@x.io", "")])
    assert p.to == ["x@x.io"] and p.cc == []


def test_invalid_and_empty_are_skipped_and_masked():
    p = plan([("good@x.io, not-an-email, a@b, @x.io, bad..dot@x.io", None), (None, "secret.person@@x.io")])
    assert p.to == ["good@x.io"] and p.cc == []
    assert len(p.invalid) == 5
    for _, tok in p.invalid:
        assert tok.endswith("***") and len(tok) <= 5          # never the full token
    assert "secret.person" not in str(p.invalid)


def test_summary_text_format():
    t = es.format_recipient_summary(plan([("o@x.io, bad", "d@x.io, o@x.io")]))
    for line in ["Consolidated Email Notification Summary:", "Total repositories processed: 1", "Application Owner Recipients (TO):",
                 "Total unique recipients: 1", "Developer Recipients (CC):", "Duplicate email addresses removed: 1",
                 "Invalid email addresses skipped: 1"]:
        assert line in t
    assert "o@x.io" not in t                 # the summary shows counts only, no addresses


def test_removing_a_row_drops_its_addresses_unless_shared():
    rows = [("a@x.io", "dev1@x.io"), ("b@x.io", "dev1@x.io, dev2@x.io")]
    assert plan(rows).to == ["a@x.io", "b@x.io"] and plan(rows).cc == ["dev1@x.io", "dev2@x.io"]
    after = plan(rows[:1])
    assert after.to == ["a@x.io"] and after.cc == ["dev1@x.io"]        # b / dev2 gone, dev1 kept (still on row 1)


def test_no_valid_to_means_no_send(caplog):
    cfg = es.EmailConfig(server="smtp.x", port=587, username="u", password="p", sender="f@x.io", to=[], cc=["dev@x.io"])
    calls = []
    r = es.send_report(cfg, "s", "body", [], smtp_factory=lambda *a, **k: calls.append(1))
    assert not r.sent and "no valid TO recipient" in r.message and calls == []


def test_config_validation_of_recipients_source():
    assert load_settings(ROOT / "config/settings.yaml", {})["email"]["recipients_source"] == "excel"
    with pytest.raises(ValueError, match="recipients_source"):
        from src.config import normalise, DEFAULTS
        import copy
        d = copy.deepcopy(DEFAULTS); d["email"]["recipients_source"] = "bogus"
        normalise(d)
