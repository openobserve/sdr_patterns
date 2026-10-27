#!/usr/bin/env python3
"""Compare llm_pricing.json with the LiteLLM price feed and report what may need updating.

Prints a Markdown report of (1) catalogue entries whose default-tier price differs from the
feed, (2) first-party models the catalogue does not price at all, and (3) models an
approximate (prefix/suffix) catalogue pattern bills at a price other than the feed's, which
usually means a new variant is being billed as its parent. The feed is a lead, not a source
of truth: every change it points at must be confirmed on the provider's own pricing page
before it goes into the catalogue.

scripts/llm_pricing_reviewed.json records decisions so they are not reported again: `ignore`
patterns for models deliberately left unpriced, and `feed_disagrees` for feed prices a
maintainer checked against the provider page and found wrong. A `feed_disagrees` row is hidden
only while the feed still shows those exact values, so a later feed change resurfaces it.

Usage: check_llm_pricing_drift.py [llm_pricing.json] [--feed URL_OR_PATH] [--json | --count]
Exit code is 0 whether or not drift is found; 2 on a usage or fetch error.
"""

import argparse
import json
import os
import re
import sys
import urllib.request

FEED_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)

# Feed provider id -> (catalogue provider name, the model families it makes). First-party APIs
# only: resellers and clouds (bedrock, azure, vertex, ...) price the same models differently,
# and a first-party API that also serves other vendors' models (dashscope) does too.
PROVIDERS = {
    "openai": ("OpenAI", r"gpt-|chatgpt-|o\d|codex|text-embedding-"),
    "anthropic": ("Anthropic", r"claude-"),
    "gemini": ("Google", r"gemini-|gemma-"),
    "deepseek": ("DeepSeek", r"deepseek-"),
    "moonshot": ("Moonshot", r"kimi-|moonshot-"),
    "xai": ("xAI", r"grok-"),
    "mistral": ("Mistral", r"(?:mistral|codestral|magistral|devstral|ministral|pixtral)-"),
    "codestral": ("Mistral", r"codestral-"),
    "cohere_chat": ("Cohere", r"command-|c4ai-aya-"),
    "cohere": ("Cohere", r"embed-"),
    "zai": ("Z.ai", r"glm-"),
    "minimax": ("MiniMax", r"minimax-"),
    "dashscope": ("Alibaba", r"qwen|qwq-"),
}
MODES = {"chat", "responses", "completion", "embedding"}
# Model families the catalogue does not price per token (audio, realtime, fine-tunes, ...).
SKIP = re.compile(
    r"(?i)(?:^ft:|audio|realtime|tts|transcribe|whisper|dall-e|search-preview|computer-use"
    r"|moderation|-rerank|^babbage|^davinci|container|sample_spec)"
)
# Feed field -> catalogue price key.
PRICE_FIELDS = {
    "input_cost_per_token": "input",
    "output_cost_per_token": "output",
    "cache_read_input_token_cost": "cache_read_input_tokens",
    "cache_creation_input_token_cost": "cache_creation_input_tokens",
}
REL_TOLERANCE = 0.005
REVIEWED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_pricing_reviewed.json")


def load_feed(source):
    if re.match(r"https?://", source):
        with urllib.request.urlopen(source, timeout=60) as resp:  # noqa: S310 - fixed URL
            return json.load(resp)
    with open(source, encoding="utf-8") as f:
        return json.load(f)


def feed_models(feed):
    """Yield (model_name, catalogue_provider, prices) for first-party, per-token feed entries."""
    seen = set()
    for key, spec in feed.items():
        if not isinstance(spec, dict) or spec.get("mode") not in MODES:
            continue
        provider, families = PROVIDERS.get(spec.get("litellm_provider"), (None, None))
        if provider is None:
            continue
        name = key.split("/", 1)[1] if "/" in key else key
        if not re.match(families, name, re.IGNORECASE):
            continue
        if SKIP.search(name) or (provider, name.lower()) in seen:
            continue
        prices = {
            ours: spec[theirs]
            for theirs, ours in PRICE_FIELDS.items()
            if isinstance(spec.get(theirs), (int, float)) and not isinstance(spec[theirs], bool)
        }
        if not prices.get("input") and not prices.get("output"):
            continue
        seen.add((provider, name.lower()))
        yield name, provider, prices


def load_reviewed(path):
    """`(ignore regexes, {model id: acknowledged feed prices})` from the reviewed-decisions file."""
    if not os.path.exists(path):
        return [], {}
    with open(path, encoding="utf-8") as f:
        reviewed = json.load(f)
    ignore = [re.compile(i["pattern"], re.IGNORECASE) for i in reviewed.get("ignore", [])]
    acked = {a["model"].lower(): a["feed"] for a in reviewed.get("feed_disagrees", [])}
    return ignore, acked


def acknowledged(acked, model, changed):
    """True when every differing feed value is one a maintainer already checked and rejected."""
    feed = acked.get(model.lower())
    return feed is not None and all(
        key in feed and not differs(feed[key], v["feed"]) for key, v in changed.items()
    )


def default_tier(entry):
    """The tier with no condition, rules or time window: what an ordinary span is billed."""
    for tier in entry.get("tiers", []):
        if not (tier.get("condition") or tier.get("rules") or tier.get("utc_windows")):
            return tier
    return None


def resolve(catalogue, model):
    """Mirror the backend: the first exact match in file order, else the first approximate one."""
    approximate = None
    for entry, regex in catalogue:
        m = regex.search(model)
        if not m:
            continue
        if regex.fullmatch(model) or (m.start() == 0 and m.end() == len(model)):
            return entry, "exact"
        if approximate is None:
            approximate = entry
    return (approximate, "approximate") if approximate else (None, None)


def differs(ours, theirs):
    if ours is None:
        return True
    scale = max(abs(ours), abs(theirs), 1e-15)
    return abs(ours - theirs) / scale > REL_TOLERANCE


def per_million(v):
    return "-" if v is None else f"${v * 1e6:g}"


def check(catalogue_path, feed_source, reviewed_path=REVIEWED_FILE):
    with open(catalogue_path, encoding="utf-8") as f:
        entries = json.load(f)
    catalogue = [(e, re.compile(e["match_pattern"])) for e in entries]
    ignored, acked = load_reviewed(reviewed_path)
    feed = load_feed(feed_source)

    report = {"price_changes": [], "uncovered": [], "approximate_only": []}
    for name, provider, prices in sorted(feed_models(feed), key=lambda m: (m[1], m[0])):
        entry, kind = resolve(catalogue, name)
        if entry is None:
            if any(r.search(name) for r in ignored):
                continue
            report["uncovered"].append({"model": name, "provider": provider, "feed": prices})
            continue
        tier = default_tier(entry)
        if tier is None:
            continue
        # A zero the catalogue leaves out (an embedding's output) is the same price.
        changed = {
            key: {"catalogue": tier["prices"].get(key), "feed": value}
            for key, value in prices.items()
            if key in tier["prices"] or value
            if differs(tier["prices"].get(key), value)
        }
        if not changed or acknowledged(acked, name, changed):
            continue
        row = {"model": name, "provider": provider, "entry": entry["name"], "changes": changed}
        if kind == "approximate":
            report["approximate_only"].append(row)
        else:
            row["time_of_day"] = any(t.get("utc_windows") for t in entry["tiers"])
            report["price_changes"].append(row)
    return report


def to_markdown(report):
    out = ["# LLM pricing drift (per 1M tokens)", ""]
    out.append(f"## Price differences on exact matches ({len(report['price_changes'])})")
    out.append("Rows marked * have peak/off-peak tiers; the feed has one price, the catalogue")
    out.append("default tier is off-peak. Check both rates on the provider page.")
    out.append("")
    out.append("| Model | Entry | Key | Catalogue | Feed |")
    out.append("|---|---|---|---|---|")
    for row in report["price_changes"]:
        mark = "*" if row["time_of_day"] else ""
        for key, v in row["changes"].items():
            out.append(
                f"| {row['model']}{mark} | {row['entry']} | {key} | {per_million(v['catalogue'])} "
                f"| {per_million(v['feed'])} |"
            )
    out += ["", f"## Models no entry prices ({len(report['uncovered'])})"]
    out.append("| Provider | Model | Input | Output | Cache read | Cache write |")
    out.append("|---|---|---|---|---|---|")
    for row in report["uncovered"]:
        p = row["feed"]
        out.append(
            f"| {row['provider']} | {row['model']} | {per_million(p.get('input'))} "
            f"| {per_million(p.get('output'))} | {per_million(p.get('cache_read_input_tokens'))} "
            f"| {per_million(p.get('cache_creation_input_tokens'))} |"
        )
    out += ["", f"## Models billed by an approximate match at another price "
            f"({len(report['approximate_only'])})"]
    out.append("| Provider | Model | Billed as | Key | Billed | Feed |")
    out.append("|---|---|---|---|---|---|")
    for row in report["approximate_only"]:
        for key, v in row["changes"].items():
            out.append(
                f"| {row['provider']} | {row['model']} | {row['entry']} | {key} "
                f"| {per_million(v['catalogue'])} | {per_million(v['feed'])} |"
            )
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("catalogue", nargs="?", default="llm_pricing.json")
    parser.add_argument("--feed", default=FEED_URL, help="feed URL or local path")
    parser.add_argument("--reviewed", default=REVIEWED_FILE, help="reviewed-decisions file")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    output.add_argument("--count", action="store_true", help="print only the number of findings")
    args = parser.parse_args()
    try:
        report = check(args.catalogue, args.feed, args.reviewed)
    except (OSError, ValueError, re.error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.count:
        print(sum(len(rows) for rows in report.values()))
    elif args.json:
        print(json.dumps(report, indent=2))
    else:
        print(to_markdown(report), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
