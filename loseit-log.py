#!/usr/bin/env python3
"""Lose It! Food Logger via GWT-RPC

Unofficial CLI for logging foods to Lose It via reverse-engineered GWT-RPC API.

Usage:
    python loseit-log.py "banana" --search            # Search for food
    python loseit-log.py "banana" -m snacks --pick 1  # Log to snacks, 1st result
    python loseit-log.py "eggs" -m breakfast --pick 1 --servings 2
    python loseit-log.py "salmon" -m dinner --pick 1 --date 2026-02-01
    python loseit-log.py --replay                     # Test auth with Chobani yogurt

Authentication:
    Requires JWT token saved to ~/.config/loseit/token
    Get it from browser cookies (liauth value) after logging into loseit.com

How it works:
    1. searchFoods(query) → returns list of foods from Lose It database
    2. getUnsavedFoodLogEntry(food_pk) → returns nutrient template for food
    3. updateFoodLogEntry(entry, meal, date, servings) → saves to diary

Notes:
    - GWT-RPC protocol is complex; this implementation is heuristic but works
    - Byte arrays must be reversed (GWT serialization quirk)
    - Server only accepts 9 core nutrient ordinals (0,2,3,8,9,10,11,12,13)
    - Day keys come from getUnsavedFoodLogEntry response, not getInitializationData

⚠️  Unofficial & unsupported - use at your own risk!
"""

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone, timedelta, date

try:
    import requests
except ImportError:
    venv_path = os.path.expanduser("~/clawd/email-triage/venv/lib/python3.12/site-packages")
    if os.path.exists(venv_path):
        sys.path.insert(0, venv_path)
    import requests

# ─── Constants ───────────────────────────────────────────────────────────────

SERVICE_URL = "https://www.loseit.com/web/service"
BASE_URL = "https://d3hsih69yn4d89.cloudfront.net/web/"
POLICY_HASH = os.environ.get("LOSEIT_POLICY_HASH", "5ED2771F63B26294E45551B2D697E7B0")
STRONG_NAME = os.environ.get("LOSEIT_STRONG_NAME", "24BBC590737D4E7508A96609A56E11F3")
USER_ID = os.environ.get("LOSEIT_USER_ID", "47596378")
USER_NAME = os.environ.get("LOSEIT_USER_NAME", "Rich")
TOKEN_FILE = os.path.expanduser("~/.config/loseit/token")
HOURS_FROM_GMT = int(os.environ.get("LOSEIT_HOURS_FROM_GMT", "-5"))

MEAL_TYPES = {
    "breakfast": 0, "lunch": 1, "dinner": 2, "snacks": 3, "snack": 3,
}
MEAL_NAMES = {0: "Breakfast", 1: "Lunch", 2: "Dinner", 3: "Snacks"}

HEADERS = {
    "content-type": "text/x-gwt-rpc; charset=UTF-8",
    "x-gwt-module-base": BASE_URL,
    "x-gwt-permutation": STRONG_NAME,
    "x-loseit-gwtversion": "devmode",
    "x-loseit-hoursfromgmt": str(HOURS_FROM_GMT),
    "origin": "https://www.loseit.com",
    "referer": "https://www.loseit.com/",
}

# A known mapping from the sniffing session: 2026-02-02 -> 9164.
# Used to compute day numbers for arbitrary dates.
_DAYNUM_ANCHOR = (date(2026, 2, 2), 9164)

# ─── Auth ────────────────────────────────────────────────────────────────────

def load_token():
    token = os.environ.get("LOSEIT_TOKEN")
    if token:
        return token.strip()
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    print(f"❌ No token. Set LOSEIT_TOKEN or put token in {TOKEN_FILE}")
    sys.exit(1)


def make_session(token):
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.set("liauth", token, domain="www.loseit.com", path="/")
    s.cookies.set("fn_auth", token, domain="www.loseit.com", path="/")
    return s


# ─── GWT-RPC Core ───────────────────────────────────────────────────────────

def gwt_call(session, payload, debug=False):
    """Send GWT-RPC call, return raw response text or None on error."""
    if debug:
        print(f"  📤 Payload ({len(payload)} chars): {payload[:180]}...")
    resp = session.post(SERVICE_URL, data=payload)
    if debug:
        print(f"  📥 HTTP {resp.status_code}, {len(resp.text)} chars")
    if resp.status_code != 200:
        print(f"❌ HTTP {resp.status_code}: {resp.text[:300]}")
        return None
    text = resp.text
    if text.startswith("//EX"):
        err = re.search(r'"([^"]*)"', text)
        print(f"❌ GWT Error: {err.group(1) if err else text[:200]}")
        return None
    if not text.startswith("//OK"):
        print(f"❌ Unexpected: {text[:200]}")
        return None
    return text


def parse_gwt_response(text):
    """Parse //OK[...] → (data_tokens, string_table).

    String table is the [...] array at the end of the response.
    String refs in data are 1-indexed: ref N → string_table[N-1].
    """
    if not text or not text.startswith("//OK["):
        return [], []

    inner = text[5:-1]

    # Find string table array at the end
    bracket_start = inner.rfind(",[\"")
    if bracket_start == -1:
        # fallback: try last ',['
        bracket_start = inner.rfind(",[\"")
    bracket_start = inner.rfind(",[\"")
    # robust find for ',['
    bracket_start = inner.rfind(",[\"")
    bracket_start = inner.rfind(',[')
    if bracket_start == -1:
        return [], []

    data_str = inner[:bracket_start]
    table_str = inner[bracket_start + 1:]

    # Parse string table
    string_table = []
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', table_str):
        s = m.group(1)
        s = s.replace('\\u0026', '&').replace('\\"', '"').replace('\\\\', '\\')
        string_table.append(s)

    # Parse data tokens
    tokens = []
    for tok in data_str.split(','):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith('"') and tok.endswith('"'):
            tokens.append(tok[1:-1].replace('\\u0026', '&'))
        else:
            try:
                tokens.append(float(tok) if '.' in tok else int(tok))
            except ValueError:
                tokens.append(tok)

    return tokens, string_table


def str_ref(string_table, ref):
    """Resolve a GWT string reference. ref is 1-indexed into string_table."""
    if isinstance(ref, int) and 1 <= ref <= len(string_table):
        return string_table[ref - 1]
    return None


# ─── Helpers ────────────────────────────────────────────────────────────────

def uuid_signed_bytes(u: uuid.UUID):
    b = u.bytes
    out = []
    for x in b:
        out.append(x - 256 if x >= 128 else x)
    return out


def day_number_for(d: date) -> int:
    anchor_date, anchor_num = _DAYNUM_ANCHOR
    return anchor_num + (d - anchor_date).days


def parse_date_arg(s: str | None) -> date:
    if not s:
        return datetime.now().date()
    return datetime.strptime(s, "%Y-%m-%d").date()


# ─── Replay ──────────────────────────────────────────────────────────────────

REPLAY_PAYLOAD = (
    "7|0|28|"
    "https://d3hsih69yn4d89.cloudfront.net/web/|"
    "5ED2771F63B26294E45551B2D697E7B0|"
    "com.loseit.core.client.service.LoseItRemoteService|"
    "updateFoodLogEntry|"
    "com.loseit.core.client.service.ServiceRequestToken/1076571655|"
    "com.loseit.core.client.model.FoodLogEntry/264522954|"
    "com.loseit.core.client.model.UserId/4281239478|"
    "Rich|"
    "com.loseit.core.client.model.FoodIdentifier/2763145970|"
    "Yogurt|en-US|"
    "Greek Yogurt, Strawberry, Non Fat|"
    "Chobani|"
    "com.loseit.core.client.model.interfaces.FoodProductType/2860616120|"
    "com.loseit.healthdata.model.shared.Verification/3485154600|"
    "com.loseit.core.client.model.SimplePrimaryKey/3621315060|"
    "[B/3308590456|"
    "com.loseit.core.client.model.FoodLogEntryContext/4082213671|"
    "com.loseit.core.shared.model.DayDate/1611136587|"
    "java.util.Date/3385151746|"
    "com.loseit.core.client.model.interfaces.FoodLogEntryType/1152459170|"
    "com.loseit.core.client.model.FoodServing/1858865662|"
    "com.loseit.core.client.model.FoodNutrients/1097231324|"
    "java.util.HashMap/1797211028|"
    "com.loseit.healthdata.model.shared.food.FoodMeasurement/2371921172|"
    "java.lang.Double/858496421|"
    "com.loseit.core.client.model.FoodServingSize/63998910|"
    "com.loseit.core.client.model.FoodMeasure/1457474932|"
    "1|2|3|4|2|5|6|"
    "5|0|7|47596378|8|-5|"
    "6|9|-1|10|11|12|13|14|0|-1|15|0|"
    "ZwdI0HK|16|17|16|17|-115|-32|94|82|-48|75|64|-95|55|52|-122|-82|-16|-48|120|"
    "18|0|19|20|ZwdImkw|9164|-5|0|-1|-1|0|0|0|"
    "21|1|0|"
    "22|23|1|2|24|9|"
    "25|9|26|55|"
    "25|2|26|150|"
    "25|13|26|11|"
    "25|3|26|0|"
    "25|0|26|110|"
    "25|11|26|0|"
    "25|8|26|5|"
    "25|12|26|14|"
    "25|10|26|15|"
    "27|2|1|28|45|1|1|2|0|"
    "P__________|ZwdI0HK|16|17|16|-23|122|50|48|-46|-41|77|124|-86|-128|-99|40|-26|-33|-33|66|"
)


def do_replay(session, debug=False):
    """Replay the captured Chobani Greek Yogurt → Snacks save."""
    print("🔄 Replaying: Chobani Greek Yogurt, Strawberry, Non Fat → Snacks")
    result = gwt_call(session, REPLAY_PAYLOAD, debug=debug)
    if result:
        print("✅ Logged successfully!")
        print("   📦 Chobani Greek Yogurt, Strawberry, Non Fat")
        print("   🍽️  Meal: Snacks")
        print("   🔥 Calories: 110 | Protein: 11g | Carbs: 15g | Fat: 0g")
        return True
    return False


# ─── Delete (Replay) ─────────────────────────────────────────────────────────

_DELETE_PAYLOAD_PATH = os.path.expanduser("~/clawd/integrations/loseit/data/delete-payload.txt")


def load_delete_payload():
    """Load captured deleteFoodLogEntry payload from disk."""
    try:
        with open(_DELETE_PAYLOAD_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def do_delete_replay(session, debug=False, yes=False):
    """Replay captured deleteFoodLogEntry call (will remove an existing diary entry)."""
    payload = load_delete_payload()
    if not payload:
        print(f"❌ Delete payload not found at {_DELETE_PAYLOAD_PATH}")
        return False

    print("🗑️  Replaying: deleteFoodLogEntry (captured payload)")
    if not yes:
        try:
            ans = input("This will DELETE a food log entry. Type 'delete' to continue: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return False
        if ans != "delete":
            print("Cancelled.")
            return False

    result = gwt_call(session, payload, debug=debug)
    if result:
        print("✅ Deleted successfully! (per server response)")
        print("   📦 Entry: Chobani Greek Yogurt (captured) — 2 servings")
        return True
    return False


# ─── Search ──────────────────────────────────────────────────────────────────

def build_search_payload(query):
    """Build searchFoods GWT-RPC payload (incremental search format)."""
    strings = [
        BASE_URL,
        POLICY_HASH,
        "com.loseit.core.client.service.LoseItRemoteService",
        "searchFoods",
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",
        "java.lang.String/2004016611",
        "I",   # primitive int type
        "Z",   # primitive boolean type
        "com.loseit.core.client.model.UserId/4281239478",
        USER_NAME,
        query,
        "en-US",
    ]
    n = len(strings)
    header = f"7|0|{n}|" + "|".join(strings) + "|"
    data = f"1|2|3|4|6|5|6|6|7|8|8|5|0|9|{USER_ID}|10|{HOURS_FROM_GMT}|11|12|15|1|1|"
    return header + data


def extract_food_results(tokens, string_table):
    """Extract food results from GWT search response.

    Heuristic parser:
    - Each SearchResultFood block ends with: <16 pk bytes> 16 [B_ref] SimplePrimaryKey_ref SearchResultFood_ref
      In practice for our responses: ... <16 bytes>, 16, bytes_type_ref, pk_type_ref, food_type_ref
    - We split on that delimiter and then recover name/brand/category by mapping
      positive string refs in the chunk.

    Returns list of dicts: {name, brand, category, pk_bytes}
    """
    foods = []

    # Identify type refs
    food_type_ref = None
    pk_type_ref = None
    bytes_type_ref = None

    for i, s in enumerate(string_table):
        ref = i + 1
        if "SearchResultFood/" in s:
            food_type_ref = ref
        elif "SimplePrimaryKey/" in s:
            pk_type_ref = ref
        elif s == "[B/3308590456":
            bytes_type_ref = ref

    if not (food_type_ref and pk_type_ref and bytes_type_ref):
        return foods

    delimiter = [16, bytes_type_ref, pk_type_ref, food_type_ref]

    # Find all occurrences of delimiter
    ends = []
    for i in range(len(tokens) - 3):
        if tokens[i:i+4] == delimiter:
            ends.append(i+3)

    # Find plausible start of first entry: after first negative backref marker
    start = 0
    for i, t in enumerate(tokens[:80]):
        if isinstance(t, int) and t < 0:
            start = i + 1
            break

    prev = start
    for end in ends:
        chunk = tokens[prev:end+1]
        # PK bytes: 16 numbers immediately before the delimiter's leading 16
        # (the delimiter begins with 16, so pk bytes are chunk[-(4+16):-4])
        pk_bytes = []
        if len(chunk) >= 4 + 16:
            pk_bytes = chunk[-(4+16):-4]
            pk_bytes = [int(x) for x in pk_bytes]

        # candidate strings from chunk
        strings = []
        for t in chunk:
            if isinstance(t, int) and 1 <= t <= len(string_table):
                s = str_ref(string_table, t)
                if not s:
                    continue
                if s.startswith("com.") or s.startswith("java.") or s.startswith("["):
                    continue
                if s in {"All Foods", "BB", "BQ", "en-US", USER_NAME, "I", "Z"}:
                    continue
                strings.append(s)

        # locale appears as string "en-US" in table; category often a generic like "Pork"
        # name is usually the longest non-empty string in the entry, brand often shorter.
        strings = [s for s in strings if s is not None]
        name = ""
        brand = ""
        category = ""
        if strings:
            name = max(strings, key=lambda x: len(x))
            # category heuristic: common single-word entry or first string in table chunk
            for s in strings:
                if len(s) <= 16 and s[0].isupper() and " " not in s and s.lower() not in {"rich"}:
                    category = s
                    break
            # brand heuristic: remaining non-empty string that's not name/category
            for s in strings:
                if s and s != name and s != category and len(s) <= 30:
                    brand = s
                    break

        if name and pk_bytes and len(pk_bytes) == 16:
            foods.append({
                "name": name,
                "brand": brand,
                "category": category,
                "pk_bytes": pk_bytes,
            })

        prev = end + 1

    return foods


def search_foods(session, query, debug=False):
    """Search for foods, return list of {name, brand, category, pk_bytes}."""
    payload = build_search_payload(query)
    print(f"🔍 Searching: {query}")

    result = gwt_call(session, payload, debug=debug)
    if not result:
        return []

    tokens, string_table = parse_gwt_response(result)

    if debug:
        print(f"\n  String table ({len(string_table)} entries):")
        for i, s in enumerate(string_table):
            print(f"    [{i+1}] {s[:80]}")
        print(f"\n  Data tokens ({len(tokens)}):")
        print(f"    {tokens[:60]}...")

    if not string_table:
        return []

    foods = extract_food_results(tokens, string_table)

    return foods


def load_personal_db():
    """Load personal food database from CSV export"""
    import json
    db_path = os.path.expanduser("~/clawd/integrations/loseit/data/personal-food-db.json")
    if os.path.exists(db_path):
        with open(db_path, 'r') as f:
            return json.load(f)
    return {}


def get_personal_match(food_name, personal_db):
    """Check if food matches personal history"""
    from difflib import SequenceMatcher
    
    # Exact match
    if food_name in personal_db:
        return personal_db[food_name]
    
    # Fuzzy match (75% threshold)
    best_match = None
    best_score = 0
    food_lower = food_name.lower()
    
    for known_food in personal_db.keys():
        score = SequenceMatcher(None, food_lower, known_food.lower()).ratio()
        if score > best_score and score >= 0.75:
            best_score = score
            best_match = known_food
    
    if best_match:
        data = personal_db[best_match].copy()
        data['matched_name'] = best_match
        return data
    
    return None


def display_results(foods, limit=15):
    if not foods:
        print("  No results found.")
        return

    personal_db = load_personal_db()
    has_personal = len(personal_db) > 0

    print(f"\n{'#':>3}  {'Food':50} {'Brand'}")
    print(f"{'─'*3}  {'─'*50} {'─'*20}")
    for i, f in enumerate(foods[:limit]):
        name = (f.get('name') or '')[:50]
        brand = (f.get('brand') or '')[:20]
        if brand:
            print(f"{i+1:>3}  {name:50} {brand}")
        else:
            print(f"{i+1:>3}  {name}")
        
        # Show personal history if available
        if has_personal:
            match = get_personal_match(f.get('name') or '', personal_db)
            if match:
                qty = match.get('typical_qty', 0)
                unit = match.get('unit', '')
                cal = match.get('calories', 0)
                print(f"       📍 You usually log: {qty} {unit} = {cal:.0f} cal")


# ─── getInitializationData (for DayDate key) ────────────────────────────────

def build_get_initialization_data_payload():
    strings = [
        BASE_URL,
        POLICY_HASH,
        "com.loseit.core.client.service.LoseItRemoteService",
        "getInitializationData",
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",
        "com.loseit.core.client.model.UserId/4281239478",
        USER_NAME,
    ]
    n = len(strings)
    header = f"7|0|{n}|" + "|".join(strings) + "|"
    data = f"1|2|3|4|1|5|5|0|6|{USER_ID}|7|{HOURS_FROM_GMT}|"
    return header + data


def get_daydate_key(session, target_daynum: int, debug=False) -> str | None:
    """Best-effort lookup of the DayDate key string for a day number.

    Uses getInitializationData, which returns recent DayDate keys.
    If target is outside returned range, returns None.
    """
    payload = build_get_initialization_data_payload()
    resp = gwt_call(session, payload, debug=debug)
    if not resp:
        return None
    tokens, _st = parse_gwt_response(resp)

    # pattern in sniff: ...,-5,9164,"Zwc78Lo",...
    for i in range(len(tokens) - 2):
        if tokens[i] == target_daynum and isinstance(tokens[i+1], str):
            return tokens[i+1]
        if tokens[i] == HOURS_FROM_GMT and tokens[i+1] == target_daynum and isinstance(tokens[i+2], str):
            return tokens[i+2]
    return None


# ─── getUnsavedFoodLogEntry ─────────────────────────────────────────────────

def build_get_unsaved_food_log_entry_payload(food, locale="en-US"):
    """Build getUnsavedFoodLogEntry payload.

    Captured real method signature: 4 params
      (ServiceRequestToken, IPrimaryKey, String locale, String foodName)

    IPrimaryKey is serialized as: SimplePrimaryKey | [B | 16 | <16 signed bytes>
    """
    name = food.get("name") or ""
    pk_bytes = food.get("pk_bytes") or []
    if len(pk_bytes) != 16:
        raise ValueError("food.pk_bytes must be 16 bytes")

    strings = [
        BASE_URL,                   # 1
        POLICY_HASH,                # 2
        "com.loseit.core.client.service.LoseItRemoteService",  # 3
        "getUnsavedFoodLogEntry",   # 4
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",  # 5
        "com.loseit.core.client.model.interfaces.IPrimaryKey",  # 6
        "java.lang.String/2004016611",  # 7
        "com.loseit.core.client.model.UserId/4281239478",  # 8
        USER_NAME,                  # 9
        "com.loseit.core.client.model.SimplePrimaryKey/3621315060",  # 10
        "[B/3308590456",            # 11
        locale,                     # 12
        name,                       # 13
    ]

    n = len(strings)
    header = f"7|0|{n}|" + "|".join(strings) + "|"

    # Data section: method(1,2,3,4) | 4 params | types(5,6,7,7) | values
    data = []
    data += ["1", "2", "3", "4"]
    data += ["4"]               # 4 params
    data += ["5", "6", "7", "7"]  # param types

    # Param 1: ServiceRequestToken
    data += ["5", "0", "8", USER_ID, "9", str(HOURS_FROM_GMT)]

    # Param 2: IPrimaryKey (serialized as SimplePrimaryKey)
    # NOTE: GWT serializes byte[] in REVERSE order
    data += ["10", "11", "16"]
    data += [str(int(b)) for b in reversed(pk_bytes)]

    # Param 3: locale string
    data += ["12"]

    # Param 4: food name string
    data += ["13"]

    return header + "|".join(data) + "|"


def parse_unsaved_food_log_entry(tokens, string_table):
    """Parse getUnsavedFoodLogEntry response.

    GWT responses serialize data in REVERSE order. Key patterns:
    - Nutrients: <value>, <Double_ref>, <ordinal>, <FoodMeasurement_ref>
    - PK bytes: <16 signed bytes>, <16 (length)>, <[B_ref>, <SimplePrimaryKey_ref>
    - Serving: values near FoodServingSize ref

    Returns dict with: name, brand, category, food_pk_bytes, day_key, nutrients,
    serving_qty, food_measure_ordinal
    """
    out = {
        "name": "",
        "brand": "",
        "category": "",
        "food_pk_bytes": None,
        "day_key": "",
        "nutrients": {},
        "serving_qty": None,
        "food_measure_ordinal": None,
    }

    # Locate type refs in string table
    fm_ref = None      # FoodMeasurement
    dbl_ref = None     # Double
    bytes_ref = None   # [B
    pk_ref = None      # SimplePrimaryKey
    serving_size_ref = None
    food_measure_ref = None

    for i, s in enumerate(string_table):
        ref = i + 1
        if "FoodMeasurement/" in s:
            fm_ref = ref
        elif s == "java.lang.Double/858496421":
            dbl_ref = ref
        elif s == "[B/3308590456":
            bytes_ref = ref
        elif "SimplePrimaryKey/" in s:
            pk_ref = ref
        elif "FoodServingSize/" in s:
            serving_size_ref = ref
        elif "FoodMeasure/" in s:
            food_measure_ref = ref

    # Extract name/brand/category from string table
    user_skip = {USER_NAME, "en-US", "I", "Z", "All Foods", "P__________"}
    candidates = [s for s in string_table if s and not s.startswith(("com.", "java.", "[")) and s not in user_skip]
    if candidates:
        out["name"] = max(candidates, key=len)
        for s in candidates:
            if len(s) <= 20 and " " not in s and s[0].isupper():
                out["category"] = s
                break
        for s in candidates:
            if s and s != out["name"] and s != out["category"] and len(s) <= 30:
                out["brand"] = s
                break

    # Extract day_key (first Zw-prefixed string in tokens)
    for t in tokens:
        if isinstance(t, str) and len(t) >= 5 and t.startswith("Zw") and t != "P__________":
            out["day_key"] = t
            break

    # Food PK bytes: pattern is <16 bytes>, 16(len), [B_ref, SimplePrimaryKey_ref
    # There may be 2 PKs: entry PK (first) and food PK (second)
    if bytes_ref and pk_ref:
        pk_positions = []
        for i in range(16, len(tokens) - 2):
            if (tokens[i] == 16 and i + 2 < len(tokens) and
                    tokens[i+1] == bytes_ref and tokens[i+2] == pk_ref):
                maybe = tokens[i-16:i]
                if all(isinstance(x, (int, float)) for x in maybe):
                    pk_positions.append(([int(x) for x in maybe], i))
        # Second PK is the food PK (first is server-generated entry PK)
        if len(pk_positions) >= 2:
            out["food_pk_bytes"] = pk_positions[1][0]
        elif len(pk_positions) == 1:
            out["food_pk_bytes"] = pk_positions[0][0]

    # Nutrients: GWT response reversed pattern:
    #   <value>, <Double_ref=22>, <ordinal>, <FoodMeasurement_ref=21>
    if fm_ref and dbl_ref:
        for i in range(len(tokens) - 3):
            if (tokens[i+3] == fm_ref and tokens[i+1] == dbl_ref and
                    isinstance(tokens[i+2], int) and isinstance(tokens[i], (int, float))):
                ord_ = int(tokens[i+2])
                val = float(tokens[i])
                if 0 <= ord_ <= 30:
                    out["nutrients"][ord_] = val

    # Serving: look for FoodServingSize ref pattern
    # Pattern: <measure_ordinal>, <FoodMeasure_ref>, <count>, <qty>, <FoodServingSize_ref>
    # So qty is immediately BEFORE FoodServingSize_ref
    if serving_size_ref:
        for i in range(1, len(tokens)):
            if tokens[i] == serving_size_ref:
                if i > 0 and isinstance(tokens[i-1], (int, float)):
                    out["serving_qty"] = float(tokens[i-1])
                break

    # FoodMeasure ordinal: 1 position BEFORE the FoodMeasure_ref
    if food_measure_ref:
        for i in range(1, len(tokens)):
            if tokens[i] == food_measure_ref and isinstance(tokens[i-1], int):
                out["food_measure_ordinal"] = int(tokens[i-1])
                break

    return out


def get_unsaved_food_log_entry(session, food, debug=False):
    payload = build_get_unsaved_food_log_entry_payload(food)
    resp = gwt_call(session, payload, debug=debug)
    if not resp:
        return None
    tokens, st = parse_gwt_response(resp)
    if debug:
        print(f"  getUnsavedFoodLogEntry: string_table={len(st)} tokens={len(tokens)}")
    return parse_unsaved_food_log_entry(tokens, st)


# ─── updateFoodLogEntry ─────────────────────────────────────────────────────

def build_update_food_log_entry_payload(unsaved, meal_ordinal: int, day_key: str, day_num: int, servings: float):
    """Build updateFoodLogEntry payload from parsed unsaved entry."""

    # Scale nutrients — server only accepts the core 9 ordinals
    CORE_NUTRIENT_ORDINALS = {0, 2, 3, 8, 9, 10, 11, 12, 13}
    nutrients = {k: (v * servings) for k, v in (unsaved.get("nutrients") or {}).items()
                 if k in CORE_NUTRIENT_ORDINALS}

    category = unsaved.get("category") or ""
    name = unsaved.get("name") or ""
    brand = unsaved.get("brand") or ""
    food_pk = unsaved.get("food_pk_bytes")
    if not food_pk or len(food_pk) != 16:
        raise ValueError("missing food primary key bytes")

    entry_uuid = uuid.uuid4()
    entry_pk = uuid_signed_bytes(entry_uuid)

    # String table matches replay payload (28 entries)
    strings = [
        BASE_URL,
        POLICY_HASH,
        "com.loseit.core.client.service.LoseItRemoteService",
        "updateFoodLogEntry",
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",
        "com.loseit.core.client.model.FoodLogEntry/264522954",
        "com.loseit.core.client.model.UserId/4281239478",
        USER_NAME,
        "com.loseit.core.client.model.FoodIdentifier/2763145970",
        category or "Food",
        "en-US",
        name,
        brand,
        "com.loseit.core.client.model.interfaces.FoodProductType/2860616120",
        "com.loseit.healthdata.model.shared.Verification/3485154600",
        "com.loseit.core.client.model.SimplePrimaryKey/3621315060",
        "[B/3308590456",
        "com.loseit.core.client.model.FoodLogEntryContext/4082213671",
        "com.loseit.core.shared.model.DayDate/1611136587",
        "java.util.Date/3385151746",
        "com.loseit.core.client.model.interfaces.FoodLogEntryType/1152459170",
        "com.loseit.core.client.model.FoodServing/1858865662",
        "com.loseit.core.client.model.FoodNutrients/1097231324",
        "java.util.HashMap/1797211028",
        "com.loseit.healthdata.model.shared.food.FoodMeasurement/2371921172",
        "java.lang.Double/858496421",
        "com.loseit.core.client.model.FoodServingSize/63998910",
        "com.loseit.core.client.model.FoodMeasure/1457474932",
    ]

    n = len(strings)
    header = f"7|0|{n}|" + "|".join(strings) + "|"

    # Build data, modeled after REPLAY_PAYLOAD
    hm_size = len(nutrients)
    parts = []
    parts += ["1", "2", "3", "4", "2", "5", "6"]

    # token
    parts += ["5", "0", "7", USER_ID, "8", str(HOURS_FROM_GMT)]

    # FoodLogEntry
    parts += [
        "6",
        "9", "-1", "10", "11", "12", "13",
        "14", "0", "-1",
        "15", "0",
        # Key string: MUST be a valid DayDate key-like string (captured).
        (unsaved.get("day_key") or day_key or ""),
        "16", "17", "16",
    ]
    parts += [str(int(b)) for b in reversed(food_pk)]

    # context + daydate
    parts += [
        "18", "0",
        "19", "20", day_key, str(day_num), str(HOURS_FROM_GMT),
        "0", "-1", "-1", "0", "0", "0",
        # entry type
        "21", str(meal_ordinal), "0",
        # FoodServing + FoodNutrients
        # Pattern from replay: 22|23|1|<servings>|24|<nutrient_count>
        "22", "23",
        "1", str(int(servings)) if servings == int(servings) else str(servings),
        "24", str(hm_size),
    ]

    # Nutrient entries
    for ord_, val in sorted(nutrients.items()):
        parts += ["25", str(int(ord_)), "26", str(float(val))]

    # Serving size & measure: keep reasonable defaults; if we parsed measure ordinal we can set it.
    measure = unsaved.get("food_measure_ordinal")
    if measure is None:
        measure = 45  # container-ish default from replay

    servings_int = str(int(servings)) if servings == int(servings) else str(servings)
    parts += [
        "27", servings_int, "1",
        "28", str(int(measure)), "1",
        "1", servings_int, "0",
        "P__________",
        (unsaved.get("day_key") or day_key or ""),
        "16", "17", "16",
    ]
    parts += [str(int(b)) for b in reversed(entry_pk)]

    return header + "|".join(parts) + "|"


def log_food(session, food, meal: str, when: date, servings: float, debug=False):
    meal_ord = MEAL_TYPES[meal]
    day_num = day_number_for(when)
    unsaved = get_unsaved_food_log_entry(session, food, debug=debug)
    if not unsaved:
        print("❌ getUnsavedFoodLogEntry failed")
        return False

    # Prefer day_key from unsaved response; fall back to getInitializationData
    day_key = unsaved.get("day_key") or get_daydate_key(session, day_num, debug=debug) or ""

    # Prefer original selected metadata
    if food.get("name"):
        unsaved["name"] = food["name"]
    if food.get("brand"):
        unsaved["brand"] = food["brand"]
    if food.get("category"):
        unsaved["category"] = food["category"]
    if food.get("pk_bytes"):
        unsaved["food_pk_bytes"] = food["pk_bytes"]

    payload = build_update_food_log_entry_payload(unsaved, meal_ord, day_key, day_num, servings)
    resp = gwt_call(session, payload, debug=debug)
    if not resp:
        return False

    print("✅ Logged successfully!")
    print(f"   📦 {unsaved.get('name','(food)')}")
    if unsaved.get("brand"):
        print(f"   🏷️  {unsaved['brand']}")
    print(f"   🍽️  Meal: {MEAL_NAMES.get(meal_ord, meal)}")
    print(f"   📅 Date: {when.isoformat()} (day {day_num})")
    if servings != 1:
        print(f"   🔢 Servings: {servings}")
    if unsaved.get("nutrients"):
        cals = unsaved["nutrients"].get(0)
        if cals is not None:
            print(f"   🔥 Calories (scaled): {cals * servings:.0f}")
    return True


# ─── getDailyDetailsIncludingPendingForDate ─────────────────────────────────

def build_get_daily_details_payload(target_date: date, day_key: str) -> str:
    """Build getDailyDetailsIncludingPendingForDate GWT-RPC payload.

    Returns the wire payload for fetching today's food/exercise/notes/goals.
    """
    strings = [
        BASE_URL,
        POLICY_HASH,
        "com.loseit.core.client.service.LoseItRemoteService",
        "getDailyDetailsIncludingPendingForDate",
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",
        "com.loseit.core.shared.model.DayDate/1611136587",
        "com.loseit.core.client.model.UserId/4281239478",
        USER_NAME,
        "java.util.Date/3385151746",
    ]
    header = f"7|0|{len(strings)}|" + "|".join(strings) + "|"
    day_num = day_number_for(target_date)
    data = (
        f"1|2|3|4|2|5|6|"
        f"5|0|7|{USER_ID}|8|{HOURS_FROM_GMT}|"
        f"6|9|{day_key}|{day_num}|{HOURS_FROM_GMT}|"
    )
    return header + data


# Nutrient ordinal → label (the 9 core nutrients tracked by the API)
NUTRIENT_NAMES = {
    0: "Calories", 2: "Fat", 3: "Sat Fat", 8: "Cholesterol",
    9: "Sodium", 10: "Carbs", 11: "Fiber", 12: "Sugar", 13: "Protein",
}


def parse_daily_details_response(text):
    """Parse getDailyDetailsIncludingPendingForDate → list of FoodLogEntry dicts.

    Each entry dict contains the data needed to construct a deleteFoodLogEntry
    payload: PKs, day keys, meal/extra ordinals, food info, servings, nutrients,
    and the FoodMeasure ordinal.

    GWT serializes objects in reverse, so the response is parsed by walking the
    token stream and anchoring on SimplePrimaryKey markers (the 16,[B_ref,PK_ref
    triple). For each FoodLogEntry the diary contains TWO consecutive PK blocks
    sharing the same day_key — the first is the FOOD primary key (inside the
    FoodIdentifier) and the second is the ENTRY primary key.
    """
    tokens, strings = parse_gwt_response(text)
    if not strings:
        return []

    # Resolve all the type refs we care about
    refs = {}
    for i, s in enumerate(strings):
        r = i + 1
        if s == "[B/3308590456": refs["bytes"] = r
        elif s.startswith("com.loseit.core.client.model.SimplePrimaryKey/"): refs["pk"] = r
        elif s.startswith("com.loseit.core.client.model.FoodLogEntry/"): refs["food_log_entry"] = r
        elif s.startswith("com.loseit.core.client.model.interfaces.FoodLogEntryType/"): refs["meal"] = r
        elif s.startswith("com.loseit.core.client.model.interfaces.FoodLogEntryTypeExtra/"): refs["extra"] = r
        elif s.startswith("com.loseit.core.client.model.FoodMeasure/"): refs["food_measure"] = r
        elif s.startswith("com.loseit.healthdata.model.shared.food.FoodMeasurement/"): refs["food_measurement"] = r
        elif s == "java.lang.Double/858496421": refs["double"] = r
        elif s.startswith("java.util.HashMap/"): refs["hashmap"] = r
        elif s.startswith("com.loseit.core.client.model.FoodIdentifier/"): refs["food_id"] = r

    if not (refs.get("bytes") and refs.get("pk")):
        return []

    bytes_ref = refs["bytes"]
    pk_ref = refs["pk"]

    # Locate all PK blocks: 16 numeric tokens followed by [16, bytes_ref, pk_ref, "<day_key>"]
    pk_blocks = []
    for i in range(16, len(tokens) - 3):
        if (tokens[i] == 16 and tokens[i+1] == bytes_ref and tokens[i+2] == pk_ref
                and isinstance(tokens[i+3], str)):
            byte_slice = tokens[i-16:i]
            if all(isinstance(x, (int, float)) for x in byte_slice):
                pk_blocks.append({
                    "marker_i": i,
                    "pk_bytes": [int(x) for x in byte_slice],
                    "day_key": tokens[i+3],
                })

    # Group consecutive blocks by day_key. A FoodLogEntry has TWO PK blocks with
    # the same day_key (first = food PK, second = entry PK). Filter out non-food
    # objects (daily-summary, custom-goals, weight entries) by requiring:
    #   1. Same day_key on both PKs
    #   2. A food identifier code (a "Do…"-prefixed string) follows the first marker
    #   3. The FoodLogEntry type ref appears after the second marker
    food_id_re = re.compile(r"^Do[A-Za-z0-9]+$")
    fle_ref = refs.get("food_log_entry")
    entries = []
    i = 0
    while i < len(pk_blocks) - 1:
        a, b = pk_blocks[i], pk_blocks[i+1]
        if a["day_key"] != b["day_key"]:
            i += 1
            continue
        after_first = tokens[a["marker_i"] + 4] if a["marker_i"] + 4 < len(tokens) else None
        has_food_code = isinstance(after_first, str) and food_id_re.match(after_first)
        # Search a small window after the second marker for the FoodLogEntry ref
        has_fle_ref = False
        if fle_ref is not None:
            for j in range(b["marker_i"] + 4, min(b["marker_i"] + 20, len(tokens))):
                if tokens[j] == fle_ref:
                    has_fle_ref = True
                    break
        if not (has_food_code and has_fle_ref):
            i += 1
            continue
        entry = _extract_food_log_entry(tokens, strings, a, b, refs)
        if entry:
            entries.append(entry)
        i += 2
    return entries


def _extract_food_log_entry(tokens, strings, food_pk_block, entry_pk_block, refs):
    """Extract one FoodLogEntry from token stream between food_pk and entry_pk markers."""
    food_pk = food_pk_block["pk_bytes"]   # FIRST PK in stream = FOOD PK
    entry_pk = entry_pk_block["pk_bytes"]  # SECOND PK in stream = ENTRY PK
    day_key = food_pk_block["day_key"]
    # Token range covering this entry's middle data (between the two PK markers)
    mid_start = food_pk_block["marker_i"] + 4  # after [16, bytes_ref, pk_ref, day_key]
    mid_end = entry_pk_block["marker_i"] - 16  # before the entry_pk bytes

    # Food identifier code: the string immediately after the first marker+day_key
    food_id_code = tokens[mid_start] if mid_start < len(tokens) and isinstance(tokens[mid_start], str) else ""

    # FoodMeasure ordinal: in response order this comes after the 3 servings copies
    food_measure_ord = None
    fm_ref = refs.get("food_measure")
    if fm_ref:
        # Walk forward from mid_start; ordinal precedes the food_measure_ref token
        for j in range(mid_start, min(mid_end, mid_start + 20)):
            if tokens[j] == fm_ref and j > 0 and isinstance(tokens[j-1], (int, float)):
                food_measure_ord = int(tokens[j-1])
                break

    # Nutrient values: response has reversed pattern <value, double_ref, ordinal, food_measurement_ref>
    # We extract (ordinal, value) pairs preserving the order they appear in stream.
    nutrients_ordered = []
    fmment_ref = refs.get("food_measurement")
    dbl_ref = refs.get("double")
    if fmment_ref and dbl_ref:
        for j in range(mid_start, mid_end):
            if (tokens[j] == fmment_ref and j >= 3 and tokens[j-2] == dbl_ref
                    and isinstance(tokens[j-1], int) and isinstance(tokens[j-3], (int, float))):
                ord_ = int(tokens[j-1])
                val = float(tokens[j-3])
                if 0 <= ord_ <= 30:
                    nutrients_ordered.append((ord_, val))

    # Meal ordinal: search for meal-ref token (FoodLogEntryType); ordinal precedes it
    meal_ord = 0
    if refs.get("meal"):
        for j in range(mid_start, mid_end):
            if tokens[j] == refs["meal"] and j > 0 and isinstance(tokens[j-1], int):
                meal_ord = int(tokens[j-1])
                break

    # Extra ordinal: search for FoodLogEntryTypeExtra ref; ordinal precedes it
    extra_ord = 3  # default ExtraNone
    if refs.get("extra"):
        for j in range(mid_start, mid_end):
            if tokens[j] == refs["extra"] and j > 0 and isinstance(tokens[j-1], int):
                extra_ord = int(tokens[j-1])
                break

    # Context day_key + day_num + hours.
    # In the response stream (reverse of request), the context section comes
    # AFTER nutrients as: ..., hours_from_gmt, day_num, "<context_day_key>", ...
    # Filter out the food identifier code (starts with "Do") to find the real
    # day key (which starts with "Z" or "_").
    context_day_key = ""
    day_num = 0
    hours_from_gmt = HOURS_FROM_GMT
    for j in range(mid_start, mid_end):
        t = tokens[j]
        if (isinstance(t, str) and t != day_key and not t.startswith("Do")
                and re.match(r"^[A-Za-z0-9_$]+$", t) and len(t) >= 5):
            context_day_key = t
            # day_num precedes the context_day_key (a large int >= 5000)
            for k in range(j-1, max(j-6, mid_start), -1):
                if isinstance(tokens[k], int) and tokens[k] >= 5000:
                    day_num = int(tokens[k])
                    break
            # hours_from_gmt precedes day_num (negative int in -12..14)
            for k in range(j-1, max(j-8, mid_start), -1):
                if isinstance(tokens[k], int) and -12 <= tokens[k] <= 14 and tokens[k] != day_num:
                    hours_from_gmt = int(tokens[k])
                    break
            break

    # Food category/name/brand: walk tokens AFTER the entry_pk_block looking for
    # 3 string-refs to short food-info strings (heuristic).
    food_category = food_name = food_brand = ""
    after = entry_pk_block["marker_i"] + 4  # after [16, bytes_ref, pk_ref, day_key]
    seen_strings = []
    for j in range(after, min(after + 15, len(tokens))):
        t = tokens[j]
        if isinstance(t, int) and 1 <= t <= len(strings):
            s = strings[t-1]
            if s and not (s.startswith("com.") or s.startswith("java.") or s.startswith("[")):
                seen_strings.append(s)
                if len(seen_strings) >= 3:
                    break
    if len(seen_strings) >= 3:
        # Order in response (reversed from request): brand, name, category
        food_brand, food_name, food_category = seen_strings[:3]
    elif len(seen_strings) == 2:
        food_name, food_category = seen_strings
    elif len(seen_strings) == 1:
        food_name = seen_strings[0]

    # Servings: usually appears as 3 identical floats right after food_id_code
    servings = 1.0
    if mid_start + 4 < len(tokens):
        for j in range(mid_start + 1, mid_start + 5):
            if isinstance(tokens[j], float):
                servings = float(tokens[j])
                break

    return {
        "food_pk_response": food_pk,
        "entry_pk_response": entry_pk,
        "entry_day_key": day_key,
        "context_day_key": context_day_key,
        "day_num": day_num,
        "hours_from_gmt": hours_from_gmt,
        "meal_ordinal": meal_ord,
        "extra_ordinal": extra_ord,
        "food_measure_ordinal": food_measure_ord if food_measure_ord is not None else 27,
        "servings": servings,
        "food_identifier_code": food_id_code,
        "food_category": food_category,
        "food_name": food_name,
        "food_brand": food_brand,
        "nutrients_ordered": nutrients_ordered,
    }


def get_daily_food_log_entries(session, target_date: date, debug=False):
    """Fetch today's diary; return parsed list of FoodLogEntry dicts."""
    day_num = day_number_for(target_date)
    day_key = get_daydate_key(session, day_num, debug=debug) or ""
    if not day_key:
        print(f"⚠️  Could not resolve day key for {target_date}; using empty key")
    payload = build_get_daily_details_payload(target_date, day_key)
    resp = gwt_call(session, payload, debug=debug)
    if not resp:
        return []
    return parse_daily_details_response(resp)


def display_diary(entries, target_date: date):
    """Print today's diary entries with indexes per meal."""
    if not entries:
        print(f"  (no entries for {target_date.isoformat()})")
        return
    by_meal = {0: [], 1: [], 2: [], 3: []}
    for e in entries:
        by_meal.setdefault(e["meal_ordinal"], []).append(e)
    print(f"\n📅 Diary for {target_date.isoformat()}:")
    for m_ord in sorted(by_meal.keys()):
        meal_entries = by_meal[m_ord]
        if not meal_entries:
            continue
        print(f"\n  {MEAL_NAMES.get(m_ord, f'meal{m_ord}')}:")
        for i, e in enumerate(meal_entries):
            cals = next((v for ord_, v in e["nutrients_ordered"] if ord_ == 0), None)
            brand = f" ({e['food_brand']})" if e["food_brand"] else ""
            cal_str = f"  [{cals:.0f} cal]" if cals is not None else ""
            print(f"    {i+1}. {e['food_name']}{brand}  × {e['servings']}{cal_str}")


# ─── deleteFoodLogEntry ─────────────────────────────────────────────────────

def build_delete_food_log_entry_payload(entry):
    """Construct a deleteFoodLogEntry GWT-RPC payload from a parsed diary entry.

    Mirrors the wire format captured from the LoseIt web UI's Delete action.
    The entry's FoodIdentifier (with food PK), context, meal type, FoodServing
    with nutrients, FoodServingSize/FoodMeasure, and finally the entry's own
    SimplePrimaryKey are all serialized.
    """
    strings = [
        BASE_URL,                                                              # 1
        POLICY_HASH,                                                           # 2
        "com.loseit.core.client.service.LoseItRemoteService",                  # 3
        "deleteFoodLogEntry",                                                  # 4
        "com.loseit.core.client.service.ServiceRequestToken/1076571655",       # 5
        "com.loseit.core.client.model.FoodLogEntry/264522954",                 # 6
        "com.loseit.core.client.model.UserId/4281239478",                      # 7
        USER_NAME,                                                             # 8
        "com.loseit.core.client.model.FoodIdentifier/2763145970",              # 9
        entry.get("food_category") or "Food",                                  # 10
        entry.get("food_name") or "",                                          # 11
        entry.get("food_brand") or "",                                         # 12
        "com.loseit.core.client.model.interfaces.FoodProductType/2860616120",  # 13
        "com.loseit.core.client.model.SimplePrimaryKey/3621315060",            # 14
        "[B/3308590456",                                                       # 15
        "com.loseit.core.client.model.FoodLogEntryContext/4082213671",         # 16
        "com.loseit.core.shared.model.DayDate/1611136587",                     # 17
        "java.util.Date/3385151746",                                           # 18
        "com.loseit.core.client.model.interfaces.FoodLogEntryType/1152459170",       # 19
        "com.loseit.core.client.model.interfaces.FoodLogEntryTypeExtra/4048538730",  # 20
        "com.loseit.core.client.model.FoodServing/1858865662",                 # 21
        "com.loseit.core.client.model.FoodNutrients/1097231324",               # 22
        "java.util.HashMap/1797211028",                                        # 23
        "com.loseit.healthdata.model.shared.food.FoodMeasurement/2371921172",  # 24
        "java.lang.Double/858496421",                                          # 25
        "com.loseit.core.client.model.FoodServingSize/63998910",               # 26
        "com.loseit.core.client.model.FoodMeasure/1457474932",                 # 27
    ]
    header = f"7|0|{len(strings)}|" + "|".join(strings) + "|"

    servings = entry.get("servings", 1.0)
    servings_str = str(int(servings)) if servings == int(servings) else str(servings)

    def fmt_num(v):
        return str(int(v)) if v == int(v) else str(v)

    entry_pk = entry["entry_pk_response"]
    food_pk = entry["food_pk_response"]
    nutrients = entry.get("nutrients_ordered") or []

    parts = []
    # Method invocation
    parts += ["1", "2", "3", "4"]
    # 2 params: ServiceRequestToken, FoodLogEntry
    parts += ["2", "5", "6"]
    # ServiceRequestToken
    parts += ["5", "0", "7", USER_ID, "8", str(HOURS_FROM_GMT)]
    # FoodLogEntry header + FoodIdentifier section
    parts += ["6", "9", "-1", "10", "0", "11", "12", "13", "0", "-1", "0",
              entry["entry_day_key"], "14", "15", "16"]
    # ENTRY PK bytes — first PK position in REQUEST (FoodLogEntry's own SimplePrimaryKey).
    # Note: response stream has FOOD PK first then ENTRY PK; request order is the opposite.
    parts += [str(int(b)) for b in reversed(entry_pk)]
    # FoodLogEntryContext + DayDate
    parts += ["16", "0", "17", "18",
              entry["context_day_key"], str(entry["day_num"]), str(entry["hours_from_gmt"]),
              "0", "-1", "1", "0", "0", "0"]
    # Meal type + extra
    parts += ["19", str(entry["meal_ordinal"]), "20", str(entry["extra_ordinal"])]
    # FoodServing + FoodNutrients
    parts += ["21", "22", servings_str, servings_str, "23", str(len(nutrients))]
    # Nutrients (HashMap iteration order preserved from server)
    for ord_, val in nutrients:
        parts += ["24", str(int(ord_)), "25", fmt_num(val)]
    # FoodServingSize + FoodMeasure + food code
    parts += ["26", servings_str, "0", "27", str(entry["food_measure_ordinal"]),
              servings_str, servings_str, servings_str, "0", entry["food_identifier_code"]]
    # FOOD PK section — second PK position (the food's PK inside FoodIdentifier).
    parts += [entry["entry_day_key"], "14", "15", "16"]
    parts += [str(int(b)) for b in reversed(food_pk)]
    return header + "|".join(parts) + "|"


def delete_food_log_entry(session, entry, debug=False):
    """Send deleteFoodLogEntry; return True on success."""
    payload = build_delete_food_log_entry_payload(entry)
    resp = gwt_call(session, payload, debug=debug)
    return resp is not None


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Log food to Lose It! via GWT-RPC",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s --replay                              Test auth by logging Chobani yogurt
  %(prog)s "banana" --search                     Search for banana
  %(prog)s "pulled pork" -m dinner --pick 9      Search and log #9 to dinner
  %(prog)s "dill pickles" -m dinner --pick 1 --servings 3
""",
    )
    parser.add_argument("food", nargs="?", help="Food to search for")
    parser.add_argument("--meal", "-m", choices=list(MEAL_TYPES.keys()), default="snacks",
                        help="Meal type (default: snacks)")
    parser.add_argument("--replay", action="store_true",
                        help="Replay captured Chobani yogurt save (auth test)")
    parser.add_argument("--list", dest="list_diary", action="store_true",
                        help="List today's diary entries (use --date to view another day)")
    parser.add_argument("--delete", action="store_true",
                        help="Delete a diary entry. Pair with --pick N (-m MEAL) to choose which entry.")
    parser.add_argument("--delete-replay", action="store_true",
                        help="Replay the original author's captured Chobani delete payload (legacy)")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation for --delete / --delete-replay")
    parser.add_argument("--search", "-s", action="store_true",
                        help="Search only, don't log")
    parser.add_argument("--servings", type=float, default=1.0,
                        help="Number of servings (default: 1)")
    parser.add_argument("--date", dest="date", default=None,
                        help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument("--pick", type=int, default=None,
                        help="Auto-pick Nth search result OR Nth diary entry (1-indexed)")
    parser.add_argument("--debug", "-d", action="store_true",
                        help="Show debug output")
    parser.add_argument("--raw", action="store_true",
                        help="Show raw GWT response (search)")

    args = parser.parse_args()

    if not args.replay and not args.delete and not args.delete_replay and not args.list_diary and not args.food:
        parser.print_help()
        sys.exit(1)

    token = load_token()
    session = make_session(token)

    # ── Replay save mode ──
    if args.replay:
        success = do_replay(session, debug=args.debug)
        sys.exit(0 if success else 1)

    # ── Legacy replay delete mode (captured Chobani payload) ──
    if args.delete_replay:
        success = do_delete_replay(session, debug=args.debug, yes=args.yes)
        sys.exit(0 if success else 1)

    # ── List diary mode ──
    when = parse_date_arg(args.date)
    if args.list_diary:
        entries = get_daily_food_log_entries(session, when, debug=args.debug)
        display_diary(entries, when)
        sys.exit(0)

    # ── Delete diary entry mode ──
    if args.delete:
        entries = get_daily_food_log_entries(session, when, debug=args.debug)
        if not entries:
            print(f"❌ No diary entries for {when.isoformat()}")
            sys.exit(1)
        # Filter to the requested meal
        meal_ord = MEAL_TYPES[args.meal]
        meal_entries = [e for e in entries if e["meal_ordinal"] == meal_ord]
        if not meal_entries:
            print(f"❌ No entries logged to {MEAL_NAMES[meal_ord]} on {when.isoformat()}")
            display_diary(entries, when)
            sys.exit(1)
        if args.pick is None:
            display_diary(entries, when)
            print(f"\nUse --pick N to choose an entry from {MEAL_NAMES[meal_ord]} (1..{len(meal_entries)})")
            sys.exit(1)
        idx = args.pick - 1
        if idx < 0 or idx >= len(meal_entries):
            print(f"❌ --pick must be 1..{len(meal_entries)} for {MEAL_NAMES[meal_ord]}")
            sys.exit(1)
        target = meal_entries[idx]
        brand_str = f" ({target['food_brand']})" if target['food_brand'] else ""
        print(f"🗑️  Deleting from {MEAL_NAMES[meal_ord]}: {target['food_name']}{brand_str} × {target['servings']}")
        if not args.yes:
            try:
                ans = input("Confirm? type 'delete' to proceed: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled.")
                sys.exit(0)
            if ans != "delete":
                print("Cancelled.")
                sys.exit(0)
        ok = delete_food_log_entry(session, target, debug=args.debug)
        if ok:
            print("✅ Deleted")
            sys.exit(0)
        print("❌ Delete failed")
        sys.exit(1)

    # ── Search ──
    foods = search_foods(session, args.food, debug=args.debug)

    if args.raw:
        payload = build_search_payload(args.food)
        result = gwt_call(session, payload)
        if result:
            print(f"\n📄 Raw ({len(result)} chars):\n{result[:2000]}")

    display_results(foods)

    if args.search:
        sys.exit(0)

    if not foods:
        print("❌ No results to log.")
        sys.exit(1)

    when = parse_date_arg(args.date)

    # ── Selection ──
    if args.pick is not None:
        idx = args.pick - 1
        if idx < 0 or idx >= len(foods):
            print(f"❌ --pick must be 1..{len(foods)}")
            sys.exit(1)
    else:
        meal_name = MEAL_NAMES[MEAL_TYPES[args.meal]]
        print(f"\n🍽️  Target meal: {meal_name}")
        try:
            choice = input("\nSelect food # (or 'q' to quit): ").strip()
            if choice.lower() in ('q', 'quit', ''):
                sys.exit(0)
            idx = int(choice) - 1
            if idx < 0 or idx >= len(foods):
                print(f"Pick 1-{min(len(foods), 15)}")
                sys.exit(1)
        except (ValueError, EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)

    selected = foods[idx]
    brand_str = f" ({selected['brand']})" if selected.get('brand') else ""
    print(f"\n  Selected: {selected.get('name','')}{brand_str}")
    if selected.get('pk_bytes'):
        print(f"  PK bytes: {selected['pk_bytes']}")

    ok = log_food(session, selected, args.meal, when, args.servings, debug=args.debug)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
