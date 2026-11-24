import uuid

from app.router import Route, route

COMPANIES = [uuid.uuid5(uuid.NAMESPACE_OID, str(i)) for i in range(2000)]


def canary(percent: int, candidate: str = "v2") -> set[uuid.UUID]:
    return {c for c in COMPANIES if route(c, "v1", candidate, percent).answer == candidate}


def test_without_a_candidate_the_stable_version_answers_alone():
    assert route(COMPANIES[0], "v1", None, 50) == Route(answer="v1", shadow=None, fallback=None)


def test_zero_percent_is_shadow_only_and_hundred_is_a_full_switch():
    assert {route(c, "v1", "v2", 0) for c in COMPANIES} == {Route(answer="v1", shadow="v2", fallback=None)}
    assert {route(c, "v1", "v2", 100) for c in COMPANIES} == {Route(answer="v2", shadow="v1", fallback="v1")}


def test_canary_share_is_close_to_the_setting():
    assert 0.20 < len(canary(25)) / len(COMPANIES) < 0.30


def test_raising_the_percentage_only_adds_companies():
    assert canary(5) <= canary(25) <= canary(50)


def test_each_candidate_is_tried_on_a_different_group():
    assert canary(25, "v2") != canary(25, "v3")
