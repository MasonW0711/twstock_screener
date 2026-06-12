# -*- coding: utf-8 -*-
"""
主程式：跑完整漏斗流程並輸出 Excel。

用法：
    python run_screener.py            # 跑全市場
    python run_screener.py --max 300  # 只深掃流動性前 300 檔（試跑用，較快）

流程：
  1. 全市場快照（價量/估值，免費批次）
  2. 第一關：流動性 + 距52週高 初篩
  3. 第二關：逐檔抓 FinMind → 基本面/技術面/法人 篩選與評分
  4. 組表、排序、輸出 Excel（主表 + 四子榜 + 說明）
"""

import argparse
from collections import Counter
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

import config
import themes
import data_sources as ds
import screener as sc
import excel_report


@dataclass
class ScreenStats:
    """本次篩選的資料品質與漏斗統計。"""
    market_count: int = 0
    stage1_count: int = 0
    technical_pass_count: int = 0
    fundamentals_pass_count: int = 0
    selected_count: int = 0
    skipped: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)

    def skip(self, reason: str):
        self.skipped[reason] += 1

    def error(self, reason: str):
        self.errors[reason] += 1

    def to_dict(self) -> dict:
        return {
            "market_count": self.market_count,
            "stage1_count": self.stage1_count,
            "technical_pass_count": self.technical_pass_count,
            "fundamentals_pass_count": self.fundamentals_pass_count,
            "selected_count": self.selected_count,
            "skipped": dict(self.skipped),
            "errors": dict(self.errors),
        }


def _pct(x, cfg=None):
    """比率 → 百分比字串；None 顯示「資料不足」。"""
    cfg = cfg or config
    return f"{x:.1%}" if x is not None else cfg.NA_TEXT


def _num(x, nd=2, cfg=None):
    """數值四捨五入；None 顯示「資料不足」。"""
    cfg = cfg or config
    return round(x, nd) if x is not None else cfg.NA_TEXT


def build_row(stock_id, name, snap, fund, tech, inst, theme, pe_below, score, rating, proxies,
              margin_surge=None, cfg=None):
    cfg = cfg or config
    buy_zone, stop = sc.reference_levels(tech, cfg=cfg)
    risks = []
    if tech["below_year_line"]:
        risks.append("已近/破年線")
    if fund.get("gross_margin_drop_pp") and fund["gross_margin_drop_pp"] > 0:
        risks.append(f"毛利率季減{fund['gross_margin_drop_pp']:.1f}pp")
    if inst.get("foreign_net_lots") and inst["foreign_net_lots"] < 0:
        risks.append("外資近20日賣超")
    if snap.get("pe") and snap["pe"] > 40:
        risks.append("本益比偏高")
    if margin_surge is not None and margin_surge > cfg.MARGIN_SURGE_5D:
        risks.append(f"近5日融資餘額增{margin_surge:.1%}")
    if not risks:
        risks.append("留意大盤系統性風險")

    accel = fund.get("revenue_acceleration")
    accel_txt = "是" if accel is True else ("否" if accel is False else cfg.NA_TEXT)

    return {
        "股票代號": stock_id,
        "股票名稱": name,
        "所屬產業": snap.get("industry", ""),
        "目前股價": round(snap["close"], 2) if snap.get("close") else "",
        "成交量(張)": round(snap["lots"]) if snap.get("lots") else "",
        "本益比": round(snap["pe"], 1) if snap.get("pe") else "",
        "ROE": _pct(fund.get("roe"), cfg=cfg),
        "負債比": _pct(fund.get("debt_ratio"), cfg=cfg),
        # ---- 免費替代指標（機械式計算，非預估、非投資建議）----
        "近四季EPS": _num(fund.get("ttm_eps"), cfg=cfg),
        "EPS年增率": _pct(fund.get("eps_yoy_ttm"), cfg=cfg),
        "近3月營收YoY": _pct(fund.get("rev_yoy_3m_avg"), cfg=cfg),
        "近6月營收YoY": _pct(fund.get("rev_yoy_6m_avg"), cfg=cfg),
        "近12月營收YoY": _pct(fund.get("rev_yoy_ttm"), cfg=cfg),
        "營收是否加速": accel_txt,
        "估算合理價": _num(proxies.get("fair_price_proxy"), cfg=cfg),
        "估算上漲空間": _pct(proxies.get("upside_proxy"), cfg=cfg),
        "PEG替代值": _num(proxies.get("peg_proxy"), cfg=cfg),
        "法人近20日買賣超(張)": (
            f"外資{inst.get('foreign_net_lots', 0):+,}／投信{inst.get('trust_net_lots', 0):+,}"),
        "下半年成長題材": theme or "—",
        "主要風險": "、".join(risks),
        "建議買進區間": buy_zone,
        "停損區間": stop,
        "評分": score,
        "投資評等": rating,
        # 以下為內部排序/分榜用欄位（不輸出到主表顯示順序，但保留於 DataFrame）
        "_score": score,
        "_rev_yoy": fund.get("rev_yoy"),
        "_roe": fund.get("roe"),
        "_pe": snap.get("pe"),
        "_dist52w": tech.get("dist_from_52w_high"),
        "_theme": theme,
        "_pe_below": pe_below,
        "_upside": proxies.get("upside_proxy"),
        "_peg": proxies.get("peg_proxy"),
        "_margin_surge": margin_surge,
    }


def run_screen(max_deep=None, log=print, include_stats=False, screen_config=None):
    """執行完整篩選，回傳 (full_df, sublists, params_used, passed)。供 CLI 與 Streamlit 共用。"""
    cfg = screen_config or config.ScreenConfig.from_module()
    log("① 取得全市場快照（證交所＋櫃買）…")
    stats = ScreenStats()
    snap = ds.get_market_snapshot(log=log, cfg=cfg)
    uni = ds.get_universe(log=log, cfg=cfg)
    snap = snap.merge(uni[["stock_id", "industry"]], on="stock_id", how="left")
    stats.market_count = len(snap)
    log(f"   全市場有效個股：{len(snap)}")

    log("② 第一關：流動性 + 距52週高初篩…")
    stage1 = snap[(snap["lots"].notna()) & (snap["lots"] > cfg.MIN_DAILY_LOTS)].copy()
    stage1 = stage1.sort_values("lots", ascending=False)
    if max_deep:
        stage1 = stage1.head(max_deep)
    stats.stage1_count = len(stage1)
    log(f"   通過流動性、進入深掃：{len(stage1)} 檔")

    pe_ind = sc.industry_pe_table(snap, uni, cfg=cfg)
    total = len(stage1)
    log(f"   並行深掃（{cfg.FINMIND_WORKERS} 緒）…")

    # 逐檔處理抽成純函式，方便並行；不在工作緒呼叫 log（Streamlit 僅主緒安全），
    # 而是回傳結果與訊息，由主緒在 future 完成時統一輸出。
    # 回傳 (入選, row, 訊息, 限流旗標, 統計事件)；限流旗標讓主緒決定是否依設定停止整體掃描。
    def screen_one(s):
        sid = s["stock_id"]
        tech_passed = False
        fund_passed = False
        try:
            # 短路設計：先抓便宜的股價、過技術面，才抓較貴的財報三表與法人，盡量少打 API。
            price = ds.fetch_price_history(sid, cfg=cfg)
            tech = sc.compute_technical(price, cfg=cfg)
            if tech is None:
                return False, None, None, False, ("skip", "技術資料不足", tech_passed, fund_passed)
            if tech["dist_from_52w_high"] is None or tech["dist_from_52w_high"] > cfg.MAX_DIST_FROM_52W_HIGH:
                return False, None, None, False, ("skip", "距52週高未達門檻", tech_passed, fund_passed)
            tech_reasons = sc.technical_fail_reasons(tech, cfg=cfg)
            if tech_reasons:
                return False, None, None, False, ("skip", "、".join(tech_reasons), tech_passed, fund_passed)
            tech_passed = True
            income = ds.fetch_income_statement(sid, cfg=cfg)
            bs = ds.fetch_balance_sheet(sid, cfg=cfg)
            rev = ds.fetch_month_revenue(sid, cfg=cfg)
            fund = sc.compute_fundamentals(income, bs, rev, cfg=cfg)
            fund_reasons = sc.fundamental_fail_reasons(fund, cfg=cfg)
            if fund_reasons:
                return False, None, None, False, ("skip", "基本面：" + "、".join(fund_reasons), tech_passed, fund_passed)
            fund_passed = True
            inst = sc.compute_institutional(ds.fetch_institutional(sid, cfg=cfg), cfg=cfg)
            theme = themes.tag_themes(sid, s.get("industry", "") or "")
            pe = s.get("pe")
            pe_below = bool(pe and pe_ind.get(s.get("industry")) and pe < pe_ind[s.get("industry")])
            if cfg.REQUIRE_PE_BELOW_INDUSTRY and not pe_below:
                return False, None, None, False, ("skip", "PE未低於產業基準", tech_passed, fund_passed)
            margin_surge = None
            try:
                margin_surge = sc.compute_margin_surge(ds.fetch_margin(sid, cfg=cfg))
            except Exception:
                # 融資資料是風險提示，不應在預設模式中讓已通過股票整檔失敗。
                margin_surge = None
            if margin_surge is not None and margin_surge > cfg.MARGIN_SURGE_5D and cfg.EXCLUDE_MARGIN_SURGE:
                return False, None, None, False, ("skip", "近5日融資暴增", tech_passed, fund_passed)
            base = {"close": s.get("close"), "pe": pe,
                    "industry": s.get("industry", ""), "lots": s.get("lots")}
            proxies = sc.compute_valuation_proxies(
                fund.get("ttm_eps"), pe, pe_ind.get(s.get("industry")),
                s.get("close"), fund.get("eps_yoy_ttm"), cfg=cfg)
            metric_row = dict(base, **fund, **tech, **inst, **proxies,
                              theme=theme, pe_below_industry=pe_below)
            score, rating = sc.score_and_rate(metric_row)
            row = build_row(sid, s["name"], base, fund, tech, inst,
                            theme, pe_below, score, rating, proxies,
                            margin_surge=margin_surge, cfg=cfg)
            return True, row, f"{sid} {s['name']} ✓ 入選 評分{score} {rating}", False, ("selected", None, tech_passed, fund_passed)
        except ds.RateLimitError:
            return False, None, f"FinMind 重試後仍達流量限制，已略過 {sid}，不影響其他股票。", True, (
                "error", "FinMind限流", tech_passed, fund_passed)
        except Exception as e:
            return False, None, f"{sid} 錯誤略過：{e}", False, (
                "error", type(e).__name__, tech_passed, fund_passed)

    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=cfg.FINMIND_WORKERS) as ex:
        futures = [ex.submit(screen_one, s) for _, s in stage1.iterrows()]
        for fut in as_completed(futures):
            done += 1
            ok, row, msg, rate_limited, event = fut.result()
            if ok:
                rows.append(row)
                stats.selected_count += 1
            if event:
                kind, reason, tech_passed, fund_passed = event
                if tech_passed:
                    stats.technical_pass_count += 1
                if fund_passed:
                    stats.fundamentals_pass_count += 1
                if kind == "skip":
                    stats.skip(reason)
                elif kind == "error":
                    stats.error(reason)
            if msg:                       # 入選、限流略過或其他錯誤：逐筆顯示
                log(f"③ {done}/{total} {msg}")
            elif done % 25 == 0:          # 一般淘汰：每 25 檔回報一次進度，避免洗版
                log(f"③ {done}/{total} 已處理…")
            # 依設定：遇到流量限制即停止後續（取消尚未開始的工作）。
            if rate_limited and cfg.FINMIND_STOP_ON_RATE_LIMIT:
                log("⚠ 已達 FinMind 流量限制，依設定停止本次掃描（已完成的結果仍會輸出）。")
                for f in futures:
                    f.cancel()
                break

    if not rows:
        result = (None, None, None, stats.to_dict())
        return result if include_stats else (None, None, None, stats.selected_count)

    df = pd.DataFrame(rows).sort_values("_score", ascending=False).reset_index(drop=True)
    stats.selected_count = len(df)
    growth = df.sort_values("_rev_yoy", ascending=False).head(cfg.TOP_SUBLIST_N)
    value = df[df["_pe"].notna()].sort_values("_pe").head(cfg.TOP_SUBLIST_N)
    ai = df[df["_theme"].apply(lambda t: any(k in (t or "") for k in themes.AI_CORE_THEMES))]
    ai = ai.sort_values("_score", ascending=False).head(cfg.TOP_SUBLIST_N)
    breakout = df.sort_values("_dist52w").head(cfg.TOP_SUBLIST_N)
    sublists = {
        "最佳成長股TOP5": growth, "最佳價值股TOP5": value,
        "最佳AI題材股TOP5": ai, "最有機會創新高TOP5": breakout,
    }
    params_used = {
        "eps": "是" if cfg.REQUIRE_LAST4Q_EPS_POSITIVE else "否",
        "rev": cfg.MIN_REVENUE_YOY, "gm": cfg.MAX_GROSS_MARGIN_DROP_PP,
        "roe": cfg.MIN_ROE, "debt": cfg.MAX_DEBT_RATIO,
        "noloss": cfg.NO_LOSS_YEARS, "lots": cfg.MIN_DAILY_LOTS,
        "dist": cfg.MAX_DIST_FROM_52W_HIGH,
        "pe_below": "是" if cfg.REQUIRE_PE_BELOW_INDUSTRY else "否",
        "margin_surge": cfg.MARGIN_SURGE_5D,
        "exclude_margin_surge": "是" if cfg.EXCLUDE_MARGIN_SURGE else "否",
        "stop_buffer": cfg.STOP_BUFFER_BELOW_MID,
    }
    result = (df, sublists, params_used, stats.to_dict())
    return result if include_stats else (df, sublists, params_used, stats.selected_count)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=None,
                    help="限制深掃檔數（依流動性排序取前 N），試跑用")
    ap.add_argument("--no-safe", action="store_true",
                    help="關閉安全模式（解除深掃 100 檔上限；較易觸發 FinMind 限流）")
    args = ap.parse_args()
    # 安全模式預設於 config 匯入時套用（單緒、間隔 1s、深掃上限 100）；--no-safe 解除上限。
    cfg = config.ScreenConfig.from_module()
    if args.no_safe:
        cfg = cfg.without_safe_mode()
    max_deep = args.max if args.max is not None else cfg.MAX_DEEP_SCAN

    df, sublists, params_used, stats = run_screen(
        max_deep,
        log=lambda msg: print(msg, flush=True),
        include_stats=True,
        screen_config=cfg,
    )
    if df is None:
        print("\n沒有任何個股通過全部條件。可放寬 config.py 門檻，或先用 --max 試跑確認流程。")
        return
    show = excel_report.COLUMNS
    path = excel_report.write_report(
        df[show + [c for c in df.columns if c.startswith("_")]],
        {k: v[show] for k, v in sublists.items()}, params_used,
        stats["fundamentals_pass_count"], stats=stats, cfg=cfg)
    print(f"\n完成。入選 {len(df)} 檔，Excel 已輸出：\n{path}")


if __name__ == "__main__":
    main()
