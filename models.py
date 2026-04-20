from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional, Any, Dict
from datetime import datetime, timezone
import uuid


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------- Auth ----------
class RegisterInput(BaseModel):
    email: EmailStr
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=6, max_length=128)


class LoginInput(BaseModel):
    email: EmailStr
    password: str


class UserPublic(BaseModel):
    id: str
    email: EmailStr
    username: str
    created_at: str


class AuthResponse(BaseModel):
    token: str
    user: UserPublic


# ---------- Collection ----------
class AddCollectionItemInput(BaseModel):
    card_id: str
    quantity: int = 1
    condition: str = "NM"  # NM, LP, MP, HP, DMG
    is_foil: bool = False
    is_holo: bool = False
    notes: Optional[str] = ""


class UpdateCollectionItemInput(BaseModel):
    quantity: Optional[int] = None
    condition: Optional[str] = None
    is_foil: Optional[bool] = None
    is_holo: Optional[bool] = None
    notes: Optional[str] = None


class CollectionItem(BaseModel):
    id: str
    user_id: str
    card_id: str
    card: Dict[str, Any]
    quantity: int
    condition: str
    is_foil: bool
    is_holo: bool
    notes: str
    added_at: str


# ---------- Wishlist ----------
class WishlistAddInput(BaseModel):
    card_id: str
    priority: str = "medium"  # low, medium, high
    notes: Optional[str] = ""


class WishlistItem(BaseModel):
    id: str
    user_id: str
    card_id: str
    card: Dict[str, Any]
    priority: str
    notes: str
    added_at: str


# ---------- Trade ----------
class TradeAddInput(BaseModel):
    card_id: str
    quantity: int = 1
    condition: str = "NM"
    asking_price: Optional[float] = None
    notes: Optional[str] = ""


class TradeItem(BaseModel):
    id: str
    user_id: str
    card_id: str
    card: Dict[str, Any]
    quantity: int
    condition: str
    asking_price: Optional[float]
    notes: str
    added_at: str


# ---------- Deck ----------
class DeckCardInput(BaseModel):
    card_id: str
    quantity: int = 1


class DeckCreateInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: Optional[str] = ""
    format: Optional[str] = "standard"


class DeckUpdateInput(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    format: Optional[str] = None
    cards: Optional[List[DeckCardInput]] = None


class Deck(BaseModel):
    id: str
    user_id: str
    name: str
    description: str
    format: str
    cards: List[Dict[str, Any]]
    created_at: str
    updated_at: str


# ---------- Password reset ----------
class ForgotPasswordInput(BaseModel):
    email: EmailStr


class ResetPasswordInput(BaseModel):
    token: str
    password: str = Field(min_length=6, max_length=128)


# ---------- Public share ----------
class ShareToggleInput(BaseModel):
    enabled: bool
