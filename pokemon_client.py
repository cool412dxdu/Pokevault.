"""
Thin client over https://api.pokemontcg.io/v2 with MongoDB-backed caching.
The Pokemon TCG API is free and does not strictly require an API key.
"""
import os
import httpx
import asyncio
from typing import List, Dict, Any, Optional

BASE = os.environ.get("POKEMON_TCG_API_BASE", "https://api.pokemontcg.io/v2")
API_KEY = os.environ.get("POKEMON_TCG_API_KEY", "")

_headers = {"X-Api-Key": API_KEY} if API_KEY else {}


async def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0, headers=_headers) as client:
        r = await client.get(f"{BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def fetch_sets(db) -> List[Dict[str, Any]]:
    cached = await db.cached_sets.find({}, {"_id": 0}).to_list(None)
    if cached:
        return cached
    data = await _get("/sets", params={"orderBy": "-releaseDate", "pageSize": 250})
    sets = data.get("data", [])
    if sets:
        await db.cached_sets.delete_many({})
        await db.cached_sets.insert_many([dict(s) for s in sets])
        # mongo mutates the dicts adding _id; drop it from returned copy
        for s in sets:
            s.pop("_id", None)
    return sets


async def fetch_set(db, set_id: str) -> Optional[Dict[str, Any]]:
    cached = await db.cached_sets.find_one({"id": set_id}, {"_id": 0})
    if cached:
        return cached
    try:
        data = await _get(f"/sets/{set_id}")
        return data.get("data")
    except httpx.HTTPError:
        return None


def _build_query(q: Optional[str], set_id: Optional[str], rarity: Optional[str],
                 supertype: Optional[str], type_: Optional[str]) -> Optional[str]:
    parts = []
    if q:
        # escape any reserved chars lightly
        safe = q.replace('"', '').strip()
        if safe:
            parts.append(f'name:"*{safe}*"')
    if set_id:
        parts.append(f'set.id:{set_id}')
    if rarity:
        parts.append(f'rarity:"{rarity}"')
    if supertype:
        parts.append(f'supertype:"{supertype}"')
    if type_:
        parts.append(f'types:"{type_}"')
    return " ".join(parts) if parts else None


async def fetch_cards(q: Optional[str] = None,
                      set_id: Optional[str] = None,
                      rarity: Optional[str] = None,
                      supertype: Optional[str] = None,
                      type_: Optional[str] = None,
                      page: int = 1,
                      page_size: int = 24) -> Dict[str, Any]:
    params: Dict[str, Any] = {"page": page, "pageSize": page_size, "orderBy": "-set.releaseDate,number"}
    query = _build_query(q, set_id, rarity, supertype, type_)
    if query:
        params["q"] = query
    data = await _get("/cards", params=params)
    return {
        "data": data.get("data", []),
        "page": data.get("page", page),
        "pageSize": data.get("pageSize", page_size),
        "count": data.get("count", 0),
        "totalCount": data.get("totalCount", 0),
    }


async def fetch_card(db, card_id: str) -> Optional[Dict[str, Any]]:
    cached = await db.cached_cards.find_one({"id": card_id}, {"_id": 0})
    if cached:
        return cached
    try:
        data = await _get(f"/cards/{card_id}")
        card = data.get("data")
        if card:
            await db.cached_cards.update_one(
                {"id": card_id},
                {"$set": dict(card)},
                upsert=True,
            )
            card.pop("_id", None)
        return card
    except httpx.HTTPError:
        return None


async def fetch_cards_bulk(db, card_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Fetch many card records by id, using cache where possible."""
    if not card_ids:
        return {}
    unique = list(set(card_ids))
    cached = await db.cached_cards.find({"id": {"$in": unique}}, {"_id": 0}).to_list(None)
    found = {c["id"]: c for c in cached}
    missing = [cid for cid in unique if cid not in found]
    if missing:
        results = await asyncio.gather(*[fetch_card(db, cid) for cid in missing], return_exceptions=True)
        for cid, res in zip(missing, results):
            if isinstance(res, dict):
                found[cid] = res
    return found


async def get_card_market_price(card: Dict[str, Any]) -> float:
    """Extract a best-effort market price from a card's tcgplayer/cardmarket data."""
    if not card:
        return 0.0
    tcg = card.get("tcgplayer", {}) or {}
    prices = tcg.get("prices", {}) or {}
    for key in ("holofoil", "reverseHolofoil", "normal", "1stEditionHolofoil", "1stEditionNormal", "unlimited", "unlimitedHolofoil"):
        if key in prices:
            mkt = prices[key].get("market") or prices[key].get("mid")
            if mkt:
                return float(mkt)
    cm = card.get("cardmarket", {}) or {}
    cm_prices = cm.get("prices", {}) or {}
    for key in ("trendPrice", "averageSellPrice", "avg30"):
        if cm_prices.get(key):
            return float(cm_prices[key])
    return 0.0
