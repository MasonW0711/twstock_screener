# -*- coding: utf-8 -*-
"""
產生「內建快照（seed）」：在本機（台灣 IP 可正常連線）抓取全市場快照與股票清單，
存成 seed/snapshot.parquet 與 seed/universe.parquet，commit 進 repo。

當 app 部署在雲端（如 Streamlit Cloud）而證交所／櫃買 OpenAPI 以非台灣 IP 阻擋時，
資料層會自動改用這份 seed 作為後備，讓 app 仍能跑出結果（資料為產生 seed 當日）。

用法：
    python make_seed.py

建議定期（例如每個交易日盤後）重跑一次以更新 seed，再 commit + push。
"""

import data_sources as ds

if __name__ == "__main__":
    ds.save_seed()
    print("\n完成。請 commit 並 push seed/ 以更新雲端後備資料。")
