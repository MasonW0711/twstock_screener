# -*- coding: utf-8 -*-
"""
資料來源層：負責所有對外抓資料的工作。

架構（全部使用免費、合法的公開資料）：
  全市場批次（一次抓全部，快）：
    - 證交所 OpenAPI：上市每日收盤、本益比/淨值比
    - 櫃買 OpenAPI  ：上櫃每日收盤、本益比
  逐檔深掃（只對通過第一關的入選股，省流量）：
    - FinMind：歷史股價、月營收、損益表、資產負債表、三大法人、融資融券

所有抓取結果會快取到 cache/，重複執行同一天不會重抓。
"""

import os
import json
import time
import threading
import datetime as dt

import requests
import urllib3
import pandas as pd

import config

# 部分官方端點（如櫃買）憑證鏈不完整，必要時會以 verify=False 重試；關閉相關警告噪音。
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TWSE = "https://openapi.twse.com.tw/v1"
TPEX = "https://www.tpex.org.tw/openapi/v1"
FINMIND = "https://api.finmindtrade.com/api/v4/data"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

_session = requests.Session()
_session.headers.update(HEADERS)


class WAFBlocked(Exception):
    """資料來源防火牆（WAF）以非台灣 IP 回傳攔截頁（HTTP 200 但非 JSON）。"""


# 雲端攔截頁的特徵字串（命中即視為被 WAF 阻擋，不再重試）。
_WAF_MARKERS = ("FOR SECURITY REASONS", "安全性考量", "CAN NOT BE ACCESSED")


# 全域請求節流：跨所有並行緒，確保「請求啟動」之間至少間隔 FINMIND_MIN_INTERVAL 秒。
# 鎖只在記錄時間戳時短暫持有；實際網路 I/O 仍可在各緒間重疊。
_rate_lock = threading.Lock()
_last_request_at = [0.0]


def _rate_limit():
    with _rate_lock:
        wait = config.FINMIND_MIN_INTERVAL - (time.monotonic() - _last_request_at[0])
        if wait > 0:
            time.sleep(wait)
        _last_request_at[0] = time.monotonic()


# ===========================================================================
# 快取工具
# ===========================================================================
def _cache_path(name: str) -> str:
    return os.path.join(config.CACHE_DIR, name)


def _is_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < config.CACHE_TTL_DAYS * 86400


def _load_cache_df(name: str):
    path = _cache_path(name)
    if _is_fresh(path):
        try:
            return pd.read_parquet(path)
        except Exception:
            try:
                return pd.read_csv(path, dtype={"stock_id": str})
            except Exception:
                return None
    return None


def _save_cache_df(df: pd.DataFrame, name: str):
    path = _cache_path(name)
    try:
        df.to_parquet(path, index=False)
    except Exception:
        df.to_csv(path.replace(".parquet", ".csv"), index=False)


# ===========================================================================
# 內建快照（seed）：當雲端主機被資料來源 WAF 阻擋、無法即時抓取時的後備資料。
# 由 `python make_seed.py` 在本機產生並 commit 進 repo（見 seed/）。
# ===========================================================================
SEED_DIR = os.path.join(config.BASE_DIR, "seed")


def _seed_path(name: str) -> str:
    return os.path.join(SEED_DIR, name)


def _load_seed_df(name: str):
    """讀取內建快照（parquet 優先，CSV 後備）；不存在則回 None。"""
    for path in (_seed_path(name), _seed_path(name).replace(".parquet", ".csv")):
        if os.path.exists(path):
            try:
                if path.endswith(".parquet"):
                    return pd.read_parquet(path)
                return pd.read_csv(path, dtype={"stock_id": str})
            except Exception:
                continue
    return None


def _seed_date(name: str) -> str:
    """內建快照檔案的最後更新日期字串（給使用者參考資料新鮮度）。"""
    for path in (_seed_path(name), _seed_path(name).replace(".parquet", ".csv")):
        if os.path.exists(path):
            ts = os.path.getmtime(path)
            return dt.date.fromtimestamp(ts).isoformat()
    return "未知"


def save_seed(log=print):
    """即時抓取全市場快照與清單，存進 seed/ 供雲端後備使用。供 make_seed.py 呼叫。"""
    os.makedirs(SEED_DIR, exist_ok=True)
    log("抓取全市場快照（證交所＋櫃買）…")
    snap = _fetch_market_snapshot()
    snap.to_parquet(_seed_path("snapshot.parquet"), index=False)
    log(f"  已存 seed/snapshot.parquet（{len(snap)} 檔）")
    log("抓取全市場清單（FinMind）…")
    uni = _fetch_universe()
    uni.to_parquet(_seed_path("universe.parquet"), index=False)
    log(f"  已存 seed/universe.parquet（{len(uni)} 檔）")


# ===========================================================================
# 證交所 / 櫃買：全市場批次
# ===========================================================================
def _get_json(url: str, retries: int = 3):
    """
    抓取證交所／櫃買 OpenAPI 並解析 JSON。

    雲端主機（如 Streamlit Cloud，IP 在台灣以外）有時會收到防火牆／WAF 的
    HTML 攔截頁而非 JSON。這裡不只看 content-type，直接嘗試解析 JSON，
    並在失敗時重試；最終失敗會帶出 HTTP 狀態碼與回應片段以利除錯。
    """
    last_err = None
    snippet = ""
    status = None
    # 櫃買（tpex.org.tw）的伺服器憑證在較新的 OpenSSL 上會因
    # 「Missing Subject Key Identifier」驗證失敗；該主機抓 SSL 錯誤時退回不驗證憑證。
    verify = True
    for attempt in range(retries):
        try:
            r = _session.get(url, timeout=40, verify=verify)
            status = r.status_code
            r.raise_for_status()
            # WAF 攔截頁（HTTP 200 但內容是封鎖說明）→ 立即放棄，重試無意義。
            text = r.text or ""
            if any(m in text for m in _WAF_MARKERS):
                raise WAFBlocked(
                    f"資料來源防火牆（WAF）以非台灣 IP 阻擋：{url}（HTTP {status}）")
            # 不完全信任 content-type：有些來源把合法 JSON 標成 text/html。
            try:
                return r.json()
            except ValueError:
                snippet = text[:200].replace("\n", " ")
                last_err = "回應非 JSON"
        except requests.exceptions.SSLError as e:
            last_err = f"SSL 憑證驗證失敗：{e}"
            if verify:
                verify = False  # 下一輪不驗證憑證重試（僅限公開資料端點）
                continue
        except requests.RequestException as e:
            last_err = str(e)
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    raise ValueError(
        f"無法取得有效 JSON：{url}（HTTP {status}）。"
        f"原因：{last_err}。回應開頭：{snippet!r}。"
        "雲端主機可能被資料來源的防火牆（WAF）以非台灣 IP 阻擋；"
        "請改於本機執行，或為來源 IP 申請白名單。")


def _to_num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


def get_market_snapshot(log=print) -> pd.DataFrame:
    """
    取得全市場（上市+上櫃）最新一日的價量與估值快照。
    欄位：stock_id, name, close, lots(成交張數), pe, pb, yield_pct, board

    優先序：當日快取 → 即時抓取 → 內建快照（seed，雲端被阻擋時的後備）。
    """
    cached = _load_cache_df("snapshot.parquet")
    if cached is not None:
        return cached
    try:
        df = _fetch_market_snapshot()
        _save_cache_df(df, "snapshot.parquet")
        return df
    except Exception as e:
        seed = _load_seed_df("snapshot.parquet")
        if seed is not None:
            log(f"⚠ 即時抓取快照失敗（{e}）；改用內建快照 seed（{_seed_date('snapshot.parquet')}）。")
            return seed
        raise


def _fetch_market_snapshot() -> pd.DataFrame:
    """實際向證交所／櫃買 OpenAPI 抓取全市場快照（不含快取／後備邏輯）。"""
    rows = []

    # --- 上市每日收盤 ---
    twse_daily = _get_json(f"{TWSE}/exchangeReport/STOCK_DAY_ALL")
    for d in twse_daily:
        code = str(d.get("Code", "")).strip()
        if len(code) != 4 or not code.isdigit() or code[0] == "0":
            continue  # 排除 ETF/權證/特別股等非四碼普通股
        vol_shares = _to_num(d.get("TradeVolume"))
        rows.append({
            "stock_id": code,
            "name": str(d.get("Name", "")).strip(),
            "close": _to_num(d.get("ClosingPrice")),
            "lots": (vol_shares / 1000) if vol_shares else None,
            "board": "上市",
        })

    # --- 上市本益比/淨值比 ---
    pe_map = {}
    for d in _get_json(f"{TWSE}/exchangeReport/BWIBBU_ALL"):
        code = str(d.get("Code", "")).strip()
        pe_map[code] = {
            "pe": _to_num(d.get("PEratio")),
            "pb": _to_num(d.get("PBratio")),
            "yield_pct": _to_num(d.get("DividendYield")),
        }

    # --- 上櫃每日收盤（資料含多日，取最新日期）---
    tpex_daily = _get_json(f"{TPEX}/tpex_mainboard_daily_close_quotes")
    tpex_df = pd.DataFrame(tpex_daily)
    if not tpex_df.empty and "Date" in tpex_df.columns:
        latest = tpex_df["Date"].max()
        tpex_df = tpex_df[tpex_df["Date"] == latest]
    for _, d in tpex_df.iterrows():
        code = str(d.get("SecuritiesCompanyCode", "")).strip()
        if len(code) != 4 or not code.isdigit() or code[0] == "0":
            continue
        vol_shares = _to_num(d.get("TradingShares"))
        rows.append({
            "stock_id": code,
            "name": str(d.get("CompanyName", "")).strip(),
            "close": _to_num(d.get("Close")),
            "lots": (vol_shares / 1000) if vol_shares else None,
            "board": "上櫃",
        })

    # --- 上櫃本益比 ---
    for d in _get_json(f"{TPEX}/tpex_mainboard_peratio_analysis"):
        code = str(d.get("SecuritiesCompanyCode", "")).strip()
        pe_map[code] = {
            "pe": _to_num(d.get("PriceEarningRatio")),
            "pb": _to_num(d.get("PriceBookRatio")),
            "yield_pct": _to_num(d.get("YieldRatio")),
        }

    df = pd.DataFrame(rows).drop_duplicates("stock_id")
    df["pe"] = df["stock_id"].map(lambda c: (pe_map.get(c) or {}).get("pe"))
    df["pb"] = df["stock_id"].map(lambda c: (pe_map.get(c) or {}).get("pb"))
    df["yield_pct"] = df["stock_id"].map(lambda c: (pe_map.get(c) or {}).get("yield_pct"))
    df = df[df["close"].notna() & (df["close"] > 0)].reset_index(drop=True)
    if df.empty:
        raise ValueError("快照解析後無有效資料（來源可能回傳空內容或攔截頁）。")
    return df


def get_universe(log=print) -> pd.DataFrame:
    """全市場股票清單（含產業別），用於題材標記。欄位：stock_id, name, industry, board

    優先序：當日快取 → 即時抓取 → 內建快照（seed）。
    """
    cached = _load_cache_df("universe.parquet")
    if cached is not None:
        return cached
    try:
        df = _fetch_universe()
        if not df.empty:
            _save_cache_df(df, "universe.parquet")
            return df
        raise ValueError("FinMind 回傳空的股票清單。")
    except Exception as e:
        seed = _load_seed_df("universe.parquet")
        if seed is not None:
            log(f"⚠ 即時抓取清單失敗（{e}）；改用內建快照 seed（{_seed_date('universe.parquet')}）。")
            return seed
        raise


def _fetch_universe() -> pd.DataFrame:
    """實際向 FinMind 抓取全市場股票清單（不含快取／後備邏輯）。"""
    data = _finmind_raw("TaiwanStockInfo")
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df = df.rename(columns={
        "industry_category": "industry",
        "stock_name": "name",
    })
    df["board"] = df["type"].map({"twse": "上市", "tpex": "上櫃"}).fillna("")
    df = df[["stock_id", "name", "industry", "board"]].drop_duplicates("stock_id")
    df = df[(df["stock_id"].str.len() == 4) & (~df["stock_id"].str.startswith("0"))]
    return df


# ===========================================================================
# FinMind：逐檔抓取（含限流退避）
# ===========================================================================
def _finmind_raw(dataset: str, data_id: str = None,
                 start_date: str = None, end_date: str = None):
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    if config.FINMIND_TOKEN:
        params["token"] = config.FINMIND_TOKEN

    wait = config.FINMIND_BACKOFF
    last_err = "未知原因"
    for attempt in range(config.FINMIND_MAX_RETRY):
        try:
            _rate_limit()  # 全域節流：跨並行緒控制請求速率（取代逐次 sleep）
            r = _session.get(FINMIND, params=params, timeout=40)
            if r.status_code == 200:
                return r.json().get("data", [])  # 乾淨 200：可能為空，但屬「真的沒資料」，可快取
            # 402 / 429：流量限制 → 退避重試
            if r.status_code in (402, 429) or "limit" in r.text.lower():
                last_err = f"限流 HTTP {r.status_code}"
                print(f"  ⚠ FinMind 限流，{wait}s 後重試（{attempt+1}/{config.FINMIND_MAX_RETRY}）")
                time.sleep(wait)
                wait *= 2
                continue
            r.raise_for_status()
        except requests.RequestException as e:
            last_err = str(e)
            print(f"  ⚠ FinMind 連線錯誤：{e}，{wait}s 後重試")
            time.sleep(wait)
            wait *= 2
    # 重試耗盡才拋例外：讓上層（逐檔迴圈 / 清單抓取）能區分「失敗」與「真的沒資料」，
    # 避免把限流造成的空結果誤存進快取而卡住一整天。
    raise RuntimeError(
        f"FinMind 抓取失敗（dataset={params.get('dataset')} "
        f"data_id={params.get('data_id', '-')}）：{last_err}")


# ---- 逐檔當日快取：相同股號＋資料集在 CACHE_TTL_DAYS 內不重抓 ----
def _finmind_cached(name: str, builder):
    """name 為快取檔名；builder 為實際抓取並回傳 DataFrame 的函式。
    builder 失敗（拋例外）時不會寫入快取，交由上層處理。"""
    cached = _load_cache_df(name)
    if cached is not None:
        return cached
    df = builder()
    _save_cache_df(df, name)
    return df


def _recent_start(days: int) -> str:
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def fetch_price_history(stock_id: str) -> pd.DataFrame:
    def build():
        data = _finmind_raw("TaiwanStockPrice", stock_id,
                            _recent_start(int(config.PRICE_HISTORY_DAYS * 1.6)))
        df = pd.DataFrame(data)
        if not df.empty:
            df = df.sort_values("date").reset_index(drop=True)
        return df
    return _finmind_cached(f"fm_price_{stock_id}.parquet", build)


def fetch_month_revenue(stock_id: str) -> pd.DataFrame:
    return _finmind_cached(f"fm_rev_{stock_id}.parquet", lambda: pd.DataFrame(
        _finmind_raw("TaiwanStockMonthRevenue", stock_id, _recent_start(900))))


def fetch_income_statement(stock_id: str) -> pd.DataFrame:
    return _finmind_cached(f"fm_income_{stock_id}.parquet", lambda: pd.DataFrame(
        _finmind_raw("TaiwanStockFinancialStatements", stock_id, _recent_start(1500))))


def fetch_balance_sheet(stock_id: str) -> pd.DataFrame:
    return _finmind_cached(f"fm_bs_{stock_id}.parquet", lambda: pd.DataFrame(
        _finmind_raw("TaiwanStockBalanceSheet", stock_id, _recent_start(1500))))


def fetch_institutional(stock_id: str) -> pd.DataFrame:
    return _finmind_cached(f"fm_inst_{stock_id}.parquet", lambda: pd.DataFrame(
        _finmind_raw("TaiwanStockInstitutionalInvestorsBuySell",
                     stock_id, _recent_start(45))))


def fetch_margin(stock_id: str) -> pd.DataFrame:
    return _finmind_cached(f"fm_margin_{stock_id}.parquet", lambda: pd.DataFrame(
        _finmind_raw("TaiwanStockMarginPurchaseShortSale", stock_id, _recent_start(20))))
