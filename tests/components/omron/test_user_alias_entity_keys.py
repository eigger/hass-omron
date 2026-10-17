"""User labels must remain unique after conversion to entity-key fragments."""

from custom_components.omron.config_flow import _user_aliases_are_unique


def test_aliases_with_distinct_slugs_are_unique():
    assert _user_aliases_are_unique(["Mom 1", "Dad 1"])


def test_aliases_that_collapse_to_the_same_slug_are_rejected():
    assert not _user_aliases_are_unique(["Mom 1", "Mom_1"])
    assert not _user_aliases_are_unique(["Mom!", "Mom?"])


def test_slug_fallbacks_are_checked_for_collisions_too():
    # These labels contain no ASCII letters, so the parser uses user1/user2.
    assert not _user_aliases_are_unique(["엄마", "user1"])
