"""Pure nutrition logic: allergens, engine-output parsing, merging with official figures, cache keys.

No I/O here, so it is all unit-tested directly.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

# The 14 allergens UK food law requires businesses to declare.
UK_ALLERGENS = (
    "celery", "gluten", "crustaceans", "egg", "fish", "lupin", "milk",
    "molluscs", "mustard", "nuts", "peanuts", "sesame", "soya", "sulphites",
)
_SYNONYMS = {
    "eggs": "egg", "wheat": "gluten", "cereals containing gluten": "gluten",
    "tree nuts": "nuts", "nut": "nuts", "soy": "soya", "sulfites": "sulphites",
    "sulphur dioxide": "sulphites", "dairy": "milk", "crustacean": "crustaceans",
    "shellfish": "crustaceans", "mollusc": "molluscs", "peanut": "peanuts",
}
PROCESSING_LEVELS = ("unprocessed", "minimally_processed", "processed", "ultra_processed")
MACROS = ("kcal", "protein_g", "carbs_g", "fat_g")
OFFICIAL_CONFIDENCE = 0.95
REVIEW_THRESHOLD = 0.7


def normalise_allergen(value: str) -> str | None:
    v = re.sub(r"\s+", " ", str(value).strip().lower())
    v = _SYNONYMS.get(v, v)
    return v if v in UK_ALLERGENS else None


def normalise_declared(values: list[str]) -> list[str]:
    """Partner-declared allergens must all be recognisable; raise ValueError otherwise."""
    out, bad = set(), []
    for v in values:
        n = normalise_allergen(v)
        if n is None:
            bad.append(v)
        else:
            out.add(n)
    if bad:
        raise ValueError(f"Unknown allergen(s): {', '.join(map(str, bad))}. Use the UK 14: {', '.join(UK_ALLERGENS)}.")
    return sorted(out)


@dataclass
class EngineResult:
    is_food: bool
    dish_name: str = ""
    items: list[dict] = field(default_factory=list)
    allergens: list[str] = field(default_factory=list)
    processing_level: str = "processed"
    confidence: float = 0.5


def _num(v, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f or f < 0:  # NaN or negative
        return 0.0
    return round(min(f, hi), 1)


def parse_engine_json(text: str) -> EngineResult:
    """Parse and sanitise the model's JSON. Raises ValueError if it is unusable."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    data = json.loads(t)
    if not isinstance(data, dict) or "is_food" not in data:
        raise ValueError("engine output missing is_food")
    if not data.get("is_food"):
        return EngineResult(is_food=False)
    items = []
    for it in (data.get("items") or [])[:30]:
        if not isinstance(it, dict):
            continue
        items.append({
            "name": str(it.get("name") or "Item")[:120],
            "grams": _num(it.get("grams"), 5000),
            "kcal": _num(it.get("kcal"), 10000),
            "protein_g": _num(it.get("protein_g"), 1000),
            "carbs_g": _num(it.get("carbs_g"), 1000),
            "fat_g": _num(it.get("fat_g"), 1000),
        })
    allergens = sorted({a for a in (normalise_allergen(x) for x in (data.get("allergens") or [])) if a})
    level = data.get("processing_level")
    level = level if level in PROCESSING_LEVELS else "processed"
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = min(max(conf if conf == conf else 0.5, 0.0), 1.0)
    return EngineResult(True, str(data.get("dish_name") or "")[:200], items, allergens, level, round(conf, 2))


def sum_items(items: list[dict]) -> dict:
    return {m: round(sum(i.get(m, 0) for i in items), 1) for m in MACROS}


def build_result(ai: EngineResult, official: dict | None, declared: list[str] | None) -> dict:
    """Official figures always win; the AI fills the gaps (breakdown, processing level)."""
    if official:
        nutrition = {m: round(float(official[m]), 1) for m in MACROS}
        source, is_estimate, confidence = "partner_official", False, OFFICIAL_CONFIDENCE
    else:
        nutrition = sum_items(ai.items)
        source, is_estimate, confidence = "ai_estimate", True, ai.confidence

    if declared is not None:
        allergens = declared
        possible = [a for a in ai.allergens if a not in declared]
    else:
        allergens, possible = ai.allergens, []

    return {
        "source": source,
        "is_estimate": is_estimate,
        "confidence": confidence,
        "nutrition": nutrition,
        "items": ai.items,
        "allergens": allergens,
        # Allergens the AI thinks may be present but the partner did not declare: for the partner to check.
        "possible_allergens": possible,
        "processing_level": ai.processing_level,
        "needs_review": confidence < REVIEW_THRESHOLD or bool(possible),
    }


def input_hash(item: dict, analysis_version: str) -> str:
    """Stable cache key over the normalised input. Any change to the input → new version."""
    norm = {
        "name": re.sub(r"\s+", " ", item.get("name", "").strip().lower()),
        "description": re.sub(r"\s+", " ", (item.get("description") or "").strip().lower()),
        "ingredients": sorted(re.sub(r"\s+", " ", i.strip().lower()) for i in (item.get("ingredients") or [])),
        "portion": item.get("portion"),
        "official_nutrition": item.get("official_nutrition"),
        "declared_allergens": item.get("declared_allergens"),
        "v": analysis_version,
    }
    raw = json.dumps(norm, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


# --- Photo validation -------------------------------------------------------

ALLOWED_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif")


def sniff_image_type(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heif", b"hevc"):
        return "image/heic"
    return None
