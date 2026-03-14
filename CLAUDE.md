# 業務行事曆 — 專案規則

## 專案概述
房仲業務行程管理工具。支援三種行程情境（簽委託約、帶看、簽買賣契約），整合 Google 日曆雙向同步，並可在新增「帶看」行程時自動推送到買方管理工具。

## 專案結構
```
real-estate-calendar/
├── app.py              # Flask 後端（所有 API + 前端 HTML 內嵌於此）
├── Dockerfile
├── requirements.txt
└── static/             # 靜態資源（若有）
```

## 核心 API 端點
| 端點 | 方法 | 用途 |
|------|------|------|
| `/api/events` | GET | 查詢行程（月/週範圍） |
| `/api/events` | POST | 新增行程（含 Google Calendar 推送 + Buyer 帶看同步） |
| `/api/events/<event_id>` | GET/PUT/DELETE | 取得/更新/刪除單筆行程 |
| `/api/events/list-for-agent` | GET | Agent 查詢（需 X-Service-Key） |
| `/api/events/create-for-agent` | POST | Agent 新增行程（需 X-Service-Key） |
| `/api/suggest/properties` | GET | 物件智慧搜尋（轉發 Library） |
| `/api/suggest/buyers` | GET | 買方智慧搜尋（轉發 Buyer /api/buyer-suggest） |
| `/api/suggest/sellers` | GET | 賣方智慧搜尋（轉發 Library） |
| `/api/sync-from-google` | GET | 從 Google 日曆拉取行程同步回 Firestore |
| `/api/config` | GET | 回傳前端用 portal_url、buyer_url |
| `/api/me` | GET | 目前登入者資訊 |
| `/auth/portal-login` | GET | Portal SSO 登入（接收 token） |

## Firestore 集合
- `calendar_events`：欄位含 `type`（commission/showing/contract）、`title`、`start_dt`、`end_dt`、`seller_name`、`buyer_name`、`buyer_id`、`prop_name`、`prop_id`、`deal_price`（萬元）、`note`、`gcal_event_id`、`created_by`（email）

**注意**：依 `created_by` + `start_dt` 查詢需建立 Firestore 複合索引，否則回傳 500。

## 行程情境（type 欄位）
| type | 中文 | 必填欄位 |
|------|------|---------|
| `commission` | 簽委託約 | 賣方名稱、物件 |
| `showing` | 帶看 | 買方名稱、物件 |
| `contract` | 簽買賣契約 | 買方、賣方、物件、成交價 |

## 跨工具整合
- **Calendar → Library**：物件搜尋透過 `/api/suggest/properties`（轉發 Library 的 `/api/prop-suggest`），**回傳的是中文 key**（`案名`、`地址`），前端必須讀中文 key
- **Calendar → Buyer**：買方搜尋透過 `/api/suggest/buyers`（轉發 Buyer 的 `/api/buyer-suggest`，**公開端點，不需登入**），回傳 `{items: [{id, name, phone}]}`
- **Calendar → Buyer（帶看同步）**：新增 `showing` 行程儲存時，自動 POST 到 `BUYER_URL/api/showings/from-calendar`（帶 `X-Service-Key`）
- **Portal Agent → Calendar**：Agent 透過 `X-Service-Key` 呼叫 `/api/events/list-for-agent`、`/api/events/create-for-agent`

## 環境變數
| 變數 | 用途 |
|------|------|
| `FLASK_SECRET_KEY` | Flask session 加密（必設） |
| `SERVICE_API_KEY` | 服務間 API 金鑰（Portal Agent 呼叫用） |
| `PORTAL_URL` | Portal Cloud Run URL |
| `LIBRARY_URL` | Library Cloud Run URL（物件搜尋用） |
| `BUYER_URL` | Buyer Cloud Run URL（買方搜尋 + 帶看同步） |
| `ADMIN_EMAILS` | 管理員 email 清單 |
| `GOOGLE_CAL_CREDENTIALS_JSON` | Google 服務帳號 JSON 字串（Google 日曆同步用） |
| `GOOGLE_CAL_ID` | Google 日曆 ID，**請填 Gmail 帳號**（如 `a0911190009@gmail.com`）而非 `primary` |

## Google 日曆雙向同步
- **本系統 → Google**：新增/更新/刪除行程時自動推送（`_push_to_google_calendar`、`_update_google_calendar`、`_delete_google_calendar_event`）
- **Google → 本系統**：`GET /api/sync-from-google` 拉取最近 30–90 天行程，比對 `gcal_event_id`，時間有差異即更新 Firestore
- **自動同步**：週視圖每 10 分鐘自動觸發一次
- **`GOOGLE_CAL_ID=primary` 指向的是服務帳號自己的日曆**，不是你的個人 Gmail 日曆，請改填 Gmail 帳號

## 前端功能
- **月視圖**：月曆格子，點空白日→新增行程 Modal，點有行程日→側欄看當日清單
- **週視圖**：7 欄 × 24 小時，時間軸左側，今天紅線，支援拖曳移動行程
- **拖曳**：`_isDragging` 旗標防止拖曳結束後誤觸發 click 新增行程
- **Modal 下拉**：物件/買方候選清單改用 `position: fixed` + `getBoundingClientRect()`，避免被 `overflow: hidden` 的 Modal 裁切
- **時間智慧跟隨**：新增時開始時間改動 → 結束時間自動 +1 小時（使用者手動改過結束時間後停用）

## 部署
- **Cloud Run**：`gcloud run deploy real-estate-calendar --source . --region asia-east1 --allow-unauthenticated --clear-base-image`
- **本機**：不需要（直接用 Cloud Run）
- **同步部署**：從 `~/Projects/` 執行 `./sync-to-cloud-and-github.sh "說明"`

## 合作習慣（已磨合的規則）
- **跨工具搜尋 key 名稱**：Library 回傳中文 key（`案名`、`地址`），前端用 `item['案名'] || item.name || ''` 兩層 fallback
- **Modal 自動完成下拉**：一律用 `position: fixed` + `getBoundingClientRect()`，不用 `position: absolute`
- **跨工具 API 不能用需登入的端點**：Calendar 呼叫 Buyer 搜尋用公開的 `/api/buyer-suggest`，不用需要 session 的 `/api/buyers`
- **Firestore 複合索引**：新 Collection 若有「篩選 + 排序」組合，先到 Firebase Console 建索引再上線
