"""Tests for gazetteer-based place resolution.

The corpus this serves is conflict reporting, where a wrong answer is worse
than no answer: a point on a map asserts that an event happened somewhere
specific. These tests therefore pin the behaviour that keeps junk out
(capitalisation, population floors, function-word leads) as tightly as they
pin the behaviour that gets cities in.
"""

from __future__ import annotations

import pytest

from local_butler.apps.geonews.geo import gazetteer as gz


def place(
    gid: int,
    name: str,
    *,
    cc: str = "UA",
    admin1: str = "09",
    population: int = 200_000,
    lat: float = 48.0,
    lon: float = 37.0,
    alts: tuple[str, ...] = (),
) -> gz.Place:
    return gz.Place(
        geoname_id=gid,
        name=name,
        latitude=lat,
        longitude=lon,
        country_code=cc,
        admin1_code=admin1,
        population=population,
        alternate_names=alts,
    )


@pytest.fixture(scope="module")
def index() -> dict:
    return gz.build_index(
        [
            place(1, "Kharkiv", population=1_300_000, admin1="63"),
            place(2, "Kherson", population=66_000, admin1="65"),
            place(3, "Goma", cc="CD", admin1="06", population=78_200),
            place(4, "Taiz", cc="YE", admin1="14", population=940_600),
            place(5, "Gaza City", cc="PS", admin1="GZ", population=600_000),
            place(6, "New York City", cc="US", admin1="NY", population=8_800_000),
            place(7, "York", cc="GB", admin1="ENG", population=200_000),
            place(8, "Price", cc="US", admin1="UT", population=3_500),
            place(9, "Police", cc="PL", admin1="78", population=34_350),
            place(10, "Pokrovsk", cc="RU", admin1="10", population=196_011),
            place(11, "Pokrovsk", cc="UA", admin1="14", population=30_000),
            place(12, "Springfield", cc="US", admin1="IL", population=114_000),
            place(13, "Springfield", cc="US", admin1="MO", population=169_000),
        ]
    )


class TestNormalise:
    def test_folds_diacritics_and_case(self):
        assert gz.normalise("Saarbrücken") == "saarbrucken"
        assert gz.normalise("SAARBRÜCKEN") == "saarbrucken"

    def test_hyphen_becomes_a_separator(self):
        # A known asymmetry: an article writing "Saar-Brucken" will not match a
        # gazetteer row keyed "Saarbrücken". Both sides run through the same
        # fold, so this only bites where the source hyphenates differently.
        assert gz.normalise("Saar-Brucken") == "saar brucken"

    def test_collapses_punctuation_and_space(self):
        assert gz.normalise("New  York!") == "new york"
        assert gz.normalise("U.S.A") == "u s a"


class TestExtractMentions:
    def test_matches_conflict_cities(self, index):
        found = gz.extract_mentions("Fighting intensifies in Kharkiv and Kherson", index)
        names = {m.candidates[0].name for m in found}
        assert names == {"Kharkiv", "Kherson"}

    def test_multiword_beats_single_token(self, index):
        found = gz.extract_mentions("a story from New York City", index)
        assert [m.text for m in found] == ["New York City"]
        assert "York" not in [m.text for m in found]

    def test_lowercase_single_token_rejected(self, index):
        assert gz.extract_mentions("troops moved near kharkiv overnight", index) == []

    def test_small_town_single_token_rejected(self, index):
        assert gz.extract_mentions("the Price Index rose", index) == []

    def test_dictionary_word_town_rejected_when_small(self, index):
        assert gz.extract_mentions("the Police said nothing", index) == []

    def test_function_word_lead_rejected(self):
        idx = gz.build_index([place(99, "Portsmouth", cc="US", population=21_530)])
        idx.setdefault("the bank", []).append(idx["portsmouth"][0])
        assert gz.extract_mentions("letters from the Bank were leaked", idx) == []

    def test_repeated_occurrence_is_a_separate_mention(self, index):
        found = gz.extract_mentions("Kharkiv and Kharkiv", index)
        assert [m.text for m in found] == ["Kharkiv", "Kharkiv"]
        assert found[0].end <= found[1].start

    def test_returns_offsets_into_source(self, index):
        text = "Fighting in Kharkiv today"
        mention = gz.extract_mentions(text, index)[0]
        assert text[mention.start : mention.end] == "Kharkiv"


class TestPickCandidate:
    def test_single_candidate_is_unique(self, index):
        chosen, how = gz.pick_candidate([index["kharkiv"][0]])
        assert chosen.name == "Kharkiv"
        assert how == "unique"

    def test_no_candidates(self):
        assert gz.pick_candidate([]) == (None, "none")

    def test_most_populous_wins_without_hint(self):
        candidates = [
            place(1, "Springfield", cc="US", admin1="IL", population=114_000),
            place(2, "Springfield", cc="US", admin1="MO", population=169_000),
        ]
        chosen, how = gz.pick_candidate(candidates)
        assert chosen.admin1 == "US.MO"
        assert how == "ambiguous"

    def test_country_hint_breaks_tie(self):
        candidates = [
            place(1, "Aleppo", cc="SY", admin1="01", population=2_000_000),
            place(2, "Aleppo", cc="TR", admin1="01", population=200_000),
        ]
        chosen, how = gz.pick_candidate(candidates, hint_country="TR")
        assert chosen.country_code == "TR"
        assert how == "country_hint"

    def test_ambiguity_is_reported_not_hidden(self, index):
        chosen, how = gz.pick_candidate(index["pokrovsk"])
        assert how == "ambiguous"
        assert chosen is not None


class TestResolveArticle:
    def _row(self, **kw):
        base = {
            "id": 1,
            "title": "",
            "summary": "",
            "location_name": None,
        }
        base.update(kw)
        return base

    def test_title_wins_over_summary(self, index):
        row = self._row(title="Kharkiv under fire", summary="Correspondent in Kherson")
        got = gz.resolve_article(row, index)
        assert got.city == "Kharkiv"
        assert got.country_code == "UA"
        assert got.admin1 == "UA.63"
        assert got.confidence == "resolved"

    def test_no_place_leaves_coordinates_unset(self, index):
        got = gz.resolve_article(self._row(title="Bond markets closed higher"), index)
        assert got.latitude is None
        assert got.confidence == "none"

    def test_hierarchy_fields_are_populated_together(self, index):
        got = gz.resolve_article(self._row(title="Gaza City strike"), index)
        assert (got.city, got.admin1, got.country_code) == ("Gaza City", "PS.GZ", "PS")

    def test_admin1_format_is_country_qualified(self, index):
        got = gz.resolve_article(self._row(title="Fighting in Kherson"), index)
        assert got.admin1 == "UA.65"

    def test_country_hint_cannot_split_same_country_ties(self, index):
        # Both Springfields are American, so a US hint narrows nothing and the
        # case stays flagged for review instead of becoming a confident answer.
        row = self._row(
            title="Springfield residents react",
            location_name="United States",
        )
        got = gz.resolve_article(row, index)
        assert got.country_code == "US"
        assert got.confidence == "ambiguous"


class TestHintFor:
    @pytest.mark.parametrize(
        "location_name,expected",
        [
            ("Ukraine", "UA"),
            ("Iran", "IR"),
            ("Russia", "RU"),
            ("Palestine", "PS"),
            ("World", None),
            (None, None),
            ("Atlantis", None),
        ],
    )
    def test_known_and_unknown(self, location_name, expected):
        assert gz.hint_for(location_name) == expected


class TestDisplay:
    def test_renders_hierarchy_with_province_names(self):
        p = place(1, "Kharkiv", admin1="63")
        assert p.display({"UA.63": "Kharkiv Oblast"}) == "Kharkiv, Kharkiv Oblast, UA"

    def test_falls_back_to_code_without_lookup(self):
        assert place(1, "Kharkiv", admin1="63").display() == "Kharkiv, 63, UA"