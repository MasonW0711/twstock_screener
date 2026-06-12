# -*- coding: utf-8 -*-
"""
全市場逢低布局選股器 — 參數設定檔
所有可調整的門檻、路徑、API 設定集中在這裡，改這個檔就能調整篩選嚴格度。
"""

import os
from dataclasses import dataclass, replace

# ---------------------------------------------------------------------------
# 1. FinMind API Token
# ---------------------------------------------------------------------------
# 免費版（無 token）流量很低，跑全市場容易被限流。
# 強烈建議到 https://finmindtrade.com 免費註冊，於「會員中心」取得 API Token，
# 然後用環境變數設定（不要把 token 寫死在程式裡）：
#   Windows  :  setx FINMIND_TOKEN "你的token"
#   Mac/Linux:  export FINMIND_TOKEN="你的token"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

# --- FinMind 限流防護（避免短時間大量請求觸發 HTTP 402/429）---
# 免費 token 上限約 600 次/小時，且短時間爆量易被擋；預設採最保守設定。
FINMIND_WORKERS = 1                        # 逐檔深掃並行緒數（1＝序列，最不易觸發限流）
FINMIND_MIN_INTERVAL = 1.0                 # 跨所有並行緒的最小請求間隔（秒）
FINMIND_MAX_RETRIES = 3                    # 遇限流（402/429）時的最大重試次數
FINMIND_BACKOFF_SECONDS = [60, 180, 300]   # 第 1/2/3 次重試前的等待秒數（不足則沿用最後一個）
FINMIND_STOP_ON_RATE_LIMIT = False         # True＝一遇限流即停止整個掃描；False＝只略過該股、繼續

# TPEX OpenAPI 曾在部分 OpenSSL 版本遇到憑證鏈問題；必要時只針對 TPEX 端點退回不驗證。
ALLOW_INSECURE_TPEX_SSL_FALLBACK = True

# ---------------------------------------------------------------------------
# 2. 路徑
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")     # 原始資料快取（避免重複抓）
OUTPUT_DIR = os.path.join(BASE_DIR, "output")   # Excel 輸出
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 快取有效天數：超過就重新抓（盤後資料一天更新一次）
CACHE_TTL_DAYS = 1

# ---------------------------------------------------------------------------
# 3. 第一關：全市場流動性／位階初篩（用免費批次資料，快速縮小股池）
# ---------------------------------------------------------------------------
MIN_DAILY_LOTS = 5000          # 日成交量 > 5000 張
MAX_DIST_FROM_52W_HIGH = 0.25  # 股價距 52 週高點不超過 25%

# 跑全市場很慢時，可限制進入「第二關逐檔深掃」的最大檔數（依流動性排序取前 N）。
# 設 None 代表不限制（完整跑全市場）。建議第一次先設 300 試跑。
MAX_DEEP_SCAN = None

# ---------------------------------------------------------------------------
# 4. 第二關：基本面條件（硬門檻，全部通過才入選）
# ---------------------------------------------------------------------------
REQUIRE_LAST4Q_EPS_POSITIVE = True   # 最近四季（單季）EPS 皆為正
MIN_REVENUE_YOY = 0.15               # 近一年（TTM）營收年增率 > 15%
MAX_GROSS_MARGIN_DROP_PP = 3.0       # 最新單季毛利率較上一季衰退不超過 3 個百分點
MIN_ROE = 0.10                       # 近四季 ROE > 10%
MAX_DEBT_RATIO = 0.60                # 負債比 < 60%
NO_LOSS_YEARS = 3                    # 近三年皆無年度虧損

# ---------------------------------------------------------------------------
# 5. 技術面條件（恐慌殺盤後的逢低位階）
# ---------------------------------------------------------------------------
MA_SHORT = 20    # 月線
MA_MID = 60      # 季線
MA_LONG = 120    # 半年線
MA_YEAR = 240    # 年線
PRICE_HISTORY_DAYS = 400   # 抓取的歷史交易日數（需 > 240 才能算年線與 52 週高）

# 硬性排除（避免）：
EXCLUDE_BELOW_YEAR_LINE = True   # 已跌破年線者排除
EXCLUDE_BEAR_ALIGNMENT = True    # 長空排列（MA20<MA60<MA120）者排除

# 融資暴增警示：近 5 日融資餘額增幅超過此比例 → 標記警告（預設僅警告不排除）
MARGIN_SURGE_5D = 0.20
EXCLUDE_MARGIN_SURGE = False

# 逢低位階：是否要求「在月線~半年線之間的回檔區」才視為理想買點
# （False = 不當硬門檻，只在評等加分；True = 不在理想區者排除）
REQUIRE_PULLBACK_ZONE = False

# ---------------------------------------------------------------------------
# 6. 法人條件（不當硬門檻，用於評等加分與排序）
# ---------------------------------------------------------------------------
INST_LOOKBACK_DAYS = 20   # 法人買賣超回看交易日數

# ---------------------------------------------------------------------------
# 7. 估值條件
# ---------------------------------------------------------------------------
# 產業平均本益比的計算方式："median"（中位數，抗極端值，建議）或 "mean"
INDUSTRY_PE_METHOD = "median"
REQUIRE_PE_BELOW_INDUSTRY = False  # 是否硬性要求 PE 低於產業平均（False = 僅加分）

# ---------------------------------------------------------------------------
# 8. 停損／買進參考區間（純技術位階，非投資建議）
# ---------------------------------------------------------------------------
STOP_BUFFER_BELOW_MID = 0.06   # 較嚴停損 = 季線下方緩衝比例

# ---------------------------------------------------------------------------
# 9. 輸出
# ---------------------------------------------------------------------------
TOP_N = 20            # 主表列出前幾名
TOP_SUBLIST_N = 5     # 四個子榜各取前幾名

# 前瞻性欄位的統一標記文字（保留供相容；現行主表已改用免費替代指標，不再使用）
PAID_PLACEHOLDER = "需付費資料源"
# 免費替代指標在資料不足時的顯示文字
NA_TEXT = "資料不足"
# PEG 替代值的 EPS 年增率分母下限：低於此值不計算 peg_proxy（避免分母過小導致數值失真）
PEG_MIN_GROWTH = 0.02

# ---------------------------------------------------------------------------
# 10. 安全模式（降低 FinMind 限流風險）
# ---------------------------------------------------------------------------
# True 時套用最保守設定，最不易觸發 402/429（適合免費 token、雲端或第一次跑）。
# Streamlit 側邊欄亦提供勾選；CLI 可加 --no-safe 關閉。
SAFE_MODE = True


@dataclass(frozen=True)
class ScreenConfig:
    """單次篩選使用的設定快照，避免 UI 修改全域設定。"""
    FINMIND_TOKEN: str
    FINMIND_WORKERS: int
    FINMIND_MIN_INTERVAL: float
    FINMIND_MAX_RETRIES: int
    FINMIND_BACKOFF_SECONDS: list
    FINMIND_STOP_ON_RATE_LIMIT: bool
    ALLOW_INSECURE_TPEX_SSL_FALLBACK: bool
    MIN_DAILY_LOTS: int
    MAX_DIST_FROM_52W_HIGH: float
    MAX_DEEP_SCAN: int | None
    REQUIRE_LAST4Q_EPS_POSITIVE: bool
    MIN_REVENUE_YOY: float
    MAX_GROSS_MARGIN_DROP_PP: float
    MIN_ROE: float
    MAX_DEBT_RATIO: float
    NO_LOSS_YEARS: int
    MA_SHORT: int
    MA_MID: int
    MA_LONG: int
    MA_YEAR: int
    PRICE_HISTORY_DAYS: int
    EXCLUDE_BELOW_YEAR_LINE: bool
    EXCLUDE_BEAR_ALIGNMENT: bool
    MARGIN_SURGE_5D: float
    EXCLUDE_MARGIN_SURGE: bool
    REQUIRE_PULLBACK_ZONE: bool
    INST_LOOKBACK_DAYS: int
    INDUSTRY_PE_METHOD: str
    REQUIRE_PE_BELOW_INDUSTRY: bool
    STOP_BUFFER_BELOW_MID: float
    PEG_MIN_GROWTH: float
    TOP_N: int
    TOP_SUBLIST_N: int
    NA_TEXT: str
    SAFE_MODE: bool

    @classmethod
    def from_module(cls):
        return cls(
            FINMIND_TOKEN=FINMIND_TOKEN,
            FINMIND_WORKERS=FINMIND_WORKERS,
            FINMIND_MIN_INTERVAL=FINMIND_MIN_INTERVAL,
            FINMIND_MAX_RETRIES=FINMIND_MAX_RETRIES,
            FINMIND_BACKOFF_SECONDS=list(FINMIND_BACKOFF_SECONDS),
            FINMIND_STOP_ON_RATE_LIMIT=FINMIND_STOP_ON_RATE_LIMIT,
            ALLOW_INSECURE_TPEX_SSL_FALLBACK=ALLOW_INSECURE_TPEX_SSL_FALLBACK,
            MIN_DAILY_LOTS=MIN_DAILY_LOTS,
            MAX_DIST_FROM_52W_HIGH=MAX_DIST_FROM_52W_HIGH,
            MAX_DEEP_SCAN=MAX_DEEP_SCAN,
            REQUIRE_LAST4Q_EPS_POSITIVE=REQUIRE_LAST4Q_EPS_POSITIVE,
            MIN_REVENUE_YOY=MIN_REVENUE_YOY,
            MAX_GROSS_MARGIN_DROP_PP=MAX_GROSS_MARGIN_DROP_PP,
            MIN_ROE=MIN_ROE,
            MAX_DEBT_RATIO=MAX_DEBT_RATIO,
            NO_LOSS_YEARS=NO_LOSS_YEARS,
            MA_SHORT=MA_SHORT,
            MA_MID=MA_MID,
            MA_LONG=MA_LONG,
            MA_YEAR=MA_YEAR,
            PRICE_HISTORY_DAYS=PRICE_HISTORY_DAYS,
            EXCLUDE_BELOW_YEAR_LINE=EXCLUDE_BELOW_YEAR_LINE,
            EXCLUDE_BEAR_ALIGNMENT=EXCLUDE_BEAR_ALIGNMENT,
            MARGIN_SURGE_5D=MARGIN_SURGE_5D,
            EXCLUDE_MARGIN_SURGE=EXCLUDE_MARGIN_SURGE,
            REQUIRE_PULLBACK_ZONE=REQUIRE_PULLBACK_ZONE,
            INST_LOOKBACK_DAYS=INST_LOOKBACK_DAYS,
            INDUSTRY_PE_METHOD=INDUSTRY_PE_METHOD,
            REQUIRE_PE_BELOW_INDUSTRY=REQUIRE_PE_BELOW_INDUSTRY,
            STOP_BUFFER_BELOW_MID=STOP_BUFFER_BELOW_MID,
            PEG_MIN_GROWTH=PEG_MIN_GROWTH,
            TOP_N=TOP_N,
            TOP_SUBLIST_N=TOP_SUBLIST_N,
            NA_TEXT=NA_TEXT,
            SAFE_MODE=SAFE_MODE,
        )

    def with_overrides(self, **kwargs):
        return replace(self, **kwargs)

    def with_safe_mode(self):
        return replace(
            self,
            SAFE_MODE=True,
            FINMIND_WORKERS=1,
            FINMIND_MIN_INTERVAL=max(self.FINMIND_MIN_INTERVAL, 1.0),
            MAX_DEEP_SCAN=100 if not self.MAX_DEEP_SCAN else min(self.MAX_DEEP_SCAN, 100),
        )

    def without_safe_mode(self):
        return replace(self, SAFE_MODE=False, MAX_DEEP_SCAN=None)


def apply_safe_mode():
    """套用保守參數：單緒、請求間隔 ≥1 秒、深掃上限 100。可重複呼叫（idempotent）。"""
    global FINMIND_WORKERS, FINMIND_MIN_INTERVAL, MAX_DEEP_SCAN
    FINMIND_WORKERS = 1
    FINMIND_MIN_INTERVAL = max(FINMIND_MIN_INTERVAL, 1.0)
    MAX_DEEP_SCAN = 100 if not MAX_DEEP_SCAN else min(MAX_DEEP_SCAN, 100)


if SAFE_MODE:
    apply_safe_mode()
