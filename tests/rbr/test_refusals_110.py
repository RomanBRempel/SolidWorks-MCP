"""Malformed input reaches the agent as a named refusal, not as a bare ValueError / TypeError
(a non-SolidWorksError also sends the guard into its full UI release), and a few edge cases
found while the pure functions of 1.10.0 were put under test."""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import dialogs, instance, query, scope, sketch_datum


def _change(delta=0.0, bodies=()):
    return {"volume_mm3": {"before": 100.0, "after": 100.0 + delta, "delta": delta},
            "body_count": {"before": 1, "after": 1}, "bodies": list(bodies), "whats_wrong": [],
            "new_under_defined_sketches": []}


def test_expect_changed_bodies_empty_means_no_body_may_change():
    still = [{"body": "A", "delta_mm3": 0.0}]
    moved = [{"body": "A", "delta_mm3": -5.0}]
    assert scope.check_expect({"changed_bodies": []}, _change(bodies=still)) == []
    failed = scope.check_expect({"changed_bodies": []}, _change(-5.0, moved))
    assert len(failed) == 1 and "['A'] did" in failed[0]


@pytest.mark.parametrize("bounds", [[5], [1, 2, 3], ["a", "b"], "wide"])
def test_in_range_refuses_what_is_no_number_or_pair(bounds):
    with pytest.raises(SolidWorksError, match=r"number or \[min, max\]"):
        query.in_range(5.0, bounds)


def test_in_range_takes_open_ends():
    assert query.in_range(5.0, [None, 6]) and query.in_range(5.0, [4, None]) and not query.in_range(5.0, [6, None])


@pytest.mark.parametrize("token", ["ref:" + "!" * 30, "ref:" + "a b c d e f g h i j k l m n o", "ref:"])
def test_from_token_refuses_text_that_is_no_reference(token):
    with pytest.raises(SolidWorksError, match="not a valid entity reference"):
        query.from_token(token)


@pytest.mark.parametrize("point", [[math.nan, 0, 0], [0, math.inf, 0], "@nan,0,0", "@0,0,inf"])
def test_a_point_needs_finite_numbers(point):
    with pytest.raises(SolidWorksError):
        sketch_datum.parse_point(point)


def test_pid_from_the_environment_takes_ascii_digits_only():
    assert instance.wanted_pid({instance.PID_ENV: " 9816 "}) == 9816
    with pytest.raises(SolidWorksError, match="process id"):
        instance.wanted_pid({instance.PID_ENV: "²"})   # a digit to str.isdigit, not to int()


def test_a_disabled_default_button_is_not_offered():
    dialog = {"hwnd": 1, "title": "t", "text": "q", "modal": True,
              "buttons": [{"id": 1, "text": "OK", "enabled": False, "default": True},
                          {"id": 2, "text": "Cancel", "enabled": True, "default": False}]}
    shown = dialogs.public(dialog)
    assert shown["buttons"] == ["Cancel"] and "default_button" not in shown
