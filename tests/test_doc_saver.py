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
    assert d.people_in_text("Policy holder: Ruma Roy; nominee Kiaan. Proposer Maan.") == ["ruma"]
    assert d.people_in_text("Rumania trip") == []


def test_receipt_line():
    line = d.receipt_line({"uploaded_by": "owner", "people": ["owner", "ruma"], "tags": ["#tax"],
                           "related": ["ITR-V.pdf", "Challan"]})
    assert line == "\n🔗 from you · about Ruma · #tax · related: ITR-V.pdf; Challan"
    assert d.receipt_line({}) == ""


def test_roles_separate_subject_from_mention(monkeypatch):
    monkeypatch.setattr(d, "_name_map", lambda: {"manab roy": "owner", "manab": "owner", "ganak roy": "ganak_roy",
                                                 "ganak": "ganak_roy", "ruma": "ruma", "kiaan": "kiaan"})
    monkeypatch.setattr(d, "valid_subjects", lambda c: list(c))
    tax = "Name of Assessee Manab Roy\nFather's Name Ganak Roy\nAddress Danga Para\nPAN BXVPR8900H"
    roles = d.people_roles(tax)
    assert roles == {"subjects": ["owner"], "mentions": [("ganak_roy", "father")]}
    assert d.people_in_text(tax) == []
    policy = "Policy holder: Ruma. Nominee: Kiaan. Ruma pays monthly; Ruma's plan matures 2030."
    assert d.people_roles(policy) == {"subjects": ["ruma"], "mentions": [("kiaan", "nominee")]}
    line = d.receipt_line({"uploaded_by": "owner", "people": [], "about_owner": True, "tags": ["#tax"],
                           "mentions": [("ganak_roy", "father")], "related": []})
    assert line == "\n🔗 from you · about you · mentions Ganak Roy (father) · #tax"


def test_understanding_is_gated_by_text_and_registry(monkeypatch):
    from kyraan.store import doc_understanding as u, persons
    monkeypatch.setattr(persons, "name_map", lambda: {"manab roy": "owner", "ganak roy": "ganak_roy", "ruma": "ruma"})
    text = "Name of Assessee Manab Roy\nFather's Name Ganak Roy\nPAN BXVPR8900H\nAcknowledgement No 327331780040926\nSTATE BANK OF INDIA"
    data = {"kind": "tax-return", "title": "ITR computation AY 2026-27",
            "subjects": ["Manab Roy", "Ruma"], "mentions": [{"name": "Ganak Roy", "role": "father"}],
            "issuer": "Income Tax Department", "ids": ["BXVPR8900H", "327331780040926", "FAKE99999"],
            "dates": {"document": "2026-09-04", "due": "bad", "period": "AY 2026-27"},
            "amounts": ["₹5,000 self-assessment tax"], "summary": "Return filed under 115BAC."}
    got = u.gate(data, text)
    assert got["subjects"] == ["owner"]                      # Ruma is not in the text
    assert got["mentions"] == [("ganak_roy", "father")]
    assert got["ids"] == ["BXVPR8900H", "327331780040926"]  # FAKE99999 not in the text
    assert got["issuer"] == ""                               # not in the text either
    assert str(got["date"]) == "2026-09-04" and got["due"] is None and got["kind"] == "tax-return"
    assert d.links_for(["id:BXVPR8900H", "#tax"], ["id:bxvpr8900h", "#receipt"])   # one shared id links
