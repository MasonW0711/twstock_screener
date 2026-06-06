# 台股逢低布局選股器 — 系統規格書（SPEC）

> 版本：對應 commit `08479ad`（2026-06）。本文件描述系統「現況」行為，並標注已知效能與雲端限制。

---

## 1. 目的與範圍

對**全台股上市＋上櫃普通股**，依五大類條件（基本面、技術面、法人、估值、成長題材）做機械式篩選與評分，產出：

- Excel 多工作表報告（主表前 20 名 ＋ 四大子榜 ＋ 參數/免責）。
- 互動式 Streamlit 介面（即時調參數、過濾、下載）。

**非目標 / 明確排除**：不使用任何付費資料源、不抓券商共識 EPS／目標價／法人預估。原本的付費佔位欄位已改為**免費公開資料機械式計算的替代指標**（TTM EPS、EPS 年增率、月營收 YoY、估算合理價、估算上漲空間、PEG 替代值，見 §6/§8）。所有輸出皆為機械式計算，非投資建議；`估算合理價` 非券商目標價、`PEG替代值` 非法人預估 PEG。

---

## 2. 資料來源

| 用途 | 來源 | 主機 | 抓取方式 | 雲端可用性 |
|------|------|------|----------|-----------|
| 上市每日收盤價量 | TWSE OpenAPI `exchangeReport/STOCK_DAY_ALL` | `openapi.twse.com.tw` | 全市場批次（1 次） | ⚠️ **WAF 阻擋非台灣 IP** |
| 上市本益比/淨值比/殖利率 | TWSE OpenAPI `exchangeReport/BWIBBU_ALL` | `openapi.twse.com.tw` | 全市場批次（1 次） | ⚠️ 同上 |
| 上櫃每日收盤價量 | TPEX OpenAPI `tpex_mainboard_daily_close_quotes` | `www.tpex.org.tw` | 全市場批次（1 次） | ⚠️ WAF + SSL 憑證問題 |
| 上櫃本益比 | TPEX OpenAPI `tpex_mainboard_peratio_analysis` | `www.tpex.org.tw` | 全市場批次（1 次） | ⚠️ 同上 |
| 股票清單＋產業別 | FinMind `TaiwanStockInfo` | `api.finmindtrade.com` | 全市場批次（1 次） | ✅ 可用 |
| 逐檔歷史股價 | FinMind `TaiwanStockPrice` | `api.finmindtrade.com` | **逐檔**（每股 1 次） | ✅ 可用 |
| 逐檔月營收 | FinMind `TaiwanStockMonthRevenue` | `api.finmindtrade.com` | **逐檔** | ✅ 可用 |
| 逐檔損益表 | FinMind `TaiwanStockFinancialStatements` | `api.finmindtrade.com` | **逐檔** | ✅ 可用 |
| 逐檔資產負債表 | FinMind `TaiwanStockBalanceSheet` | `api.finmindtrade.com` | **逐檔** | ✅ 可用 |
| 逐檔三大法人買賣超 | FinMind `TaiwanStockInstitutionalInvestorsBuySell` | `api.finmindtrade.com` | **逐檔** | ✅ 可用 |
| 逐檔融資融券（目前未進主流程） | FinMind `TaiwanStockMarginPurchaseShortSale` | `api.finmindtrade.com` | **逐檔** | ✅ 可用 |

**金鑰**：`FINMIND_TOKEN`（環境變數，或 Streamlit secrets）。無 token 可跑但流量上限極低。

---

## 3. 系統架構與模組

```
config.py         所有可調門檻、路徑、API 參數（單一設定來源）
themes.py         題材 → 代表股對照 + 產業關鍵字輔助標記
data_sources.py   抓取層：TWSE/TPEX 批次、FinMind 逐檔、快取、seed 後備、限流退避
screener.py       純計算層：指標計算 + 條件判斷 + 評分評等（無 I/O）
run_screener.py   流程編排：三階段漏斗 + 組表 + 排序 + 分榜
excel_report.py   Excel 多工作表輸出
app.py            Streamlit 介面（呼叫 run_screener.run_screen）
make_seed.py      本機產生雲端後備快照 seed/
seed/             內建快照後備（snapshot.parquet, universe.parquet）
cache/            當日原始資料快取
output/           Excel 輸出
```

**分層原則**：`screener.py` 為純函式（給 DataFrame → 回指標/布林/分數，不碰網路）；`data_sources.py` 包辦所有 I/O；`run_screener.py` 串接。

---

## 4. 篩選漏斗流程（`run_screener.run_screen`）

```
① 全市場快照            get_market_snapshot() + get_universe()，合併產業別
   ↓                    （優先序：當日快取 → 即時抓取 → seed 後備）
② 第一關（批次，快）    流動性 lots > MIN_DAILY_LOTS，依 lots 降冪排序
   ↓                    若 max_deep 設定，取前 N 檔進入深掃
③ 第二關（逐檔，慢）    對每檔依序抓 FinMind、算指標、套硬門檻
   ↓                    技術面 gate → 距52週高 gate → 基本面硬門檻
④ 組表 / 排序 / 分榜    依 _score 降冪；產四子榜；輸出
```

### 第二關每檔的處理順序（短路設計）
1. `fetch_price_history` → `compute_technical`；`None` 或資料不足 → 跳過。
2. 距 52 週高 > `MAX_DIST_FROM_52W_HIGH` → 跳過。
3. `technical_pass`（排除破年線、長空排列、選配回檔區）→ 不過跳過。
4. `fetch_income_statement` + `fetch_balance_sheet` + `fetch_month_revenue` → `compute_fundamentals`。
5. `fundamentals_pass`（全部硬門檻）→ 不過跳過；過則 `passed += 1`。
6. `fetch_institutional` → 法人加分；`tag_themes` 題材；PE vs 產業中位數。
7. `score_and_rate` → 評分評等 → `build_row`。

> 設計意圖：先用便宜的技術面 gate 過濾，才抓較貴的財報三表，減少 FinMind 呼叫。

---

## 5. 篩選條件（門檻集中於 `config.py`）

### 第一關（批次）
- `MIN_DAILY_LOTS = 5000`：日成交量（張）。
- `MAX_DIST_FROM_52W_HIGH = 0.25`：距 52 週高 ≤ 25%（實際在第二關用價格序列判定）。
- `MAX_DEEP_SCAN = None`：深掃檔數上限（None＝全市場）。

### 第二關 基本面（硬門檻，全過才入選）
- `REQUIRE_LAST4Q_EPS_POSITIVE = True`：最近 4 季單季 EPS 皆 > 0。
- `MIN_REVENUE_YOY = 0.15`：近一年 TTM 營收年增率 > 15%。
- `MAX_GROSS_MARGIN_DROP_PP = 3.0`：最新單季毛利率較上季衰退 ≤ 3pp。
- `MIN_ROE = 0.10`：近四季 ROE > 10%（歸屬母公司淨利 / 平均權益）。
- `MAX_DEBT_RATIO = 0.60`：負債比 < 60%。
- `NO_LOSS_YEARS = 3`：近 3 個「四季齊全」年度皆無虧損。

### 技術面（硬性排除）
- `EXCLUDE_BELOW_YEAR_LINE = True`：跌破年線（MA240）排除。
- `EXCLUDE_BEAR_ALIGNMENT = True`：長空排列（MA20<MA60<MA120）排除。
- `REQUIRE_PULLBACK_ZONE = False`：是否強制要求回檔區（預設僅加分）。
- MA 參數：`MA_SHORT=20 / MA_MID=60 / MA_LONG=120 / MA_YEAR=240`；`PRICE_HISTORY_DAYS=400`。

### 法人 / 估值（加分，非硬門檻）
- `INST_LOOKBACK_DAYS = 20`：法人買賣超回看交易日。
- `INDUSTRY_PE_METHOD = "median"`：產業 PE 比較基準（PE 須 0<pe<200）。

> 會計處理：FinMind `TaiwanStockFinancialStatements` 已是單季值，`_income_series` 直接使用；年度淨利需「四季齊全」才採計（避免半年/前三季污染年度無虧損判斷）。

---

## 6. 評分與評等（`score_and_rate`）

加分項（各 +1 或 +2）：營收 YoY（>30%:+2, >20%:+1）、ROE（>20%:+2, >15%:+1）、外資淨買超、投信淨買超、PE 低於產業、多頭排列、回檔區、有題材。
免費替代指標加分（各 +1，缺資料不加不扣）：EPS年增率>20%、營收加速（近3月YoY>近12月YoY）、估算上漲空間>20%、PEG替代值<1.5。

- `score ≥ 7 → A+`；`≥ 5 → A`；其餘 `B`（門檻維持不變）。
- 評等僅為「條件符合度」，非投資建議。

**免費替代指標公式**（`screener.py`，資料不足回 `None`）：
- `ttm_eps`＝近4季EPS合計；`eps_yoy_ttm`＝近4季合計÷前4季合計−1（需≥8季、前期>0）。
- `rev_yoy_3m/6m_avg`＝月YoY（rev[i]/rev[i-12]−1）最後3/6個平均；`rev_yoy_ttm`＝近12月÷前12月−1；`revenue_acceleration`＝近3月YoY>近12月YoY。
- `fair_price_proxy`＝`ttm_eps × 產業PE中位數`（EPS>0、0<PE中位數≤200）；`upside_proxy`＝合理價÷現價−1。
- `peg_proxy`＝`pe ÷ (eps_yoy_ttm×100)`（PE>0、`eps_yoy_ttm ≥ PEG_MIN_GROWTH=0.02`）。

**參考位階**（`reference_levels`，純技術）：建議買進區間＝季線~月線；停損＝跌破年線（無則半年線）。

---

## 7. 快取與後備（seed）機制

| 層 | 內容 | 範圍 | TTL |
|----|------|------|-----|
| **當日快取** `cache/` | `snapshot.parquet`, `universe.parquet` | 全市場批次 | `CACHE_TTL_DAYS=1` |
| **當日快取** `cache/` | `fm_<dataset>_<股號>.parquet` | **逐檔 FinMind（價格/財報/法人）** | `CACHE_TTL_DAYS=1` |
| **即時抓取** | 直接打 API | 全部 | — |
| **內建快照** `seed/` | `snapshot.parquet`, `universe.parquet` | 僅全市場批次 | 由 `make_seed.py` 手動更新 |

取用優先序（全市場）：**當日快取 → 即時抓取 → seed 後備**（`get_market_snapshot` / `get_universe`）。
逐檔：**當日快取 → 即時抓取**（成功才寫快取；限流/連線失敗會拋例外，不污染快取）。

> 同一交易日重跑（含 Streamlit 調參數）逐檔資料全走快取，近乎即時。隔交易日自動失效。

---

## 8. 輸出

- Excel：`output/逢低布局選股_YYYYMMDD.xlsx`。
  - 工作表：`前20名逢低布局`、`最佳成長股TOP5`、`最佳價值股TOP5`、`最佳AI題材股TOP5`、`最有機會創新高TOP5`、`參數與免責`。
  - 主表 24 欄（`excel_report.COLUMNS`）：基本資訊（代號/名稱/產業/股價/成交量/PE/ROE/負債比）＋免費替代指標（近四季EPS、EPS年增率、近3/6/12月營收YoY、營收是否加速、估算合理價、估算上漲空間、PEG替代值）＋法人/題材/風險/買進停損區間/評分/評等。`估算合理價、估算上漲空間、PEG替代值` 以淡黃底標記為「機械式估算」。
  - 免費替代指標公式見 §6；資料不足之欄位顯示「資料不足」（`config.NA_TEXT`）。
- Streamlit：主表 + 四子榜分頁 + 依評等/題材過濾 + 下載 Excel。
- 子榜定義：成長（_rev_yoy 降冪）、價值（_pe 升冪）、AI（命中 `AI_CORE_THEMES`）、創新高（_dist52w 升冪），各取 `TOP_SUBLIST_N=5`。

---

## 9. 效能與雲端設計（已處理）

### P1. 雲端 WAF 阻擋 — 以「快速退回 seed」緩解
TWSE/TPEX OpenAPI 對非台灣 IP 回傳 HTML 攔截頁（HTTP 200 但非 JSON）。
- `_get_json` 會偵測攔截頁特徵（`FOR SECURITY REASONS` / `安全性考量` / `CAN NOT BE ACCESSED`）→ 立即拋 `WAFBlocked`、**不重試**（過去 3 次重試含 ~6s sleep，現為 1 次即退）。
- 全市場快照隨即退回 `seed/`，介面顯示簡潔訊息與 seed 日期。
- ⚠️ 仍有限制：雲端的全市場快照（流動性清單＋顯示用 PE）為 seed 當日；**逐檔技術/財報/法人仍為即時**（FinMind 不受 WAF 限）。FinMind「整市場單日查詢」需付費等級，故無法靠它取代 seed。

### P2. 執行速度 — 逐檔快取 + 可調並行
- **逐檔當日快取**：同日重跑（含 Streamlit 調參數）近乎即時（實測 30 檔冷跑 ~17s → 熱跑 ~0.6s）。只有成功且非空才寫快取；失敗/空資料不寫、不覆蓋既有檔。
- **可調並行**：`ThreadPoolExecutor(max_workers=FINMIND_WORKERS)`，`log()` 統一在主緒輸出（Streamlit 安全）。預設保守 `FINMIND_WORKERS=1`。
- **全域節流取代逐次 sleep**：跨緒共用 lock，請求間隔 `FINMIND_MIN_INTERVAL`（預設 1.0s），速率與緒數解耦。

### P3. FinMind 限流（402/429）防護
- 共用請求函式 `_finmind_raw`：遇 402/429 依 `FINMIND_BACKOFF_SECONDS=[60,180,300]` 退避，最多重試 `FINMIND_MAX_RETRIES=3` 次；用盡才拋 `RateLimitError`（其他錯誤拋 `RuntimeError`）。
- 單股限流只略過該股、不中斷整體（`run_screen` 捕捉 `RateLimitError`）；全市場清單/快照限流則退回 seed。
- `FINMIND_STOP_ON_RATE_LIMIT`（預設 False）：True 時一遇限流即停止整個掃描並輸出已完成部分。
- **安全模式 `SAFE_MODE`（預設 True）**：套用單緒、間隔 ≥1s、`MAX_DEEP_SCAN≤100`，最不易觸發限流。CLI `--no-safe` 解除上限；Streamlit 側邊欄提供勾選。
- ⚠️ 免費 token 上限 600 次/小時：耗盡時每個失敗呼叫最多等 540s（60+180+300）才放棄；額度緊張時建議設 `FINMIND_STOP_ON_RATE_LIMIT=True` 或等約 1 小時恢復。逐檔快取可大幅降低重跑的請求量。

---

## 10. 執行方式

```bash
pip install -r requirements.txt
export FINMIND_TOKEN="..."        # 強烈建議

python run_screener.py            # 全市場
python run_screener.py --max 300  # 深掃前 300 檔（試跑）
streamlit run app.py              # 互動介面

python make_seed.py               # 本機更新雲端後備 seed/
```

依賴：`requests, pandas, openpyxl, pyarrow, streamlit`。
