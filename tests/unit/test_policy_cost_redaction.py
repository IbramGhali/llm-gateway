from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from llm_gateway.config import GatewayConfig, Settings, load_config
from llm_gateway.evaluation import pii_dataset
from llm_gateway.gateway import cost
from llm_gateway.prompts import PromptRegistry
from llm_gateway.redaction import redact
from llm_gateway.reliability import Breakers


def test_committed_config_is_valid() -> None:
    config = load_config(Settings().config_path)
    assert config.chain("smart") == ["claude-opus", "demo-large", "claude-sonnet"]
    assert config.chain("claude-sonnet") == ["claude-sonnet"]
    assert config.chain("nope") == []


def test_teams_cannot_use_unpriced_models() -> None:
    data = load_config(Settings().config_path).model_dump()
    data["teams"]["support"]["allow"].append("gpt")  # gpt has no price yet
    with pytest.raises(ValidationError, match="no price"):
        GatewayConfig.model_validate(data)


def test_routes_must_reference_known_models() -> None:
    data = load_config(Settings().config_path).model_dump()
    data["routes"]["broken"] = ["does-not-exist"]
    with pytest.raises(ValidationError, match="unknown model"):
        GatewayConfig.model_validate(data)


def test_cost_maths_uses_decimals_and_rounds_to_micro_dollars() -> None:
    assert cost(1_000_000, 1_000_000, Decimal("4.00"), Decimal("20.00")) == Decimal("24.000000")
    assert cost(1234, 567, Decimal("0.10"), Decimal("0.50")) == Decimal("0.000407")
    assert cost(0, 0, Decimal("4"), Decimal("20")) == Decimal("0.000000")


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("mail me at jo.smith+tag@example.co.uk", "EMAIL"),
        ("card 4111 1111 1111 1111 please", "CARD"),
        ("call +61 3 9123 4567 today", "PHONE"),
        ("mobile 0412 345 678", "PHONE"),
        ("tax file 123 456 782", "TAX_ID"),
        ("from 192.168.10.24", "IP"),
        ("IBAN GB82 WEST 1234 5698 7654 32", "IBAN"),
    ],
)
def test_detectors(text: str, label: str) -> None:
    result = redact(text)
    assert f"[{label}]" in result.text and result.found.get(label) == 1


def test_non_luhn_numbers_and_amounts_are_kept() -> None:
    text = "Order 1234 5678 9012 3456 cost $249.99 on 12 March"
    assert redact(text).text.count("$249.99") == 1
    assert "1234 5678 9012 3456" in redact(text).text  # fails the Luhn check: not a card


def test_synthetic_pii_set_is_fully_redacted() -> None:
    for text, values in pii_dataset(n=200):
        scrubbed = redact(text).text
        assert all(v not in scrubbed for v in values), (text, scrubbed)


def test_prompt_registry_versions(tmp_path: Path) -> None:
    registry = PromptRegistry(Settings().prompts_dir)
    latest = registry.get("summarise-ticket")
    assert latest.ref == "summarise-ticket@v2"
    assert registry.get("summarise-ticket", 1).ref == "summarise-ticket@v1"
    with pytest.raises(ValueError, match="needs variable"):
        latest.render({})
    with pytest.raises(LookupError):
        registry.get("summarise-ticket", 9)


def test_breaker_opens_and_recovers() -> None:
    breakers = Breakers(threshold=2, cooldown=0.05)
    breakers.record("m", ok=False)
    assert breakers.allow("m")
    breakers.record("m", ok=False)
    assert not breakers.allow("m") and breakers.trips() == {"m": 1}
    time.sleep(0.2)  # generous margin: Windows timer resolution is about 15 ms
    assert breakers.allow("m")  # half-open trial
    breakers.record("m", ok=True)
    assert breakers.allow("m")
