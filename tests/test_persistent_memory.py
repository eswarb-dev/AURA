from jarvis.memory.memory_manager import MemoryManager
from jarvis.memory.memory_types import MemoryConfig
from jarvis.memory.redaction import redact_for_memory, redact_metadata_for_memory


def test_memory_redacts_secret_like_values():
    text = "my api key is sk-test-1234567890abcdef"
    redacted = redact_for_memory(text)
    assert "sk-test-1234567890abcdef" not in redacted
    assert "[REDACTED]" in redacted


def test_metadata_redacts_audio_and_clipboard_values():
    safe = redact_metadata_for_memory({
        "audio_base64": "a" * 200,
        "clipboard_text": "password=abc12345",
        "provider_route": "deterministic_tool_command",
    })
    assert safe["audio_base64"] == "[REDACTED]"
    assert safe["clipboard_text"] == "[REDACTED]"
    assert safe["provider_route"] == "deterministic_tool_command"


def test_memory_disabled_status_and_context_do_not_fail():
    manager = MemoryManager(MemoryConfig(backend="disabled", local_fallback=False))
    status = manager.memory_status()
    context = manager.load_context_for_turn("00000000-0000-0000-0000-000000000000", "hello")
    assert status["enabled"] is False
    assert context.backend == "disabled"


def test_deterministic_fact_extraction_categories():
    manager = MemoryManager(MemoryConfig(backend="disabled", local_fallback=False))
    extracted = manager.maybe_extract_memory_facts("remember that I prefer Tamil input but English technical terms are okay")
    assert extracted == []

    manager = MemoryManager(MemoryConfig(backend="local_fallback", local_fallback=True))
    facts = manager.maybe_extract_memory_facts("remember that I prefer Tamil input but English technical terms are okay")
    assert facts
    assert facts[0]["category"] == "language_preference"
