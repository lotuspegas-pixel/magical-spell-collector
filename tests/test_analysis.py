from spell_collector.analyzer import analyze_text, split_rituals
from spell_collector.dedupe import compare_texts, content_hash
from spell_collector.safety import assess_safety


def _source() -> dict[str, object]:
    return {
        "id": "abc123",
        "post_id": "abc123",
        "subreddit": "Spells",
        "url": "https://www.reddit.com/r/Spells/comments/abc123/example/",
        "author": "archived-user",
        "created_utc": 1_700_000_000,
    }


def test_complete_procedural_ritual_is_detected_without_inventing() -> None:
    text = """Protection candle ritual

Materials:
- one white candle
- a bowl of water

Instructions:
1. Place the bowl beside the candle.
2. Light the candle.
3. Say: My home is guarded.

Closing:
- Extinguish the candle.
"""
    result = analyze_text(title="Protection candle ritual", text=text, source=_source())
    assert result.completeness_status == "COMPLETE"
    assert result.materials == ["one white candle", "a bowl of water"]
    assert result.instructions[0] == "Place the bowl beside the candle."
    assert result.rights_status.startswith("UNKNOWN")
    assert "salt" not in str(result.to_dict()).lower()


def test_request_for_a_spell_is_not_complete() -> None:
    result = analyze_text(
        title="Need a love spell?",
        text="Can someone recommend a spell for me? I do not know what to do.",
        source=_source(),
    )
    assert result.completeness_status == "INCOMPLETE"


def test_multiple_explicit_spells_are_split() -> None:
    text = """Spell 1
Instructions:
1. Write a word.
2. Fold the paper.

Spell 2
Instructions:
1. Place a stone.
2. Hold the stone.
"""
    parts = split_rituals("Two rites", text)
    assert len(parts) == 2


def test_dedupe_normalizes_small_formatting_differences() -> None:
    left = "1. Light the candle.\n2. Say the words."
    right = "LIGHT the candle; say the words"
    assessment = compare_texts(left, right)
    assert assessment.relation in {"DUPLICATE", "REVIEW"}
    assert content_hash(left) == content_hash(left)


def test_dangerous_substance_blocks_publication() -> None:
    result = assess_safety("Mix bleach and ammonia in a closed room.")
    assert result.status == "DANGEROUS"
    assert result.publication_allowed is False


def test_detailed_uncertain_plan_requires_review() -> None:
    text = """Materials:
- one candle
Instructions:
1. Write a name.
2. Fold the paper.
3. Place it by the candle.
I am not sure if this is correct. Please help me.
"""
    result = analyze_text(title="Spell plan help", text=text, source=_source())
    assert result.completeness_status == "REVIEW"


def test_binding_with_explicit_free_choice_is_not_auto_flagged() -> None:
    result = assess_safety(
        "A binding spell. May the connection grow through free choice, if it is right for both."
    )
    assert result.status == "NO_RISK_DETECTED"
