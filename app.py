# -*- coding: utf-8 -*-
"""
Streamlit 介面：互動式跑篩選、看結果、下載 Excel。

啟動：
    streamlit run app.py
"""

import streamlit as st

import config
import themes
import run_screener
import excel_report

# Streamlit Cloud 不會把 secrets 自動寫進環境變數，這裡手動橋接 FinMind Token。
# 在 Streamlit Cloud 的 App settings → Secrets 設定：FINMIND_TOKEN = "你的token"
base_config = config.ScreenConfig.from_module()
if not base_config.FINMIND_TOKEN:
    try:
        base_config = base_config.with_overrides(FINMIND_TOKEN=st.secrets.get("FINMIND_TOKEN", ""))
    except Exception:
        pass

st.set_page_config(page_title="台股逢低布局選股器", layout="wide")
st.title("台股恐慌殺盤・逢低布局選股器")
st.caption("資料：證交所／櫃買 OpenAPI＋FinMind（皆免費公開資料）。本工具僅供研究，非投資建議。")

# ---------------- 側邊欄：參數 ----------------
with st.sidebar:
    st.header("篩選參數")
    min_daily_lots = st.number_input("日成交量門檻（張）", 0, 100000, base_config.MIN_DAILY_LOTS, 500)
    max_dist_from_52w_high = st.slider("距52週高上限", 0.0, 1.0, base_config.MAX_DIST_FROM_52W_HIGH, 0.05)
    min_revenue_yoy = st.slider("營收年增率下限", 0.0, 1.0, base_config.MIN_REVENUE_YOY, 0.05)
    min_roe = st.slider("ROE 下限", 0.0, 0.5, base_config.MIN_ROE, 0.01)
    max_debt_ratio = st.slider("負債比上限", 0.0, 1.0, base_config.MAX_DEBT_RATIO, 0.05)
    no_loss_years = st.number_input("近 N 年無虧損", 1, 5, base_config.NO_LOSS_YEARS)
    st.divider()
    st.header("進階風險設定")
    require_pe_below_industry = st.checkbox(
        "要求 PE 低於產業基準",
        value=base_config.REQUIRE_PE_BELOW_INDUSTRY,
        help="勾選後，PE 未低於同產業中位數者會被排除。")
    margin_surge_5d = st.slider(
        "融資暴增警示門檻（近5日）",
        0.0, 1.0, base_config.MARGIN_SURGE_5D, 0.05,
        help="超過此增幅會列入主要風險；若下方勾選排除，會直接淘汰。")
    exclude_margin_surge = st.checkbox(
        "排除融資暴增",
        value=base_config.EXCLUDE_MARGIN_SURGE)
    stop_buffer_below_mid = st.slider(
        "停損緩衝（季線下方）",
        0.0, 0.2, base_config.STOP_BUFFER_BELOW_MID, 0.01)
    st.divider()
    max_deep = st.number_input(
        "深掃檔數上限（依流動性取前 N；0＝全市場）", 0, 2000, 300, 50,
        help="全市場（0）較慢，建議先用 300 試跑。建議先設定 FINMIND_TOKEN 環境變數以提高流量上限。")
    safe_mode = st.checkbox(
        "安全模式：降低 FinMind 限流風險", value=base_config.SAFE_MODE,
        help="勾選後使用保守設定（單緒、請求間隔 1 秒、深掃上限 100 檔），最不易觸發 FinMind 402 限流。")
    run = st.button("執行篩選", type="primary")

runtime_config = base_config.with_overrides(
    MIN_DAILY_LOTS=int(min_daily_lots),
    MAX_DIST_FROM_52W_HIGH=max_dist_from_52w_high,
    MIN_REVENUE_YOY=min_revenue_yoy,
    MIN_ROE=min_roe,
    MAX_DEBT_RATIO=max_debt_ratio,
    NO_LOSS_YEARS=int(no_loss_years),
    REQUIRE_PE_BELOW_INDUSTRY=require_pe_below_industry,
    MARGIN_SURGE_5D=margin_surge_5d,
    EXCLUDE_MARGIN_SURGE=exclude_margin_surge,
    STOP_BUFFER_BELOW_MID=stop_buffer_below_mid,
    SAFE_MODE=safe_mode,
)

# 安全模式：套用保守的 FinMind 設定，並把深掃檔數壓到 100 檔以內。
if safe_mode:
    runtime_config = runtime_config.with_safe_mode()
    if max_deep == 0 or max_deep > 100:
        max_deep = 100
else:
    runtime_config = runtime_config.without_safe_mode()

# ---------------- 執行 ----------------
if run:
    logs = st.empty()
    buf = []

    def log(msg):
        buf.append(str(msg))
        logs.code("\n".join(buf[-12:]))

    try:
        with st.spinner("篩選中…（深掃越多檔越久）"):
            df, sublists, params, stats = run_screener.run_screen(
                max_deep=(None if max_deep == 0 else max_deep),
                log=log,
                include_stats=True,
                screen_config=runtime_config)
    except Exception as e:
        st.error(f"抓取資料失敗：{e}")
        st.info("若部署在雲端（如 Streamlit Cloud），證交所／櫃買 OpenAPI 可能"
                "以非台灣 IP 阻擋雲端主機。可改於本機執行，或稍後重試。")
        st.stop()
    if df is None:
        st.warning("沒有任何個股通過全部條件，請放寬左側門檻後再試。")
        if stats:
            st.caption(
                f"本次進入深掃 {stats.get('stage1_count', 0)} 檔；"
                f"錯誤 {sum(stats.get('errors', {}).values())} 檔、"
                f"略過 {sum(stats.get('skipped', {}).values())} 檔。")
        st.stop()
    st.session_state["result"] = (df, sublists, params, stats, runtime_config)

# ---------------- 顯示 ----------------
if "result" in st.session_state:
    df, sublists, params, stats, result_config = st.session_state["result"]
    show = excel_report.COLUMNS

    fundamentals_passed = stats.get("fundamentals_pass_count", len(df))
    st.success(f"通過全部基本面硬門檻：{fundamentals_passed} 檔；最終入選並評分：{len(df)} 檔。")
    with st.expander("本次資料品質摘要"):
        st.write({
            "全市場有效個股": stats.get("market_count"),
            "進入逐檔深掃": stats.get("stage1_count"),
            "通過技術面與位階篩選": stats.get("technical_pass_count"),
            "通過基本面硬門檻": fundamentals_passed,
            "最終入選": stats.get("selected_count", len(df)),
            "略過原因": stats.get("skipped", {}),
            "錯誤原因": stats.get("errors", {}),
        })

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

    st.subheader(f"主表・前 {result_config.TOP_N} 名")
    st.dataframe(view[show].head(result_config.TOP_N), use_container_width=True, hide_index=True)
    st.caption("估算合理價、估算上漲空間、PEG替代值，皆為公開資料機械式計算結果，"
               "不是券商共識目標價，也不是投資建議。")

    st.subheader("四大子榜")
    tabs = st.tabs(list(sublists.keys()))
    for tab, (name, sdf) in zip(tabs, sublists.items()):
        with tab:
            st.dataframe(sdf[show], use_container_width=True, hide_index=True)

    # 下載 Excel（Streamlit 直接用記憶體內容，避免每次 rerun 都寫 output/ 檔案）
    report_bytes = excel_report.build_report_bytes(
        df[show + [c for c in df.columns if c.startswith("_")]],
        {k: v[show] for k, v in sublists.items()}, params, fundamentals_passed,
        stats=stats, cfg=result_config)
    st.download_button("下載 Excel 報告", report_bytes,
                       file_name="逢低布局選股.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
else:
    st.info("請於左側設定參數後按「執行篩選」。EPS／營收成長、估算合理價、PEG替代值等欄位，"
            "皆由免費公開資料（FinMind 財報與月營收）機械式計算，非券商預估、非投資建議。")
