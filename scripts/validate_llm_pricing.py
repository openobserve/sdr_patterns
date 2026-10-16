#!/usr/bin/env python3
"""Validate llm_pricing.json against the rules OpenObserve relies on when it syncs it.

Mirrors the backend's parsing and tier validation (an entry it rejects is silently skipped by
the sync), checks that binaries which ignore `rules` still price at standard rates, and that
tier selection is never left to chance: with ranking, no two tiers may match one span with
equal rank; with first-match (no rules, no priority), no tier may be unreachable.
"""

import json
import math
import re
import sys
from itertools import combinations

KNOWN_OPS = {"in", "not_in", "exists", "not_exists", "gt", "gte", "lt", "lte"}
SET_OPS = {"in", "not_in"}
NUMERIC_OPS = {"gt", "gte", "lt", "lte"}
PRESENCE_OPS = {"exists", "not_exists"}
CONDITION_OPS = {"gt", "gte", "lt", "lte", "eq", "neq"}
MAX_TIERS, MAX_RULES, MAX_KEYS, MAX_VALUES, MAX_LEN = 32, 8, 16, 64, 256
MAX_PRIORITY = 1000
MAX_PATTERN_LEN = 512
MINUTES_PER_DAY = 1440
USAGE_PREFIX = "usage."


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def byte_len(s):
    return len(s.encode("utf-8"))


def norm_key(key):
    return re.sub(r"[_.\-]", "", key).lower()


def usage_key(key):
    key = key.strip()
    return key[len(USAGE_PREFIX):] if key[: len(USAGE_PREFIX)].lower() == USAGE_PREFIX else None


def norm_value(v):
    """Backend `in` matches case-insensitively, and numerically when both sides are numbers."""
    s = str(v).strip()
    try:
        f = float(s)
    except ValueError:
        return s.lower()
    return repr(f) if math.isfinite(f) else s.lower()


# ---- structure: everything the backend's serde parsing and validate_tiers reject ----


def check_rule(where, rule, errors):
    if not isinstance(rule, dict):
        errors.append(f"{where}: rule must be an object")
        return
    op = rule.get("op")
    if op not in KNOWN_OPS:
        errors.append(f"{where}: unknown op {op!r}")
    keys, values = rule.get("keys"), rule.get("values", [])
    if not isinstance(keys, list) or not isinstance(values, list):
        errors.append(f"{where}: keys and values must be lists")
        return
    if not keys or not all(isinstance(k, str) and k.strip() for k in keys):
        errors.append(f"{where}: rule keys must be non-empty strings")
        return
    if len(keys) > MAX_KEYS or len(values) > MAX_VALUES:
        errors.append(f"{where}: rule exceeds {MAX_KEYS} keys / {MAX_VALUES} values")
    if not all(isinstance(v, str) for v in values):
        errors.append(f"{where}: rule values must be strings")
        return
    if any(byte_len(s) > MAX_LEN for s in keys + values):
        errors.append(f"{where}: keys and values must be at most {MAX_LEN} bytes")
    usage = [u for u in map(usage_key, keys) if u is not None]
    if any(not u.strip() for u in usage):
        errors.append(f"{where}: a usage key must name a usage type after 'usage.'")
    if usage and op in PRESENCE_OPS:
        errors.append(f"{where}: {op} never varies on a usage key (a missing count reads as 0)")
    if op in SET_OPS and not any(v.strip() for v in values):
        errors.append(f"{where}: {op} needs values")
    if op in NUMERIC_OPS and not is_number(rule.get("value")):
        errors.append(f"{where}: {op} needs a finite numeric value")


def check_condition(where, cond, errors):
    if not isinstance(cond, dict):
        errors.append(f"{where}: condition must be an object")
        return
    if not isinstance(cond.get("usage_key"), str) or not cond["usage_key"].strip():
        errors.append(f"{where}: condition needs a non-empty usage_key")
    if cond.get("operator", "gt") not in CONDITION_OPS:
        errors.append(f"{where}: unknown condition operator {cond.get('operator')!r}")
    if not is_number(cond.get("value", 0)):
        errors.append(f"{where}: condition value must be a finite number")


def check_windows(where, windows, errors):
    if not isinstance(windows, list):
        errors.append(f"{where}: utc_windows must be a list")
        return
    for w in windows:
        start, end = (w.get("start_minute", 0), w.get("end_minute", 0)) if isinstance(w, dict) else (None, None)
        if not (is_int(start) and is_int(end) and 0 <= start <= MINUTES_PER_DAY and 0 <= end <= MINUTES_PER_DAY):
            errors.append(f"{where}: window bounds must be integers within 0-{MINUTES_PER_DAY}")
        elif start % MINUTES_PER_DAY == end % MINUTES_PER_DAY:
            errors.append(f"{where}: window start and end are the same ({start})")


def check_tier(where, tier, errors):
    if not isinstance(tier, dict):
        errors.append(f"{where}: tier must be an object")
        return
    if not isinstance(tier.get("name"), str) or not tier["name"].strip():
        errors.append(f"{where}: tier needs a name")
    prices = tier.get("prices")
    if not isinstance(prices, dict) or not prices:
        errors.append(f"{where}: no prices")
    else:
        for key, price in prices.items():
            if not is_number(price) or price < 0:
                errors.append(f"{where}: price {key!r} must be a finite number >= 0")
    if "param_conditions" in tier:
        errors.append(f"{where}: param_conditions is not supported; use rules")
    priority = tier.get("priority", 0)
    if not is_int(priority) or abs(priority) > MAX_PRIORITY:
        errors.append(f"{where}: priority must be an integer within ±{MAX_PRIORITY}")
    if tier.get("condition") is not None:
        check_condition(where, tier["condition"], errors)
    check_windows(where, tier.get("utc_windows") or [], errors)
    rules = tier.get("rules") or []
    if not isinstance(rules, list):
        errors.append(f"{where}: rules must be a list")
        return
    if len(rules) > MAX_RULES:
        errors.append(f"{where}: more than {MAX_RULES} rules")
    for i, rule in enumerate(rules):
        check_rule(f"{where} rule {i}", rule, errors)


def check_entry_fields(name, entry, errors):
    for field in ("provider", "description"):
        if not isinstance(entry.get(field, ""), str):
            errors.append(f"{name}: {field} must be a string")
    pattern = entry.get("match_pattern")
    if not isinstance(pattern, str) or not pattern:
        errors.append(f"{name}: missing match_pattern")
    elif len(pattern) > MAX_PATTERN_LEN:
        errors.append(f"{name}: match_pattern longer than {MAX_PATTERN_LEN} characters")
    else:
        try:
            re.compile(pattern)
        except re.error as e:
            errors.append(f"{name}: invalid match_pattern: {e}")


# ---- tier selection: only reached once every tier is well-formed ----


def restrictions(tier):
    """Canonical restriction items, duplicates kept because the backend's rank counts every rule."""
    items = []
    cond = tier.get("condition")
    if cond:
        items.append(("cond", cond["usage_key"], cond.get("operator", "gt"), float(cond.get("value", 0))))
    windows = tier.get("utc_windows") or []
    if windows:
        items.append(("win", tuple((w.get("start_minute", 0), w.get("end_minute", 0)) for w in windows)))
    for rule in tier.get("rules") or []:
        items.append(
            (
                "rule",
                tuple(norm_key(k) for k in rule["keys"]),
                rule["op"],
                tuple(sorted(norm_value(v) for v in rule.get("values", []))),
                rule.get("value"),
            )
        )
    return items


def rank(tier):
    return (tier.get("priority", 0), len(restrictions(tier)))


def is_default(tier):
    return not restrictions(tier)


def ranks_tiers(tiers):
    return any(t.get("rules") or t.get("priority", 0) for t in tiers)


def cond_range(cond):
    v = float(cond.get("value", 0))
    op = cond.get("operator", "gt")
    if op in ("gt", "gte"):
        return (v, math.inf, op == "gte", False)
    if op in ("lt", "lte"):
        return (-math.inf, v, False, op == "lte")
    if op == "eq":
        return (v, v, True, True)
    return None


def disjoint_ranges(a, b):
    lo_a, hi_a, lo_in_a, hi_in_a = a
    lo_b, hi_b, lo_in_b, hi_in_b = b
    if hi_a < lo_b or (hi_a == lo_b and not (hi_in_a and lo_in_b)):
        return True
    if hi_b < lo_a or (hi_b == lo_a and not (hi_in_b and lo_in_a)):
        return True
    return False


def range_within(inner, outer):
    lo_i, hi_i, lo_in_i, hi_in_i = inner
    lo_o, hi_o, lo_in_o, hi_in_o = outer
    lo_ok = lo_i > lo_o or (lo_i == lo_o and (lo_in_o or not lo_in_i))
    hi_ok = hi_i < hi_o or (hi_i == hi_o and (hi_in_o or not hi_in_i))
    return lo_ok and hi_ok


def window_minutes(windows):
    minutes = set()
    for w in windows:
        start, end = w.get("start_minute", 0) % MINUTES_PER_DAY, w.get("end_minute", 0) % MINUTES_PER_DAY
        span = range(start, end) if start < end else [*range(start, MINUTES_PER_DAY), *range(0, end)]
        minutes.update(span)
    return minutes


def exclusive(a, b):
    """True only when no span can match both tiers."""
    for ra in a.get("rules") or []:
        for rb in b.get("rules") or []:
            same_keys = [norm_key(k) for k in ra["keys"]] == [norm_key(k) for k in rb["keys"]]
            if not same_keys or {ra["op"], rb["op"]} - SET_OPS:
                continue
            va = {norm_value(v) for v in ra["values"]}
            vb = {norm_value(v) for v in rb["values"]}
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
    wa, wb = a.get("utc_windows") or [], b.get("utc_windows") or []
    return bool(wa and wb and not window_minutes(wa) & window_minutes(wb))


def implies(later, earlier):
    """True when every span matching `later` also matches `earlier`."""
    ce, cl = earlier.get("condition"), later.get("condition")
    if ce:
        if not cl or cl["usage_key"] != ce["usage_key"]:
            return False
        re_, rl = cond_range(ce), cond_range(cl)
        same = cl.get("operator", "gt") == ce.get("operator", "gt") and float(cl["value"]) == float(ce["value"])
        if not same and not (re_ and rl and range_within(rl, re_)):
            return False
    we, wl = earlier.get("utc_windows") or [], later.get("utc_windows") or []
    if we and not (wl and window_minutes(wl) <= window_minutes(we)):
        return False
    return not earlier.get("rules")


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


def check_unreachable(name, tiers, errors):
    """First-match: a tier is dead when an earlier restricted tier matches every span it matches."""
    restricted = [t for t in tiers if not is_default(t)]
    for i, later in enumerate(restricted):
        shadow = next((e for e in restricted[:i] if implies(later, e)), None)
        if shadow:
            errors.append(
                f"{name}: tier {later['name']!r} can never be selected; the earlier tier "
                f"{shadow['name']!r} matches every span it matches (list it first or set priority)"
            )


def check_selection(name, tiers, errors):
    if not is_default(tiers[0]):
        errors.append(f"{name}: the first tier must be the unrestricted default")
    if sum(map(is_default, tiers)) > 1:
        errors.append(f"{name}: only one tier may be unrestricted")
    check_old_binary_layout(name, tiers, errors)
    if ranks_tiers(tiers):
        check_ambiguity(name, tiers, errors)
    else:
        check_unreachable(name, tiers, errors)


def validate(entries):
    errors = []
    if not isinstance(entries, list):
        return ["top level must be a list"]
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("every entry must be an object")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"entry with match_pattern {entry.get('match_pattern')!r} has no name")
            continue
        if name in seen:
            errors.append(f"{name}: duplicate entry name")
        seen.add(name)
        check_entry_fields(name, entry, errors)
        tiers = entry.get("tiers")
        if not isinstance(tiers, list) or not tiers or len(tiers) > MAX_TIERS:
            errors.append(f"{name}: needs 1-{MAX_TIERS} tiers")
            continue
        before = len(errors)
        for i, tier in enumerate(tiers):
            check_tier(f"{name} / {tier.get('name', i) if isinstance(tier, dict) else i}", tier, errors)
        if len(errors) > before:
            continue  # the selection checks below assume well-formed tiers
        tier_names = [t["name"] for t in tiers]
        if len(set(tier_names)) != len(tier_names):
            errors.append(f"{name}: duplicate tier names")
        check_selection(name, tiers, errors)
    return errors


def main(path):
    with open(path) as f:
        entries = json.load(f)
    errors = validate(entries)
    for e in errors:
        print(f"error: {e}")
    print(f"{len(entries) if isinstance(entries, list) else 0} entries checked, {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "llm_pricing.json"))
