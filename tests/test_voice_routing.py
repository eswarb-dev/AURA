import asyncio

from jarvis import diagnostics


async def _call_voice_turn(payload):
    return await diagnostics.voice_bench_loop(payload)


def test_wake_with_command_routes_smalltalk():
    payload = {
        "conversation_id": "mic-wake-command-test",
        "recent_turns": [],
        "raw_transcript": "hey ra how was your day",
        "normalized_transcript": "how was your day",
        "wake_detected": True,
        "command_after_wake": True,
        "wake_phrase": "hey aura",
        "aura_awake": True,
        "wake_required": True,
        "auto_tts": False,
        "effective_address": "Sir",
        "frontend_kind": "smalltalk",
        "vision_context": {
            "camera_status": "active",
            "tracking_status": "active",
            "people_count": 1,
            "tracks": [],
        },
    }
    result = asyncio.run(_call_voice_turn(payload))
    assert result["providers"]["brain"] == "deterministic_smalltalk"
    assert result["response_text"].lower().startswith(("running smoothly", "i'm online", "glad to hear"))


def test_junk_stt_routes_local_junk_filter():
    payload = {
        "conversation_id": "mic-junk-test",
        "recent_turns": [],
        "raw_transcript": "voice command voice input",
        "normalized_transcript": "voice command voice input",
        "aura_awake": True,
        "wake_required": True,
        "auto_tts": False,
        "effective_address": "Sir",
        "frontend_kind": "junk_stt",
    }
    result = asyncio.run(_call_voice_turn(payload))
    assert result["providers"]["brain"] == "deterministic_junk_stt"
    assert "didn’t catch" in result["response_text"] or "didn't catch" in result["response_text"]
