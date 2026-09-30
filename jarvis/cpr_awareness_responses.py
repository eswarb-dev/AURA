"""Mode-aware deterministic CPR awareness responses for A.U.R.A public demos."""
from __future__ import annotations

import os
import re
from typing import Any


CPR_PROVIDER_ROUTE = "deterministic_cpr_awareness"


CPR_QUESTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": 1,
        "type": "survey",
        "question": "Have you heard of CPR before?",
        "options": ("A. Yes", "B. No"),
        "correct_option": None,
        "answer": None,
        "direct_answer": None,
        "survey_label": "No single correct answer - awareness survey",
        "explanation": "CPR means Cardiopulmonary Resuscitation. It is an emergency lifesaving procedure used when someone is unresponsive and not breathing normally.",
        "aliases": ("have you heard of cpr before",),
    },
    {
        "id": 2,
        "type": "quiz",
        "question": "What does CPR stand for?",
        "options": ("A. Cardiac Pulse Recovery", "B. Cardiopulmonary Resuscitation", "C. Cardiac Pressure Response"),
        "correct_option": "B",
        "answer": "Cardiopulmonary Resuscitation",
        "direct_answer": "CPR stands for Cardiopulmonary Resuscitation.",
        "explanation": "CPR helps maintain blood circulation during a cardiac emergency until further medical help becomes available.",
        "aliases": ("what does cpr stand for", "cpr full form", "full form of cpr"),
    },
    {
        "id": 3,
        "type": "quiz",
        "question": "What is cardiac arrest?",
        "options": ("A. A sudden loss of effective heart activity", "B. A mild fever", "C. Chest muscle pain"),
        "correct_option": "A",
        "answer": "A sudden loss of effective heart activity",
        "direct_answer": "Cardiac arrest is a sudden loss of effective heart activity.",
        "explanation": "Cardiac arrest occurs when the heart suddenly stops pumping blood effectively. It is a medical emergency requiring immediate action.",
        "aliases": ("what is cardiac arrest",),
    },
    {
        "id": 4,
        "type": "quiz",
        "question": "Is cardiac arrest the same as a heart attack?",
        "options": ("A. Yes", "B. No", "C. I don't know"),
        "correct_option": "B",
        "answer": "No",
        "direct_answer": "No. Cardiac arrest is not the same as a heart attack.",
        "explanation": "A heart attack involves blocked blood flow to part of the heart, whereas cardiac arrest means the heart suddenly stops pumping effectively. A heart attack can sometimes lead to cardiac arrest.",
        "aliases": ("is cardiac arrest the same as a heart attack", "cardiac arrest same as heart attack"),
    },
    {
        "id": 5,
        "type": "quiz",
        "question": "When should CPR be started?",
        "options": ("A. When someone is sleeping", "B. When someone is unresponsive and not breathing normally", "C. When someone has a headache"),
        "correct_option": "B",
        "answer": "When someone is unresponsive and not breathing normally",
        "direct_answer": "CPR should be started when someone is unresponsive and not breathing normally.",
        "explanation": "Immediate CPR can help maintain blood flow to vital organs when a person is unresponsive and not breathing normally.",
        "aliases": ("when should cpr be started", "when to start cpr"),
    },
    {
        "id": 6,
        "type": "quiz",
        "question": "What should you do when you find someone suddenly collapsed?",
        "options": ("A. Give them water", "B. Check responsiveness and breathing and call for emergency help", "C. Leave them alone"),
        "correct_option": "B",
        "answer": "Check responsiveness and breathing and call for emergency help",
        "direct_answer": "Check responsiveness and breathing and call for emergency help.",
        "explanation": "Check whether the person responds and is breathing normally, call for emergency medical help, and take appropriate action immediately.",
        "aliases": ("what should you do when you find someone suddenly collapsed", "someone suddenly collapsed what should you do"),
    },
    {
        "id": 7,
        "type": "quiz",
        "question": "What is the emergency number in India?",
        "options": ("A. 100", "B. 108", "C. 112"),
        "correct_option": "C",
        "answer": "112",
        "direct_answer": "The emergency number in India is 112.",
        "explanation": "112 is India's unified emergency number. Emergency help should be called without delay.",
        "aliases": ("what is the emergency number in india", "emergency number in india"),
    },
    {
        "id": 8,
        "type": "quiz",
        "question": "Where should chest compressions be given?",
        "options": ("A. On the stomach", "B. On the centre of the chest", "C. On the shoulder"),
        "correct_option": "B",
        "answer": "On the centre of the chest",
        "direct_answer": "Chest compressions should be given on the centre of the chest.",
        "explanation": "For an adult, chest compressions are performed on the centre of the chest.",
        "aliases": ("where should chest compressions be given", "where to give chest compressions"),
    },
    {
        "id": 9,
        "type": "quiz",
        "question": "What is the recommended rate of adult chest compressions?",
        "options": ("A. 40-60 per minute", "B. 100-120 per minute", "C. 150-180 per minute"),
        "correct_option": "B",
        "answer": "100-120 per minute",
        "direct_answer": "The recommended adult chest compression rate is 100 to 120 per minute.",
        "explanation": "Adult CPR should use a compression rate of approximately 100 to 120 compressions per minute.",
        "aliases": ("what is the recommended rate of adult chest compressions", "adult chest compression rate", "recommended rate of chest compressions"),
    },
    {
        "id": 10,
        "type": "quiz",
        "question": "How deep should adult chest compressions generally be?",
        "options": ("A. About 1 cm", "B. At least 5 cm, about 2 inches", "C. About 10 cm"),
        "correct_option": "B",
        "answer": "At least 5 cm, about 2 inches",
        "direct_answer": "Adult chest compressions should generally be at least 5 cm deep, about 2 inches.",
        "explanation": "Adult compressions should generally be at least 5 cm deep, while allowing the chest to return fully between compressions.",
        "aliases": ("how deep should adult chest compressions generally be", "adult chest compression depth", "how deep chest compressions"),
    },
    {
        "id": 11,
        "type": "quiz",
        "question": "What does AED stand for?",
        "options": ("A. Automated External Defibrillator", "B. Automatic Emergency Device", "C. Advanced Electric Detector"),
        "correct_option": "A",
        "answer": "Automated External Defibrillator",
        "direct_answer": "AED stands for Automated External Defibrillator.",
        "explanation": "An AED analyses the heart rhythm and, when appropriate, delivers an electrical shock to help restore a normal rhythm.",
        "aliases": ("what does aed stand for", "aed full form", "full form of aed"),
    },
    {
        "id": 12,
        "type": "quiz",
        "question": "Can an untrained person use an AED?",
        "options": ("A. No, only doctors can use it", "B. Yes, following the device's instructions", "C. Only nurses can use it"),
        "correct_option": "B",
        "answer": "Yes, following the device's instructions",
        "direct_answer": "Yes. An untrained person can use an AED by following the device's instructions.",
        "explanation": "AEDs are designed to guide users using voice and visual instructions. The user should follow the device and emergency responder guidance.",
        "aliases": ("can an untrained person use an aed", "can untrained person use aed"),
    },
    {
        "id": 13,
        "type": "quiz",
        "question": "Should you stop CPR to look for an AED?",
        "options": ("A. Yes", "B. No, continue CPR while the AED is obtained and prepared"),
        "correct_option": "B",
        "answer": "No, continue CPR while the AED is obtained and prepared",
        "direct_answer": "No. CPR should continue while the AED is obtained and prepared.",
        "explanation": "CPR should continue while someone retrieves and prepares the AED, with interruptions kept as short as possible.",
        "aliases": ("should you stop cpr to look for an aed", "stop cpr to look for aed"),
    },
    {
        "id": 14,
        "type": "quiz",
        "question": "Can a person who is not a doctor learn CPR?",
        "options": ("A. Yes", "B. No", "C. Only medical students can learn"),
        "correct_option": "A",
        "answer": "Yes",
        "direct_answer": "Yes. A person who is not a doctor can learn CPR.",
        "explanation": "Members of the public can learn CPR. Training can help people respond more confidently during an emergency.",
        "aliases": ("can a person who is not a doctor learn cpr", "can non doctors learn cpr", "can anyone learn cpr"),
    },
    {
        "id": 15,
        "type": "survey",
        "question": "Have you ever received CPR training?",
        "options": ("A. Yes", "B. No", "C. I have seen CPR but never received training"),
        "correct_option": None,
        "answer": None,
        "direct_answer": None,
        "survey_label": "No single correct answer - personal survey",
        "explanation": "Watching CPR is different from practising it. Hands-on training can help build practical skills and confidence.",
        "aliases": ("have you ever received cpr training",),
    },
    {
        "id": 16,
        "type": "survey",
        "question": "Would you feel confident performing CPR in an emergency?",
        "options": ("A. Yes", "B. No", "C. Not sure"),
        "correct_option": None,
        "answer": None,
        "direct_answer": None,
        "survey_label": "No single correct answer - personal survey",
        "explanation": "Feeling unsure is normal. CPR training can help people recognise emergencies and respond more confidently.",
        "aliases": ("would you feel confident performing cpr in an emergency", "confident performing cpr"),
    },
    {
        "id": 17,
        "type": "quiz",
        "question": "Why is early CPR important?",
        "options": ("A. It can help maintain blood flow to the brain and other vital organs", "B. It reduces body temperature", "C. It helps the person sleep"),
        "correct_option": "A",
        "answer": "It can help maintain blood flow to the brain and other vital organs",
        "direct_answer": "Early CPR is important because it can help maintain blood flow to the brain and other vital organs.",
        "explanation": "During cardiac arrest, CPR helps circulate blood to vital organs until further medical care becomes available.",
        "aliases": ("why is early cpr important", "importance of early cpr"),
    },
    {
        "id": 18,
        "type": "quiz",
        "question": "What should you NOT do when someone is unconscious and not breathing normally?",
        "options": ("A. Call for emergency help", "B. Start CPR when appropriate", "C. Give them food or water"),
        "correct_option": "C",
        "answer": "Give them food or water",
        "direct_answer": "Do not give food or water to someone who is unconscious and not breathing normally.",
        "explanation": "Do not give food or water to an unconscious person. Focus on emergency response, CPR when appropriate, and obtaining professional help.",
        "aliases": ("what should you not do when someone is unconscious and not breathing normally", "what not to do when someone is unconscious and not breathing"),
    },
    {
        "id": 19,
        "type": "survey",
        "question": "Do you think every college student should learn basic CPR?",
        "options": ("A. Yes", "B. No", "C. Not sure"),
        "correct_option": None,
        "answer": None,
        "direct_answer": None,
        "survey_label": "No single correct answer - opinion survey",
        "explanation": "CPR is a valuable life skill, and learning it can help people respond appropriately during a cardiac emergency.",
        "aliases": ("do you think every college student should learn basic cpr", "should every college student learn basic cpr"),
    },
    {
        "id": 20,
        "type": "survey",
        "question": "Would you be ready to learn CPR and become a lifesaver?",
        "options": ("A. YES!", "B. I want to learn more", "C. I already know CPR"),
        "correct_option": None,
        "answer": None,
        "direct_answer": None,
        "survey_label": "No single correct answer - awareness/motivation survey",
        "explanation": "Learning CPR can help a person become better prepared to respond when someone needs emergency help.",
        "aliases": ("would you be ready to learn cpr and become a lifesaver", "ready to learn cpr", "become a lifesaver"),
    },
)


def cpr_awareness_enabled() -> bool:
    return os.environ.get("AURA_CPR_AWARENESS_MODE", "true").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _norm(text: str) -> str:
    cleaned = (text or "").lower().replace("c.p.r.", "cpr").replace("a.e.d.", "aed")
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _option_value(option: str) -> str:
    return option.split(".", 1)[1].strip() if "." in option else option.strip()


def _selected_option(text: str) -> str | None:
    norm = _norm(text)
    match = re.search(r"\b(?:answer|option|choose|selected)?\s*([abc])\b", norm)
    return match.group(1).upper() if match else None


def format_direct_answer(item: dict[str, Any]) -> str:
    direct = item.get("direct_answer")
    if direct:
        return f"{direct} {item['explanation']}"
    return f"{item['question']} {item.get('survey_label') or 'No single correct answer'}. {item['explanation']}"


def format_quiz_question(item: dict[str, Any]) -> str:
    lines = [f"CPR Awareness Question {item['id']}:", item["question"]]
    lines.extend(item["options"])
    return "\n".join(lines)


def format_correct_feedback(item: dict[str, Any]) -> str:
    return f"Correct. The answer is {item['answer']}. {item['explanation']}"


def format_wrong_feedback(item: dict[str, Any]) -> str:
    return f"That's not the correct answer. The correct answer is {item['answer']}. {item['explanation']}"


def format_survey_response(item: dict[str, Any]) -> str:
    return f"Thanks for sharing. {item['explanation']}"


def build_cpr_response(item: dict[str, Any], *, mode: str, selected_option: str | None = None) -> str:
    if mode == "direct":
        return format_direct_answer(item)
    if mode == "quiz_question":
        return format_quiz_question(item)
    if mode == "quiz_answer":
        if item.get("type") == "survey":
            return format_survey_response(item)
        if (selected_option or "").upper() == str(item.get("correct_option") or "").upper():
            return format_correct_feedback(item)
        return format_wrong_feedback(item)
    raise ValueError(f"Unsupported CPR response mode: {mode}")


def _summary() -> str:
    questions = "; ".join(f"{item['id']}. {item['question']}" for item in CPR_QUESTIONS)
    return f"CPR Awareness Robot has 20 deterministic public questions: {questions}"


def _result(
    intent: str,
    response_text: str,
    normalized_text: str,
    question_id: int | None = None,
    matched_phrase: str | None = None,
    mode: str = "direct",
) -> dict[str, Any]:
    return {
        "matched": True,
        "intent": intent,
        "response_text": response_text,
        "provider_route": CPR_PROVIDER_ROUTE,
        "debug": {
            "enabled": True,
            "matched": True,
            "intent": intent,
            "question_id": question_id,
            "matched_phrase": matched_phrase,
            "mode": mode,
            "normalized_text": normalized_text,
            "canonical_content_used": True,
            "cloud_bypassed": True,
            "cloud_bypass_reason": "cpr_awareness_canonical_content",
        },
    }


def match_cpr_awareness_response(text: str) -> dict[str, Any] | None:
    if not cpr_awareness_enabled():
        return None
    norm = _norm(text)
    if not norm:
        return None

    if "cpr" in norm and any(phrase in norm for phrase in ("all questions", "20 questions", "quiz questions", "awareness questions", "question list")):
        return _result("cpr_awareness.list", _summary(), norm, mode="list")

    answer_match = re.search(r"\b(?:answer|option|choose|selected)\s+([abc])\s+(?:for\s+)?(?:cpr\s+)?(?:question|q)\s*(?:number\s*)?([1-9]|1[0-9]|20)\b", norm)
    if not answer_match:
        answer_match = re.search(r"\b(?:cpr\s+)?(?:question|q)\s*(?:number\s*)?([1-9]|1[0-9]|20)\s+(?:answer|option|choose|selected)\s+([abc])\b", norm)
        if answer_match:
            question_id = int(answer_match.group(1))
            selected = answer_match.group(2).upper()
        else:
            question_id = 0
            selected = None
    else:
        selected = answer_match.group(1).upper()
        question_id = int(answer_match.group(2))
    if selected and question_id:
        item = CPR_QUESTIONS[question_id - 1]
        return _result(
            f"cpr_awareness.question_{question_id}.answer",
            build_cpr_response(item, mode="quiz_answer", selected_option=selected),
            norm,
            question_id,
            f"answer_{selected}",
            mode="quiz_answer",
        )

    number_match = re.search(r"\b(?:cpr\s+)?(?:question|q)\s*(?:number\s*)?([1-9]|1[0-9]|20)\b", norm)
    if number_match:
        question_id = int(number_match.group(1))
        item = CPR_QUESTIONS[question_id - 1]
        return _result(
            f"cpr_awareness.question_{question_id}",
            build_cpr_response(item, mode="quiz_question"),
            norm,
            question_id,
            "question_number",
            mode="quiz_question",
        )

    for item in CPR_QUESTIONS:
        for phrase in item["aliases"]:
            phrase_norm = _norm(phrase)
            if phrase_norm and phrase_norm in norm:
                return _result(
                    f"cpr_awareness.question_{item['id']}",
                    build_cpr_response(item, mode="direct"),
                    norm,
                    int(item["id"]),
                    phrase,
                    mode="direct",
                )
    return None
