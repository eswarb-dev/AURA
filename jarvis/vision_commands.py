from __future__ import annotations

VISION_COMMANDS = {
    "start camera",
    "stop camera",
    "start tracking",
    "stop tracking",
    "track me",
    "track owner",
    "track everyone",
    "track all people",
    "who am i",
    "who is here",
    "how many people do you see",
    "focus on me",
    "scan the room",
    "clear tracking",
}

IDENTITY_PROFILE_QUESTIONS = {
    "do you have any owner enrolled images",
    "do you have my owner image",
    "is owner face enrolled",
    "is my face enrolled",
    "do you know my face",
    "is owner voice enrolled",
    "is my voice enrolled",
    "do you know my voice",
    "am i enrolled",
    "is owner profile ready",
}

FUZZY_VISION_PATTERNS = (
    "can you see me",
    "can you see my hand",
    "are you able to see my hand",
    "do you see my hand",
    "hand visible",
    "spectacles",
    "glasses",
    "am i wearing glasses",
    "am i wearing spectacles",
    "can you specify if i am wearing spectacles",
    "can specify if i am wearing any spectacles",
    "can you see my glasses",
    "do i have spectacles",
    "are you able to see me",
    "are you able to see em",
    "can you able to see me",
    "are able to see me",
    "do you see me",
    "do you see anyone",
    "can you see anyone",
    "can you detect me",
    "who am i",
    "who is here",
    "what do you see",
    "can you check the person",
    "person you see",
    "people you see",
    "through your live video",
    "through your live videos",
    "through your camera",
    "through the camera",
    "through camera",
    "camera feed",
    "video feed",
    "live feed",
    "camera frame",
    "border",
    "inside frame",
    "outside frame",
    "visible or not",
    "am i visible",
    "person visible",
    "face visible",
    "look at me",
    "check the person",
)

NOISY_VISION_CLARIFICATION_PATTERNS = (
    "horrible to see me",
)

NOISY_VISION_NEGATIVE_WORDS = (
    "horrible", "terrible", "awful", "bad", "hate", "scary", "creepy",
)

PEOPLE_COUNT_PATTERNS = (
    "how many person",
    "how many persons",
    "how many people",
    "how many users",
    "how many faces",
    "how many are there",
    "how many do you see",
    "how many on your camera",
    "how many persons on your camera",
    "how many persons on your camera phone",
    "count people",
    "count persons",
    "people count",
    "persons count",
)

ADDRESS_VISIBLE_PERSON_PATTERNS = (
    "address the person you see on camera",
    "address the person you see in camera",
    "address the person you see through camera",
    "address the person in camera",
    "address the person in the camera",
    "address the person on live feed",
    "address whoever is in front of you",
    "greet the person in camera",
    "greet the person in the camera",
    "greet the person you see",
    "say hello to the person in camera",
    "say hello to the person in the camera",
    "say hello to the person you see",
    "how should you address the person",
    "call the person sir or ma'am",
    "call the person sir or mam",
    "address visible person",
    "address the visible person",
    "address current person",
    "address detected person",
)

SENSITIVE_VISION_ATTRIBUTE_PATTERNS = {
    "gender_inference": (
        "identify gender",
        "detect gender",
        "tell gender",
        "recognize gender",
        "classify gender",
        "guess gender",
        "person's gender",
        "persons gender",
        "person gender",
        "person's sex",
        "persons sex",
        "person sex",
        "male or female",
        "man or woman",
        "boy or girl",
        "is this person male",
        "is this person female",
        "is that person male",
        "is that person female",
        "are they male",
        "are they female",
        "is he male",
        "is she female",
        "is this person a man",
        "is this person a woman",
        "is that person a man",
        "is that person a woman",
        "gender through live feed",
        "gender from camera",
        "gender in camera",
        "gender by face",
        "gender from face",
        "gender through camera",
        "gender in live feed",
    ),
}


def normalize_vision_stt(text: str) -> str:
    normalized = " ".join(text.lower().strip().split())
    normalized = normalized.replace("life video", "live video").replace("life videos", "live videos").replace("life it", "live feed")
    normalized = normalized.replace("life feed", "live feed").replace("see em", "see me").replace("wakeup", "wake up")
    normalized = normalized.replace(" cam ", " camera ").replace(" glass ", " glasses ")
    normalized = normalized.replace("phone camera", "camera feed")
    return " ".join(normalized.split())


def is_vision_command(text: str) -> bool:
    normalized = normalize_vision_stt(text)
    if detect_sensitive_vision_attribute(normalized):
        return True
    if detect_live_camera_address_intent(normalized):
        return True
    if needs_vision_clarification(normalized):
        return True
    if normalized in VISION_COMMANDS:
        return True
    if any(p in normalized for p in PEOPLE_COUNT_PATTERNS):
        return True
    return any(p in normalized for p in FUZZY_VISION_PATTERNS)


def needs_vision_clarification(text: str) -> bool:
    normalized = normalize_vision_stt(text)
    if any(pattern in normalized for pattern in NOISY_VISION_CLARIFICATION_PATTERNS):
        return True
    return "see me" in normalized and any(word in normalized.split() for word in NOISY_VISION_NEGATIVE_WORDS)


def detect_sensitive_vision_attribute(text: str) -> str | None:
    normalized = normalize_vision_stt(text).replace("’", "'")
    if any(pattern in normalized for pattern in SENSITIVE_VISION_ATTRIBUTE_PATTERNS["gender_inference"]):
        return "gender_inference"
    gender_words = ("gender", "sex", "male", "female", "man", "woman", "boy", "girl")
    camera_words = ("camera", "feed", "face", "live", "video")
    inference_words = ("identify", "detect", "tell", "recognize", "classify", "guess", "know", "find")
    if any(g in normalized for g in gender_words) and any(c in normalized for c in camera_words) and any(i in normalized for i in inference_words):
        return "gender_inference"
    return None


def detect_live_camera_address_intent(text: str) -> str | None:
    normalized = normalize_vision_stt(text).replace("’", "'")
    if any(pattern in normalized for pattern in ADDRESS_VISIBLE_PERSON_PATTERNS):
        return "address_visible_person"
    address_words = ("address", "greet", "say hello", "call")
    visible_person_words = ("person you see", "visible person", "current person", "detected person", "person in camera", "person in the camera", "whoever is in front")
    if any(word in normalized for word in address_words) and any(word in normalized for word in visible_person_words):
        return "address_visible_person"
    return None


OWNER_IDENTITY_QUESTIONS = (
    "is the person your owner",
    "is this person your owner",
    "is it your owner",
    "is he your owner",
    "is she your owner",
    "is the person you see your owner",
    "person you see is your owner or not",
    "the person your owner",
    "owner or not",
    "am i your owner",
    "do you recognize me",
    "can you recognize me",
    "who am i",
    "is owner visible",
    "is your owner visible",
    "are you able to see your owner",
    "can you see your owner",
    "owner visible or not",
)


def is_owner_identity_question(text: str) -> bool:
    normalized = " ".join(text.lower().strip().replace("?", " ").replace(".", " ").split())
    return any(pattern in normalized for pattern in OWNER_IDENTITY_QUESTIONS)


def is_identity_profile_question(text: str) -> bool:
    normalized = " ".join(text.lower().strip().split())
    return any(pattern in normalized for pattern in IDENTITY_PROFILE_QUESTIONS)


def vision_match_reason(text: str) -> str:
    normalized = normalize_vision_stt(text)
    sensitive_attribute = detect_sensitive_vision_attribute(normalized)
    if sensitive_attribute:
        return f"vision_sensitive_attribute.{sensitive_attribute}"
    address_intent = detect_live_camera_address_intent(normalized)
    if address_intent:
        return f"vision_identity.{address_intent}"
    if needs_vision_clarification(normalized):
        return "vision_clarification"
    if is_owner_identity_question(text):
        return "identity_owner_question"
    if normalized in VISION_COMMANDS:
        return f"exact_command:{normalized}"
    if "who am i" in normalized:
        return "vision_who_am_i"
    if "who is here" in normalized or "what do you see" in normalized:
        return "vision_who_is_here"
    if any(p in normalized for p in PEOPLE_COUNT_PATTERNS):
        return "vision_people_count"
    if any(term in normalized for term in ("border", "frame", "inside", "outside", "check the person", "visible or not")):
        return "vision_visibility_or_frame"
    if any(p in normalized for p in FUZZY_VISION_PATTERNS):
        return "vision_fuzzy_intent"
    return "vision_general"


def is_vision_followup_command(text: str, recent_turns: list[dict[str, str]] | None = None) -> bool:
    normalized = normalize_vision_stt(text)
    
    # Direct follow-up phrases
    direct_phrases = (
        "but there where", "but there were", "there are two people", "there were two people",
        "there were 2 people", "there were 2 peoples", "where 2 peoples", "actually there are 2",
        "actually there were 2", "no there are 2", "no there were 2", "it is 2 people",
        "you missed one", "there are more people", "there are two persons", "not one, two",
        "count is 2", "people count is 2", "2 people", "2 persons"
    )
    if any(phrase in normalized for phrase in direct_phrases):
        return True

    if not recent_turns:
        return False

    last_turn = recent_turns[-1] if recent_turns else {}
    last_assistant = str(last_turn.get("assistant") or "").lower()
    vision_keywords = ("camera feed", "i can see", "person", "people", "tracking", "owner", "visible")
    if any(kw in last_assistant for kw in vision_keywords):
        correction_words = ("but", "actually", "no", "wrong", "missed", "there are", "there were", "count", "people", "person", "two", "three", "2", "3")
        if any(w in normalized for w in correction_words):
            return True

    return False
