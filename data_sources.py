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
import logging
import threading
import warnings
import datetime as dt

import requests
import urllib3
import pandas as pd

import config

# 部分官方端點（如櫃買）憑證鏈不完整，必要時會以 verify=False 重試；
# fallback 僅限 TPEX，且會記錄 warning。

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


class RateLimitError(Exception):
    """FinMind 重試後仍達流量上限（HTTP 402/429）。讓上層只略過該股、不中斷整體。"""


# 資料層診斷 log：限流重試／略過以 warning 顯示（預設可見）；
# 「cache hit／fetch api」等高頻來源標記以 debug 顯示（預設安靜，需要時設 DEBUG 才看）。
_logger = logging.getLogger("finmind")

# 雲端攔截頁的特徵字串（命中即視為被 WAF 阻擋，不再重試）。
_WAF_MARKERS = ("FOR SECURITY REASONS", "安全性考量", "CAN NOT BE ACCESSED")


# 全域請求節流：跨所有並行緒，確保「請求啟動」之間至少間隔 FINMIND_MIN_INTERVAL 秒。
# 鎖只在記錄時間戳時短暫持有；實際網路 I/O 仍可在各緒間重疊。
_rate_lock = threading.Lock()
_last_request_at = [0.0]


def _rate_limit(cfg=None):
    cfg = cfg or config
    with _rate_lock:
        wait = cfg.FINMIND_MIN_INTERVAL - (time.monotonic() - _last_request_at[0])
        if wait > 0:
            time.sleep(wait)
        _last_request_at[0] = time.monotonic()


def _backoff_seconds(attempt: int, cfg=None) -> int:
    """第 attempt 次重試前的等待秒數；超出清單長度則沿用最後一個。"""
    cfg = cfg or config
    waits = cfg.FINMIND_BACKOFF_SECONDS
    return waits[attempt] if attempt < len(waits) else waits[-1]


# ===========================================================================
# 快取工具
# ===========================================================================
def _cache_path(name: str) -> str:
    return os.path.join(config.CACHE_DIR, name)


def _cache_candidates(name: str):
    path = _cache_path(name)
    if path.endswith(".parquet"):
        return [path, path.replace(".parquet", ".csv")]
    if path.endswith(".csv"):
        return [path, path.replace(".csv", ".parquet")]
    return [path]


def _is_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < config.CACHE_TTL_DAYS * 86400


def _load_cache_df(name: str):
    for path in _cache_candidates(name):
        if not _is_fresh(path):
            continue
        try:
            if path.endswith(".parquet"):
                return pd.read_parquet(path)
            return pd.read_csv(path, dtype={"stock_id": str})
        except Exception:
            continue
    return None


def _save_cache_df(df: pd.DataFrame, name: str):
    path = _cache_path(name)
    try:
        df.to_parquet(path, index=False)
    except Exception:
        df.to_csv(path.replace(".parquet", ".csv"), index=False)


def _validate_snapshot_df(df: pd.DataFrame, source: str):
    _require_columns(df, ["stock_id", "name", "close", "lots", "pe", "pb", "yield_pct"], source, allow_empty=False)


def _validate_universe_df(df: pd.DataFrame, source: str):
    _require_columns(df, ["stock_id", "name", "industry", "board"], source, allow_empty=False)


# ===========================================================================
# 內建快照（seed）：當雲端主機被資料來源 WAF 阻擋、無法即時抓取時的後備資料。
# 由 `python make_seed.py` 在本機產生並 commit 進 repo（見 seed/）。
# ===========================================================================
SEED_DIR = os.path.join(config.BASE_DIR, "seed")
SEED_METADATA = os.path.join(SEED_DIR, "metadata.json")


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


def _load_seed_metadata() -> dict:
    try:
        with open(SEED_METADATA, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _seed_date(name: str) -> str:
    """內建快照 metadata 的產生日期字串（給使用者參考資料新鮮度）。"""
    meta = _load_seed_metadata()
    if name.startswith("snapshot"):
        value = meta.get("snapshot_generated_at") or meta.get("generated_at")
    elif name.startswith("universe"):
        value = meta.get("universe_generated_at") or meta.get("generated_at")
    else:
        value = meta.get("generated_at")
    if value:
        return str(value)[:10]
    return "未知"


def _write_seed_metadata(snapshot: pd.DataFrame, universe: pd.DataFrame):
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    meta = {
        "generated_at": now,
        "snapshot_generated_at": now,
        "universe_generated_at": now,
        "snapshot_rows": int(len(snapshot)),
        "universe_rows": int(len(universe)),
    }
    with open(SEED_METADATA, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


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
    _write_seed_metadata(snap, uni)
    log("  已更新 seed/metadata.json")


# ===========================================================================
# 證交所 / 櫃買：全市場批次
# ===========================================================================
def _get_json(url: str, retries: int = 3, cfg=None):
    """
    抓取證交所／櫃買 OpenAPI 並解析 JSON。

    雲端主機（如 Streamlit Cloud，IP 在台灣以外）有時會收到防火牆／WAF 的
    HTML 攔截頁而非 JSON。這裡不只看 content-type，直接嘗試解析 JSON，
    並在失敗時重試；最終失敗會帶出 HTTP 狀態碼與回應片段以利除錯。
    """
    cfg = cfg or config
    last_err = None
    snippet = ""
    status = None
    # 櫃買（tpex.org.tw）的伺服器憑證在較新的 OpenSSL 上會因
    # 「Missing Subject Key Identifier」驗證失敗；該主機抓 SSL 錯誤時退回不驗證憑證。
    verify = True
    for attempt in range(retries):
        try:
            if verify:
                r = _session.get(url, timeout=40, verify=True)
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                    r = _session.get(url, timeout=40, verify=False)
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
            allow_tpex_fallback = (
                verify
                and cfg.ALLOW_INSECURE_TPEX_SSL_FALLBACK
                and url.startswith(TPEX)
            )
            if allow_tpex_fallback:
                _logger.warning("TPEX SSL 驗證失敗，改以不驗證憑證重試公開資料端點：%s", url)
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


def _require_columns(df: pd.DataFrame, required, source: str, allow_empty: bool = True):
    """確認資料來源欄位符合預期；欄位變更時提供明確錯誤。"""
    if df.empty and allow_empty:
        return
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{source} 回傳欄位缺失：{', '.join(missing)}")


def get_market_snapshot(log=print, cfg=None) -> pd.DataFrame:
    """
    取得全市場（上市+上櫃）最新一日的價量與估值快照。
    欄位：stock_id, name, close, lots(成交張數), pe, pb, yield_pct, board

    優先序：當日快取 → 即時抓取 → 內建快照（seed，雲端被阻擋時的後備）。
    """
    cached = _load_cache_df("snapshot.parquet")
    if cached is not None:
        _validate_snapshot_df(cached, "cache snapshot")
        return cached
    try:
        df = _fetch_market_snapshot(cfg=cfg)
        _save_cache_df(df, "snapshot.parquet")
        return df
    except Exception as e:
        seed = _load_seed_df("snapshot.parquet")
        if seed is not None:
            _validate_snapshot_df(seed, "seed snapshot")
            log(f"⚠ 即時抓取快照失敗（{e}）；改用內建快照 seed（{_seed_date('snapshot.parquet')}）。")
            return seed
        raise


def _fetch_market_snapshot(cfg=None) -> pd.DataFrame:
    """實際向證交所／櫃買 OpenAPI 抓取全市場快照（不含快取／後備邏輯）。"""
    cfg = cfg or config
    rows = []

    # --- 上市每日收盤 ---
    twse_daily = _get_json(f"{TWSE}/exchangeReport/STOCK_DAY_ALL", cfg=cfg)
    twse_daily_df = pd.DataFrame(twse_daily)
    _require_columns(twse_daily_df, ["Code", "Name", "ClosingPrice", "TradeVolume"], "TWSE STOCK_DAY_ALL",
                     allow_empty=False)
    for _, d in twse_daily_df.iterrows():
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
    twse_pe_df = pd.DataFrame(_get_json(f"{TWSE}/exchangeReport/BWIBBU_ALL", cfg=cfg))
    _require_columns(twse_pe_df, ["Code", "PEratio", "PBratio", "DividendYield"], "TWSE BWIBBU_ALL",
                     allow_empty=False)
    for _, d in twse_pe_df.iterrows():
        code = str(d.get("Code", "")).strip()
        pe_map[code] = {
            "pe": _to_num(d.get("PEratio")),
            "pb": _to_num(d.get("PBratio")),
            "yield_pct": _to_num(d.get("DividendYield")),
        }

    # --- 上櫃每日收盤（資料含多日，取最新日期）---
    tpex_daily = _get_json(f"{TPEX}/tpex_mainboard_daily_close_quotes", cfg=cfg)
    tpex_df = pd.DataFrame(tpex_daily)
    _require_columns(
        tpex_df,
        ["Date", "SecuritiesCompanyCode", "CompanyName", "Close", "TradingShares"],
        "TPEX daily close",
        allow_empty=False,
    )
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
    tpex_pe_df = pd.DataFrame(_get_json(f"{TPEX}/tpex_mainboard_peratio_analysis", cfg=cfg))
    _require_columns(
        tpex_pe_df,
        ["SecuritiesCompanyCode", "PriceEarningRatio", "PriceBookRatio", "YieldRatio"],
        "TPEX PE analysis",
        allow_empty=False,
    )
    for _, d in tpex_pe_df.iterrows():
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
    _validate_snapshot_df(df, "market snapshot")
    return df


def get_universe(log=print, cfg=None) -> pd.DataFrame:
    """全市場股票清單（含產業別），用於題材標記。欄位：stock_id, name, industry, board

    優先序：當日快取 → 即時抓取 → 內建快照（seed）。
    """
    cached = _load_cache_df("universe.parquet")
    if cached is not None:
        _validate_universe_df(cached, "cache universe")
        return cached
    try:
        df = _fetch_universe(cfg=cfg)
        if not df.empty:
            _save_cache_df(df, "universe.parquet")
            return df
        raise ValueError("FinMind 回傳空的股票清單。")
    except Exception as e:
        seed = _load_seed_df("universe.parquet")
        if seed is not None:
            _validate_universe_df(seed, "seed universe")
            log(f"⚠ 即時抓取清單失敗（{e}）；改用內建快照 seed（{_seed_date('universe.parquet')}）。")
            return seed
        raise


def _fetch_universe(cfg=None) -> pd.DataFrame:
    """實際向 FinMind 抓取全市場股票清單（不含快取／後備邏輯）。"""
    data = _finmind_raw("TaiwanStockInfo", cfg=cfg)
    df = pd.DataFrame(data)
    if df.empty:
        return df
    _require_columns(df, ["stock_id", "stock_name", "industry_category", "type"], "FinMind TaiwanStockInfo")
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
                 start_date: str = None, end_date: str = None, cfg=None):
    cfg = cfg or config
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    if cfg.FINMIND_TOKEN:
        params["token"] = cfg.FINMIND_TOKEN

    did = data_id or "-"
    last_err = "未知原因"
    rate_limited = False
    # 共 1 次首抓 + 最多 FINMIND_MAX_RETRIES 次重試。
    for attempt in range(cfg.FINMIND_MAX_RETRIES + 1):
        try:
            _rate_limit(cfg=cfg)  # 全域節流：跨並行緒控制請求速率（每次請求至少間隔 MIN_INTERVAL）
            r = _session.get(FINMIND, params=params, timeout=40)
            if r.status_code == 200:
                payload = r.json()
                if not isinstance(payload, dict) or "data" not in payload:
                    msg = str(payload.get("msg") if isinstance(payload, dict) else payload)
                    if "limit" in msg.lower() or "upper" in msg.lower():
                        rate_limited = True
                        last_err = f"限流訊息：{msg}"
                        if attempt < cfg.FINMIND_MAX_RETRIES:
                            wait = _backoff_seconds(attempt, cfg=cfg)
                            _logger.warning(
                                "FinMind 限流訊息：dataset=%s data_id=%s，等待 %d 秒後重試 %d/%d",
                                dataset, did, wait, attempt + 1, cfg.FINMIND_MAX_RETRIES)
                            time.sleep(wait)
                            continue
                        break
                    raise RuntimeError(
                        f"FinMind 回應缺少 data 欄位（dataset={dataset} data_id={did}）：{msg}")
                if attempt > 0:
                    _logger.warning("FinMind 重試成功：%s %s", did, dataset)
                return payload.get("data", [])  # 乾淨 200：可能為空（真的沒資料）
            # 402 / 429（或訊息含 limit／upper limit）→ 流量限制：退避後重試
            if r.status_code in (402, 429) or "limit" in r.text.lower():
                rate_limited = True
                last_err = f"限流 HTTP {r.status_code}"
                if attempt < cfg.FINMIND_MAX_RETRIES:
                    wait = _backoff_seconds(attempt, cfg=cfg)
                    _logger.warning(
                        "FinMind 限流 %s：dataset=%s data_id=%s，等待 %d 秒後重試 %d/%d",
                        r.status_code, dataset, did, wait, attempt + 1, cfg.FINMIND_MAX_RETRIES)
                    time.sleep(wait)
                    continue
                break  # 重試用盡
            r.raise_for_status()
        except requests.RequestException as e:
            last_err = str(e)
            if attempt < cfg.FINMIND_MAX_RETRIES:
                wait = _backoff_seconds(attempt, cfg=cfg)
                _logger.warning("FinMind 連線錯誤：%s，等待 %d 秒後重試 %d/%d",
                                e, wait, attempt + 1, cfg.FINMIND_MAX_RETRIES)
                time.sleep(wait)
                continue
            break  # 重試用盡
    # 重試用盡才拋例外（上層只略過該股、不寫快取、不中斷整體）。
    # 限流與其他錯誤用不同例外型別，讓上層能給出更清楚的訊息。
    if rate_limited:
        raise RateLimitError(
            f"FinMind 重試後仍達流量限制（dataset={dataset} data_id={did}）：{last_err}")
    raise RuntimeError(
        f"FinMind 抓取失敗（dataset={dataset} data_id={did}）：{last_err}")


# ---- 逐檔當日快取：相同股號＋資料集在 CACHE_TTL_DAYS 內不重抓 ----
def _finmind_cached(name: str, builder, required_columns=None, source: str = None):
    """name 為快取檔名；builder 為實際抓取並回傳 DataFrame 的函式。

    快取規則（避免壞資料污染）：
      - 當日快取存在 → 直接用，不打 API（cache hit）。
      - builder 失敗（限流／連線錯誤）會拋例外 → 不寫快取，交由上層略過。
      - 抓到空資料 → 不寫快取，也不覆蓋既有檔（保留先前可能可用的舊資料）。
      - 只有成功且非空才寫入快取。
    """
    cached = _load_cache_df(name)
    if cached is not None:
        if required_columns:
            _require_columns(cached, required_columns, source or name)
        _logger.debug("cache hit: %s", name)
        return cached
    _logger.debug("fetch api: %s", name)
    df = builder()
    if required_columns:
        _require_columns(df, required_columns, source or name)
    if df is not None and not df.empty:
        _save_cache_df(df, name)
    return df


def _recent_start(days: int) -> str:
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def fetch_price_history(stock_id: str, cfg=None) -> pd.DataFrame:
    cfg = cfg or config
    def build():
        data = _finmind_raw("TaiwanStockPrice", stock_id,
                            _recent_start(int(cfg.PRICE_HISTORY_DAYS * 1.6)), cfg=cfg)
        df = pd.DataFrame(data)
        if not df.empty:
            _require_columns(df, ["date", "close"], f"FinMind TaiwanStockPrice {stock_id}")
            df = df.sort_values("date").reset_index(drop=True)
        return df
    return _finmind_cached(
        f"fm_price_{stock_id}.parquet", build,
        required_columns=["date", "close"],
        source=f"FinMind TaiwanStockPrice {stock_id}",
    )


def fetch_month_revenue(stock_id: str, cfg=None) -> pd.DataFrame:
    def build():
        df = pd.DataFrame(_finmind_raw("TaiwanStockMonthRevenue", stock_id, _recent_start(900), cfg=cfg))
        _require_columns(df, ["date", "revenue"], f"FinMind TaiwanStockMonthRevenue {stock_id}")
        return df
    return _finmind_cached(
        f"fm_rev_{stock_id}.parquet", build,
        required_columns=["date", "revenue"],
        source=f"FinMind TaiwanStockMonthRevenue {stock_id}",
    )


def fetch_income_statement(stock_id: str, cfg=None) -> pd.DataFrame:
    def build():
        df = pd.DataFrame(_finmind_raw("TaiwanStockFinancialStatements", stock_id, _recent_start(1500), cfg=cfg))
        _require_columns(df, ["date", "type", "value"], f"FinMind TaiwanStockFinancialStatements {stock_id}")
        return df
    return _finmind_cached(
        f"fm_income_{stock_id}.parquet", build,
        required_columns=["date", "type", "value"],
        source=f"FinMind TaiwanStockFinancialStatements {stock_id}",
    )


def fetch_balance_sheet(stock_id: str, cfg=None) -> pd.DataFrame:
    def build():
        df = pd.DataFrame(_finmind_raw("TaiwanStockBalanceSheet", stock_id, _recent_start(1500), cfg=cfg))
        _require_columns(df, ["date", "type", "value"], f"FinMind TaiwanStockBalanceSheet {stock_id}")
        return df
    return _finmind_cached(
        f"fm_bs_{stock_id}.parquet", build,
        required_columns=["date", "type", "value"],
        source=f"FinMind TaiwanStockBalanceSheet {stock_id}",
    )


def fetch_institutional(stock_id: str, cfg=None) -> pd.DataFrame:
    def build():
        df = pd.DataFrame(_finmind_raw("TaiwanStockInstitutionalInvestorsBuySell",
                                       stock_id, _recent_start(45), cfg=cfg))
        _require_columns(df, ["date", "name", "buy", "sell"],
                         f"FinMind TaiwanStockInstitutionalInvestorsBuySell {stock_id}")
        return df
    return _finmind_cached(
        f"fm_inst_{stock_id}.parquet", build,
        required_columns=["date", "name", "buy", "sell"],
        source=f"FinMind TaiwanStockInstitutionalInvestorsBuySell {stock_id}",
    )


def fetch_margin(stock_id: str, cfg=None) -> pd.DataFrame:
    def build():
        df = pd.DataFrame(_finmind_raw("TaiwanStockMarginPurchaseShortSale", stock_id, _recent_start(20), cfg=cfg))
        _require_columns(df, ["date", "MarginPurchaseTodayBalance"],
                         f"FinMind TaiwanStockMarginPurchaseShortSale {stock_id}")
        return df
    return _finmind_cached(
        f"fm_margin_{stock_id}.parquet", build,
        required_columns=["date", "MarginPurchaseTodayBalance"],
        source=f"FinMind TaiwanStockMarginPurchaseShortSale {stock_id}",
    )
