"""Synthetic world for the phase 2 integration tests (task 2f). EVERYTHING is invented: company names, people,
titles, ids, emails (example.com, plus one fake gmail.com address), phones. The patterns are the ones the first
live dry run exposed (assistants with multi-role titles, Catapult-style duplicate pairs, debt / fund / rumour /
acquisition scoops), re-created with fake names. No connector is called, no real data is read."""
from __future__ import annotations

from datetime import timedelta

from tests.test_integration_pipeline import NOW, Builder, World, _iso

HEAD_PROD = "Head of Product"
# ~120 characters, several roles joined: the shape of the real 'Executive Assistant' junk title
LONG_EA_TITLE = ("Executive Assistant to the Chief Executive Officer, Chief Product Officer, Head of Digital and "
                 "Director of Customer Experience; Board Secretary")
PERSONAL_EMAIL = "fakeperson.personal77@gmail.com"
SHARED_NAMES = [f"Shared Person{i:02d}" for i in range(1, 14)]          # 13 names on both Catapult accounts


def _person(b: Builder, n: int, j: int, title: str, name: str, **kw) -> dict:
    """b.prospect + FirstName / LastName on the Salesforce Contact row (real Contacts carry them; the 1g builder
    leaves them out and the duplicate evidence reads them)."""
    p = b.prospect(n, j, title, name=name, **kw)
    first, _, last = name.partition(" ")
    b.w.contacts[-1].update({"FirstName": first, "LastName": last})
    return p


def _scoop(b: Builder, n: int, company: str, typ: str, desc: str, days: float, topics=("Product Management",)):
    b._scoop_n += 1
    sid = f"FAKE-SCOOP-{b._scoop_n:05d}"
    t = b.now - timedelta(days=days)
    b.w.scoops.append({"id": sid, "type": "Scoop", "attributes": {
        "id": sid, "company": {"id": f"FAKE-ZI-{n:04d}", "name": company}, "description": desc,
        "publishedDate": _iso(t), "originalPublishedDate": _iso(t),
        "topics": [{"id": f"T{i}", "topic": x} for i, x in enumerate(topics)],
        "types": [{"id": "Y1", "type": typ}]}})


def _acct(b: Builder, n: int, name: str, domain: str | None = None, **kw) -> str:
    b.account(n, name, state=kw.pop("state", "New South Wales"), emp=kw.pop("emp", 220), **kw)
    # a distinct registrable domain per account (the 1g builder's www.fakeN.example.com all share example.com)
    b.w.sf_rows[-1]["Website"] = f"www.{domain or f'fakeco{n}.com.au'}"
    return name


def phase2_world() -> World:
    b = Builder()
    m = b.w.meta
    exec_move = "{co} has appointed a new Head of Product to lead its digital product team."
    M_A = "Mergers & Acquisitions (M&A)"

    # (i) assistants ---------------------------------------------------------------------------------------
    co = _acct(b, 101, "Quill Only Assistant Pty Ltd")
    _person(b, 101, 1, LONG_EA_TITLE, "Ava Assistantone")
    _scoop(b, 101, co, "Executive Move", exec_move.format(co="Quill"), 2)
    co = _acct(b, 102, "Reed Assistant And Clean Pty Ltd")
    _person(b, 102, 1, LONG_EA_TITLE, "Ben Assistanttwo")
    _person(b, 102, 2, HEAD_PROD, "Cleo Cleancontact")
    _scoop(b, 102, co, "Executive Move", exec_move.format(co="Reed"), 2)
    co = _acct(b, 103, "Sable Assistant Director Pty Ltd")
    _person(b, 103, 1, "Assistant Director of Product", "Dev Assistdirector")
    _scoop(b, 103, co, "Executive Move", exec_move.format(co="Sable"), 3)

    # (ii) Catapult-like duplicate pair + Bank Australia / Arab Bank style false positives -----------------
    cat_a = _acct(b, 110, "Catapult Communications", "catapultcomm.com.au")
    _person(b, 110, 1, "VP Product", "Morgan Sharedvp")
    for i, nm in enumerate(SHARED_NAMES, 2):
        _person(b, 110, i, "Software Engineer", nm)
    for d, (typ, text, topics) in enumerate([
            ("Executive Move", exec_move.format(co="Catapult Communications"), ("Product Management",)),
            ("Funding", "Catapult Communications raised $30 million in Series C funding.", ("Funding",)),
            ("Product Launch", "Catapult Communications launched a new mobile app for customers.", ("Product Launch",)),
            ("Partnership", "Catapult Communications announced a software partnership with a customer experience platform.", ("Partnership",)),
            ("New Hire", "Catapult Communications hired a new VP of Customer Experience.", ("Customer Experience",)),
            ("Executive Move", "Catapult Communications appointed a new Chief Digital Officer.", ("Digital Transformation",)),
            ("Funding", "Catapult Communications secured a $5 million growth round.", ("Funding",)),
            ("Product Launch", "Catapult Communications launched a new digital platform for clients.", ("Product Launch",)),
            ("New Hire", "Catapult Communications hired a new Director of Product.", ("Product Management",))], 1):
        _scoop(b, 110, cat_a, typ, text, d * 3)
    cat_b = _acct(b, 111, "Catapult", "catapult-sports.com.au")
    _person(b, 111, 1, "VP Product", "Morgan Sharedvp")
    for i, nm in enumerate(SHARED_NAMES, 2):
        _person(b, 111, i, "Software Engineer", nm)
    _scoop(b, 111, cat_b, "Funding", "Catapult raised $2 million in seed funding.", 12, ("Funding",))
    co = _acct(b, 112, "Bank Australia Pty Ltd", "bankaustralia.com.au")
    _person(b, 112, 1, HEAD_PROD, "Eli Bankone")
    _scoop(b, 112, co, "Executive Move", exec_move.format(co="Bank Australia"), 4)
    co = _acct(b, 113, "Arab Bank Pty Ltd", "arabbank.com.au")
    _person(b, 113, 1, HEAD_PROD, "Fay Bankdos")
    _scoop(b, 113, co, "Executive Move", exec_move.format(co="Arab Bank"), 5)

    # (iii) triggers ---------------------------------------------------------------------------------------
    co = _acct(b, 120, "Labware Supplies Pty Ltd")
    _person(b, 120, 1, HEAD_PROD, "Gus Labware")
    _scoop(b, 120, co, "Product Launch", "Labware Supplies launched a new range of lab consumables for research labs.", 4, ("Product Launch",))
    co = _acct(b, 121, "Eqtbound Bank Pty Ltd")
    _person(b, 121, 1, HEAD_PROD, "Hal Eqtbound")
    _scoop(b, 121, co, "Funding", "Eqtbound Bank Pty Ltd to be acquired by EQT in a deal valued at $1.2 billion.", 6, ("Funding",))
    co = _acct(b, 122, "Holder Capital Pty Ltd")
    _person(b, 122, 1, HEAD_PROD, "Ida Holder")
    _scoop(b, 122, co, M_A, "Holder Capital Pty Ltd has agreed to acquire a 30% stake in Foo Digital Pty Ltd.", 4, ("M&A",))
    co = _acct(b, 123, "Foo Digital Pty Ltd")
    _person(b, 123, 1, HEAD_PROD, "Jon Foodigital")
    _scoop(b, 123, co, M_A, "Holder Capital Pty Ltd has agreed to acquire a 30% stake in Foo Digital Pty Ltd.", 4, ("M&A",))
    co = _acct(b, 124, "Airlike Infrastructure Pty Ltd")
    _person(b, 124, 1, HEAD_PROD, "Kim Airlike")
    _scoop(b, 124, co, "Funding", "Airlike Infrastructure Pty Ltd secured a $2 billion debt facility from a banking syndicate.", 5, ("Funding",))
    co = _acct(b, 125, "Chartlike Property Group Pty Ltd")
    _person(b, 125, 1, HEAD_PROD, "Lee Chartlike")
    _scoop(b, 125, co, "Product Launch", "Chartlike Property Group launched a new property fund targeting $500 million.", 5, ("Product Launch",))
    co = _acct(b, 126, "Cfopromoted Pty Ltd")
    _person(b, 126, 1, HEAD_PROD, "Max Cfopromoted")
    _scoop(b, 126, co, "Executive Move", "Pat Fakename has been promoted from CFO to CEO at Cfopromoted Pty Ltd.", 86, ("Executive Move",))
    co = _acct(b, 127, "Acqrumour Pty Ltd")
    _person(b, 127, 1, HEAD_PROD, "Nia Acqrumour")
    _scoop(b, 127, co, M_A, "Acqrumour Pty Ltd is reportedly in talks to acquire Target Rumoured Pty Ltd, a software company.", 5, ("M&A",))
    co = _acct(b, 128, "Digicontrol Pty Ltd")
    _person(b, 128, 1, HEAD_PROD, "Oli Digicontrol")
    _scoop(b, 128, co, "Product Launch", "Digicontrol launched a new mobile banking app for customers.", 5, ("Product Launch",))
    co = _acct(b, 129, "Plainfunding Pty Ltd")
    _person(b, 129, 1, HEAD_PROD, "Pam Plainfunding")
    _scoop(b, 129, co, "Funding", "Plainfunding raised $20 million in Series B funding.", 5, ("Funding",))

    co = _acct(b, 132, "Fullacquirer Pty Ltd")           # control for the rumour: a CONFIRMED acquirer of a digital business
    _person(b, 132, 1, HEAD_PROD, "Rae Fullacquirer")
    _scoop(b, 132, co, M_A, "Fullacquirer Pty Ltd has acquired Target Complete Pty Ltd, a software company.", 5, ("M&A",))
    co = _acct(b, 133, "Ambigretail Pty Ltd")            # role may be a goods role: kept, flagged ambiguous
    _person(b, 133, 1, HEAD_PROD, "Sia Ambigretail")
    _scoop(b, 133, co, "Executive Move", "Ambigretail has appointed a new Head of Product, Footwear.", 3)

    # (iv) personal email ----------------------------------------------------------------------------------
    co = _acct(b, 130, "Personalmail Pty Ltd")
    _person(b, 130, 1, HEAD_PROD, "Quin Personalmail", email=PERSONAL_EMAIL)
    _scoop(b, 130, co, "Executive Move", exec_move.format(co="Personalmail"), 1)
    co = _acct(b, 131, "Normalmail Pty Ltd")
    p = _person(b, 131, 1, HEAD_PROD, "Rex Normalmail")
    _scoop(b, 131, co, "Executive Move", exec_move.format(co="Normalmail"), 6)
    for d in (30, 40, 50, 60, 70, 80):          # call history: a seat is never empty
        b.call(p, "No Answer", d, duration=20)
    m["pairs"] = {"catapult": (110, 111), "bank": (112, 113)}
    return b.w
