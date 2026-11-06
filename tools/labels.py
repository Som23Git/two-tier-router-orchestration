"""The label set: one definition used by Laya, the LLM classifiers, the
workflow and the benchmark, so every classifier answers the same questions.

Descriptions are short on purpose: Laya packs every option of a question into
about 192 tokens, and the LLM prompt is sent with every message.

Avoid negation in descriptions. The first wording described "other" as
"neither records an expense nor asks about spending", and Laya, an encoder
that matches words, put 35 of 36 expense messages under "other". Short,
positive, user-voiced descriptions lifted intent accuracy from 42% to 75% on a
24-message sample. An LLM reads either wording correctly.
"""

import json

INTENTS = {
    "index": "save this purchase to my expenses",
    "search": "look up my past spending",
    "other": "something else: greeting, help, reminder, budget or general question",
}

CATEGORIES = {
    "coffee": "coffee shops and cafes",
    "groceries": "supermarkets and food shopping",
    "dining": "restaurants, takeaway and food delivery",
    "transport": "taxis, rideshare, fuel, parking, public transit",
    "travel": "flights, hotels and holiday bookings",
    "housing": "rent, mortgage and utility bills",
    "shopping": "clothes, electronics and general retail",
    "entertainment": "movies, concerts, games and subscriptions",
    "health": "pharmacy, doctor, dentist and gym",
    "all": "overall spending across every category",
}

PERIODS = {
    "today": "today",
    "this_week": "the current week",
    "last_week": "the previous week",
    "this_month": "the current month",
    "last_month": "the previous month",
    "this_year": "the current year",
    "all_time": "all recorded history, overall",
    "not_applicable": "a purchase being saved right now",
}

QUESTIONS = {
    "intent": ("What does the user want to do with this message?", INTENTS),
    "category": ("Which spending category is the message about?", CATEGORIES),
    "period": ("Which time period does the message ask about?", PERIODS),
}


def laya_questions():
    """The `questions` object of a Laya /v1/systemone request: one choice question per field."""
    return {
        qid: {"type": "choice", "instructions": instructions, "criteria": dict(options)}
        for qid, (instructions, options) in QUESTIONS.items()
    }


def llm_prompt(message):
    """The same three questions for a generative model, answered as plain JSON."""
    lines = ["Classify the user's message. Answer each question with exactly one of its options.", ""]
    for qid, (instructions, options) in QUESTIONS.items():
        lines.append(f"{qid}: {instructions}")
        lines += [f"  - {name}: {desc}" for name, desc in options.items()]
        lines.append("")
    lines += [
        'Respond with only this JSON object, no prose and no code fence:',
        '{"intent": "<option>", "category": "<option>", "period": "<option>"}',
        "",
        f"Message: {message}",
    ]
    return "\n".join(lines)


def llm_prompt_template():
    """llm_prompt with a Liquid placeholder for the message, for the workflow YAML."""
    return llm_prompt("{{ inputs.text }}")


if __name__ == "__main__":
    print(json.dumps(laya_questions(), indent=2))
    print(llm_prompt("paid $4.50 for a flat white at Blue Bottle"))
