#!/usr/bin/env python3
"""Build server/lib/cartesia_voice_pool.json, the curated voices a new agent may be given.

Reads Cartesia's whole catalogue with the Host's own key (as the Host reads it; the key is
never printed), or a saved copy of it with --catalogue FILE, and keeps the voices that follow
the rules the hand-picked voices of issue 476 followed:

- English, public, not the account's own, active.
- A native English accent: the voice's first accent is native and English, from a country
  where English is spoken natively (no "accented English" voices).
- No novelty voices: no emotion variants of another voice (Sad, Angry, Scared ...), no
  characters, announcers, performers, whisper, meditation or entertainment voices.
- One voice per name (the oldest), so two agents never show the same voice name.
- Ordered so that neighbours differ: a round robin over gender, then country, then the
  description's first word. The pool walk starts at a hash of the agent's name, so the
  order only decides which voice comes next when one is taken.

Usage: python3 scripts/build_cartesia_voice_pool.py [--catalogue FILE] [--out FILE]
Re-run it when Cartesia's catalogue changes; the diff of the data file is the review.
"""
from __future__ import annotations

import argparse
import itertools
import json
import pathlib
import sys
from collections import OrderedDict, defaultdict

REPO = pathlib.Path(__file__).resolve().parents[1]
OUT = REPO / "server" / "lib" / "cartesia_voice_pool.json"

NATIVE_COUNTRIES = {"US", "GB", "AU", "CA", "NZ", "IE", "ZA", "SG", "IN"}
EMOTIONS = {"sad", "angry", "scared", "surprised", "disgusted", "curious", "happy", "yelling",
            "excited"}
NOVELTY_TAGLINE = ("cheerleader", "broadway", "baritone", "meditation", "yogi", "whisper", "oracle",
                   "monarch", "sportscaster", "announcer", "performer", "actor", "warden",
                   "old time", "goofy", "animated", "commentator", "guru")
NOVELTY_DESCRIPTION = ("character", "entertainment", "gaming", "mystical", "silly", "aggressive",
                       "echo-y", "accented male", "accented female")


def _catalogue(path: str | None) -> list[dict]:
    if path:
        return json.loads(pathlib.Path(path).read_text())
    sys.path.insert(0, str(REPO / "server"))
    from lib import cartesia_voices, config
    key = config.load().cartesia_key()
    if not key:
        raise SystemExit("no Cartesia key configured on this Host")
    return cartesia_voices.all_voices(key)


def _native(row: dict) -> bool:
    accents = row.get("accents") or []
    first = accents[0] if accents else {}
    return (first.get("is_native") is True and str(first.get("locale") or "").startswith("en-")
            and str(row.get("country") or "") in NATIVE_COUNTRIES)


def _novelty(row: dict, names_seen: dict[str, int]) -> bool:
    tagline = str(row.get("tagline") or "").strip().lower()
    description = str(row.get("description") or "").lower()
    first = tagline.split(" ")[0] if tagline else ""
    if first in EMOTIONS and names_seen[str(row.get("name") or "").strip().casefold()] > 1:
        return True
    return any(w in tagline for w in NOVELTY_TAGLINE) or any(w in description for w in NOVELTY_DESCRIPTION)


def eligible(rows: list[dict]) -> list[dict]:
    rows = [r for r in rows
            if str(r.get("language") or "") == "en"
            and (str(r.get("access") or "") == "public" or str(r.get("visibility") or "") in ("all", "public"))
            and r.get("is_owner") is False
            and str(r.get("status") or "active") == "active"
            and r.get("id") and str(r.get("name") or "").strip()]
    names_seen: dict[str, int] = defaultdict(int)
    for r in rows:
        names_seen[str(r["name"]).strip().casefold()] += 1
    kept = [r for r in rows if _native(r) and not _novelty(r, names_seen)]
    one_per_name: "OrderedDict[str, dict]" = OrderedDict()
    for r in sorted(kept, key=lambda r: (str(r.get("created_at") or ""), str(r["id"]))):
        one_per_name.setdefault(str(r["name"]).strip().casefold(), r)
    return list(one_per_name.values())


def spread(rows: list[dict]) -> list[dict]:
    """Round robin over gender, within it over country, within that over description word."""
    def robin(groups: dict) -> list:
        lanes = [groups[k] for k in sorted(groups)]
        return [x for batch in itertools.zip_longest(*lanes) for x in batch if x is not None]

    by_gender: dict[str, dict[str, dict[str, list]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in sorted(rows, key=lambda r: (str(r["name"]).casefold(), str(r["id"]))):
        word = (str(r.get("description") or "").split() or [""])[0].strip(",.").lower()
        by_gender[str(r.get("gender") or "")][str(r.get("country") or "")][word].append(r)
    per_gender = {g: robin({c: robin(words) for c, words in countries.items()})
                  for g, countries in by_gender.items()}
    return robin(per_gender)


def build(rows: list[dict]) -> dict:
    voices = [{"id": str(r["id"]), "name": str(r["name"]).strip(), "gender": str(r.get("gender") or ""),
               "country": str(r.get("country") or ""),
               "description": " ".join(str(r.get("description") or "").split())}
              for r in spread(eligible(rows))]
    return {"provider": "cartesia", "language": "en",
            "rules": "scripts/build_cartesia_voice_pool.py", "voices": voices}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalogue", help="a saved catalogue (JSON list of Cartesia voice rows)")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    data = build(_catalogue(args.catalogue))
    pathlib.Path(args.out).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(data['voices'])} voices -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
