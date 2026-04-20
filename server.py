from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import os
import uuid
import logging
import csv
import io
import re
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import FastAPI, APIRouter, Depends, HTTPException, Request, UploadFile, File, Query
from fastapi.responses import StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient

from auth_utils import (
    hash_password, verify_password,
    create_access_token, decode_token, extract_token,
)
from models import (
    RegisterInput, LoginInput, AuthResponse, UserPublic,
    AddCollectionItemInput, UpdateCollectionItemInput,
    WishlistAddInput, TradeAddInput,
    DeckCreateInput, DeckUpdateInput, DeckCardInput,
    ForgotPasswordInput, ResetPasswordInput, ShareToggleInput,
)
import pokemon_client as pc

# Mongo
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI(title="PokeVault API")
api = APIRouter(prefix="/api")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- Auth dependency ----------
async def get_current_user(request: Request) -> dict:
    token = extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    user_id = payload.get("sub")
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


# ---------- Auth routes ----------
@api.post("/auth/register", response_model=AuthResponse)
async def register(body: RegisterInput):
    email = body.email.lower()
    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user_id = str(uuid.uuid4())
    doc = {
        "id": user_id,
        "email": email,
        "username": body.username,
        "password_hash": hash_password(body.password),
        "created_at": now_iso(),
    }
    await db.users.insert_one(doc)
    token = create_access_token(user_id, email)
    return {
        "token": token,
        "user": {"id": user_id, "email": email, "username": body.username, "created_at": doc["created_at"]},
    }


@api.post("/auth/login", response_model=AuthResponse)
async def login(body: LoginInput):
    email = body.email.lower()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_access_token(user["id"], email)
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "username": user["username"],
            "created_at": user["created_at"],
        },
    }


@api.get("/auth/me", response_model=UserPublic)
async def me(user=Depends(get_current_user)):
    return {
        "id": user["id"],
        "email": user["email"],
        "username": user["username"],
        "created_at": user["created_at"],
    }


# ---------- Card catalog ----------
@api.get("/sets")
async def list_sets():
    sets = await pc.fetch_sets(db)
    # remove _id if present
    for s in sets:
        s.pop("_id", None)
    return sets


@api.get("/sets/{set_id}")
async def get_set(set_id: str):
    s = await pc.fetch_set(db, set_id)
    if not s:
        raise HTTPException(status_code=404, detail="Set not found")
    s.pop("_id", None)
    return s


@api.get("/cards")
async def list_cards(
    q: Optional[str] = None,
    set_id: Optional[str] = Query(default=None, alias="set"),
    rarity: Optional[str] = None,
    supertype: Optional[str] = None,
    type: Optional[str] = Query(default=None),
    page: int = 1,
    page_size: int = Query(default=24, ge=1, le=60),
):
    result = await pc.fetch_cards(q=q, set_id=set_id, rarity=rarity,
                                  supertype=supertype, type_=type,
                                  page=page, page_size=page_size)
    # strip any _id
    for c in result.get("data", []):
        c.pop("_id", None)
    return result


@api.get("/cards/{card_id}")
async def get_card(card_id: str):
    card = await pc.fetch_card(db, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    card.pop("_id", None)
    return card


# ---------- Collection ----------
@api.get("/collection")
async def list_collection(user=Depends(get_current_user),
                          q: Optional[str] = None,
                          condition: Optional[str] = None):
    query = {"user_id": user["id"]}
    if condition:
        query["condition"] = condition
    items = await db.collection_items.find(query, {"_id": 0}).sort("added_at", -1).to_list(5000)
    card_ids = [i["card_id"] for i in items]
    cards = await pc.fetch_cards_bulk(db, card_ids)
    out = []
    for item in items:
        card = cards.get(item["card_id"], {"id": item["card_id"], "name": "Unknown"})
        card.pop("_id", None)
        if q:
            if q.lower() not in (card.get("name", "") or "").lower():
                continue
        out.append({**item, "card": card})
    return out


@api.get("/collection/stats")
async def collection_stats(user=Depends(get_current_user)):
    items = await db.collection_items.find({"user_id": user["id"]}, {"_id": 0}).to_list(5000)
    card_ids = [i["card_id"] for i in items]
    cards = await pc.fetch_cards_bulk(db, card_ids)
    total_qty = 0
    unique = len(set(card_ids))
    total_value = 0.0
    sets_seen = set()
    by_rarity: dict = {}
    for item in items:
        card = cards.get(item["card_id"], {})
        qty = item.get("quantity", 1)
        total_qty += qty
        price = await pc.get_card_market_price(card)
        if item.get("is_foil") or item.get("is_holo"):
            price *= 1.2
        total_value += price * qty
        set_obj = card.get("set") or {}
        if set_obj.get("id"):
            sets_seen.add(set_obj["id"])
        rarity = card.get("rarity", "Unknown") or "Unknown"
        by_rarity[rarity] = by_rarity.get(rarity, 0) + qty
    total_sets = await db.cached_sets.count_documents({})
    stats_out = {
        "total_cards": total_qty,
        "unique_cards": unique,
        "total_value": round(total_value, 2),
        "sets_collected": len(sets_seen),
        "total_sets": total_sets,
        "by_rarity": by_rarity,
    }
    # daily snapshot (one per user per calendar day)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await db.value_snapshots.update_one(
        {"user_id": user["id"], "date": today},
        {"$set": {
            "user_id": user["id"],
            "date": today,
            "total_value": stats_out["total_value"],
            "total_cards": stats_out["total_cards"],
            "unique_cards": stats_out["unique_cards"],
            "recorded_at": now_iso(),
        }},
        upsert=True,
    )
    return stats_out


@api.post("/collection")
async def add_collection_item(body: AddCollectionItemInput, user=Depends(get_current_user)):
    # ensure card exists in cache
    card = await pc.fetch_card(db, body.card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    item = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "card_id": body.card_id,
        "quantity": body.quantity,
        "condition": body.condition,
        "is_foil": body.is_foil,
        "is_holo": body.is_holo,
        "notes": body.notes or "",
        "added_at": now_iso(),
    }
    await db.collection_items.insert_one(dict(item))
    item.pop("_id", None)
    card.pop("_id", None)
    return {**item, "card": card}


@api.patch("/collection/{item_id}")
async def update_collection_item(item_id: str, body: UpdateCollectionItemInput,
                                 user=Depends(get_current_user)):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")
    res = await db.collection_items.update_one(
        {"id": item_id, "user_id": user["id"]}, {"$set": updates}
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Item not found")
    item = await db.collection_items.find_one({"id": item_id}, {"_id": 0})
    return item


@api.delete("/collection/{item_id}")
async def delete_collection_item(item_id: str, user=Depends(get_current_user)):
    res = await db.collection_items.delete_one({"id": item_id, "user_id": user["id"]})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


# ---------- CSV Import / Export ----------
@api.get("/collection/export.csv")
async def export_csv(user=Depends(get_current_user)):
    items = await db.collection_items.find({"user_id": user["id"]}, {"_id": 0}).to_list(10000)
    cards = await pc.fetch_cards_bulk(db, [i["card_id"] for i in items])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["card_id", "name", "set", "number", "rarity", "quantity",
                     "condition", "is_foil", "is_holo", "notes"])
    for i in items:
        c = cards.get(i["card_id"], {})
        set_name = (c.get("set") or {}).get("name", "")
        writer.writerow([
            i["card_id"], c.get("name", ""), set_name, c.get("number", ""),
            c.get("rarity", ""), i.get("quantity", 1), i.get("condition", "NM"),
            i.get("is_foil", False), i.get("is_holo", False), i.get("notes", ""),
        ])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=collection.csv"})


@api.post("/collection/import")
async def import_csv(file: UploadFile = File(...), user=Depends(get_current_user)):
    contents = (await file.read()).decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(contents))
    inserted = 0
    errors = 0
    for row in reader:
        try:
            card_id = (row.get("card_id") or "").strip()
            if not card_id:
                errors += 1
                continue
            card = await pc.fetch_card(db, card_id)
            if not card:
                errors += 1
                continue
            item = {
                "id": str(uuid.uuid4()),
                "user_id": user["id"],
                "card_id": card_id,
                "quantity": int(row.get("quantity") or 1),
                "condition": (row.get("condition") or "NM").upper(),
                "is_foil": str(row.get("is_foil", "false")).lower() == "true",
                "is_holo": str(row.get("is_holo", "false")).lower() == "true",
                "notes": row.get("notes", "") or "",
                "added_at": now_iso(),
            }
            await db.collection_items.insert_one(dict(item))
            inserted += 1
        except Exception:
            errors += 1
    return {"inserted": inserted, "errors": errors}


# ---------- Wishlist ----------
@api.get("/wishlist")
async def list_wishlist(user=Depends(get_current_user)):
    items = await db.wishlist.find({"user_id": user["id"]}, {"_id": 0}).sort("added_at", -1).to_list(5000)
    cards = await pc.fetch_cards_bulk(db, [i["card_id"] for i in items])
    for i in items:
        c = cards.get(i["card_id"], {"id": i["card_id"], "name": "Unknown"})
        c.pop("_id", None)
        i["card"] = c
    return items


@api.post("/wishlist")
async def add_wishlist(body: WishlistAddInput, user=Depends(get_current_user)):
    card = await pc.fetch_card(db, body.card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    item = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "card_id": body.card_id,
        "priority": body.priority,
        "notes": body.notes or "",
        "added_at": now_iso(),
    }
    await db.wishlist.insert_one(dict(item))
    item.pop("_id", None)
    card.pop("_id", None)
    return {**item, "card": card}


@api.delete("/wishlist/{item_id}")
async def delete_wishlist(item_id: str, user=Depends(get_current_user)):
    res = await db.wishlist.delete_one({"id": item_id, "user_id": user["id"]})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


# ---------- Trades ----------
@api.get("/trades")
async def list_trades(user=Depends(get_current_user)):
    items = await db.trades.find({"user_id": user["id"]}, {"_id": 0}).sort("added_at", -1).to_list(5000)
    cards = await pc.fetch_cards_bulk(db, [i["card_id"] for i in items])
    for i in items:
        c = cards.get(i["card_id"], {"id": i["card_id"], "name": "Unknown"})
        c.pop("_id", None)
        i["card"] = c
    return items


@api.post("/trades")
async def add_trade(body: TradeAddInput, user=Depends(get_current_user)):
    card = await pc.fetch_card(db, body.card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    item = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "card_id": body.card_id,
        "quantity": body.quantity,
        "condition": body.condition,
        "asking_price": body.asking_price,
        "notes": body.notes or "",
        "added_at": now_iso(),
    }
    await db.trades.insert_one(dict(item))
    item.pop("_id", None)
    card.pop("_id", None)
    return {**item, "card": card}


@api.delete("/trades/{item_id}")
async def delete_trade(item_id: str, user=Depends(get_current_user)):
    res = await db.trades.delete_one({"id": item_id, "user_id": user["id"]})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True}


# ---------- Decks ----------
@api.get("/decks")
async def list_decks(user=Depends(get_current_user)):
    decks = await db.decks.find({"user_id": user["id"]}, {"_id": 0}).sort("updated_at", -1).to_list(500)
    return decks


@api.post("/decks")
async def create_deck(body: DeckCreateInput, user=Depends(get_current_user)):
    deck = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "name": body.name,
        "description": body.description or "",
        "format": body.format or "standard",
        "cards": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    await db.decks.insert_one(dict(deck))
    deck.pop("_id", None)
    return deck


@api.get("/decks/{deck_id}")
async def get_deck(deck_id: str, user=Depends(get_current_user)):
    deck = await db.decks.find_one({"id": deck_id, "user_id": user["id"]}, {"_id": 0})
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    card_ids = [c["card_id"] for c in deck.get("cards", [])]
    cards = await pc.fetch_cards_bulk(db, card_ids)
    for dc in deck.get("cards", []):
        c = cards.get(dc["card_id"], {"id": dc["card_id"], "name": "Unknown"})
        c.pop("_id", None)
        dc["card"] = c
    return deck


@api.patch("/decks/{deck_id}")
async def update_deck(deck_id: str, body: DeckUpdateInput, user=Depends(get_current_user)):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "cards" in updates:
        # Ensure cache for each card
        updates["cards"] = [{"card_id": c["card_id"], "quantity": c["quantity"]} for c in updates["cards"]]
        # Best effort cache fill
        await pc.fetch_cards_bulk(db, [c["card_id"] for c in updates["cards"]])
    updates["updated_at"] = now_iso()
    res = await db.decks.update_one({"id": deck_id, "user_id": user["id"]}, {"$set": updates})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Deck not found")
    deck = await db.decks.find_one({"id": deck_id}, {"_id": 0})
    return deck


@api.delete("/decks/{deck_id}")
async def delete_deck(deck_id: str, user=Depends(get_current_user)):
    res = await db.decks.delete_one({"id": deck_id, "user_id": user["id"]})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Deck not found")
    return {"ok": True}


# ---------- Password reset ----------
@api.post("/auth/forgot-password")
async def forgot_password(body: ForgotPasswordInput):
    email = body.email.lower()
    user = await db.users.find_one({"email": email})
    # always return ok to avoid email enumeration
    if user:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        await db.password_reset_tokens.insert_one({
            "token": token,
            "user_id": user["id"],
            "email": email,
            "expires_at": expires_at,
            "used": False,
            "created_at": now_iso(),
        })
        logger.info(f"[PASSWORD RESET] for {email}: token={token}")
        # In dev/demo, surface token so the user can complete the flow without email.
        return {"ok": True, "message": "Reset token created. Check server logs or the link below.", "dev_token": token}
    return {"ok": True, "message": "If an account exists, a reset link was generated."}


@api.post("/auth/reset-password")
async def reset_password(body: ResetPasswordInput):
    rec = await db.password_reset_tokens.find_one({"token": body.token, "used": False})
    if not rec:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    expires_at = rec.get("expires_at")
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at and expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Token expired")
    await db.users.update_one(
        {"id": rec["user_id"]},
        {"$set": {"password_hash": hash_password(body.password)}},
    )
    await db.password_reset_tokens.update_one(
        {"token": body.token},
        {"$set": {"used": True, "used_at": now_iso()}},
    )
    return {"ok": True}


# ---------- Sets progress (per-user set completion) ----------
@api.get("/collection/sets-progress")
async def sets_progress(user=Depends(get_current_user)):
    items = await db.collection_items.find({"user_id": user["id"]}, {"_id": 0, "card_id": 1}).to_list(10000)
    card_ids = list({i["card_id"] for i in items})
    if not card_ids:
        return {}
    cards = await pc.fetch_cards_bulk(db, card_ids)
    by_set: dict = {}
    for cid, card in cards.items():
        set_obj = card.get("set") or {}
        sid = set_obj.get("id")
        if not sid:
            continue
        by_set.setdefault(sid, set()).add(cid)
    return {sid: len(ids) for sid, ids in by_set.items()}


# ---------- Value history ----------
@api.get("/collection/value-history")
async def value_history(user=Depends(get_current_user), days: int = 90):
    snaps = await db.value_snapshots.find(
        {"user_id": user["id"]},
        {"_id": 0, "date": 1, "total_value": 1, "total_cards": 1}
    ).sort("date", 1).to_list(1000)
    return snaps[-days:]


# ---------- Public share ----------
def _gen_slug() -> str:
    # short, readable slug
    return secrets.token_urlsafe(6).replace("_", "").replace("-", "").lower()[:10]


@api.get("/user/share")
async def get_share(user=Depends(get_current_user)):
    u = await db.users.find_one({"id": user["id"]}, {"_id": 0, "share_slug": 1, "share_enabled": 1})
    return {"enabled": bool(u.get("share_enabled")), "slug": u.get("share_slug") or None}


@api.post("/user/share")
async def toggle_share(body: ShareToggleInput, user=Depends(get_current_user)):
    u = await db.users.find_one({"id": user["id"]}, {"_id": 0, "share_slug": 1})
    slug = u.get("share_slug") if u else None
    if body.enabled and not slug:
        # ensure unique
        for _ in range(5):
            candidate = _gen_slug()
            if not await db.users.find_one({"share_slug": candidate}):
                slug = candidate
                break
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {"share_enabled": body.enabled, "share_slug": slug}},
    )
    return {"enabled": body.enabled, "slug": slug if body.enabled else None}


@api.get("/public/vault/{slug}")
async def public_vault(slug: str):
    u = await db.users.find_one({"share_slug": slug, "share_enabled": True}, {"_id": 0})
    if not u:
        raise HTTPException(status_code=404, detail="Vault not found")
    items = await db.collection_items.find({"user_id": u["id"]}, {"_id": 0}).sort("added_at", -1).to_list(5000)
    card_ids = [i["card_id"] for i in items]
    cards = await pc.fetch_cards_bulk(db, card_ids)
    hydrated = []
    total_qty = 0
    total_value = 0.0
    sets_seen = set()
    for item in items:
        card = cards.get(item["card_id"], {"id": item["card_id"], "name": "Unknown"})
        card.pop("_id", None)
        qty = item.get("quantity", 1)
        total_qty += qty
        price = await pc.get_card_market_price(card)
        if item.get("is_foil") or item.get("is_holo"):
            price *= 1.2
        total_value += price * qty
        set_obj = card.get("set") or {}
        if set_obj.get("id"):
            sets_seen.add(set_obj["id"])
        hydrated.append({
            "id": item["id"],
            "card_id": item["card_id"],
            "quantity": qty,
            "condition": item.get("condition", "NM"),
            "is_foil": item.get("is_foil", False),
            "is_holo": item.get("is_holo", False),
            "card": card,
        })
    return {
        "username": u.get("username"),
        "member_since": u.get("created_at"),
        "stats": {
            "total_cards": total_qty,
            "unique_cards": len(set(card_ids)),
            "total_value": round(total_value, 2),
            "sets_collected": len(sets_seen),
        },
        "items": hydrated,
    }


# ---------- Health ----------
@api.get("/")
async def root():
    return {"message": "PokeVault API", "status": "ok"}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@app.on_event("startup")
async def startup():
    try:
        await db.users.create_index("email", unique=True)
        await db.collection_items.create_index([("user_id", 1), ("card_id", 1)])
        await db.wishlist.create_index("user_id")
        await db.trades.create_index("user_id")
        await db.decks.create_index("user_id")
        await db.cached_cards.create_index("id", unique=True)
        await db.cached_sets.create_index("id", unique=True)
        await db.value_snapshots.create_index([("user_id", 1), ("date", 1)], unique=True)
        await db.password_reset_tokens.create_index("token", unique=True)
        await db.users.create_index("share_slug")
    except Exception as e:
        logger.warning(f"Index setup: {e}")


@app.on_event("shutdown")
async def shutdown():
    client.close()
