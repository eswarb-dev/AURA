"""Conservative offline explanations used only after cloud reasoning fails."""
import re

GENERAL_PROVIDER_TIMEOUT_SECONDS = 6


def is_general_question(query: str) -> bool:
    text = " ".join(query.lower().strip().split())
    return bool(re.match(r"^(what is|who is|why|how|explain|tell me about|meaning of|define)\b", text)) or bool(
        re.fullmatch(r"[a-z][a-z -]*[?.!]?", text) and 1 <= len(text.split()) <= 3
    )


def build_general_helpful_fallback(query: str) -> str:
    text = " ".join(str(query).lower().replace("\u2019", "'").split()).strip(" .?!")
    # Match complete concepts, not keywords inside sensitive or live questions.
    topic = re.sub(r"^(?:what is|explain|tell me about|meaning of|define)\s+", "", text)
    topic = re.sub(r"^(?:a|an|the)\s+", "", topic)
    if topic == 'ai':
        topic = 'artificial intelligence'
    definitions = {
        "cup": "A cup is a small container used for drinking liquids like water, tea, or coffee, Sir.",
        "quantum computing": "Quantum computing uses quantum bits, or qubits, and effects such as superposition and entanglement to process information. It may solve certain problems more efficiently than classical computing, Sir.",
        "artificial intelligence": "Artificial intelligence is the field of building computer systems that perform tasks such as recognizing patterns, understanding language, and making predictions, Sir.",
        "machine learning": "Machine learning is a branch of artificial intelligence in which systems learn patterns from data to make predictions or decisions, Sir.",
        "computer": "A computer is an electronic device that follows instructions to process data, store information, and produce results, Sir.",
        "engineering": "Engineering applies science and mathematics to design, build, and improve practical systems, structures, and machines, Sir.",
        "science": "Science studies the natural world through observation, experiments, and evidence-based explanations, Sir.",
        "science topics": "Science includes physics, chemistry, biology, and Earth science. Which topic would you like to explore, Sir?",
    }
    if topic in definitions:
        if topic == "quantum computing" and text.startswith("tell me about"):
            return "Quantum computing uses qubits instead of ordinary bits. Superposition and entanglement let quantum algorithms process information differently, potentially helping with some problems in chemistry, cryptography, and optimization, Sir."
        return definitions[topic]
    if re.search(r"\b(current|currently|today|latest|live|president|prime minister|price|weather|news)\b", text):
        return "That needs a current, verified source, Sir. Please check an official live source for an up-to-date answer."
    return "I can still help, Sir. Please ask the question again in a simpler way."
