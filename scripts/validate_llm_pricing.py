#!/usr/bin/env python3
"""Validate llm_pricing.json against the rules OpenObserve relies on when it syncs it.

Checks structure, that binaries which ignore `rules` still price at standard rates, and that
no two tiers can match the same span with equal rank (which would leave list order to decide).
"""

import json
import math
import re
import sys
from itertools import combinations

KNOWN_OPS = {"in", "not_in", "exists", "not_exists", "gt", "gte", "lt", "lte"}
SET_OPS = {"in", "not_in"}
NUMERIC_OPS = {"gt", "gte", "lt", "lte"}
MAX_TIERS, MAX_RULES, MAX_KEYS, MAX_VALUES, MAX_LEN = 32, 8, 16, 64, 256


def norm_key(key):
    return re.sub(r"[_.\-]", "", key).lower()


def restrictions(tier):
    """Canonical restriction items, duplicates kept because the backend's rank counts every rule."""
    items = []
    cond = tier.get("condition")
    if cond:
        items.append(("cond", cond["usage_key"], cond["operator"], float(cond["value"])))
    windows = tier.get("utc_windows") or []
    if windows:
        items.append(("win", tuple((w["start_minute"], w["end_minute"]) for w in windows)))
    for rule in tier.get("rules") or []:
        items.append(
            (
                "rule",
                tuple(norm_key(k) for k in rule["keys"]),
                rule["op"],
                tuple(sorted(str(v).strip().lower() for v in rule.get("values", []))),
                rule.get("value"),
            )
        )
    return items


def rank(tier):
    return (tier.get("priority", 0), len(restrictions(tier)))


def is_default(tier):
    return not restrictions(tier)


def cond_range(cond):
    v = float(cond["value"])
    op = cond["operator"]
    if op in ("gt", "gte"):
        return (v, math.inf, op == "gte", False)
    if op in ("lt", "lte"):
        return (-math.inf, v, False, op == "lte")
    return None


def disjoint_ranges(a, b):
    lo_a, hi_a, lo_in_a, hi_in_a = a
    lo_b, hi_b, lo_in_b, hi_in_b = b
    if hi_a < lo_b or (hi_a == lo_b and not (hi_in_a and lo_in_b)):
        return True
    if hi_b < lo_a or (hi_b == lo_a and not (hi_in_b and lo_in_a)):
        return True
    return False


def exclusive(a, b):
    """True only when no span can match both tiers."""
    for ra in a.get("rules") or []:
        for rb in b.get("rules") or []:
            same_keys = [norm_key(k) for k in ra["keys"]] == [norm_key(k) for k in rb["keys"]]
            if not same_keys or {ra["op"], rb["op"]} - SET_OPS:
                continue
            va = {str(v).strip().lower() for v in ra["values"]}
            vb = {str(v).strip().lower() for v in rb["values"]}
            ops = (ra["op"], rb["op"])
            if ops == ("in", "in") and not va & vb:
                return True
            if (ops == ("in", "not_in") and va <= vb) or (ops == ("not_in", "in") and vb <= va):
                return True
    ca, cb = a.get("condition"), b.get("condition")
    if ca and cb and ca["usage_key"] == cb["usage_key"]:
        ra, rb = cond_range(ca), cond_range(cb)
        if ra and rb and disjoint_ranges(ra, rb):
            return True
    return False


def check_rule(where, rule, errors):
    if rule.get("op") not in KNOWN_OPS:
        errors.append(f"{where}: unknown op {rule.get('op')!r}")
    keys = rule.get("keys") or []
    if not isinstance(keys, list) or not isinstance(rule.get("values", []), list):
        errors.append(f"{where}: keys and values must be lists")
        return
    if not keys or not all(isinstance(k, str) and k.strip() for k in keys):
        errors.append(f"{where}: rule keys must be non-empty strings")
    if len(keys) > MAX_KEYS or len(rule.get("values", [])) > MAX_VALUES:
        errors.append(f"{where}: rule exceeds {MAX_KEYS} keys / {MAX_VALUES} values")
    for s in keys + rule.get("values", []):
        if not isinstance(s, str) or len(s) > MAX_LEN:
            errors.append(f"{where}: keys and values must be strings of at most {MAX_LEN} chars")
    if rule.get("op") in SET_OPS and not any(str(v).strip() for v in rule.get("values", [])):
        errors.append(f"{where}: {rule['op']} needs values")
    if rule.get("op") in NUMERIC_OPS:
        value = rule.get("value")
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            errors.append(f"{where}: {rule['op']} needs a finite numeric value")


def check_tier(where, tier, errors):
    prices = tier.get("prices") or {}
    if not prices:
        errors.append(f"{where}: no prices")
    for key, price in prices.items():
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0:
            errors.append(f"{where}: price {key!r} must be a finite number >= 0")
    rules = tier.get("rules") or []
    if len(rules) > MAX_RULES:
        errors.append(f"{where}: more than {MAX_RULES} rules")
    for i, rule in enumerate(rules):
        check_rule(f"{where} rule {i}", rule, errors)
    if "param_conditions" in tier:
        errors.append(f"{where}: param_conditions is not supported; use rules")


def check_old_binary_layout(name, tiers, errors):
    """Binaries without `rules` see a rule tier as its condition/window alone, or as unrestricted."""
    for i, tier in enumerate(tiers):
        if not tier.get("rules"):
            continue
        cond, windows = tier.get("condition"), tier.get("utc_windows") or []
        if not cond and not windows:
            continue  # the unrestricted tier is first, so old binaries never fall back to this one
        twin = [
            j
            for j, t in enumerate(tiers[:i])
            if not t.get("rules") and t.get("condition") == cond and (t.get("utc_windows") or []) == windows
        ]
        if not twin:
            errors.append(
                f"{name} / {tier['name']}: tier with rules plus a condition or window needs an earlier "
                "tier with the same condition/window and no rules, or old binaries charge its rate"
            )


def check_ambiguity(name, tiers, errors):
    restricted = [t for t in tiers if not is_default(t)]
    for a, b in combinations(restricted, 2):
        if rank(a) != rank(b) or exclusive(a, b):
            continue
        # A tier whose restrictions all come from a or b matches any span both match; outranking them settles it.
        union = set(restrictions(a)) | set(restrictions(b))
        if any(rank(t) > rank(a) and set(restrictions(t)) <= union for t in restricted):
            continue
        errors.append(
            f"{name}: tiers {a['name']!r} and {b['name']!r} can match the same span with equal rank; "
            "add a combined tier covering both or set priority"
        )


def validate(entries):
    errors = []
    if not isinstance(entries, list):
        return ["top level must be a list"]
    seen = set()
    for entry in entries:
        name = entry.get("name", "<unnamed>")
        if name in seen:
            errors.append(f"{name}: duplicate entry name")
        seen.add(name)
        if not entry.get("match_pattern"):
            errors.append(f"{name}: missing match_pattern")
        else:
            try:
                re.compile(entry["match_pattern"])
            except re.error as e:
                errors.append(f"{name}: invalid match_pattern: {e}")
        tiers = entry.get("tiers") or []
        if not tiers or len(tiers) > MAX_TIERS:
            errors.append(f"{name}: needs 1-{MAX_TIERS} tiers")
            continue
        tier_names = [t.get("name") for t in tiers]
        if len(set(tier_names)) != len(tier_names):
            errors.append(f"{name}: duplicate tier names")
        before = len(errors)
        for tier in tiers:
            check_tier(f"{name} / {tier.get('name')}", tier, errors)
        if len(errors) > before:
            continue  # the layout checks below assume well-formed tiers
        if not is_default(tiers[0]):
            errors.append(f"{name}: the first tier must be the unrestricted default")
        check_old_binary_layout(name, tiers, errors)
        check_ambiguity(name, tiers, errors)
    return errors


def main(path):
    with open(path) as f:
        entries = json.load(f)
    errors = validate(entries)
    for e in errors:
        print(f"error: {e}")
    print(f"{len(entries)} entries checked, {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "llm_pricing.json"))
