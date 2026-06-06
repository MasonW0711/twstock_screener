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

import sys
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

import config
import themes
import data_sources as ds
import screener as sc
import excel_report


def build_row(stock_id, name, snap, fund, tech, inst, theme, pe_below, score, rating):
    buy_zone, stop = sc.reference_levels(tech)
    risks = []
    if tech["below_year_line"]:
        risks.append("已近/破年線")
    if fund.get("gross_margin_drop_pp") and fund["gross_margin_drop_pp"] > 0:
        risks.append(f"毛利率季減{fund['gross_margin_drop_pp']:.1f}pp")
    if inst.get("foreign_net_lots") and inst["foreign_net_lots"] < 0:
        risks.append("外資近20日賣超")
    if snap.get("pe") and snap["pe"] > 40:
        risks.append("本益比偏高")
    if not risks:
        risks.append("留意大盤系統性風險")

    return {
        "股票代號": stock_id,
        "股票名稱": name,
        "所屬產業": snap.get("industry", ""),
        "目前股價": round(snap["close"], 2) if snap.get("close") else "",
        "2026 EPS預估": config.PAID_PLACEHOLDER,
        "2027 EPS預估": config.PAID_PLACEHOLDER,
        "預估成長率": config.PAID_PLACEHOLDER,
        "本益比": round(snap["pe"], 1) if snap.get("pe") else "",
        "法人近20日買賣超(張)": (
            f"外資{inst.get('foreign_net_lots', 0):+,}／投信{inst.get('trust_net_lots', 0):+,}"),
        "下半年成長題材": theme or "—",
        "主要風險": "、".join(risks),
        "建議買進區間": buy_zone,
        "停損區間": stop,
        "預估合理價": config.PAID_PLACEHOLDER,
        "投資評等": rating,
        # 以下為內部排序/分榜用欄位（不輸出到主表顯示順序，但保留於 DataFrame）
        "_score": score,
        "_rev_yoy": fund.get("rev_yoy"),
        "_roe": fund.get("roe"),
        "_pe": snap.get("pe"),
        "_dist52w": tech.get("dist_from_52w_high"),
        "_theme": theme,
        "_pe_below": pe_below,
    }


def run_screen(max_deep=None, log=print):
    """執行完整篩選，回傳 (full_df, sublists, params_used, passed)。供 CLI 與 Streamlit 共用。"""
    log("① 取得全市場快照（證交所＋櫃買）…")
    snap = ds.get_market_snapshot(log=log)
    uni = ds.get_universe(log=log)
    snap = snap.merge(uni[["stock_id", "industry"]], on="stock_id", how="left")
    log(f"   全市場有效個股：{len(snap)}")

    log("② 第一關：流動性 + 距52週高初篩…")
    stage1 = snap[(snap["lots"].notna()) & (snap["lots"] > config.MIN_DAILY_LOTS)].copy()
    stage1 = stage1.sort_values("lots", ascending=False)
    if max_deep:
        stage1 = stage1.head(max_deep)
    log(f"   通過流動性、進入深掃：{len(stage1)} 檔")

    pe_ind = sc.industry_pe_table(snap, uni)
    total = len(stage1)
    log(f"   並行深掃（{config.FINMIND_WORKERS} 緒）…")

    # 逐檔處理抽成純函式，方便並行；不在工作緒呼叫 log（Streamlit 僅主緒安全），
    # 而是回傳結果與訊息，由主緒在 future 完成時統一輸出。
    # 回傳 (入選, row, 訊息, 限流旗標)；限流旗標讓主緒決定是否依設定停止整體掃描。
    def screen_one(s):
        sid = s["stock_id"]
        try:
            # 短路設計：先抓便宜的股價、過技術面，才抓較貴的財報三表與法人，盡量少打 API。
            price = ds.fetch_price_history(sid)
            tech = sc.compute_technical(price)
            if tech is None:
                return False, None, None, False
            if tech["dist_from_52w_high"] is None or tech["dist_from_52w_high"] > config.MAX_DIST_FROM_52W_HIGH:
                return False, None, None, False
            if not sc.technical_pass(tech):
                return False, None, None, False
            income = ds.fetch_income_statement(sid)
            bs = ds.fetch_balance_sheet(sid)
            rev = ds.fetch_month_revenue(sid)
            fund = sc.compute_fundamentals(income, bs, rev)
            if not sc.fundamentals_pass(fund):
                return False, None, None, False
            inst = sc.compute_institutional(ds.fetch_institutional(sid))
            theme = themes.tag_themes(sid, s.get("industry", "") or "")
            pe = s.get("pe")
            pe_below = bool(pe and pe_ind.get(s.get("industry")) and pe < pe_ind[s.get("industry")])
            base = {"close": s.get("close"), "pe": pe, "industry": s.get("industry", "")}
            metric_row = dict(base, **fund, **tech, **inst, theme=theme, pe_below_industry=pe_below)
            score, rating = sc.score_and_rate(metric_row)
            row = build_row(sid, s["name"], base, fund, tech, inst,
                            theme, pe_below, score, rating)
            return True, row, f"{sid} {s['name']} ✓ 入選 評分{score} {rating}", False
        except ds.RateLimitError:
            return False, None, f"FinMind 重試後仍達流量限制，已略過 {sid}，不影響其他股票。", True
        except Exception as e:
            return False, None, f"{sid} 錯誤略過：{e}", False

    rows, passed, done = [], 0, 0
    with ThreadPoolExecutor(max_workers=config.FINMIND_WORKERS) as ex:
        futures = [ex.submit(screen_one, s) for _, s in stage1.iterrows()]
        for fut in as_completed(futures):
            done += 1
            ok, row, msg, rate_limited = fut.result()
            if ok:
                passed += 1
                rows.append(row)
            if msg:                       # 入選、限流略過或其他錯誤：逐筆顯示
                log(f"③ {done}/{total} {msg}")
            elif done % 25 == 0:          # 一般淘汰：每 25 檔回報一次進度，避免洗版
                log(f"③ {done}/{total} 已處理…")
            # 依設定：遇到流量限制即停止後續（取消尚未開始的工作）。
            if rate_limited and config.FINMIND_STOP_ON_RATE_LIMIT:
                log("⚠ 已達 FinMind 流量限制，依設定停止本次掃描（已完成的結果仍會輸出）。")
                for f in futures:
                    f.cancel()
                break

    if not rows:
        return None, None, None, passed

    df = pd.DataFrame(rows).sort_values("_score", ascending=False).reset_index(drop=True)
    growth = df.sort_values("_rev_yoy", ascending=False).head(config.TOP_SUBLIST_N)
    value = df[df["_pe"].notna()].sort_values("_pe").head(config.TOP_SUBLIST_N)
    ai = df[df["_theme"].apply(lambda t: any(k in (t or "") for k in themes.AI_CORE_THEMES))]
    ai = ai.sort_values("_score", ascending=False).head(config.TOP_SUBLIST_N)
    breakout = df.sort_values("_dist52w").head(config.TOP_SUBLIST_N)
    sublists = {
        "最佳成長股TOP5": growth, "最佳價值股TOP5": value,
        "最佳AI題材股TOP5": ai, "最有機會創新高TOP5": breakout,
    }
    params_used = {
        "eps": "是" if config.REQUIRE_LAST4Q_EPS_POSITIVE else "否",
        "rev": config.MIN_REVENUE_YOY, "gm": config.MAX_GROSS_MARGIN_DROP_PP,
        "roe": config.MIN_ROE, "debt": config.MAX_DEBT_RATIO,
        "noloss": config.NO_LOSS_YEARS, "lots": config.MIN_DAILY_LOTS,
        "dist": config.MAX_DIST_FROM_52W_HIGH,
    }
    return df, sublists, params_used, passed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=None,
                    help="限制深掃檔數（依流動性排序取前 N），試跑用")
    ap.add_argument("--no-safe", action="store_true",
                    help="關閉安全模式（解除深掃 100 檔上限；較易觸發 FinMind 限流）")
    args = ap.parse_args()
    # 安全模式預設於 config 匯入時套用（單緒、間隔 1s、深掃上限 100）；--no-safe 解除上限。
    if args.no_safe:
        config.SAFE_MODE = False
        config.MAX_DEEP_SCAN = None
    max_deep = args.max if args.max is not None else config.MAX_DEEP_SCAN

    df, sublists, params_used, passed = run_screen(max_deep)
    if df is None:
        print("\n沒有任何個股通過全部條件。可放寬 config.py 門檻，或先用 --max 試跑確認流程。")
        return
    show = excel_report.COLUMNS
    path = excel_report.write_report(
        df[show + [c for c in df.columns if c.startswith("_")]],
        {k: v[show] for k, v in sublists.items()}, params_used, passed)
    print(f"\n完成。入選 {len(df)} 檔，Excel 已輸出：\n{path}")


if __name__ == "__main__":
    main()
