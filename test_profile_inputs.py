"""Matching and profile import take their profile input explicitly."""
import json
import os

import matcher

PROFILE = {"languages": {"python": 1.0, "java": 0.5}, "skills": {"backend": 1.0}, "score_saturation": 2}


def test_rank_by_score_is_stable_so_input_order_breaks_ties():
    rows = [
        {"id": "a", "title": "Store Clerk"},
        {"id": "b", "title": "Backend Engineer"},
        {"id": "c", "title": "Python Backend Engineer"},
        {"id": "d", "title": "Backend Developer"},
    ]
    assert matcher.rank(rows, PROFILE, by_score=True) == [
        {"id": "c", "title": "Python Backend Engineer", "match_score": 100},
        {"id": "b", "title": "Backend Engineer", "match_score": 50},
        {"id": "d", "title": "Backend Developer", "match_score": 50},
        {"id": "a", "title": "Store Clerk", "match_score": 0},
    ]


def test_rank_without_score_sort_keeps_input_order():
    rows = [{"id": "a", "title": "Store Clerk"}, {"id": "c", "title": "Python Backend Engineer"}]
    assert matcher.rank(rows, PROFILE, by_score=False) == [
        {"id": "a", "title": "Store Clerk", "match_score": 0},
        {"id": "c", "title": "Python Backend Engineer", "match_score": 100},
    ]


def test_with_match_appends_score_and_breakdown_in_order():
    job = {"id": "x", "title": "Python Engineer", "domain": "SWE"}
    profile = {"languages": {"python": 1.0, "java": 0.5}, "skills": {"backend": 0.75}, "score_saturation": 4}
    result = matcher.with_match(job, profile)
    assert list(result) == ["id", "title", "domain", "match_score", "match"]
    assert result == {
        "id": "x", "title": "Python Engineer", "domain": "SWE",
        "match_score": 25,
        "match": {
            "matched_terms": {"python": 1.0},
            "missing_terms": {"java": 0.5, "backend": 0.75},
            "matched_weight": 1.0,
        },
    }


def test_profile_file_serves_default_until_the_file_appears(tmp_path):
    path = tmp_path / "profile.json"
    source = matcher.ProfileFile(path)
    assert source.current() == matcher.default_profile()
    path.write_text(json.dumps({"keywords": {"engineer": 1}}), encoding="utf-8")
    assert source.current() == {"keywords": {"engineer": 1}}


def test_profile_file_reuses_snapshot_until_mtime_changes(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"languages": {"python": 2}}), encoding="utf-8")
    source = matcher.ProfileFile(path)
    first = source.current()
    assert source.current() is first
    path.write_text(json.dumps({"languages": {"rust": 3}}), encoding="utf-8")
    os.utime(path, ns=(0, path.stat().st_mtime_ns + 1))
    assert source.current() == {"languages": {"rust": 3}}
