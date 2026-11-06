import json

from spell_collector.ollama import OllamaAnalyzer


def test_ollama_validation_downgrades_untraceable_evidence() -> None:
    response = {
        "message": {
            "content": json.dumps(
                {
                    "spells": [
                        {
                            "title": "Generated title",
                            "purpose": "protection",
                            "materials": [],
                            "preparation": [],
                            "instructions": ["Invented step"],
                            "incantation": "",
                            "timing": "",
                            "closing_steps": [],
                            "language": "en",
                            "description_nl": "Broninterpretatie.",
                            "status": "COMPLETE",
                            "completeness_score": 90,
                            "confidence": 0.9,
                            "evidence": {
                                "purpose": ["protection"],
                                "instructions": ["not in source"],
                            },
                        }
                    ]
                }
            )
        }
    }

    def fake_request(_request, _timeout):
        return json.dumps(response).encode()

    analyzer = OllamaAnalyzer("local-model", request_fn=fake_request)
    result = analyzer.analyze("A protection ritual with no instructions.")
    assert result.spells[0]["status"] == "REVIEW"
    assert result.spells[0]["confidence"] == 0.49
    assert result.validation_errors
