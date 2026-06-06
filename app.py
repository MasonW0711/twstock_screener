# -*- coding: utf-8 -*-
"""
Streamlit 介面：互動式跑篩選、看結果、下載 Excel。

啟動：
    streamlit run app.py
"""

import io
import datetime as dt
import pandas as pd
import streamlit as st

import config
import themes
import run_screener
import excel_report

# Streamlit Cloud 不會把 secrets 自動寫進環境變數，這裡手動橋接 FinMind Token。
# 在 Streamlit Cloud 的 App settings → Secrets 設定：FINMIND_TOKEN = "你的token"
if not config.FINMIND_TOKEN:
    try:
        config.FINMIND_TOKEN = st.secrets.get("FINMIND_TOKEN", "")
    except Exception:
        pass

st.set_page_config(page_title="台股逢低布局選股器", layout="wide")
st.title("台股恐慌殺盤・逢低布局選股器")
st.caption("資料：證交所／櫃買 OpenAPI＋FinMind（皆免費公開資料）。本工具僅供研究，非投資建議。")

# ---------------- 側邊欄：參數 ----------------
with st.sidebar:
    st.header("篩選參數")
    config.MIN_DAILY_LOTS = st.number_input("日成交量門檻（張）", 0, 100000, config.MIN_DAILY_LOTS, 500)
    config.MAX_DIST_FROM_52W_HIGH = st.slider("距52週高上限", 0.0, 1.0, config.MAX_DIST_FROM_52W_HIGH, 0.05)
    config.MIN_REVENUE_YOY = st.slider("營收年增率下限", 0.0, 1.0, config.MIN_REVENUE_YOY, 0.05)
    config.MIN_ROE = st.slider("ROE 下限", 0.0, 0.5, config.MIN_ROE, 0.01)
    config.MAX_DEBT_RATIO = st.slider("負債比上限", 0.0, 1.0, config.MAX_DEBT_RATIO, 0.05)
    config.NO_LOSS_YEARS = st.number_input("近 N 年無虧損", 1, 5, config.NO_LOSS_YEARS)
    st.divider()
    max_deep = st.number_input(
        "深掃檔數上限（依流動性取前 N；0＝全市場）", 0, 2000, 300, 50,
        help="全市場（0）較慢，建議先用 300 試跑。建議先設定 FINMIND_TOKEN 環境變數以提高流量上限。")
    safe_mode = st.checkbox(
        "安全模式：降低 FinMind 限流風險", value=config.SAFE_MODE,
        help="勾選後使用保守設定（單緒、請求間隔 1 秒、深掃上限 100 檔），最不易觸發 FinMind 402 限流。")
    run = st.button("執行篩選", type="primary")

# 安全模式：套用保守的 FinMind 設定，並把深掃檔數壓到 100 檔以內。
if safe_mode:
    config.apply_safe_mode()
    if max_deep == 0 or max_deep > 100:
        max_deep = 100
else:
    config.SAFE_MODE = False

# ---------------- 執行 ----------------
if run:
    logs = st.empty()
    buf = []

    def log(msg):
        buf.append(str(msg))
        logs.code("\n".join(buf[-12:]))

    try:
        with st.spinner("篩選中…（深掃越多檔越久）"):
            df, sublists, params, passed = run_screener.run_screen(
                max_deep=(None if max_deep == 0 else max_deep), log=log)
    except Exception as e:
        st.error(f"抓取資料失敗：{e}")
        st.info("若部署在雲端（如 Streamlit Cloud），證交所／櫃買 OpenAPI 可能"
                "以非台灣 IP 阻擋雲端主機。可改於本機執行，或稍後重試。")
        st.stop()
    if df is None:
        st.warning("沒有任何個股通過全部條件，請放寬左側門檻後再試。")
        st.stop()
    st.session_state["result"] = (df, sublists, params, passed)

# ---------------- 顯示 ----------------
if "result" in st.session_state:
    df, sublists, params, passed = st.session_state["result"]
    show = excel_report.COLUMNS

    st.success(f"通過全部基本面硬門檻：{passed} 檔；最終入選並評分：{len(df)} 檔。")

    c1, c2 = st.columns([3, 1])
    with c2:
        ratings = ["全部"] + sorted(df["投資評等"].unique().tolist())
        pick = st.selectbox("依評等篩選", ratings)
        only_ai = st.checkbox("只看 AI 核心題材")
    view = df.copy()
    if pick != "全部":
        view = view[view["投資評等"] == pick]
    if only_ai:
        view = view[view["_theme"].apply(lambda t: any(k in (t or "") for k in themes.AI_CORE_THEMES))]

    st.subheader(f"主表・前 {config.TOP_N} 名")
    st.dataframe(view[show].head(config.TOP_N), use_container_width=True, hide_index=True)

    st.subheader("四大子榜")
    tabs = st.tabs(list(sublists.keys()))
    for tab, (name, sdf) in zip(tabs, sublists.items()):
        with tab:
            st.dataframe(sdf[show], use_container_width=True, hide_index=True)

    # 下載 Excel
    path = excel_report.write_report(
        df[show + [c for c in df.columns if c.startswith("_")]],
        {k: v[show] for k, v in sublists.items()}, params, passed)
    with open(path, "rb") as f:
        st.download_button("下載 Excel 報告", f.read(),
                           file_name=path.split("/")[-1],
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
else:
    st.info("請於左側設定參數後按「執行篩選」。前瞻性欄位（2026/27 EPS 預估、目標價、PEG）需付費資料源，已於報表中標記。")
