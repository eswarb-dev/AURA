from jarvis import identity


def test_tracking_error_does_not_use_stale_preferred_address():
    resolved = identity.resolve_effective_address({
        "gesture_tracking_state": "active",
        "people_tracking_state": "error",
        "identity_state": "matched",
        "matched_profile": {
            "id": "owner",
            "display_name": "Vathanya",
            "preferred_address": "Ma'am",
        },
        "identity_confidence": 0.95,
        "active_profile_is_unambiguous": True,
        "live_identity_fresh": True,
    })
    assert resolved["effective_address"] == "Sir"
    assert resolved["address_source"] == "default"
    assert resolved["live_identity_address_allowed"] is False
    assert resolved["used_visual_gender_inference"] is False


def test_active_matched_profile_uses_explicit_preferred_address():
    resolved = identity.resolve_effective_address({
        "gesture_tracking_state": "off",
        "people_tracking_state": "active",
        "identity_state": "matched",
        "matched_profile": {
            "id": "owner",
            "display_name": "Vathanya",
            "preferred_address": "Ma'am",
        },
        "identity_confidence": 0.95,
        "active_profile_is_unambiguous": True,
        "live_identity_fresh": True,
    })
    assert resolved["effective_address"] == "Ma'am"
    assert resolved["address_source"] == "explicit_profile_live_identity"
    assert resolved["live_identity_address_allowed"] is True
    assert resolved["used_visual_gender_inference"] is False


def test_multi_person_without_unambiguous_match_uses_default():
    resolved = identity.resolve_effective_address({
        "gesture_tracking_state": "error",
        "people_tracking_state": "active",
        "identity_state": "matched",
        "matched_profile": {
            "id": "owner",
            "display_name": "Vathanya",
            "preferred_address": "Ma'am",
        },
        "identity_confidence": 0.95,
        "active_profile_is_unambiguous": False,
        "live_identity_fresh": True,
        "people_count": 2,
    })
    assert resolved["effective_address"] == "Sir"
    assert resolved["address_source"] == "default"
    assert resolved["live_identity_address_allowed"] is False


def test_matched_profile_without_preferred_address_uses_default():
    resolved = identity.resolve_effective_address({
        "gesture_tracking_state": "off",
        "people_tracking_state": "active",
        "identity_state": "matched",
        "matched_profile": {
            "id": "owner",
            "display_name": "Vathanya",
        },
        "identity_confidence": 0.95,
        "active_profile_is_unambiguous": True,
        "live_identity_fresh": True,
    })
    assert resolved["effective_address"] == "Sir"
    assert resolved["address_source"] == "default"
    assert resolved["live_identity_address_allowed"] is False
    assert resolved["used_visual_gender_inference"] is False


def test_gesture_tracking_active_people_tracking_error_cannot_change_address():
    resolved = identity.resolve_effective_address({
        "gesture_tracking_state": "active",
        "people_tracking_state": "error",
        "identity_state": "matched",
        "matched_profile": {
            "id": "owner",
            "display_name": "Vathanya",
            "preferred_address": "Ma'am",
        },
        "identity_confidence": 0.95,
        "active_profile_is_unambiguous": True,
        "live_identity_fresh": True,
    })
    assert resolved["effective_address"] == "Sir"
    assert resolved["address_source"] == "default"
    assert resolved["live_identity_address_allowed"] is False


def test_legacy_tracking_state_compat_maps_to_people_tracking():
    resolved = identity.resolve_effective_address({
        "tracking_state": "active",
        "identity_state": "matched",
        "matched_profile": {
            "id": "owner",
            "display_name": "Vathanya",
            "preferred_address": "Ma'am",
        },
        "identity_confidence": 0.95,
        "active_profile_is_unambiguous": True,
        "live_identity_fresh": True,
    })
    assert resolved["effective_address"] == "Ma'am"
    assert resolved["address_source"] == "explicit_profile_live_identity"
    assert resolved["people_tracking_state"] == "active"
