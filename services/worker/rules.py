"""Behaviour rules: each one matches events and adds to the sample's score.

A rule's "match" names an event type plus regular expressions for its fields;
an event matches when every expression matches its field (re.search).
"""
import re
from pathlib import Path

import yaml

MALICIOUS_SCORE = 60
SUSPICIOUS_SCORE = 25


def load_rules(path=Path(__file__).with_name("rules.yaml")):
    rules = yaml.safe_load(Path(path).read_text())
    for rule in rules:
        match = dict(rule["match"])
        rule["_event"] = match.pop("event")
        rule["_fields"] = {k: re.compile(v) for k, v in match.items()}
    return rules


def _matches(rule, event):
    return event["event"] == rule["_event"] and all(
        pattern.search(event.get(field, "")) for field, pattern in rule["_fields"].items())


def evaluate(rules, events):
    """Return (signatures, score, verdict). Each rule counts once, however often it fires."""
    signatures = []
    for rule in rules:
        hits = [e for e in events if _matches(rule, e)]
        if hits:
            evidence = [{k: v for k, v in e.items() if k != "event"} for e in hits[:5]]
            signatures.append({"id": rule["id"], "description": rule["description"],
                               "score": rule["score"], "evidence": evidence})
    score = min(sum(s["score"] for s in signatures), 100)
    if score >= MALICIOUS_SCORE:
        verdict = "malicious"
    elif score >= SUSPICIOUS_SCORE:
        verdict = "suspicious"
    else:
        verdict = "clean"
    return signatures, score, verdict
