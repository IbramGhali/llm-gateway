"""PII redaction. Applied to everything before it is logged, and to prompts before they reach
a provider for teams that require it. Detectors are deliberately conservative about what
they leave behind: card numbers are Luhn-checked, but anything that looks like one of these
identifiers is masked."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass(frozen=True)
class Detector:
    label: str
    pattern: re.Pattern[str]
    check: Callable[[str], bool] = lambda _: True


DETECTORS: list[Detector] = [
    Detector("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    Detector("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b"), lambda m: _luhn(re.sub(r"\D", "", m))),
    Detector("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")),
    Detector("TAX_ID", re.compile(r"\b\d{3}[ -]?\d{3}[ -]?\d{3}\b")),
    # A phone number needs a prefix (+country code, (0X) or 0X area code, or an 04 mobile), so
    # order numbers and other long digit runs are not mistaken for phones.
    Detector(
        "PHONE",
        re.compile(
            r"(?:\+\d{1,3}[ -]?\d{1,2}|\(0\d\)|\b0\d)[ -]?\d{4}[ -]?\d{4}\b"
            r"|\+\d{1,3}[ -]?\d{2,4}[ -]?\d{3,4}[ -]?\d{3,4}\b"
            r"|\b04\d{2}[ -]?\d{3}[ -]?\d{3}\b"
        ),
    ),
    Detector("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


@dataclass
class Redaction:
    text: str
    found: dict[str, int]


def redact(text: str) -> Redaction:
    found: dict[str, int] = {}
    for detector in DETECTORS:

        def replace(match: re.Match[str], d: Detector = detector) -> str:
            if not d.check(match.group(0)):
                return match.group(0)
            found[d.label] = found.get(d.label, 0) + 1
            return f"[{d.label}]"

        text = detector.pattern.sub(replace, text)
    return Redaction(text, found)
