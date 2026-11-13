"""Unit tests: name cleaning, reviewer names, listing match, review picking, competitor rules, CSV input,
and the three documented edge cases end to end with a fake treg (no network, no spend)."""
from __future__ import annotations

import pytest

from review_intel import pipeline, providers
from review_intel.config import Settings
from review_intel.http import Meter
from review_intel.logic import (clean_summary, competitors, match_listing, pick_competitors, pick_low_reviews,
                                 token_set_ratio)
from review_intel.names import clean_company_name, clean_or_blank, name_violations, reviewer_name
from review_intel.website import root_domain


# ---------------- Part 3 name cleaning ----------------

@pytest.mark.parametrize("raw,expected", [
    ("Bright Smiles Family Dentistry, LLC | Best Dentist in Austin TX", "Bright Smiles Family Dentistry"),
    ("Smile Studio Dental Group - Dentist in Pasadena, CA", "Smile Studio Dental Group"),
    ("Harmony Dental Care-Cosmetic & Emergency Dentist", "Harmony Dental Care"),
    ("Glen Dental Studio: Stepan Harutyunyan, DMD", "Glen Dental Studio"),
    ("Dr. John Smith, DDS - Family & Cosmetic Dentistry", "Dr. John Smith"),
    ("TERESA GONZALEZ REDER DDS, INC.", "Teresa Gonzalez Reder"),
    ("777 DENTAL AND IMPLANTS INC", "777 Dental and Implants"),
    ("Lone Star Dental, TX 78701", "Lone Star Dental"),
    ("Sunset Dental Care (Dr. Lee)", "Sunset Dental Care"),
])
def test_clean_company_name(raw, expected):
    name, _ = clean_company_name(raw)
    assert name == expected
    assert name_violations(name) == []


@pytest.mark.parametrize("keep", ["Dental Care of Texas", "Jean-Pierre Dental", "Smile-Dental Arts"])
def test_cleaner_leaves_real_names_alone(keep):
    assert clean_company_name(keep)[0] == keep


@pytest.mark.parametrize("bad", ["Best Dentist Near Me", "Smile Co. LLC", "Bright Dental | Austin", "Joe Smith DDS",
                                 "Happy Teeth, CA", "SUPER DENTAL CLINIC GROUP", ""])
def test_name_violations_catch_leftovers(bad):
    assert name_violations(bad)


def test_clean_or_blank_never_returns_a_failing_name():
    assert clean_or_blank("Emergency Dentist in Glendale | AppointPanda") in ("", "AppointPanda")
    assert clean_or_blank("") == ""


# ---------------- reviewer names ----------------

@pytest.mark.parametrize("raw,expected", [
    ("sarah miller", "Sarah M."), ("JAMES ROBERT", "James R."), ("Kim", "Kim"), ("", "A patient"),
    ("kevin jan", "Kevin J."), ("Omar “Tlacaelel” Solis", "Omar S."), ("G. Coleman Richdale", "G. R."),
    ("Mary-Kate O'Neil", "Mary-Kate O."), ("  ", "A patient"), ("\u0425\u0435\u0439\u0434\u0438 \u0425\u0443\u043d\u043a\u043e", "Kheydi K."),
])
def test_reviewer_name(raw, expected):
    assert reviewer_name(raw) == expected


def test_summary_cleanup():
    assert clean_summary("Waited two hours past appointment.") == "waited two hours past appointment"
    assert clean_summary("rude") == ""


# ---------------- domains ----------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.BrightSmiles.com/locations/austin?utm=x", "brightsmiles.com"),
    ("dental.co.uk", "dental.co.uk"), ("http://glendale.cadentalgroup.com/", "cadentalgroup.com"),
    ("https://joe.wixsite.com/dental", "joe.wixsite.com"), ("", ""), ("not a url", ""),
])
def test_root_domain(url, expected):
    assert root_domain(url) == expected


# ---------------- listing match ----------------

def P(name, website="", address="1 Main St, Austin, TX 78701", rating=4.5, reviews=50, lat=30.27, lng=-97.74,
      pid=None, cat="Dentist", position=1):
    return {"name": name, "website": website, "address": address, "rating": rating, "review_count": reviews,
            "lat": lat, "lng": lng, "place_id": pid or ("ChIJ" + name.replace(" ", "")), "cid": "", "category": cat,
            "categories": [cat], "closed": False, "position": position}


def test_match_by_domain_prefers_same_zip_for_multi_location():
    a = P("Bright Smiles North", "https://brightsmiles.com/north", "9 Oak St, Austin, TX 78758")
    b = P("Bright Smiles Downtown", "https://www.brightsmiles.com/downtown", "1 Main St, Austin, TX 78701")
    place, how = match_listing([a, b], "brightsmiles.com", "78701", "Austin")
    assert place is b and how == "website domain"


def test_match_by_site_name_only_in_same_zip_and_no_conflicting_website():
    other_site = P("Bright Smiles Dental", "https://othersite.com")
    no_site = P("Bright Smiles Dental", "", "5 Elm St, Austin, TX 78701")
    place, _ = match_listing([other_site], "brightsmiles.com", "78701", "Austin", site_name="Bright Smiles Dental | Austin")
    assert place is None  # a similar name with a different website is a different business
    place, how = match_listing([no_site], "brightsmiles.com", "78701", "Austin", site_name="Bright Smiles Dental | Austin")
    assert place is no_site and "name" in how


def test_no_match_returns_none():
    assert match_listing([P("Other Dental", "https://other.com")], "brightsmiles.com", "78701", "Austin") == (None, "")


def test_token_set_ratio():
    assert token_set_ratio("Bright Smiles Dental", "Bright Smiles Dental Austin") == 100
    assert token_set_ratio("Bright Smiles", "Oak Family Dentistry") < 60


# ---------------- bad reviews ----------------

def R(i, rating, text, date):
    return {"id": str(i), "author": f"Pat {i}", "rating": rating, "text": text, "date": date}


def test_pick_low_reviews_most_recent_with_text_only():
    newest = [R(1, 5, "great", "2026-11-01"), R(2, 1, "waited 2 hours", "2026-10-01"), R(3, 2, "", "2026-09-01"),
              R(4, 1, "billed twice", "2026-08-01"), R(5, 3, "meh", "2026-07-01")]
    lowest = [R(6, 1, "rude staff", "2024-01-01"), R(4, 1, "billed twice", "2026-08-01")]
    low, star_only = pick_low_reviews(newest, lowest, want=5)
    assert [r["id"] for r in low] == ["2", "4", "6"]  # newest first, deduped
    assert star_only == 1


# ---------------- competitors ----------------

PROSPECT = {"name": "Bright Smiles Dental", "domain": "brightsmiles.com", "rating": 3.5, "lat": 30.27, "lng": -97.74,
            "place_id": "ChIJprospect", "cid": ""}


def test_competitor_rules():
    cands = [
        P("Bright Smiles Dental", "https://brightsmiles.com", pid="ChIJprospect"),            # itself
        P("Bright Smiles Kids", "https://brightsmiles.com/kids", lat=30.28),                  # same domain
        P("Oak Dental Lab", "https://oaklab.com", lat=30.28, cat="Dental laboratory"),        # not a practice
        P("Lower Rated Dental", "https://lower.com", rating=3.4, lat=30.28),                  # not higher rated
        P("Tiny Dental", "https://tiny.com", reviews=4, lat=30.28),                           # < 10 reviews
        P("Far Dental", "https://far.com", lat=31.5),                                         # > 5 mi
        P("Same Building Dental", "https://same.com", lat=30.27, lng=-97.74),                 # same address
        P("Austin Family Dentistry, LLC | Best Dentist in Austin", "https://afd.com", lat=30.28),  # cleaned
        P("Austin Family Dentistry", "https://afd.com/2", lat=30.285),                        # brand repeat
        P("Lakeside Dental Care", "https://lakeside.com", lat=30.29),
        P("Hillcrest Orthodontics", "https://hill.com", lat=30.26, cat="Orthodontist"),
        P("Fourth Dental", "https://fourth.com", lat=30.26),
    ]
    out, dropped = competitors(PROSPECT, cands)
    assert [c["name"] for c in out] == ["Austin Family Dentistry", "Lakeside Dental Care", "Hillcrest Orthodontics"]
    assert dropped["the prospect itself"] == 1 and dropped["same website as prospect"] == 1
    assert dropped["not a dental practice"] == 1 and dropped["rated 4.0 or lower"] == 1
    assert dropped["fewer than 10 reviews"] == 1 and dropped["more than 5 mi away"] == 1
    assert dropped["same address as prospect"] == 1


def test_fewer_than_three_competitors_returns_what_exists():
    out, _ = competitors(PROSPECT, [P("Only One Dental", "https://one.com", lat=30.28)])
    assert len(out) == 1


# ---------------- CSV input ----------------

def test_parse_csv_aliases_and_zip_padding():
    header, rows, colmap = pipeline.parse_csv("Website,Town,ST,Postal Code,Owner\nhttps://www.x.com/a,Newark,nj,7102,Ann\n")
    inp, prob = pipeline.normalise_input(rows[0], colmap)
    assert inp == {"domain": "x.com", "city": "Newark", "state": "NJ", "zip": "07102"} and not prob
    assert "Owner" in header


def test_parse_csv_rejects_missing_columns():
    with pytest.raises(pipeline.InputError, match="zip"):
        pipeline.parse_csv("domain,city,state\nx.com,Austin,TX\n")


# ---------------- edge cases end to end (fake treg) ----------------

class FakeTreg:
    def __init__(self, places, reviews):
        self.places, self.reviews = places, reviews

    def call(self, tool, body=None, query=None, est_usd=0):
        if tool == "crawl4ai.google.serp.maps":
            rows = self.places if body["query"] == "dentist" else []
            return {"rows": [{"name": p["name"], "place_id": p["place_id"], "address": p["address"], "latitude": p["lat"],
                              "longitude": p["lng"], "rating": p["rating"], "review_count": p["review_count"],
                              "website": p["website"], "category": p["category"], "categories": p["categories"]}
                             for p in rows]}
        if tool == "crawl4ai.google.maps.reviews":
            return {"rows": [{"review_id": r["id"], "author": {"name": r["author"]}, "rating": r["rating"],
                              "text": r["text"], "date": r["date"]} for r in self.reviews]}
        if tool in ("litescrape.google.serp.local", "litescrape.google.maps.reviews"):
            return {"local_results": [], "reviews": []}
        raise AssertionError(tool)


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_homepage", lambda d: {"ok": False, "title": "", "error": "offline"})
    monkeypatch.setattr(pipeline, "summarise",
                        lambda texts, **k: ({i: "waited two hours past appointment" for i in texts}, ""))
    s = Settings()
    return s


def test_edge_fewer_than_three_bad_reviews(fake_env):
    me = P("Bright Smiles Dental", "https://brightsmiles.com", rating=3.5, reviews=20)
    revs = [R(1, 1, "waited forever", "2026-10-01"), R(2, 5, "great", "2026-09-01"), R(3, 2, "", "2026-08-01")]
    comps = [P(f"Rival {n} Dental", f"https://rival{n}.com", lat=30.27 + n / 100) for n in range(1, 4)]
    out = pipeline.process_row({"domain": "brightsmiles.com", "city": "Austin", "state": "TX", "zip": "78701"},
                               FakeTreg([me] + comps, revs), fake_env, Meter(1))
    assert out["status"] == "partial" and out["bad_review_count"] == 1
    assert out["bad_reviews"] == "• Pat - waited two hours past appointment"  # digits are not a surname
    assert "only 1" in out["notes"] and "star-only" in out["notes"]
    assert out["competitor_3"]


def test_edge_listing_not_found(fake_env):
    comps = [P(f"Rival {n} Dental", f"https://rival{n}.com", lat=30.27 + n / 100) for n in range(1, 4)]
    out = pipeline.process_row({"domain": "nolisting.com", "city": "Austin", "state": "TX", "zip": "78701"},
                               FakeTreg(comps, []), fake_env, Meter(1))
    assert out["status"] == "no_listing" and out["bad_reviews"] == ""
    assert "Google listing not found" in out["notes"] and "ZIP centre" in out["notes"]


def test_edge_fewer_than_three_competitors(fake_env):
    me = P("Bright Smiles Dental", "https://brightsmiles.com", rating=4.6, reviews=20)
    revs = [R(i, 1, f"complaint {i}", f"2026-10-0{i}") for i in range(1, 6)]
    others = [P("Lower Dental", "https://lower.com", rating=4.0, lat=30.28),
              P("Better Dental", "https://better.com", rating=4.9, lat=30.28)]
    out = pipeline.process_row({"domain": "brightsmiles.com", "city": "Austin", "state": "TX", "zip": "78701"},
                               FakeTreg([me] + others, revs), fake_env, Meter(1))
    assert out["status"] == "partial" and out["competitor_1"] == "Better Dental" and out["competitor_2"] == ""
    assert "only 1 real competitor" in out["notes"]


def test_budget_cap_blocks_paid_calls():
    from review_intel.http import BudgetExceeded
    m = Meter(0.0001)
    m.add("x", 0.0001)
    with pytest.raises(BudgetExceeded):
        m.reserve(0.0002, "next call")


def test_provider_normalisers_accept_litescrape_shape():
    p = providers._place_ls({"title": "A Dental", "gps_coordinates": {"latitude": 1, "longitude": 2}, "rating": 4.1,
                             "reviews": 12, "links": {"website": "https://a.com"}, "type": "Dentist"})
    assert p["website"] == "https://a.com" and p["review_count"] == 12 and p["lat"] == 1


def test_match_by_phone_on_homepage_when_listing_points_at_another_domain():
    from review_intel.website import phones_in
    listing = P("H&H Dental Center - Dr. Hayk Hayrapetian, DDS", "https://hhdental.center/",
                "815 E Colorado St #240, Glendale, CA 91205")
    listing["phone"] = "+1 818-265-0500"
    far = dict(listing, address="1 Elm St, Pasadena, CA 91101", name="Elsewhere")
    phones = phones_in('<a href="tel:8182650500">Call (818) 265-0500</a>')
    assert phones == ["8182650500"]
    place, how = match_listing([far, listing], "hhdentalcenter.com", "91205", "Glendale", "HH Dental Center", phones)
    assert place is listing and "phone" in how
    assert match_listing([far], "hhdentalcenter.com", "91205", "Glendale", "", phones) == (None, "")


def test_generic_site_title_never_matches_by_name():
    pdc = P("PDC Healthcare", "", "24 Main St, Valencia, CA 91355")
    assert match_listing([pdc], "wandkdentistryvalencia.com", "91355", "Valencia", site_name="PDC") == (None, "")
    other = P("Austin Dental Care", "", "1 Main St, Austin, TX 78701")
    assert match_listing([other], "x.com", "78701", "Austin", site_name="Dental Care") == (None, "")


# ---------------- competitor rating floor + widening (Giannis, 13 Nov 2026) ----------------

LOW_PROSPECT = dict(PROSPECT, rating=1.7)


def test_sub_four_competitor_excluded_even_if_above_prospect():
    out, dropped = competitors(LOW_PROSPECT, [P("Starrett City Dental", "https://sc.com", rating=2.9, lat=30.28),
                                              P("Good Dental", "https://good.com", rating=4.6, lat=30.28)])
    assert [c["name"] for c in out] == ["Good Dental"]
    assert dropped["rated 4.0 or lower"] == 1


def test_exactly_four_point_zero_is_excluded():
    out, _ = competitors(LOW_PROSPECT, [P("Eldridge Dental", "https://e.com", rating=4.0, lat=30.28),
                                        P("Woodhaven Dental", "https://w.com", rating=4.01, lat=30.28)])
    assert [c["name"] for c in out] == ["Woodhaven Dental"]


def test_widens_to_city_same_city_first_when_fewer_than_three_within_radius():
    near = P("Near Dental", "https://near.com", rating=4.5, lat=30.28)                           # ~0.7 mi
    far_other_city = P("Far Other Dental", "https://foc.com", rating=4.9, lat=30.37,
                       address="1 Oak St, Round Rock, TX 78664")                                  # ~6.9 mi
    far_same_city = P("Far Austin Dental", "https://fac.com", rating=4.4, lat=30.40,
                      address="9 Elm St, Austin, TX 78758")                                       # ~9 mi
    too_far = P("Way Out Dental", "https://wo.com", rating=4.9, lat=30.60)                       # ~23 mi
    low_far = P("Low Far Dental", "https://lf.com", rating=3.9, lat=30.30)
    out, _, widened = pick_competitors(LOW_PROSPECT, [too_far, far_other_city, near, far_same_city, low_far],
                                       city="Austin", radius_mi=5, city_radius_mi=15)
    assert widened
    assert [c["name"] for c in out] == ["Near Dental", "Far Austin Dental", "Far Other Dental"]
    assert all(c["rating"] > 4.0 for c in out) and all(c["distance_mi"] <= 15 for c in out)


def test_no_widening_when_three_found_inside_radius():
    cands = [P(f"Rival {n} Dental", f"https://r{n}.com", rating=4.5, lat=30.27 + n / 100) for n in range(1, 5)]
    out, _, widened = pick_competitors(LOW_PROSPECT, cands, city="Austin")
    assert len(out) == 3 and not widened


def test_one_or_two_results_never_padded_with_low_rated(fake_env):
    me = P("Bright Smiles Dental", "https://brightsmiles.com", rating=1.7, reviews=20)
    revs = [R(i, 1, f"complaint {i}", f"2026-10-0{i}") for i in range(1, 6)]
    others = [P("Good Dental", "https://good.com", rating=4.7, lat=30.28),
              P("Okay Dental", "https://okay.com", rating=4.2, lat=30.33),
              P("Meh Dental", "https://meh.com", rating=4.0, lat=30.28),
              P("Poor Dental", "https://poor.com", rating=2.9, lat=30.28)]
    out = pipeline.process_row({"domain": "brightsmiles.com", "city": "Austin", "state": "TX", "zip": "78701"},
                               FakeTreg([me] + others, revs), fake_env, Meter(1))
    assert out["status"] == "partial"
    assert [out["competitor_1"], out["competitor_2"], out["competitor_3"]] == ["Good Dental", "Okay Dental", ""]
    assert out["competitor_1_rating"] == 4.7 and out["competitor_2_reviews"] == 50
    assert out["competitor_3_rating"] == "" and "only 2 real competitor" in out["notes"]
    assert "widened" in out["competitor_search"]
