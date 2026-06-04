"""Wikipedia connector disambiguation guards.

Regression net for the 2026-06 wrong-entity bug: the connector used to take
Wikipedia's top search hit blindly, so "CBD College" → Auckland city centre and
"Smith Chason College" → a basketball player. The two gates below must keep the
right entity and reject everything else. Null is always better than wrong.
"""
from landing_scraper.connectors.wikipedia import (
    _looks_like_institution,
    _name_matches,
)


class TestLooksLikeInstitution:
    def test_accepts_colleges_and_universities(self):
        assert _looks_like_institution(
            "private college", "Pomona College is a private liberal arts college.")
        assert _looks_like_institution(
            "", "UEI College is a private for-profit career college.")

    def test_rejects_cities_and_places(self):
        assert not _looks_like_institution(
            "city in California",
            "Escondido is a city in San Diego County, California.")
        assert not _looks_like_institution(
            "area in Auckland",
            "The Auckland Central Business District is the city centre.")


class TestNameMatches:
    def test_exact_and_close(self):
        assert _name_matches("Pomona College", "Pomona College", "Pomona College is...")
        assert _name_matches("UEI College", "UEI College", "UEI College is a college.")

    def test_branch_suffix_maps_to_parent(self):
        # "-Riverside"/"-Visalia"/"-Orange County" branches → parent article
        assert _name_matches("UEI College-Riverside", "UEI College", "UEI College is...")
        assert _name_matches(
            "San Joaquin Valley College-Visalia", "San Joaquin Valley College",
            "San Joaquin Valley College (SJVC) is a private for-profit college.")
        assert _name_matches(
            "West Coast University-Orange County", "West Coast University",
            "West Coast University is a private for-profit university.")

    def test_verbose_ipeds_name_vs_common_name(self):
        # Token-subset: common short form is a subset of the verbose IPEDS name
        assert _name_matches(
            "El Camino Community College District", "El Camino College",
            "El Camino College is a community college.")
        assert _name_matches(
            "Vanguard University of Southern California", "Vanguard University",
            "Vanguard University is a private university.")

    def test_rebrand_caught_via_extract(self):
        # MTI's article is titled "Campus (college)" but extract names it
        assert _name_matches(
            "MTI College", "Campus (college)",
            "Campus, formerly MTI College, is a private for-profit junior college.")

    def test_rejects_different_entity_same_token(self):
        assert not _name_matches("CBD College", "Auckland CBD",
                                 "The Auckland Central Business District...")
        assert not _name_matches("Smith Chason College", "Chasson Randle",
                                 "Chasson Randle is an American basketball player.")
        assert not _name_matches("West Coast University-Orange County",
                                 "Orange Coast College",
                                 "Orange Coast College is a community college.")
        assert not _name_matches("Reach University", "A Man's Reach",
                                 "A Man's Reach is a 1960 ...")
        assert not _name_matches("Poway Adult School", "Palomar College",
                                 "Palomar College is a community college.")
