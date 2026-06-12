# -*- coding: utf-8 -*-
"""
篩選核心：把單一個股的原始資料 → 計算各項指標 → 套用五大類條件 → 評分評等。

重要會計處理：
台灣損益表是「年度累計」（Q1=Q1、Q2=上半年累計、Q3=前三季累計、Q4=全年）。
本模組會先「去累計」還原成單季數字，才能正確判斷單季 EPS 與單季毛利率。
"""

import numpy as np
import pandas as pd

import config


# ===========================================================================
# 共用：把損益表的累計值還原為單季值
# ===========================================================================
def _quarter_key(date_str: str):
    y, m, _ = date_str.split("-")
    q = {"03": 1, "06": 2, "09": 3, "12": 4}.get(m)
    return (int(y), q) if q else None


def _income_series(income_df: pd.DataFrame, type_name: str):
    """
    取出某科目的單季序列 [(year, q, value), ...] 依時間排序。
    註：FinMind 的 TaiwanStockFinancialStatements 已是「單季值」，直接使用即可。
    """
    if income_df.empty:
        return []
    sub = income_df[income_df["type"] == type_name][["date", "value"]]
    out = []
    for r in sub.to_dict("records"):
        k = _quarter_key(r["date"])
        if k and r["value"] is not None:
            out.append((k[0], k[1], float(r["value"])))
    return sorted(out, key=lambda x: (x[0], x[1]))


def _bs_latest(bs_df: pd.DataFrame, type_name: str, n_ago: int = 0):
    """取資產負債表某科目最新（或往前第 n_ago 季）的值"""
    if bs_df.empty:
        return None
    sub = bs_df[bs_df["type"] == type_name].copy()
    if sub.empty:
        return None
    sub = sub.sort_values("date")
    idx = -1 - n_ago
    try:
        return float(sub.iloc[idx]["value"])
    except Exception:
        return None


# ===========================================================================
# 基本面指標
# ===========================================================================
def compute_fundamentals(income_df, bs_df, revenue_df, cfg=None):
    cfg = cfg or config
    m = {}

    eps_q = _income_series(income_df, "EPS")
    m["last4q_eps"] = [v for (_, _, v) in eps_q[-4:]]
    m["eps_last4q_positive"] = (len(m["last4q_eps"]) == 4
                                and all(v is not None and v > 0 for v in m["last4q_eps"]))
    m["eps_ttm"] = sum(v for v in m["last4q_eps"] if v is not None) if m["last4q_eps"] else None

    # 單季毛利率（最新 vs 上一季）
    rev_q = dict(((y, q), v) for (y, q, v) in _income_series(income_df, "Revenue"))
    gp_q = dict(((y, q), v) for (y, q, v) in _income_series(income_df, "GrossProfit"))
    common = sorted(set(rev_q) & set(gp_q))
    gms = []
    for k in common:
        if rev_q[k] and gp_q[k] is not None and rev_q[k] != 0:
            gms.append((k, gp_q[k] / rev_q[k] * 100))
    m["gross_margin_latest"] = gms[-1][1] if gms else None
    m["gross_margin_prev"] = gms[-2][1] if len(gms) >= 2 else None
    if m["gross_margin_latest"] is not None and m["gross_margin_prev"] is not None:
        m["gross_margin_drop_pp"] = m["gross_margin_prev"] - m["gross_margin_latest"]
        m["gross_margin_ok"] = m["gross_margin_drop_pp"] <= cfg.MAX_GROSS_MARGIN_DROP_PP
    else:
        m["gross_margin_drop_pp"] = None
        m["gross_margin_ok"] = False

    # ROE（近四季淨利 / 平均權益），用歸屬母公司
    ni_q = _income_series(income_df, "EquityAttributableToOwnersOfParent")
    ni_ttm = sum(v for (_, _, v) in ni_q[-4:] if v is not None) if len(ni_q) >= 4 else None
    eq_now = _bs_latest(bs_df, "EquityAttributableToOwnersOfParent", 0)
    eq_4ago = _bs_latest(bs_df, "EquityAttributableToOwnersOfParent", 4)
    if ni_ttm is not None and eq_now:
        avg_eq = (eq_now + eq_4ago) / 2 if eq_4ago else eq_now
        m["roe"] = ni_ttm / avg_eq if avg_eq else None
    else:
        m["roe"] = None
    m["roe_ok"] = m["roe"] is not None and m["roe"] > cfg.MIN_ROE

    # 負債比
    liab = _bs_latest(bs_df, "Liabilities", 0)
    assets = _bs_latest(bs_df, "TotalAssets", 0)
    m["debt_ratio"] = (liab / assets) if (liab is not None and assets) else None
    m["debt_ratio_ok"] = m["debt_ratio"] is not None and m["debt_ratio"] < cfg.MAX_DEBT_RATIO

    # 近三年無虧損（年度淨利 = 該年四個單季淨利加總，需該年四季齊全）
    ni_year = {}
    for (y, q, v) in _income_series(income_df, "IncomeAfterTaxes"):
        if v is not None:
            ni_year.setdefault(y, {})[q] = v
    full = {y: sum(qs.values()) for y, qs in ni_year.items() if len(qs) == 4}
    full_years = sorted(full.keys())[-cfg.NO_LOSS_YEARS:]
    m["no_loss_years"] = (len(full_years) >= cfg.NO_LOSS_YEARS
                          and all(full[y] > 0 for y in full_years))
    m["checked_years"] = full_years

    # 近一年（TTM）營收年增率
    m["rev_yoy"] = _revenue_ttm_yoy(revenue_df)
    m["rev_yoy_ok"] = m["rev_yoy"] is not None and m["rev_yoy"] > cfg.MIN_REVENUE_YOY

    # ---- 免費替代指標（公開歷史資料機械式計算，非預估、非投資建議）----
    m.update(_eps_free_metrics(eps_q))
    m.update(_revenue_free_metrics(revenue_df, m["rev_yoy"]))

    return m


def _revenue_ttm_yoy(revenue_df: pd.DataFrame):
    if revenue_df.empty or "revenue" not in revenue_df.columns:
        return None
    df = revenue_df.sort_values("date")
    rev = df["revenue"].astype(float).tolist()
    if len(rev) < 24:
        return None
    ttm_now = sum(rev[-12:])
    ttm_prev = sum(rev[-24:-12])
    if ttm_prev == 0:
        return None
    return ttm_now / ttm_prev - 1


def _eps_free_metrics(eps_q):
    """由 EPS 單季序列 [(year,q,value),...] 算免費替代指標；資料不足回 None。"""
    vals = [v for (_, _, v) in eps_q]
    out = {"ttm_eps": None, "eps_yoy_ttm": None,
           "eps_trend_8q": None, "eps_positive_quarters": None}
    if not vals:
        return out
    if len(vals) >= 4:
        out["ttm_eps"] = sum(vals[-4:])                      # 最近四季 EPS 合計
    if len(vals) >= 8:                                       # 近四季合計 vs 前四季合計
        now, prev = sum(vals[-4:]), sum(vals[-8:-4])
        if prev > 0:                                         # 前期須為正才有意義
            out["eps_yoy_ttm"] = now / prev - 1
    recent = vals[-8:]                                       # 近八季趨勢
    if len(recent) >= 4:
        out["eps_trend_8q"] = float(np.polyfit(np.arange(len(recent)), recent, 1)[0])
        out["eps_positive_quarters"] = sum(1 for v in recent if v > 0)
    return out


def _revenue_free_metrics(revenue_df: pd.DataFrame, rev_yoy_ttm):
    """由月營收算 3/6 月 YoY 平均與是否加速；資料不足回 None。"""
    out = {"rev_yoy_ttm": rev_yoy_ttm, "rev_yoy_3m_avg": None,
           "rev_yoy_6m_avg": None, "revenue_acceleration": None}
    if revenue_df.empty or "revenue" not in revenue_df.columns:
        return out
    rev = revenue_df.sort_values("date")["revenue"].astype(float).tolist()
    # 月 YoY 序列：rev[i] / rev[i-12] - 1（同月前一年須 > 0）
    yoy = [rev[i] / rev[i - 12] - 1 for i in range(12, len(rev)) if rev[i - 12] > 0]
    if len(yoy) >= 3:
        out["rev_yoy_3m_avg"] = sum(yoy[-3:]) / 3
    if len(yoy) >= 6:
        out["rev_yoy_6m_avg"] = sum(yoy[-6:]) / 6
    if out["rev_yoy_3m_avg"] is not None and rev_yoy_ttm is not None:
        out["revenue_acceleration"] = out["rev_yoy_3m_avg"] > rev_yoy_ttm
    return out


def fundamentals_pass(m, cfg=None) -> bool:
    return not fundamental_fail_reasons(m, cfg=cfg)


def fundamental_fail_reasons(m, cfg=None) -> list:
    """回傳基本面未通過原因，供流程統計與使用者提示。"""
    cfg = cfg or config
    reasons = []
    if cfg.REQUIRE_LAST4Q_EPS_POSITIVE and not m["eps_last4q_positive"]:
        reasons.append("最近四季EPS未全為正")
    if not m["rev_yoy_ok"]:
        reasons.append("營收年增率未達門檻或資料不足")
    if not m["gross_margin_ok"]:
        reasons.append("毛利率衰退超標或資料不足")
    if not m["roe_ok"]:
        reasons.append("ROE未達門檻或資料不足")
    if not m["debt_ratio_ok"]:
        reasons.append("負債比超標或資料不足")
    if not m["no_loss_years"]:
        reasons.append("近年獲利紀錄未達門檻或資料不足")
    return reasons


# ===========================================================================
# 技術面指標
# ===========================================================================
def compute_technical(price_df: pd.DataFrame, cfg=None):
    cfg = cfg or config
    t = {}
    if price_df.empty or "close" not in price_df.columns:
        return None
    close = price_df["close"].astype(float)
    high = price_df["max"].astype(float) if "max" in price_df.columns else close
    if len(close) < cfg.MA_MID:
        return None

    def ma(n):
        return close.rolling(n).mean().iloc[-1] if len(close) >= n else None

    last = close.iloc[-1]
    t["close"] = last
    t["ma20"] = ma(cfg.MA_SHORT)
    t["ma60"] = ma(cfg.MA_MID)
    t["ma120"] = ma(cfg.MA_LONG)
    t["ma240"] = ma(cfg.MA_YEAR)

    window = min(len(high), 252)
    t["high_52w"] = high.tail(window).max()
    t["dist_from_52w_high"] = ((t["high_52w"] - last) / t["high_52w"]
                               if t["high_52w"] else None)

    a20, a60, a120 = t["ma20"], t["ma60"], t["ma120"]
    t["bull_alignment"] = bool(a20 and a60 and a120 and a20 > a60 > a120 and last > a20)
    t["bear_alignment"] = bool(a20 and a60 and a120 and a20 < a60 < a120)
    t["below_year_line"] = bool(t["ma240"] and last < t["ma240"])

    # 恐慌殺盤後的位階判讀
    pos = []
    if a20 and a60:
        if last < a20 and last > a60:
            pos.append("跌破月線未破季線")
        if abs(last - a60) / a60 <= 0.03:
            pos.append("季線附近")
    if t["ma120"] and abs(last - t["ma120"]) / t["ma120"] <= 0.03:
        pos.append("回測半年線")
    t["pullback_zone"] = bool(pos)
    t["position_note"] = "、".join(pos) if pos else "—"
    return t


def technical_pass(t, cfg=None) -> bool:
    return not technical_fail_reasons(t, cfg=cfg)


def technical_fail_reasons(t, cfg=None) -> list:
    """回傳技術面未通過原因，供流程統計與使用者提示。"""
    cfg = cfg or config
    if t is None:
        return ["技術資料不足"]
    reasons = []
    if cfg.EXCLUDE_BELOW_YEAR_LINE and t["below_year_line"]:
        reasons.append("跌破年線")
    if cfg.EXCLUDE_BEAR_ALIGNMENT and t["bear_alignment"]:
        reasons.append("長空排列")
    if cfg.REQUIRE_PULLBACK_ZONE and not t["pullback_zone"]:
        reasons.append("不在回檔區")
    return reasons


# ===========================================================================
# 法人 20 日買賣超
# ===========================================================================
def compute_institutional(inst_df: pd.DataFrame, cfg=None):
    cfg = cfg or config
    out = {"foreign_net_lots": None, "trust_net_lots": None}
    if inst_df.empty:
        return out
    df = inst_df.copy()
    dates = sorted(df["date"].unique())[-cfg.INST_LOOKBACK_DAYS:]
    df = df[df["date"].isin(dates)]
    df["net"] = df["buy"].astype(float) - df["sell"].astype(float)
    fore = df[df["name"].isin(["Foreign_Investor", "Foreign_Dealer_Self"])]["net"].sum()
    trust = df[df["name"] == "Investment_Trust"]["net"].sum()
    out["foreign_net_lots"] = round(fore / 1000)   # 股 → 張
    out["trust_net_lots"] = round(trust / 1000)
    return out


def compute_margin_surge(margin_df: pd.DataFrame):
    if margin_df.empty or "MarginPurchaseTodayBalance" not in margin_df.columns:
        return None
    bal = margin_df.sort_values("date")["MarginPurchaseTodayBalance"].astype(float).tolist()
    if len(bal) < 6 or bal[-6] == 0:
        return None
    return bal[-1] / bal[-6] - 1


# ===========================================================================
# 估值：產業平均本益比
# ===========================================================================
def industry_pe_table(snapshot: pd.DataFrame, universe: pd.DataFrame, cfg=None):
    cfg = cfg or config
    df = snapshot.copy()
    if "industry" not in df.columns:
        df = df.merge(universe[["stock_id", "industry"]], on="stock_id", how="left")
    df = df[df["pe"].notna() & (df["pe"] > 0) & (df["pe"] < 200)]
    agg = "median" if cfg.INDUSTRY_PE_METHOD == "median" else "mean"
    return df.groupby("industry")["pe"].agg(agg).to_dict()


def compute_valuation_proxies(ttm_eps, pe, industry_pe_median, current_price, eps_yoy_ttm, cfg=None):
    """免費估值替代指標（機械式計算，非券商目標價／法人預估，非投資建議）。

    回 dict：
      fair_price_proxy = ttm_eps × 產業PE中位數
      upside_proxy     = fair_price_proxy / 現價 - 1
      peg_proxy        = pe / (eps_yoy_ttm × 100)
    任一輸入不合格則對應值為 None。
    """
    cfg = cfg or config
    out = {"fair_price_proxy": None, "upside_proxy": None, "peg_proxy": None}

    # 估算合理價：需 ttm_eps>0 且產業PE中位數有效（0<pe<=200）
    if (ttm_eps is not None and ttm_eps > 0 and industry_pe_median is not None
            and 0 < industry_pe_median <= 200):
        out["fair_price_proxy"] = ttm_eps * industry_pe_median
        if current_price and current_price > 0:
            out["upside_proxy"] = out["fair_price_proxy"] / current_price - 1

    # PEG 替代值：需 pe>0、eps 年增率>0 且不過小（避免分母過小失真）
    if (pe is not None and pe > 0 and eps_yoy_ttm is not None
            and eps_yoy_ttm >= cfg.PEG_MIN_GROWTH):
        out["peg_proxy"] = pe / (eps_yoy_ttm * 100)

    return out


# ===========================================================================
# 評分 → 評等（純條件符合度，非投資建議）
# ===========================================================================
def score_and_rate(row) -> tuple:
    score = 0
    if row.get("rev_yoy") and row["rev_yoy"] > 0.30:
        score += 2
    elif row.get("rev_yoy") and row["rev_yoy"] > 0.20:
        score += 1
    if row.get("roe") and row["roe"] > 0.20:
        score += 2
    elif row.get("roe") and row["roe"] > 0.15:
        score += 1
    if row.get("foreign_net_lots") and row["foreign_net_lots"] > 0:
        score += 1
    if row.get("trust_net_lots") and row["trust_net_lots"] > 0:
        score += 1
    if row.get("pe_below_industry"):
        score += 1
    if row.get("bull_alignment"):
        score += 1
    if row.get("pullback_zone"):
        score += 1
    if row.get("theme"):
        score += 1

    # 免費替代指標加分（缺資料不加、不扣；異常值因條件不成立而自動不計）
    if row.get("eps_yoy_ttm") and row["eps_yoy_ttm"] > 0.20:
        score += 1
    if row.get("revenue_acceleration") is True:
        score += 1
    if row.get("upside_proxy") and row["upside_proxy"] > 0.20:
        score += 1
    if row.get("peg_proxy") and row["peg_proxy"] < 1.5:
        score += 1

    if score >= 7:
        rating = "A+"
    elif score >= 5:
        rating = "A"
    else:
        rating = "B"
    return score, rating


def reference_levels(t, cfg=None):
    """純技術參考位階（非投資建議）：買進參考區間、停損參考、回檔支撐。"""
    cfg = cfg or config
    a20, a60, a120, a240 = t["ma20"], t["ma60"], t["ma120"], t["ma240"]
    if a20 and a60:
        lo, hi = sorted([a20, a60])
        buy_zone = f"{lo:.1f}–{hi:.1f}（季線~月線）"
    else:
        buy_zone = "—"
    if a60:
        stop_level = a60 * (1 - cfg.STOP_BUFFER_BELOW_MID)
        stop = f"{stop_level:.1f}（季線下方{cfg.STOP_BUFFER_BELOW_MID:.0%}）"
    elif a240:
        stop = f"{a240:.1f}（跌破年線）"
    elif a120:
        stop = f"{a120:.1f}（跌破半年線）"
    else:
        stop = "—"
    return buy_zone, stop
