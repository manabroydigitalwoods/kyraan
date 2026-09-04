"""The document saver engine (owner 2026-09-04): a strict forget sweep,
entity linking, the receipt line."""
from kyraan.store import documents as d


def test_sweep_needs_the_facts_distinctive_words(monkeypatch):
    monkeypatch.setattr(d, "_name_map", lambda: {"roy": "owner", "ruma": "ruma", "biren": "biren_roy"})
    tax = "Name of assessee MANAB ROY. Father's name BIREN ROY. PAN ABCDE1234F. Total income 12,00,000"
    assert d.sweep_hit("Father's name is Biren Roy", tax)             # every distinctive word recurs: fair
    assert not d.sweep_hit("User goes by the name Ruma", tax)         # "name" alone is nothing
    assert not d.sweep_hit("Every day, from 10:00 AM to 9:00 PM, remind me every 5 minutes to drink water",
                           "A photo of the family at dinner every day this week, drink in hand")
    assert d.SWEEPABLE_KINDS == ("photo", "moment")


def test_links_for_shares_named_things():
    assert d.links_for(["PAN ABCDE1234F", "State Bank of India", "#tax"], ["pan abcde1234f", "STATE BANK OF INDIA", "#receipt"])
    assert d.links_for(["Cybergrove Solutions", "#invoice"], ["cybergrove solutions", "#invoice"])
    assert not d.links_for(["Cybergrove Solutions", "#invoice"], ["cybergrove solutions", "#tax"])
    assert not d.links_for(["#tax"], ["#tax"])


def test_people_in_text_uses_the_registry(monkeypatch):
    monkeypatch.setattr(d, "_name_map", lambda: {"ruma": "ruma", "ruma roy": "ruma", "maan": "owner", "kiaan": "kiaan"})
    monkeypatch.setattr(d, "valid_subjects", lambda c: list(c))
    assert d.people_in_text("Policy holder: Ruma Roy; nominee Kiaan. Proposer Maan.") == ["ruma", "kiaan"]
    assert d.people_in_text("Rumania trip") == []


def test_receipt_line():
    line = d.receipt_line({"uploaded_by": "owner", "people": ["owner", "ruma"], "tags": ["#tax"],
                           "related": ["ITR-V.pdf", "Challan"]})
    assert line == "\n🔗 from you · about Ruma · #tax · related: ITR-V.pdf; Challan"
    assert d.receipt_line({}) == ""
