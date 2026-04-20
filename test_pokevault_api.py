"""
PokeVault backend API tests.

Covers: auth (register/login/me), sets, cards, collection (CRUD + stats + CSV export),
wishlist, trades, decks.

The Pokemon TCG API (pokemontcg.io) is a live dependency. First /api/sets call
may take ~15-20s as it populates Mongo cache. Subsequent calls hit cache.
"""
import os
import io
import csv
import time
import uuid
import pytest
import requests

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', 'https://card-vault-93.preview.emergentagent.com').rstrip('/')
API = f"{BASE_URL}/api"

# Real card id known to exist in the Pokemon TCG API
KNOWN_CARD_ID = "base1-4"  # Charizard
KNOWN_CARD_ID_2 = "swsh4-25"


# ------------------- fixtures -------------------
@pytest.fixture(scope="session")
def http():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def user_creds():
    suffix = uuid.uuid4().hex[:8]
    return {
        "email": f"test_tester_{suffix}@example.com",
        "username": f"tester_{suffix}",
        "password": "pokepoke123",
    }


@pytest.fixture(scope="session")
def auth_token(http, user_creds):
    r = http.post(f"{API}/auth/register", json=user_creds, timeout=30)
    assert r.status_code == 200, f"register failed: {r.status_code} {r.text}"
    data = r.json()
    assert "token" in data and "user" in data
    assert data["user"]["email"] == user_creds["email"].lower()
    assert data["user"]["username"] == user_creds["username"]
    assert isinstance(data["token"], str) and len(data["token"]) > 20
    return data["token"]


@pytest.fixture(scope="session")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}


# ------------------- health -------------------
def test_health(http):
    r = http.get(f"{API}/", timeout=10)
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


# ------------------- auth -------------------
class TestAuth:
    def test_register_duplicate_email(self, http, user_creds, auth_token):
        # auth_token fixture already registered; second attempt should 400
        r = http.post(f"{API}/auth/register", json=user_creds, timeout=30)
        assert r.status_code == 400

    def test_login_success(self, http, user_creds, auth_token):
        r = http.post(f"{API}/auth/login",
                      json={"email": user_creds["email"], "password": user_creds["password"]},
                      timeout=30)
        assert r.status_code == 200
        data = r.json()
        assert "token" in data
        assert data["user"]["email"] == user_creds["email"].lower()

    def test_login_invalid(self, http, user_creds):
        r = http.post(f"{API}/auth/login",
                      json={"email": user_creds["email"], "password": "wrongpass"}, timeout=15)
        assert r.status_code == 401

    def test_me_with_token(self, http, auth_headers, user_creds):
        r = http.get(f"{API}/auth/me", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data["email"] == user_creds["email"].lower()
        assert data["username"] == user_creds["username"]

    def test_me_without_token(self, http):
        r = http.get(f"{API}/auth/me", timeout=10)
        assert r.status_code == 401


# ------------------- sets & cards -------------------
class TestCatalog:
    def test_list_sets(self, http):
        # Potentially slow on first hit (cache populate)
        r = http.get(f"{API}/sets", timeout=60)
        assert r.status_code == 200
        sets = r.json()
        assert isinstance(sets, list)
        assert len(sets) >= 100, f"expected 100+ sets, got {len(sets)}"
        sample = sets[0]
        assert "id" in sample and "name" in sample
        assert "_id" not in sample

    def test_list_cards_default(self, http):
        r = http.get(f"{API}/cards", timeout=45)
        assert r.status_code == 200
        data = r.json()
        assert "data" in data and isinstance(data["data"], list)
        assert data.get("pageSize") == 24
        # Cards may be rate-limited occasionally; accept >=0 but warn on empty
        assert "totalCount" in data

    def test_list_cards_with_filters(self, http):
        r = http.get(f"{API}/cards",
                     params={"q": "Charizard", "page": 1, "page_size": 10}, timeout=45)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data.get("data"), list)

    def test_get_card_by_id(self, http):
        r = http.get(f"{API}/cards/{KNOWN_CARD_ID}", timeout=30)
        assert r.status_code == 200
        card = r.json()
        assert card["id"] == KNOWN_CARD_ID
        assert "_id" not in card
        assert card.get("name", "").lower().startswith("char")

    def test_get_card_not_found(self, http):
        r = http.get(f"{API}/cards/does-not-exist-xyz-123", timeout=30)
        assert r.status_code == 404


# ------------------- collection -------------------
class TestCollection:
    _created_id = None

    def test_add_collection_item(self, http, auth_headers):
        payload = {
            "card_id": KNOWN_CARD_ID,
            "quantity": 2,
            "condition": "NM",
            "is_foil": False,
            "is_holo": True,
            "notes": "TEST item",
        }
        r = http.post(f"{API}/collection", headers=auth_headers, json=payload, timeout=30)
        assert r.status_code == 200, r.text
        item = r.json()
        assert item["card_id"] == KNOWN_CARD_ID
        assert item["quantity"] == 2
        assert item["is_holo"] is True
        assert "id" in item
        assert item["card"]["id"] == KNOWN_CARD_ID
        TestCollection._created_id = item["id"]

    def test_list_collection_contains_item(self, http, auth_headers):
        r = http.get(f"{API}/collection", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list)
        assert any(i["id"] == TestCollection._created_id for i in items)
        # hydrated card
        item = next(i for i in items if i["id"] == TestCollection._created_id)
        assert item["card"]["id"] == KNOWN_CARD_ID

    def test_collection_stats(self, http, auth_headers):
        r = http.get(f"{API}/collection/stats", headers=auth_headers, timeout=30)
        assert r.status_code == 200
        stats = r.json()
        for key in ("total_cards", "unique_cards", "total_value",
                    "sets_collected", "total_sets", "by_rarity"):
            assert key in stats, f"missing {key} in stats"
        assert stats["total_cards"] >= 2
        assert stats["unique_cards"] >= 1
        assert isinstance(stats["by_rarity"], dict)

    def test_patch_collection(self, http, auth_headers):
        assert TestCollection._created_id
        r = http.patch(f"{API}/collection/{TestCollection._created_id}",
                       headers=auth_headers, json={"quantity": 5}, timeout=15)
        assert r.status_code == 200
        # verify via GET
        r2 = http.get(f"{API}/collection", headers=auth_headers, timeout=15)
        items = r2.json()
        item = next(i for i in items if i["id"] == TestCollection._created_id)
        assert item["quantity"] == 5

    def test_export_csv(self, http, auth_headers):
        # uses new session to grab raw text
        r = requests.get(f"{API}/collection/export.csv",
                         headers={"Authorization": auth_headers["Authorization"]}, timeout=30)
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")
        body = r.text
        reader = csv.reader(io.StringIO(body))
        rows = list(reader)
        assert rows[0][0] == "card_id"
        assert any(KNOWN_CARD_ID in row for row in rows[1:])

    def test_delete_collection(self, http, auth_headers):
        assert TestCollection._created_id
        r = http.delete(f"{API}/collection/{TestCollection._created_id}",
                        headers=auth_headers, timeout=15)
        assert r.status_code == 200
        # verify removal
        r2 = http.get(f"{API}/collection", headers=auth_headers, timeout=15)
        items = r2.json()
        assert not any(i["id"] == TestCollection._created_id for i in items)


# ------------------- wishlist -------------------
class TestWishlist:
    _wid = None

    def test_add(self, http, auth_headers):
        r = http.post(f"{API}/wishlist", headers=auth_headers,
                      json={"card_id": KNOWN_CARD_ID, "priority": "high", "notes": "TEST"},
                      timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["card_id"] == KNOWN_CARD_ID
        assert data["priority"] == "high"
        TestWishlist._wid = data["id"]

    def test_list(self, http, auth_headers):
        r = http.get(f"{API}/wishlist", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        items = r.json()
        assert any(i["id"] == TestWishlist._wid for i in items)

    def test_delete(self, http, auth_headers):
        r = http.delete(f"{API}/wishlist/{TestWishlist._wid}",
                        headers=auth_headers, timeout=15)
        assert r.status_code == 200


# ------------------- trades -------------------
class TestTrades:
    _tid = None

    def test_add(self, http, auth_headers):
        r = http.post(f"{API}/trades", headers=auth_headers,
                      json={"card_id": KNOWN_CARD_ID, "quantity": 1,
                            "condition": "NM", "asking_price": 25.50, "notes": "TEST"},
                      timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["card_id"] == KNOWN_CARD_ID
        assert data["asking_price"] == 25.50
        TestTrades._tid = data["id"]

    def test_list(self, http, auth_headers):
        r = http.get(f"{API}/trades", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        assert any(i["id"] == TestTrades._tid for i in r.json())

    def test_delete(self, http, auth_headers):
        r = http.delete(f"{API}/trades/{TestTrades._tid}", headers=auth_headers, timeout=15)
        assert r.status_code == 200


# ------------------- decks -------------------
class TestDecks:
    _did = None

    def test_create_deck(self, http, auth_headers):
        r = http.post(f"{API}/decks", headers=auth_headers,
                      json={"name": "TEST Deck", "description": "test deck",
                            "format": "standard"}, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "TEST Deck"
        assert data["format"] == "standard"
        assert data["cards"] == []
        TestDecks._did = data["id"]

    def test_list_decks(self, http, auth_headers):
        r = http.get(f"{API}/decks", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        assert any(d["id"] == TestDecks._did for d in r.json())

    def test_update_deck_add_cards(self, http, auth_headers):
        r = http.patch(f"{API}/decks/{TestDecks._did}", headers=auth_headers,
                       json={"cards": [{"card_id": KNOWN_CARD_ID, "quantity": 4}]},
                       timeout=30)
        assert r.status_code == 200
        deck = r.json()
        assert len(deck["cards"]) == 1
        assert deck["cards"][0]["card_id"] == KNOWN_CARD_ID

    def test_get_deck_hydrated(self, http, auth_headers):
        r = http.get(f"{API}/decks/{TestDecks._did}", headers=auth_headers, timeout=30)
        assert r.status_code == 200
        deck = r.json()
        assert deck["id"] == TestDecks._did
        assert len(deck["cards"]) == 1
        dc = deck["cards"][0]
        assert dc["card_id"] == KNOWN_CARD_ID
        assert "card" in dc and dc["card"]["id"] == KNOWN_CARD_ID

    def test_delete_deck(self, http, auth_headers):
        r = http.delete(f"{API}/decks/{TestDecks._did}", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        r2 = http.get(f"{API}/decks/{TestDecks._did}", headers=auth_headers, timeout=15)
        assert r2.status_code == 404


# ------------------- protected endpoints unauthenticated -------------------
@pytest.mark.parametrize("path,method", [
    ("/collection", "GET"),
    ("/collection/stats", "GET"),
    ("/wishlist", "GET"),
    ("/trades", "GET"),
    ("/decks", "GET"),
])
def test_protected_unauth(http, path, method):
    r = http.request(method, f"{API}{path}", timeout=15)
    assert r.status_code == 401


# ------------------- password reset (new) -------------------
class TestPasswordReset:
    _new_password = "newpass999"

    def test_forgot_password_existing_email_returns_dev_token(self, http, user_creds, auth_token):
        r = http.post(f"{API}/auth/forgot-password",
                      json={"email": user_creds["email"]}, timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("ok") is True
        assert "dev_token" in data, "dev_token must be surfaced for demo flow"
        assert isinstance(data["dev_token"], str) and len(data["dev_token"]) > 20
        TestPasswordReset._token = data["dev_token"]

    def test_forgot_password_unknown_email_no_enumeration(self, http):
        r = http.post(f"{API}/auth/forgot-password",
                      json={"email": f"nobody_{uuid.uuid4().hex[:6]}@example.com"}, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data.get("ok") is True
        assert "dev_token" not in data, "Must NOT reveal whether email exists"

    def test_reset_password_invalid_token(self, http):
        r = http.post(f"{API}/auth/reset-password",
                      json={"token": "not-a-real-token-xyz", "password": "whatever123"},
                      timeout=15)
        assert r.status_code == 400

    def test_reset_password_valid_then_login_with_new(self, http, user_creds):
        token = getattr(TestPasswordReset, "_token", None)
        assert token, "Forgot-password test must run first to populate token"
        r = http.post(f"{API}/auth/reset-password",
                      json={"token": token, "password": TestPasswordReset._new_password},
                      timeout=15)
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True

        # old password should now fail
        r_old = http.post(f"{API}/auth/login",
                          json={"email": user_creds["email"], "password": user_creds["password"]},
                          timeout=15)
        assert r_old.status_code == 401

        # new password should succeed
        r_new = http.post(f"{API}/auth/login",
                          json={"email": user_creds["email"],
                                "password": TestPasswordReset._new_password},
                          timeout=15)
        assert r_new.status_code == 200
        # persist new token for next tests (user_creds-based auth_headers is session scoped
        # and uses old password; but we issued a fresh token via login above which is fine)
        # Also restore password back for remaining tests via a new forgot/reset cycle.
        r2 = http.post(f"{API}/auth/forgot-password",
                       json={"email": user_creds["email"]}, timeout=15)
        tok2 = r2.json()["dev_token"]
        r3 = http.post(f"{API}/auth/reset-password",
                       json={"token": tok2, "password": user_creds["password"]}, timeout=15)
        assert r3.status_code == 200

    def test_reset_password_token_reuse_fails(self, http, user_creds):
        # generate fresh, use once, try to use again
        r = http.post(f"{API}/auth/forgot-password",
                      json={"email": user_creds["email"]}, timeout=15)
        tok = r.json()["dev_token"]
        r1 = http.post(f"{API}/auth/reset-password",
                       json={"token": tok, "password": user_creds["password"]}, timeout=15)
        assert r1.status_code == 200
        r2 = http.post(f"{API}/auth/reset-password",
                       json={"token": tok, "password": "anotherpass456"}, timeout=15)
        assert r2.status_code == 400


# ------------------- sets progress (new) -------------------
class TestSetsProgress:
    _item_id = None

    def test_empty_progress(self, http, auth_headers):
        # at this point collection is empty (previous tests cleaned up)
        r = http.get(f"{API}/collection/sets-progress", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        # Could be empty OR contain zero — spec says "Returns {} for empty collection"
        assert data == {}, f"expected empty dict for empty collection, got {data}"

    def test_progress_after_add(self, http, auth_headers):
        r_add = http.post(f"{API}/collection", headers=auth_headers,
                          json={"card_id": KNOWN_CARD_ID, "quantity": 1,
                                "condition": "NM", "is_foil": False,
                                "is_holo": False, "notes": "TEST progress"}, timeout=30)
        assert r_add.status_code == 200
        TestSetsProgress._item_id = r_add.json()["id"]

        r = http.get(f"{API}/collection/sets-progress", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        # base1-4 belongs to set "base1" — owned unique count should be >= 1
        assert "base1" in data, f"expected 'base1' in progress map, got keys={list(data.keys())}"
        assert data["base1"] >= 1

    def test_unauth(self, http):
        r = http.get(f"{API}/collection/sets-progress", timeout=10)
        assert r.status_code == 401


# ------------------- value history (new) -------------------
class TestValueHistory:
    def test_stats_creates_snapshot(self, http, auth_headers):
        # call stats first so a snapshot is upserted
        r_stats = http.get(f"{API}/collection/stats", headers=auth_headers, timeout=30)
        assert r_stats.status_code == 200
        r = http.get(f"{API}/collection/value-history", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        snaps = r.json()
        assert isinstance(snaps, list)
        assert len(snaps) >= 1
        last = snaps[-1]
        for key in ("date", "total_value", "total_cards"):
            assert key in last, f"missing {key} in snapshot"
        assert isinstance(last["total_value"], (int, float))

    def test_unauth(self, http):
        r = http.get(f"{API}/collection/value-history", timeout=10)
        assert r.status_code == 401


# ------------------- share / public vault (new) -------------------
class TestShare:
    _slug = None

    def test_get_share_default_disabled(self, http, auth_headers):
        r = http.get(f"{API}/user/share", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data["enabled"] is False
        assert data["slug"] is None

    def test_enable_share_creates_slug(self, http, auth_headers):
        r = http.post(f"{API}/user/share", headers=auth_headers,
                      json={"enabled": True}, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data["enabled"] is True
        assert isinstance(data["slug"], str) and len(data["slug"]) > 0
        TestShare._slug = data["slug"]

        # subsequent GET reflects persisted state
        r2 = http.get(f"{API}/user/share", headers=auth_headers, timeout=15)
        d2 = r2.json()
        assert d2["enabled"] is True
        assert d2["slug"] == TestShare._slug

    def test_public_vault_returns_data(self, http):
        slug = TestShare._slug
        assert slug
        r = http.get(f"{API}/public/vault/{slug}", timeout=20)
        assert r.status_code == 200
        vault = r.json()
        assert "username" in vault
        assert "stats" in vault
        for key in ("total_cards", "unique_cards", "total_value", "sets_collected"):
            assert key in vault["stats"]
        assert "items" in vault
        assert isinstance(vault["items"], list)
        # the progress-test added base1-4 earlier, so expect it here too
        assert any(it["card_id"] == KNOWN_CARD_ID for it in vault["items"])

    def test_public_vault_invalid_slug_404(self, http):
        r = http.get(f"{API}/public/vault/does-not-exist-zz", timeout=15)
        assert r.status_code == 404

    def test_disable_share_hides_vault(self, http, auth_headers):
        slug = TestShare._slug
        r = http.post(f"{API}/user/share", headers=auth_headers,
                      json={"enabled": False}, timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data["enabled"] is False
        # public vault should now 404
        r2 = http.get(f"{API}/public/vault/{slug}", timeout=15)
        assert r2.status_code == 404

    def test_public_vault_unauth_ok(self, http):
        # sanity: the endpoint does not require auth (already tested above implicitly)
        r = http.get(f"{API}/public/vault/any-bogus-slug-123", timeout=10)
        assert r.status_code == 404

