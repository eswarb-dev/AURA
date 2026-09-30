from jarvis.voice_intents import normalize_aura_transcript


def test_cosmo_normalizes_to_aura():
    result = normalize_aura_transcript("Hey Cosmo")
    assert result["wake_detected"] is True
    assert result["alias_matched"] == "cosmo"
    assert result["normalized"] == "hey aura"


def test_wakeup_normalizes_to_aura():
    result = normalize_aura_transcript("Wakeup")
    assert result["wake_detected"] is True
    assert result["alias_matched"] == "wakeup"
    assert result["normalized"] == "hey aura"


def test_wake_up_command_preserves_suffix():
    result = normalize_aura_transcript("wake up what is your status")
    assert result["wake_detected"] is True
    assert result["alias_matched"] == "wake up"
    assert result["normalized"] == "hey aura what is your status"


def test_abba_normalizes_to_aura():
    result = normalize_aura_transcript("Hey Abba")
    assert result["wake_detected"] is True
    assert result["normalized"] == "hey aura"


def test_ora_command_preserves_suffix():
    result = normalize_aura_transcript("Hey Ora status")
    assert result["wake_detected"] is True
    assert result["normalized"] == "hey aura status"


def test_aurora_command_preserves_suffix():
    result = normalize_aura_transcript("hello aurora what is your status")
    assert result["wake_detected"] is True
    assert result["normalized"] == "hey aura what is your status"


def test_unrelated_text_is_not_forced_to_aura():
    result = normalize_aura_transcript("you are online")
    assert result["wake_detected"] is False
    assert result["normalized"] == "you are online"
