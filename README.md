# 台股恐慌殺盤・逢低布局選股器

依五大類條件（基本面、下半年成長題材、法人、技術面、估值）對全市場上市櫃股票篩選，
輸出 Excel 多工作表（主表＋四大子榜），並附互動式 Streamlit 介面。

---

## 一、資料來源（全部免費合法公開資料）

| 用途 | 來源 |
|------|------|
| 全市場每日價量、本益比/淨值比（一次抓全部） | 證交所 OpenAPI、櫃買中心 OpenAPI |
| 逐檔 EPS／毛利率／ROE／負債比／營收年增／三大法人 | FinMind 開放資料 |

**前瞻性資料（免費源取不到，報表中以淡黃底標記「需付費資料源」）：**
2026／2027 EPS 預估、預估成長率、預估合理價、法人共識目標價、PEG。
這些需要 TEJ／CMoney 等付費資料庫或券商研究報告才能補入。

---

## 二、安裝步驟（第一次使用）

1. 安裝 Python 3.10 以上。
2. 在本資料夾開啟終端機（VS Code 可用「終端機 → 新增終端機」），安裝套件：

   ```bash
   pip install -r requirements.txt
   ```

3. **（強烈建議）申請 FinMind 免費 Token 以提高流量上限**
   - 到 https://finmindtrade.com 免費註冊，於會員中心取得 API Token。
   - 設定環境變數（擇一）：
     - Windows：`setx FINMIND_TOKEN "你的token"`（設定後需重開終端機）
     - Mac／Linux：`export FINMIND_TOKEN="你的token"`
   - 不設定也能跑，但免費無 token 流量很低，跑全市場會很慢且容易被限流。

---

## 三、執行方式

### 方式 A：命令列，直接產出 Excel

```bash
python run_screener.py            # 跑全市場（較慢，建議設好 token）
python run_screener.py --max 300  # 只深掃流動性前 300 檔（第一次試跑建議用這個）
```

完成後 Excel 會輸出到 `output/逢低布局選股_日期.xlsx`，含工作表：

- 前20名逢低布局（15 欄主表）
- 最佳成長股 TOP5
- 最佳價值股 TOP5
- 最佳 AI 題材股 TOP5
- 最有機會創新高 TOP5
- 參數與免責

### 方式 B：Streamlit 互動介面

```bash
streamlit run app.py
```

瀏覽器會開啟介面，可在左側即時調整門檻、執行篩選、依評等／題材過濾、並下載 Excel。

### 方式 C：部署到 Streamlit Community Cloud（免費，公開網址）

1. 將本專案推到 GitHub（已是 git repo 可直接用）。
2. 到 https://share.streamlit.io 用 GitHub 帳號登入，按 **New app**。
3. 選擇此 repo、分支 `main`、主程式填 `app.py`，按 **Deploy**。
4. （建議）在 **App settings → Secrets** 貼上 FinMind token 以提高流量上限：

   ```toml
   FINMIND_TOKEN = "你的token"
   ```

   程式會自動讀取此 secret，不需改任何程式碼。

#### 內建快照（seed）後備機制

證交所／櫃買 OpenAPI 可能以「非台灣 IP」阻擋雲端主機（回傳攔截頁而非 JSON）。
為此，全市場快照與股票清單支援**後備資料**：

- 抓取優先序：**當日快取 → 即時抓取 → 內建快照 `seed/`**。
- 當雲端即時抓取失敗時，自動改用 repo 內的 `seed/snapshot.parquet`、`seed/universe.parquet`，
  介面會顯示「改用內建快照」與其資料日期，app 仍能跑出結果（資料為 seed 產生當日）。

更新 seed（建議於本機、台灣 IP 環境，定期盤後執行）：

```bash
python make_seed.py     # 重新抓取並寫入 seed/
git add seed/ && git commit -m "Update seed" && git push
```

> 註：逐檔基本面／法人資料來自 FinMind（`api.finmindtrade.com`），與證交所不同主機，
> 雲端通常可正常連線；上述後備僅針對證交所／櫃買的全市場批次資料。

---

## 四、篩選邏輯重點

- **基本面（硬門檻，全過才入選）**：最近四季單季 EPS 皆為正、近一年（TTM）營收年增率 > 15%、
  最新單季毛利率衰退 ≤ 3pp、ROE > 10%、負債比 < 60%、近三年無年度虧損。
- **技術面**：計算月／季／半年／年線與多頭排列、距 52 週高；排除跌破年線與長空排列；
  標記「跌破月線未破季線／季線附近／回測半年線」等逢低位階。
- **法人**：外資、投信近 20 日淨買賣超（張），用於評等加分與排序。
- **估值**：本益比與同產業中位數比較。
- **評等**：A+／A／B 為各客觀條件的「符合度評分」；「建議買進區間／停損區間」為
  移動平均線推導之**技術參考位階**——皆為機械式計算，非投資建議。

> 注意：負債比 < 60% 會系統性排除多數 AI 伺服器代工（EMS，如緯創、廣達、鴻海），
> 因其應付帳款龐大、負債比結構性偏高。若要納入這類個股，可於 `config.py` 放寬
> `MAX_DEBT_RATIO`，或改採「不含應付帳款的有息負債比」（需另接資料）。

---

## 五、調整門檻

所有門檻集中在 `config.py`，例如：

```python
MIN_DAILY_LOTS = 5000          # 日成交量 > 5000 張
MAX_DIST_FROM_52W_HIGH = 0.25  # 距 52 週高不超過 25%
MIN_REVENUE_YOY = 0.15         # 營收年增 > 15%
MIN_ROE = 0.10                 # ROE > 10%
MAX_DEBT_RATIO = 0.60          # 負債比 < 60%
MAX_DEEP_SCAN = None           # 深掃檔數上限（None＝全市場；試跑可設 300）
```

題材與代表股清單在 `themes.py`，可自由增刪。

---

## 六、檔案結構

```
twstock_screener/
├─ config.py          參數設定（門檻、token、路徑）
├─ themes.py          題材 → 代表股對照
├─ data_sources.py    證交所／櫃買／FinMind 資料抓取（含快取、限流退避）
├─ screener.py        篩選與指標計算核心
├─ excel_report.py    Excel 多工作表輸出
├─ run_screener.py    命令列主程式
├─ app.py             Streamlit 介面
├─ make_seed.py       產生雲端後備快照（seed/）
├─ requirements.txt
├─ seed/              內建快照後備（即時抓取被阻擋時使用）
├─ cache/             原始資料快取（同一天不重抓）
└─ output/            Excel 輸出
```

---

## 七、免責聲明

本工具僅供研究與資料整理之用，不構成任何投資建議或要約，提供者非投資顧問。
資料可能存在誤差或延遲，所有篩選結果與參考位階皆為機械式計算，
實際投資決策與風險應由使用者自行判斷與承擔，交易請以官方公告與券商資訊為準。
