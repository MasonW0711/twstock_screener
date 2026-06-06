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
import datetime as dt

import requests
import pandas as pd

import config

TWSE = "https://openapi.twse.com.tw/v1"
TPEX = "https://www.tpex.org.tw/openapi/v1"
FINMIND = "https://api.finmindtrade.com/api/v4/data"
HEADERS = {"User-Agent": "Mozilla/5.0"}

_session = requests.Session()
_session.headers.update(HEADERS)


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
# 證交所 / 櫃買：全市場批次
# ===========================================================================
def _get_json(url: str):
    r = _session.get(url, timeout=40)
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if "json" not in ct:
        raise ValueError(f"非 JSON 回應：{url}（端點可能已變更，請至官網確認）")
    return r.json()


def _to_num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


def get_market_snapshot() -> pd.DataFrame:
    """
    取得全市場（上市+上櫃）最新一日的價量與估值快照。
    欄位：stock_id, name, close, lots(成交張數), pe, pb, yield_pct, board
    """
    cached = _load_cache_df("snapshot.parquet")
    if cached is not None:
        return cached

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

    _save_cache_df(df, "snapshot.parquet")
    return df


def get_universe() -> pd.DataFrame:
    """全市場股票清單（含產業別），用於題材標記。欄位：stock_id, name, industry, board"""
    cached = _load_cache_df("universe.parquet")
    if cached is not None:
        return cached

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
    _save_cache_df(df, "universe.parquet")
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
    for attempt in range(config.FINMIND_MAX_RETRY):
        try:
            r = _session.get(FINMIND, params=params, timeout=40)
            if r.status_code == 200:
                time.sleep(config.FINMIND_SLEEP)
                return r.json().get("data", [])
            # 402 / 429：流量限制 → 退避重試
            if r.status_code in (402, 429) or "limit" in r.text.lower():
                print(f"  ⚠ FinMind 限流，{wait}s 後重試（{attempt+1}/{config.FINMIND_MAX_RETRY}）")
                time.sleep(wait)
                wait *= 2
                continue
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  ⚠ FinMind 連線錯誤：{e}，{wait}s 後重試")
            time.sleep(wait)
            wait *= 2
    return []


def _recent_start(days: int) -> str:
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def fetch_price_history(stock_id: str) -> pd.DataFrame:
    data = _finmind_raw("TaiwanStockPrice", stock_id,
                        _recent_start(int(config.PRICE_HISTORY_DAYS * 1.6)))
    df = pd.DataFrame(data)
    if not df.empty:
        df = df.sort_values("date").reset_index(drop=True)
    return df


def fetch_month_revenue(stock_id: str) -> pd.DataFrame:
    data = _finmind_raw("TaiwanStockMonthRevenue", stock_id, _recent_start(900))
    return pd.DataFrame(data)


def fetch_income_statement(stock_id: str) -> pd.DataFrame:
    data = _finmind_raw("TaiwanStockFinancialStatements", stock_id, _recent_start(1500))
    return pd.DataFrame(data)


def fetch_balance_sheet(stock_id: str) -> pd.DataFrame:
    data = _finmind_raw("TaiwanStockBalanceSheet", stock_id, _recent_start(1500))
    return pd.DataFrame(data)


def fetch_institutional(stock_id: str) -> pd.DataFrame:
    data = _finmind_raw("TaiwanStockInstitutionalInvestorsBuySell",
                        stock_id, _recent_start(45))
    return pd.DataFrame(data)


def fetch_margin(stock_id: str) -> pd.DataFrame:
    data = _finmind_raw("TaiwanStockMarginPurchaseShortSale",
                        stock_id, _recent_start(20))
    return pd.DataFrame(data)
