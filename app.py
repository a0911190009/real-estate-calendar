# -*- coding: utf-8 -*-
"""
房仲工具 — 業務行事曆（real-estate-calendar）
管理業務行程：簽委託約、帶看、簽買賣契約。
和 Google 日曆單向同步（推送）。
Firestore 集合：
  events/   行程資料
"""

import os
import json
import uuid
import logging
from datetime import datetime, timezone, timedelta

import requests as http_requests
from flask import Flask, request, session, redirect, jsonify, send_from_directory
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

# ── 讀取 .env ──
try:
    from dotenv import load_dotenv
    _dir = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(_dir, ".env"), os.path.join(_dir, "..", ".env")):
        if os.path.isfile(p):
            load_dotenv(p, override=False)
            break
except Exception:
    pass

# ── Firestore ──
try:
    from google.cloud import firestore as _firestore
    _db = None
except ImportError:
    _firestore = None
    _db = None


def _get_db():
    """取得 Firestore client（延遲初始化）。"""
    global _db
    if _db is not None:
        return _db
    if _firestore is None:
        return None
    try:
        _db = _firestore.Client(
            project=os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT")
        )
        return _db
    except Exception as e:
        logging.warning("Calendar: Firestore 初始化失敗: %s", e)
        return None


# ── Flask ──
app = Flask(__name__)

# 跨工具回饋系統
from feedback_endpoint import bp as _feedback_bp
app.register_blueprint(_feedback_bp)

_secret = os.environ.get("FLASK_SECRET_KEY", "")
if not _secret:
    logging.warning("FLASK_SECRET_KEY 未設定，使用預設 dev key，請盡快補上環境變數。")
app.secret_key = _secret or "dev-only-insecure-key"
# SameSite=None：Portal 跨站跳轉後瀏覽器才能正確帶 session cookie
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)  # 手機瀏覽器會清除沒有到期日的 session cookie，設 30 天保持登入

# ─── 開發模式：自動模擬登入 ───
@app.before_request
def auto_login_dev():
    """本地開發時，SKIP_AUTH=true 會自動模擬登入，跳過 Portal token 驗證"""
    if os.getenv('SKIP_AUTH'):
        session.permanent = True  # 讓 cookie 帶 30 天到期日，手機不會被清除
        session['user_email'] = 'dev@test.com'
        session['user_name'] = '開發測試'

PORTAL_URL      = (os.environ.get("PORTAL_URL") or "").strip()
LIBRARY_URL     = (os.environ.get("LIBRARY_URL") or "").strip()
BUYER_URL       = (os.environ.get("BUYER_URL") or "").strip()
PEOPLE_URL      = (os.environ.get("PEOPLE_URL") or "").strip()  # 人脈管理 URL（生日提醒用）
ADMIN_EMAILS    = [e.strip() for e in (os.environ.get("ADMIN_EMAILS") or "").split(",") if e.strip()]
SERVICE_KEY     = os.environ.get("SERVICE_KEY", "")
SERVICE_API_KEY = os.environ.get("SERVICE_API_KEY", "") or SERVICE_KEY  # 統一用 SERVICE_API_KEY

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
GENERAL_FEEDBACK_FILE = os.path.join(_APP_DIR, "general_feedback.json")


def _verify_service_key():
    """驗證 X-Service-Key header。"""
    import hmac as _hmac
    key_to_check = SERVICE_API_KEY or SERVICE_KEY
    if not key_to_check:
        return False
    key = request.headers.get("X-Service-Key", "")
    return _hmac.compare_digest(key, key_to_check)
TOKEN_SERIALIZER = URLSafeTimedSerializer(app.secret_key)
TOKEN_MAX_AGE   = 300  # 5 分鐘，容忍 Cloud Run cold start

# Google Calendar API 設定（服務帳號 JSON 或 OAuth credentials）
GOOGLE_CAL_CREDENTIALS_JSON = os.environ.get("GOOGLE_CAL_CREDENTIALS_JSON", "")
GOOGLE_CAL_ID = os.environ.get("GOOGLE_CAL_ID", "primary")


def _is_admin(email):
    return email in ADMIN_EMAILS


def _load_general_feedback():
    """讀取通用反饋列表"""
    if os.path.exists(GENERAL_FEEDBACK_FILE):
        try:
            with open(GENERAL_FEEDBACK_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []


def _atomic_write(fpath, data_str):
    """原子寫入：先寫 .tmp，fsync 後再 os.replace，讀取時永遠是完整檔案。"""
    os.makedirs(os.path.dirname(fpath), exist_ok=True)
    tmp = fpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data_str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, fpath)


VALID_THEME_STYLES = ["navy", "forest", "amber", "minimal", "rose", "oled"]


# ── LOG 工具函式 ──
def log_event(event_type, user_id="", detail=None):
    """記錄業務事件，輸出至 Cloud Logging（Cloud Run stdout 自動收集）。"""
    print(json.dumps({
        "time": datetime.now(timezone.utc).isoformat(),
        "event": event_type,   # 事件名稱，例如 "calendar_add"
        "user": user_id,
        "detail": detail or {}
    }, ensure_ascii=False), flush=True)


@app.route("/api/client-log", methods=["POST"])
def api_client_log():
    """接收前端 JS 錯誤，記錄至 Cloud Logging。"""
    data = request.get_json(silent=True) or {}
    log_event("client_error", detail=data)
    return jsonify({"ok": True})


@app.route("/api/theme", methods=["GET"])
def api_theme_get():
    """取得目前全局外觀風格（跨工具共用 Firestore system_settings/theme）。"""
    db = _get_db()
    style, mode = "navy", None
    if db:
        try:
            doc = db.collection("system_settings").document("theme").get()
            if doc.exists:
                d = doc.to_dict()
                style = d.get("style", "navy")
                mode = d.get("mode")
        except Exception:
            pass
    return jsonify({"style": style, "mode": mode})


@app.route("/api/theme", methods=["POST"])
def api_theme_set():
    """設定外觀風格（style 只有管理員能改，mode 任何登入者都能改）。"""
    email = session.get("user_email", "")
    if not email:
        return jsonify({"error": "請先登入"}), 401
    data = request.get_json(silent=True) or {}
    update = {}
    if "style" in data:
        if not _is_admin(email):
            return jsonify({"error": "無管理權限"}), 403
        style = data["style"]
        if style not in VALID_THEME_STYLES:
            return jsonify({"error": "無效風格"}), 400
        update["style"] = style
    if "mode" in data:
        mode = data["mode"]
        if mode in ("dark", "light", "system"):
            update["mode"] = mode
    if update:
        db = _get_db()
        if db:
            try:
                db.collection("system_settings").document("theme").set(update, merge=True)
            except Exception as e:
                return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


def _require_user():
    """
    確認使用者已登入。
    回傳 (email, None) 成功；(None, (msg, status)) 失敗。
    """
    email = session.get("user_email")
    if not email:
        return None, ({"error": "未登入", "redirect": PORTAL_URL or "/"}, 401)
    return email, None


# ══════════════════════════════════════════
#  登入 / 登出
# ══════════════════════════════════════════

@app.route("/auth/portal-login", methods=["GET", "POST"])
def auth_portal_login():
    """Portal 跳轉過來時，驗證 token 建立 session。"""
    token = request.form.get("token") or request.args.get("token", "")
    if not token:
        return redirect(PORTAL_URL or "/")
    try:
        payload = TOKEN_SERIALIZER.loads(token, salt="portal-sso", max_age=TOKEN_MAX_AGE)
    except (SignatureExpired, BadSignature, Exception):
        return redirect(PORTAL_URL or "/")
    email = payload.get("email", "")
    if not email:
        return redirect(PORTAL_URL or "/")
    tab = request.args.get("tab") or request.form.get("tab", "")
    session.permanent = True  # 讓 cookie 帶 30 天到期日，手機不會被清除
    session["user_email"] = email
    session["user_name"]  = payload.get("name", "")
    session["user_picture"] = payload.get("picture", "")
    session.modified = True
    # 有指定分頁時，redirect 到 /?tab=xxx（同域 redirect，SameSite 不影響）
    if tab:
        return redirect(f"/?tab={tab}")
    # 直接 serve 靜態首頁（不做任何 redirect），Set-Cookie 與 HTML 在同一個 response
    # 避免 Chrome SameSite 問題：跨站 redirect 後瀏覽器帶不到剛設的 cookie
    return send_from_directory("static", "index.html")


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"redirect": PORTAL_URL or "/"})


@app.route("/api/config")
def api_config():
    """回傳前端需要的設定（portal_url），不需要登入。"""
    return jsonify({
        "portal_url": PORTAL_URL or "/",
        "buyer_url":  BUYER_URL  or "",
    })


@app.route("/api/me")
def api_me():
    email, err = _require_user()
    if err:
        return jsonify(err[0]), err[1]
    return jsonify({
        "email":    email,
        "name":     session.get("user_name", ""),
        "picture":  session.get("user_picture", ""),
        "is_admin": _is_admin(email),
    })


# ══════════════════════════════════════════
#  行程（events 集合）
# ══════════════════════════════════════════

def _events_col():
    """取得 events Firestore collection。"""
    db = _get_db()
    if db is None:
        return None
    return db.collection("calendar_events")


def _parse_birthday_md(bday):
    """把人脈管理的生日字串解析成 (month, day, birth_year or None)。

    支援格式：YYYY-MM-DD / YYYY/MM/DD / MM-DD / MM/DD。
    解析不出來回傳 None。
    """
    s = str(bday or "").strip().replace("/", "-")
    parts = [p for p in s.split("-") if p != ""]
    try:
        if len(parts) >= 3:                       # YYYY-MM-DD
            y, m, dd = int(parts[0]), int(parts[1]), int(parts[2])
            birth_year = y if 1900 <= y <= 2100 else None
            return (m, dd, birth_year)
        if len(parts) == 2:                       # MM-DD（沒有年）
            return (int(parts[0]), int(parts[1]), None)
    except ValueError:
        return None
    return None


def _build_birthday_events(email, start_dt, end_dt):
    """跟「人脈管理」要有填生日的人脈，組成唯讀的生日提醒虛擬行程。

    依使用者設定：每位客戶生日「前一天」+「當天」各一筆。
    這些行程不存 Firestore、不推 Google 日曆，純提醒用。
    任何錯誤都靜默回傳 []，不影響原本行程載入。
    """
    if not PEOPLE_URL:
        return []
    try:
        headers = {}
        if SERVICE_API_KEY:
            headers["X-Service-Key"] = SERVICE_API_KEY
        r = http_requests.get(
            f"{PEOPLE_URL.rstrip('/')}/api/people/birthdays-for-calendar",
            params={"email": email},
            headers=headers,
            timeout=5,
        )
        if not r.ok:
            return []
        people = (r.json() or {}).get("items", []) or []
    except Exception as e:
        logging.warning("birthday fetch error: %s", e)
        return []

    start_d = start_dt.date()
    end_d   = end_dt.date()
    events  = []

    for p in people:
        md = _parse_birthday_md(p.get("birthday"))
        if not md:
            continue
        month, day, birth_year = md
        name = (p.get("name") or "").strip() or "客戶"
        pid  = p.get("id") or ""

        # 查詢範圍可能跨年（如 12 月看到隔年 1 月），逐年產生
        for y in range(start_d.year, end_d.year + 1):
            try:
                bdate = datetime(y, month, day).date()
            except ValueError:
                # 2/29 等不存在 → 退到當年 2/28
                if month == 2 and day == 29:
                    bdate = datetime(y, 2, 28).date()
                else:
                    continue
            age = (y - birth_year) if birth_year else None
            # ('eve' = 前一天提醒, 'day' = 生日當天)
            for when, the_date in (("eve", bdate - timedelta(days=1)), ("day", bdate)):
                if not (start_d <= the_date <= end_d):
                    continue
                ds = the_date.isoformat()
                if when == "day":
                    title = f"🎂 {name} 生日" + (f"（{age} 歲）" if age and age > 0 else "")
                else:
                    title = f"🎂 明天是 {name} 生日"
                events.append({
                    "id": f"bday-{pid}-{ds}-{when}",
                    "type": "birthday",
                    "is_birthday": True,
                    "title": title,
                    "start_dt": f"{ds}T00:00",
                    "end_dt":   f"{ds}T23:59",
                    "note": "來自人脈管理的生日提醒",
                    # 給前端做「打電話 / LINE 祝賀」用
                    "_person_id":   pid,
                    "_person_name": name,
                    "_phone":       (p.get("phone") or "").strip(),
                    "_line_id":     (p.get("line_id") or "").strip(),
                    "_when":        when,
                })
    return events


@app.route("/api/events", methods=["GET"])
def api_events_list():
    """
    列出行程清單。
    查詢參數：
      start=YYYY-MM-DD   起始日期（預設當月第一天）
      end=YYYY-MM-DD     結束日期（預設當月最後一天）
    管理員可看全部，一般用戶只看自己的。
    """
    email, err = _require_user()
    if err:
        return jsonify(err[0]), err[1]

    col = _events_col()
    if col is None:
        return jsonify({"error": "Firestore 不可用"}), 503

    # 時間範圍篩選
    start_str = request.args.get("start", "")
    end_str   = request.args.get("end", "")
    now = datetime.now(timezone(timedelta(hours=8)))
    if start_str:
        try:
            start_dt = datetime.fromisoformat(start_str).replace(tzinfo=timezone(timedelta(hours=8)))
        except Exception:
            start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if end_str:
        try:
            end_dt = datetime.fromisoformat(end_str).replace(tzinfo=timezone(timedelta(hours=8)))
        except Exception:
            # 下個月第一天
            if now.month == 12:
                end_dt = now.replace(year=now.year+1, month=1, day=1, hour=23, minute=59)
            else:
                end_dt = now.replace(month=now.month+1, day=1, hour=23, minute=59)
    else:
        if now.month == 12:
            end_dt = now.replace(year=now.year+1, month=1, day=1, hour=23, minute=59)
        else:
            end_dt = now.replace(month=now.month+1, day=1, hour=23, minute=59)

    # 管理員看全部，一般用戶只看自己
    if _is_admin(email):
        query = col.where("start_dt", ">=", start_dt.isoformat()).where("start_dt", "<=", end_dt.isoformat())
    else:
        query = col.where("created_by", "==", email).where("start_dt", ">=", start_dt.isoformat()).where("start_dt", "<=", end_dt.isoformat())

    docs = query.stream()
    events = []
    for doc in docs:
        d = doc.to_dict()
        d["id"] = doc.id
        events.append(d)

    # 併入人脈管理的生日提醒（唯讀虛擬行程，不存 Firestore）
    try:
        events.extend(_build_birthday_events(email, start_dt, end_dt))
    except Exception as e:
        logging.warning("merge birthday events failed: %s", e)

    # 記錄用戶載入了行事曆（只記第一次，有帶 start 參數才算主動查看）
    if start_str:
        log_event("calendar_view", user_id=email, detail={"start": start_str, "count": len(events)})
    return jsonify(events)


@app.route("/api/events", methods=["POST"])
def api_events_create():
    """
    新增行程。
    欄位：
      type          行程類型：commission（簽委託）/ showing（帶看）/ contract（簽買賣）
      title         行程標題（選填，自動生成時也可覆蓋）
      start_dt      開始時間 ISO 8601（台灣時區）
      end_dt        結束時間 ISO 8601
      note          備註
      seller_name   賣方姓名（commission / contract 用）
      buyer_name    買方姓名（showing / contract 用）
      buyer_id      買方 ID（對應 Buyer 工具）
      prop_name     物件名稱
      prop_id       物件 ID（對應 Library）
      deal_price    成交價（contract 用，萬元）
    """
    email, err = _require_user()
    if err:
        return jsonify(err[0]), err[1]
    log_event("calendar_event_create", user_id=email)

    data = request.get_json(silent=True) or {}
    event_type = (data.get("type") or "").strip()
    if event_type not in ("commission", "showing", "contract", "other"):
        return jsonify({"error": "行程類型無效，需為 commission / showing / contract / other"}), 400

    start_dt = (data.get("start_dt") or "").strip()
    if not start_dt:
        return jsonify({"error": "請填寫開始時間"}), 400

    # 驗證必填欄位
    if event_type == "commission":
        if not data.get("seller_name"):
            return jsonify({"error": "簽委託約需填寫賣方姓名"}), 400
        if not data.get("prop_name"):
            return jsonify({"error": "簽委託約需填寫物件名稱"}), 400
    elif event_type == "showing":
        if not data.get("buyer_name"):
            return jsonify({"error": "帶看需填寫買方姓名"}), 400
        if not data.get("prop_name"):
            return jsonify({"error": "帶看需填寫物件名稱"}), 400
    elif event_type == "contract":
        if not data.get("buyer_name"):
            return jsonify({"error": "簽買賣契約需填寫買方姓名"}), 400
        if not data.get("seller_name"):
            return jsonify({"error": "簽買賣契約需填寫賣方姓名"}), 400
        if not data.get("prop_name"):
            return jsonify({"error": "簽買賣契約需填寫物件名稱"}), 400

    # 自動生成標題
    type_label = {"commission": "簽委託約", "showing": "帶看", "contract": "簽買賣契約", "other": "其他行程"}
    auto_title = type_label[event_type]
    if event_type == "commission":
        auto_title += f"｜{data.get('prop_name', '')}（賣方：{data.get('seller_name', '')}）"
    elif event_type == "showing":
        auto_title += f"｜{data.get('prop_name', '')}（買方：{data.get('buyer_name', '')}）"
    elif event_type == "contract":
        auto_title += f"｜{data.get('prop_name', '')}（買方：{data.get('buyer_name', '')} × 賣方：{data.get('seller_name', '')}）"

    title = (data.get("title") or "").strip() or auto_title

    now_iso = datetime.now(timezone(timedelta(hours=8))).isoformat()
    event_id = str(uuid.uuid4())

    doc = {
        "id":          event_id,
        "type":        event_type,
        "title":       title,
        "start_dt":    start_dt,
        "end_dt":      (data.get("end_dt") or "").strip(),
        "note":        (data.get("note") or "").strip(),
        "seller_name": (data.get("seller_name") or "").strip(),
        "buyer_name":  (data.get("buyer_name") or "").strip(),
        "buyer_id":    (data.get("buyer_id") or "").strip(),
        "prop_name":   (data.get("prop_name") or "").strip(),
        "prop_id":     (data.get("prop_id") or "").strip(),
        "deal_price":  data.get("deal_price"),  # 萬元
        "created_by":  email,
        "created_at":  now_iso,
        "updated_at":  now_iso,
        "gcal_event_id": "",  # 推送到 Google 日曆後填入
    }

    col = _events_col()
    if col is None:
        return jsonify({"error": "Firestore 不可用"}), 503

    col.document(event_id).set(doc)

    # 嘗試推送到 Google 日曆
    gcal_id = _push_to_google_calendar(doc)
    if gcal_id:
        col.document(event_id).update({"gcal_event_id": gcal_id})
        doc["gcal_event_id"] = gcal_id

    # 帶看行程 → 自動同步到買方管理的帶看紀錄
    _push_showing_to_buyer(doc)

    return jsonify(doc), 201


@app.route("/api/events/<event_id>", methods=["GET"])
def api_event_get(event_id):
    """取得單筆行程。"""
    email, err = _require_user()
    if err:
        return jsonify(err[0]), err[1]

    col = _events_col()
    if col is None:
        return jsonify({"error": "Firestore 不可用"}), 503

    doc_ref = col.document(event_id)
    doc = doc_ref.get()
    if not doc.exists:
        return jsonify({"error": "找不到此行程"}), 404

    d = doc.to_dict()
    d["id"] = doc.id

    # 一般用戶只能看自己的
    if not _is_admin(email) and d.get("created_by") != email:
        return jsonify({"error": "無權限"}), 403

    return jsonify(d)


@app.route("/api/events/<event_id>", methods=["PUT"])
def api_event_update(event_id):
    """更新行程。"""
    email, err = _require_user()
    if err:
        return jsonify(err[0]), err[1]

    col = _events_col()
    if col is None:
        return jsonify({"error": "Firestore 不可用"}), 503

    doc_ref = col.document(event_id)
    doc = doc_ref.get()
    if not doc.exists:
        return jsonify({"error": "找不到此行程"}), 404

    d = doc.to_dict()
    if not _is_admin(email) and d.get("created_by") != email:
        return jsonify({"error": "無權限"}), 403

    data = request.get_json(silent=True) or {}
    now_iso = datetime.now(timezone(timedelta(hours=8))).isoformat()

    # 可更新的欄位
    allowed = ["title", "start_dt", "end_dt", "note", "seller_name", "buyer_name",
               "buyer_id", "prop_name", "prop_id", "deal_price"]
    updates = {k: data[k] for k in allowed if k in data}
    updates["updated_at"] = now_iso

    doc_ref.update(updates)
    merged = {**d, **updates, "id": event_id}

    # 更新 Google 日曆
    if d.get("gcal_event_id"):
        _update_google_calendar(d["gcal_event_id"], merged)

    return jsonify(merged)


@app.route("/api/events/<event_id>", methods=["DELETE"])
def api_event_delete(event_id):
    """刪除行程。"""
    email, err = _require_user()
    if err:
        return jsonify(err[0]), err[1]

    col = _events_col()
    if col is None:
        return jsonify({"error": "Firestore 不可用"}), 503

    doc_ref = col.document(event_id)
    doc = doc_ref.get()
    if not doc.exists:
        return jsonify({"error": "找不到此行程"}), 404

    d = doc.to_dict()
    if not _is_admin(email) and d.get("created_by") != email:
        return jsonify({"error": "無權限"}), 403

    # 刪除 Google 日曆事件
    if d.get("gcal_event_id"):
        _delete_google_calendar_event(d["gcal_event_id"])

    doc_ref.delete()
    return jsonify({"ok": True})


# ══════════════════════════════════════════
#  Agent 專用端點（X-Service-Key 驗證）
# ══════════════════════════════════════════

@app.route("/api/events/list-for-agent", methods=["GET"])
def api_events_list_for_agent():
    """Agent 專用：列出指定用戶的行程（簡化版）。
    Query: email=xxx, start=YYYY-MM-DD, end=YYYY-MM-DD"""
    if not _verify_service_key():
        return jsonify({"error": "需要有效的 X-Service-Key"}), 401
    email = (request.args.get("email") or "").strip()
    if not email:
        return jsonify({"error": "缺少 email"}), 400
    col = _events_col()
    if col is None:
        return jsonify({"items": []})
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8)))
    start_str = request.args.get("start", now.strftime("%Y-%m-%d"))
    end_str   = request.args.get("end", (now + timedelta(days=7)).strftime("%Y-%m-%d"))
    try:
        docs = col.where("created_by", "==", email).stream()
        items = []
        for d in docs:
            item = d.to_dict()
            item["id"] = d.id
            start_dt = item.get("start_dt", "")
            if start_str <= start_dt[:10] <= end_str:
                items.append({
                    "id": d.id,
                    "type": item.get("type", ""),
                    "title": item.get("title", ""),
                    "start_dt": start_dt,
                    "end_dt": item.get("end_dt", ""),
                    "buyer_name": item.get("buyer_name", ""),
                    "prop_name": item.get("prop_name", ""),
                    "note": item.get("note", ""),
                })
        items.sort(key=lambda x: x.get("start_dt", ""))
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/events/create-for-agent", methods=["POST"])
def api_events_create_for_agent():
    """Agent 專用：代指定用戶新增行程。
    Body: { email, type, title, start_dt, end_dt, buyer_name, prop_name, note }"""
    if not _verify_service_key():
        return jsonify({"error": "需要有效的 X-Service-Key"}), 401
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"error": "缺少 email"}), 400
    event_type = (data.get("type") or "showing").strip()
    if event_type not in ("commission", "showing", "contract", "other"):
        event_type = "showing"
    col = _events_col()
    if col is None:
        return jsonify({"error": "Firestore 不可用"}), 503
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8)))
    start_dt = data.get("start_dt") or now.strftime("%Y-%m-%dT10:00:00")
    end_dt   = data.get("end_dt")   or (now + timedelta(hours=1)).strftime("%Y-%m-%dT11:00:00")
    # 自動產生標題
    title = data.get("title") or ""
    if not title:
        type_map = {"showing": "帶看", "commission": "簽委託", "contract": "簽買賣", "other": "其他"}
        label = type_map.get(event_type, "行程")
        buyer = data.get("buyer_name", "")
        prop  = data.get("prop_name", "")
        title = f"{label}" + (f"－{buyer}" if buyer else "") + (f"｜{prop}" if prop else "")
    event = {
        "type": event_type, "title": title,
        "start_dt": start_dt, "end_dt": end_dt,
        "buyer_name": data.get("buyer_name", ""),
        "prop_name": data.get("prop_name", ""),
        "note": data.get("note", ""),
        "created_by": email,
        "created_at": now.isoformat(),
    }
    try:
        doc_ref = col.document()
        doc_ref.set(event)
        return jsonify({"ok": True, "id": doc_ref.id, "title": title})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════
#  搜尋建議（從 Library / Buyer 撈資料）
# ══════════════════════════════════════════

@app.route("/api/suggest/properties")
def api_suggest_properties():
    """
    搜尋物件建議清單。
    查詢參數：q=關鍵字
    向 Library 服務查詢。
    """
    email, err = _require_user()
    if err:
        return jsonify(err[0]), err[1]

    q = (request.args.get("q") or "").strip()
    if not LIBRARY_URL:
        return jsonify({"items": [], "warning": "LIBRARY_URL 未設定"})

    try:
        # 呼叫 Library 的物件搜尋建議 API（帶 session cookie 用 Service Key 代理）
        headers = {}
        if SERVICE_KEY:
            headers["X-Service-Key"] = SERVICE_KEY
        r = http_requests.get(
            f"{LIBRARY_URL.rstrip('/')}/api/prop-suggest",
            params={"q": q, "user": email},
            headers=headers,
            timeout=5,
        )
        if r.ok:
            return jsonify(r.json())
    except Exception as e:
        logging.warning("suggest properties error: %s", e)

    return jsonify({"items": []})


@app.route("/api/suggest/buyers")
def api_suggest_buyers():
    """
    搜尋買方建議清單。
    查詢參數：q=關鍵字
    向 Buyer 服務查詢。
    """
    email, err = _require_user()
    if err:
        return jsonify(err[0]), err[1]

    q = (request.args.get("q") or "").strip()
    if not BUYER_URL:
        return jsonify({"items": [], "warning": "BUYER_URL 未設定"})

    try:
        # 呼叫 Buyer 工具的公開搜尋端點（不需 session，直接帶關鍵字）
        r = http_requests.get(
            f"{BUYER_URL.rstrip('/')}/api/buyer-suggest",
            params={"q": q},
            timeout=5,
        )
        if r.ok:
            return jsonify(r.json())
    except Exception as e:
        logging.warning("suggest buyers error: %s", e)

    return jsonify({"items": []})


@app.route("/api/suggest/sellers")
def api_suggest_sellers():
    """
    搜尋賣方建議清單（從 Library 物件的屋主欄位取得）。
    查詢參數：q=關鍵字
    """
    email, err = _require_user()
    if err:
        return jsonify(err[0]), err[1]

    q = (request.args.get("q") or "").strip()
    if not LIBRARY_URL:
        return jsonify({"items": [], "warning": "LIBRARY_URL 未設定"})

    # Library 暫無獨立屋主 API，從物件清單取 seller/owner 欄位
    # 未來可補充獨立賣方 API
    return jsonify({"items": [], "note": "賣方建議待 Library 補充屋主 API"})


# ══════════════════════════════════════════
#  Google Calendar 推送
# ══════════════════════════════════════════

def _get_gcal_service():
    """
    建立 Google Calendar API service。
    優先使用服務帳號（GOOGLE_CAL_CREDENTIALS_JSON 環境變數，JSON 字串）。
    本機開發可設定 GOOGLE_APPLICATION_CREDENTIALS 指向 JSON 檔案。
    """
    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account

        if GOOGLE_CAL_CREDENTIALS_JSON:
            # 從環境變數取服務帳號 JSON
            cred_info = json.loads(GOOGLE_CAL_CREDENTIALS_JSON)
            creds = service_account.Credentials.from_service_account_info(
                cred_info,
                scopes=["https://www.googleapis.com/auth/calendar"],
            )
        else:
            # 使用 GOOGLE_APPLICATION_CREDENTIALS 或 ADC
            from google.auth import default as gauth_default
            creds, _ = gauth_default(scopes=["https://www.googleapis.com/auth/calendar"])

        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        logging.warning("Google Calendar service 建立失敗: %s", e)
        return None


def _build_gcal_body(event: dict) -> dict:
    """將行程 dict 轉換成 Google Calendar event body。"""
    start = event.get("start_dt", "")
    end = event.get("end_dt", "") or start

    # Google Calendar 需要 dateTime 或 date 格式
    def to_gcal_time(iso_str):
        if "T" in iso_str:
            # 有時間
            if "+" not in iso_str and iso_str[-1] != "Z":
                iso_str += "+08:00"
            return {"dateTime": iso_str, "timeZone": "Asia/Taipei"}
        else:
            return {"date": iso_str}

    type_emoji = {"commission": "📋", "showing": "🏠", "contract": "🤝", "other": "📌"}
    emoji = type_emoji.get(event.get("type", ""), "📅")

    description_lines = []
    if event.get("seller_name"):
        description_lines.append(f"賣方：{event['seller_name']}")
    if event.get("buyer_name"):
        description_lines.append(f"買方：{event['buyer_name']}")
    if event.get("prop_name"):
        description_lines.append(f"物件：{event['prop_name']}")
    if event.get("deal_price"):
        description_lines.append(f"成交價：{event['deal_price']} 萬")
    if event.get("note"):
        description_lines.append(f"備註：{event['note']}")

    return {
        "summary": f"{emoji} {event.get('title', '')}",
        "description": "\n".join(description_lines),
        "start": to_gcal_time(start),
        "end": to_gcal_time(end),
        "colorId": {"commission": "5", "showing": "2", "contract": "11", "other": "8"}.get(event.get("type", ""), "1"),
    }


def _push_showing_to_buyer(event: dict):
    """
    帶看行程儲存後，自動在買方管理新增帶看紀錄。
    只有 type=showing 且有 buyer_id 時才推送。
    失敗僅記錄 warning，不影響行事曆本身的回應。
    """
    if event.get("type") != "showing":
        return
    buyer_id = (event.get("buyer_id") or "").strip()
    if not buyer_id:
        return   # 沒有選擇買方 ID，無法建立紀錄

    buyer_url = BUYER_URL.rstrip("/")
    if not buyer_url:
        logging.warning("BUYER_URL 未設定，無法推送帶看紀錄")
        return

    secret_key = app.secret_key  # 與 Buyer 共享同一把 FLASK_SECRET_KEY

    # 從 start_dt 取日期部分（YYYY-MM-DD）
    date_str = (event.get("start_dt") or "")[:10]

    payload = {
        "secret":            secret_key,
        "buyer_id":          buyer_id,
        "buyer_name":        event.get("buyer_name", ""),
        "prop_id":           event.get("prop_id", ""),
        "prop_name":         event.get("prop_name", ""),
        "prop_address":      "",    # 行事曆目前未儲存地址，留空
        "date":              date_str,
        "calendar_event_id": event.get("id", ""),
        "note":              event.get("note", ""),
    }
    try:
        r = http_requests.post(
            f"{buyer_url}/api/showings/from-calendar",
            json=payload,
            timeout=8,
        )
        if r.ok:
            logging.info("帶看紀錄已推送到 Buyer，showing_id=%s", r.json().get("id"))
        else:
            logging.warning("推送帶看紀錄失敗：%s %s", r.status_code, r.text[:200])
    except Exception as e:
        logging.warning("推送帶看紀錄例外：%s", e)


def _push_to_google_calendar(event: dict) -> str:
    """推送行程到 Google 日曆，成功回傳 gcal event id，失敗回傳空字串。"""
    service = _get_gcal_service()
    if service is None:
        return ""
    try:
        body = _build_gcal_body(event)
        result = service.events().insert(calendarId=GOOGLE_CAL_ID, body=body).execute()
        return result.get("id", "")
    except Exception as e:
        logging.warning("推送 Google Calendar 失敗: %s", e)
        return ""


def _update_google_calendar(gcal_event_id: str, event: dict):
    """更新 Google 日曆上的行程。"""
    service = _get_gcal_service()
    if service is None:
        return
    try:
        body = _build_gcal_body(event)
        service.events().update(calendarId=GOOGLE_CAL_ID, eventId=gcal_event_id, body=body).execute()
    except Exception as e:
        logging.warning("更新 Google Calendar 失敗: %s", e)


def _delete_google_calendar_event(gcal_event_id: str):
    """從 Google 日曆刪除行程。"""
    service = _get_gcal_service()
    if service is None:
        return
    try:
        service.events().delete(calendarId=GOOGLE_CAL_ID, eventId=gcal_event_id).execute()
    except Exception as e:
        logging.warning("刪除 Google Calendar 事件失敗: %s", e)


def _gcal_time_to_local(gcal_time: dict) -> str:
    """把 Google Calendar 的 dateTime/date 轉成本系統格式（YYYY-MM-DDTHH:MM:SS+08:00）。"""
    if not gcal_time:
        return ""
    if "dateTime" in gcal_time:
        # 已有時區資訊，直接回傳前 19 碼加台灣時區
        dt_str = gcal_time["dateTime"]
        # 轉成 UTC+8
        try:
            from datetime import datetime, timezone, timedelta
            tz_tw = timezone(timedelta(hours=8))
            # Python 能解析帶 timezone 的 ISO 格式
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            dt_tw = dt.astimezone(tz_tw)
            return dt_tw.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            return dt_str[:19]
    elif "date" in gcal_time:
        return gcal_time["date"] + "T00:00:00"
    return ""


@app.route("/api/sync-from-google", methods=["GET"])
def api_sync_from_google():
    """
    從 Google Calendar 拉回行程變更，比對 Firestore 並更新。
    只更新「本系統已推送過、有 gcal_event_id」的行程。
    若 Google 日曆上的時間被改過，就更新 Firestore 的 start_dt / end_dt。
    若 Google 日曆上的行程已被刪除，不自動刪除本系統（避免誤刪，只標記狀態）。
    回傳 {"updated": N, "checked": M, "message": "..."}
    """
    email, err = _require_user()
    if err:
        return jsonify(err[0]), err[1]

    service = _get_gcal_service()
    if service is None:
        return jsonify({"error": "Google Calendar 服務不可用，請確認 GOOGLE_CAL_CREDENTIALS_JSON 已設定"}), 503

    col = _events_col()
    if col is None:
        return jsonify({"error": "Firestore 不可用"}), 503

    # 查詢範圍：過去 30 天到未來 90 天
    from datetime import datetime, timezone, timedelta
    tz_tw = timezone(timedelta(hours=8))
    now = datetime.now(tz_tw)
    time_min = (now - timedelta(days=30)).isoformat()
    time_max = (now + timedelta(days=90)).isoformat()

    # 從 Google Calendar 拉取行程清單
    try:
        gcal_events = []
        page_token = None
        while True:
            kwargs = dict(
                calendarId=GOOGLE_CAL_ID,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=500,
            )
            if page_token:
                kwargs["pageToken"] = page_token
            result = service.events().list(**kwargs).execute()
            gcal_events.extend(result.get("items", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:
        logging.warning("拉取 Google Calendar 行程失敗: %s", e)
        return jsonify({"error": f"Google Calendar 查詢失敗: {e}"}), 502

    # 建立 gcal_event_id → gcal event 的對照表
    gcal_map = {ev["id"]: ev for ev in gcal_events if "id" in ev}

    # 查詢 Firestore 中有 gcal_event_id 的行程（當前用戶）
    try:
        if _is_admin(email):
            docs = col.where("gcal_event_id", "!=", "").stream()
        else:
            docs = col.where("created_by", "==", email).where("gcal_event_id", "!=", "").stream()
    except Exception as e:
        logging.warning("Firestore 查詢失敗: %s", e)
        return jsonify({"error": "Firestore 查詢失敗"}), 503

    updated = 0
    checked = 0

    for doc in docs:
        d = doc.to_dict()
        gcal_id = d.get("gcal_event_id", "")
        if not gcal_id:
            continue
        checked += 1

        gcal_ev = gcal_map.get(gcal_id)
        if not gcal_ev:
            # Google 日曆上已刪除，不自動刪除本系統，只記錄
            logging.info("行程 %s 在 Google Calendar 上已不存在（gcal_id=%s）", d.get("id"), gcal_id)
            continue

        # 比對時間是否有變更
        gcal_start = _gcal_time_to_local(gcal_ev.get("start", {}))
        gcal_end   = _gcal_time_to_local(gcal_ev.get("end", {}))
        local_start = (d.get("start_dt") or "")[:19]
        local_end   = (d.get("end_dt") or "")[:19]

        # 比對標題是否有變更（去掉 emoji 前綴後比較）
        gcal_summary = gcal_ev.get("summary", "")
        # Google 標題格式："{emoji} {title}"，取空格後的部分
        gcal_title_raw = gcal_summary.split(" ", 1)[-1] if " " in gcal_summary else gcal_summary

        updates = {}
        if gcal_start and gcal_start != local_start:
            updates["start_dt"] = gcal_start
        if gcal_end and gcal_end != local_end:
            updates["end_dt"] = gcal_end

        if updates:
            updates["updated_at"] = datetime.now(tz_tw).isoformat()
            try:
                col.document(d["id"]).update(updates)
                updated += 1
                logging.info("已從 Google Calendar 更新行程 %s：%s", d.get("id"), updates)
            except Exception as e:
                logging.warning("更新 Firestore 行程 %s 失敗: %s", d.get("id"), e)

    msg = f"已檢查 {checked} 筆行程，更新 {updated} 筆"
    return jsonify({"ok": True, "checked": checked, "updated": updated, "message": msg})


# ══════════════════════════════════════════
#  前端頁面
# ══════════════════════════════════════════

@app.route("/api/general-feedback", methods=["GET"])
def api_general_feedback_get():
    """列出所有通用反饋"""
    return jsonify(_load_general_feedback())


@app.route("/api/general-feedback", methods=["POST"])
def api_general_feedback():
    """通用反饋"""
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "請輸入意見內容"}), 400

    entries = _load_general_feedback()
    entries.append({
        "text": text,
        "category": data.get("category", ""),
        "created_at": datetime.now().isoformat(),
    })
    data_str = json.dumps(entries, ensure_ascii=False, indent=2)
    _atomic_write(GENERAL_FEEDBACK_FILE, data_str)

    return jsonify({"ok": True, "total": len(entries)})


# ══════════════════════════════════════════
#  安娜日課表（外勞日常工作）— 完全公開，免登入
#  Firestore：
#    care_settings/template  → 任務模板（時段與印中文）
#    care_done/{YYYY-MM-DD}  → 該日已勾完成的 task_id 清單
# ══════════════════════════════════════════

# 預設任務模板（首次無資料時用，使用者之後可從前端編輯覆蓋）
_CARE_DEFAULT_TASKS = [
    {"id": "morning_care", "time_start": "07:30", "time_end": "09:00",
     "title_id": "Perawatan Pagi & Sarapan", "title_zh": "晨間照顧與早餐",
     "desc_id": "Membantu Ibu bangun tidur, membersihkan diri, menyiapkan sarapan yang bergizi, dan menemani Ibu makan.",
     "desc_zh": "協助媽媽起床、盥洗，準備營養的早餐並陪伴媽媽用餐。", "is_break": False},
    {"id": "environment", "time_start": "09:00", "time_end": "10:30",
     "title_id": "Peningkatan Pemeliharaan Lingkungan", "title_zh": "環境加強維護",
     "desc_id": "Setelah kamar mandi digunakan, lakukan pembersihan ekstra pada kamar mandi dan dapur; jaga lantai kamar mandi tetap kering.",
     "desc_zh": "使用完浴室後立即進行浴室與廚房的加強清潔，保持浴室地板乾燥防滑。", "is_break": False},
    {"id": "hand_rehab", "time_start": "10:30", "time_end": "11:30",
     "title_id": "Latihan Rehabilitasi Tangan", "title_zh": "手部復健練習",
     "desc_id": "Membantu Ibu melakukan latihan rehabilitasi \"melilit jari\". Gerakan sepelan mungkin, perhatikan kenyamanan Ibu.",
     "desc_zh": "協助媽媽進行「纏手指」復健，動作放慢，注意舒適度。", "is_break": False},
    {"id": "lunch", "time_start": "11:30", "time_end": "13:00",
     "title_id": "Makan Siang & Beres-beres", "title_zh": "午餐與收拾",
     "desc_id": "Menyiapkan makan siang, membantu Ibu makan, merapikan dan mencuci peralatan.",
     "desc_zh": "準備午餐、協助媽媽用餐，並將廚房與餐具收拾洗淨。", "is_break": False},
    {"id": "nap", "time_start": "13:00", "time_end": "15:00",
     "title_id": "Waktu Istirahat Siang", "title_zh": "午休時間",
     "desc_id": "Ibu akan tidur siang. Anna juga bisa beristirahat atau mengatur waktu pribadi.",
     "desc_zh": "媽媽會午睡，安娜可同步休息或自由安排私人時間。", "is_break": True},
    {"id": "leg_rehab", "time_start": "15:00", "time_end": "16:00",
     "title_id": "Pijat Rehabilitasi Kaki", "title_zh": "腿部復健按摩",
     "desc_id": "Membantu Ibu pijat rehabilitasi kaki sesuai metode video. Perhatikan reaksi Ibu, gerakan lembut.",
     "desc_zh": "依影片教學手法做腿部復健按摩，細心觀察反應，動作輕柔。", "is_break": False},
    {"id": "free_prep", "time_start": "16:00", "time_end": "17:30",
     "title_id": "Aktivitas Bebas & Menyiapkan Bahan Masakan", "title_zh": "自由活動與備料",
     "desc_id": "Menemani Ibu berjalan-jalan atau mengobrol, kemudian menyiapkan bahan makan malam.",
     "desc_zh": "陪伴媽媽散步或聊天，隨後準備晚餐食材。", "is_break": False},
    {"id": "dinner", "time_start": "17:30", "time_end": "19:00",
     "title_id": "Makan Malam & Beres-beres", "title_zh": "晚餐與收拾",
     "desc_id": "Menyiapkan makan malam, membantu Ibu makan, dan setelah makan menyelesaikan pembersihan dapur.",
     "desc_zh": "準備晚餐、協助用餐，餐後完成廚房與環境收拾清潔。", "is_break": False},
    {"id": "night_care", "time_start": "19:00", "time_end": "21:00",
     "title_id": "Pembersihan Diri Malam & Persiapan Tidur", "title_zh": "夜間盥洗與就寢準備",
     "desc_id": "Membantu Ibu mandi (perhatikan keamanan & lantai licin), dan persiapan sebelum tidur.",
     "desc_zh": "協助媽媽洗澡（注意浴室防滑安全），並協助就寢前準備。", "is_break": False},
]

# 預設核心原則（頁面頂部黃色橫幅顯示）
_CARE_DEFAULT_PRINCIPLES = [
    {"title_id": "Rehabilitasi yang Cermat", "title_zh": "細心復健",
     "text_id": "Melaksanakan pijat rehabilitasi tangan dan kaki setiap hari.",
     "text_zh": "每天落實手部與腿部的復健按摩。"},
    {"title_id": "Tingkatkan Kebersihan", "title_zh": "加強清潔",
     "text_id": "Segera membersihkan setelah digunakan, terutama kamar mandi & dapur.",
     "text_zh": "每次使用過環境後立即清理，尤其浴室與廚房。"},
    {"title_id": "Hormat & Sopan", "title_zh": "尊重禮貌",
     "text_id": "Sebelum menggunakan barang/makanan Ibu, beritahu secara lisan.",
     "text_zh": "使用媽媽的物品或食品前，請先口頭告知。"},
    {"title_id": "Komunikasi", "title_zh": "溝通須知",
     "text_id": "Penyesuaian kerja akan diberitahukan via grup atau oleh kakak laki-laki kedua. Selalu perhatikan pesan grup.",
     "text_zh": "工作調整由群組或二哥告知，請隨時留意群組訊息。"},
]


def _care_template_ref():
    """取得任務模板 doc ref；Firestore 不可用回 None。"""
    db = _get_db()
    return db.collection("care_settings").document("template") if db else None


def _care_done_ref(date_str):
    """取得某日勾選紀錄 doc ref（date_str = YYYY-MM-DD）。"""
    db = _get_db()
    return db.collection("care_done").document(date_str) if db else None


def _care_get_template():
    """讀模板；沒有就回預設（不寫入，第一次 PUT 時才會落地）。"""
    ref = _care_template_ref()
    if ref is None:
        return {"tasks": _CARE_DEFAULT_TASKS, "principles": _CARE_DEFAULT_PRINCIPLES}
    try:
        snap = ref.get()
        if snap.exists:
            d = snap.to_dict() or {}
            return {
                "tasks": d.get("tasks") or _CARE_DEFAULT_TASKS,
                "principles": d.get("principles") or _CARE_DEFAULT_PRINCIPLES,
            }
    except Exception as e:
        logging.warning("care template read error: %s", e)
    return {"tasks": _CARE_DEFAULT_TASKS, "principles": _CARE_DEFAULT_PRINCIPLES}


@app.route("/care")
def care_page():
    """安娜日課表頁面（公開，免登入）。"""
    return send_from_directory("static", "care.html")


@app.route("/api/care/tasks", methods=["GET"])
def api_care_tasks_get():
    """回傳任務模板（含核心原則）。完全公開。"""
    return jsonify(_care_get_template())


@app.route("/api/care/tasks", methods=["PUT"])
def api_care_tasks_put():
    """覆蓋任務模板。完全公開（任何人都能改）。

    body: {"tasks": [...], "principles": [...]}
    tasks 每筆需有：id, time_start, time_end, title_id, title_zh；其餘選填。
    """
    ref = _care_template_ref()
    if ref is None:
        return jsonify({"error": "Firestore 不可用"}), 503

    data = request.get_json(silent=True) or {}
    tasks = data.get("tasks")
    principles = data.get("principles")
    if not isinstance(tasks, list) or not tasks:
        return jsonify({"error": "tasks 必填且為非空陣列"}), 400

    # 簡單驗證每個 task 必要欄位
    cleaned = []
    seen_ids = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "").strip() or f"task_{i+1}"
        if tid in seen_ids:                   # id 撞名自動補序號
            tid = f"{tid}_{i+1}"
        seen_ids.add(tid)
        cleaned.append({
            "id": tid,
            "time_start": str(t.get("time_start") or "").strip(),
            "time_end":   str(t.get("time_end") or "").strip(),
            "title_id":   str(t.get("title_id") or "").strip(),
            "title_zh":   str(t.get("title_zh") or "").strip(),
            "desc_id":    str(t.get("desc_id") or "").strip(),
            "desc_zh":    str(t.get("desc_zh") or "").strip(),
            "is_break":   bool(t.get("is_break")),
        })

    payload = {
        "tasks": cleaned,
        "updated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
    }
    if isinstance(principles, list):
        payload["principles"] = [
            {
                "title_id": str(p.get("title_id") or "").strip(),
                "title_zh": str(p.get("title_zh") or "").strip(),
                "text_id":  str(p.get("text_id") or "").strip(),
                "text_zh":  str(p.get("text_zh") or "").strip(),
            }
            for p in principles if isinstance(p, dict)
        ]

    try:
        ref.set(payload, merge=True)
        return jsonify({"ok": True, "tasks": cleaned, "principles": payload.get("principles")})
    except Exception as e:
        logging.warning("care template write error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/care/done", methods=["GET"])
def api_care_done_get():
    """取得某日（或日期範圍）的勾選紀錄。

    query: date=YYYY-MM-DD（單日，回 {done:[task_id,...]}）
           或 start=YYYY-MM-DD&end=YYYY-MM-DD（範圍，回 {byDate:{date:[task_id,...]}}）
    """
    db = _get_db()
    if db is None:
        return jsonify({"done": [], "byDate": {}})
    single = (request.args.get("date") or "").strip()
    if single:
        try:
            snap = db.collection("care_done").document(single).get()
            return jsonify({"done": (snap.to_dict() or {}).get("done", []) if snap.exists else []})
        except Exception as e:
            logging.warning("care done get error: %s", e)
            return jsonify({"done": []})

    start = (request.args.get("start") or "").strip()
    end   = (request.args.get("end") or "").strip()
    by_date = {}
    if start and end:
        try:
            # care_done doc id 就是日期字串，用範圍查 document id
            docs = (db.collection("care_done")
                      .where("__name__", ">=", db.collection("care_done").document(start).path)
                      .where("__name__", "<=", db.collection("care_done").document(end).path)
                      .stream())
            for d in docs:
                by_date[d.id] = (d.to_dict() or {}).get("done", [])
        except Exception:
            # 範圍查不行就降階用 doc-by-doc（週檢視最多 7 天）
            from datetime import datetime as _dt
            try:
                s = _dt.fromisoformat(start).date()
                e = _dt.fromisoformat(end).date()
                cur = s
                while cur <= e:
                    snap = db.collection("care_done").document(cur.isoformat()).get()
                    if snap.exists:
                        by_date[cur.isoformat()] = (snap.to_dict() or {}).get("done", [])
                    cur += timedelta(days=1)
            except Exception as ee:
                logging.warning("care done range error: %s", ee)
    return jsonify({"byDate": by_date})


@app.route("/api/care/done", methods=["POST"])
def api_care_done_post():
    """勾選/取消勾選某日某任務。

    body: {"date": "YYYY-MM-DD", "task_id": "morning_care", "done": true/false}
    """
    db = _get_db()
    if db is None:
        return jsonify({"error": "Firestore 不可用"}), 503
    data = request.get_json(silent=True) or {}
    date_str = str(data.get("date") or "").strip()
    task_id  = str(data.get("task_id") or "").strip()
    done     = bool(data.get("done"))
    if not date_str or not task_id:
        return jsonify({"error": "date 與 task_id 必填"}), 400

    ref = db.collection("care_done").document(date_str)
    try:
        snap = ref.get()
        cur = list((snap.to_dict() or {}).get("done", []) if snap.exists else [])
        if done and task_id not in cur:
            cur.append(task_id)
        elif (not done) and task_id in cur:
            cur = [t for t in cur if t != task_id]
        ref.set({
            "date": date_str,
            "done": cur,
            "updated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        }, merge=True)
        return jsonify({"ok": True, "done": cur})
    except Exception as e:
        logging.warning("care done write error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    """回傳前端主頁。"""
    return send_from_directory("static", "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


# ══════════════════════════════════════════
#  啟動
# ══════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5003))
    debug = bool(os.environ.get("FLASK_DEBUG", ""))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO)
    app.run(host="0.0.0.0", port=port, debug=debug)
