import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import config
import data_sources
import excel_report
import run_screener
import screener


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _cfg(**overrides):
    return config.ScreenConfig.from_module().with_overrides(**overrides)


def _income_df():
    rows = []
    for date in ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]:
        rows.extend([
            {"date": date, "type": "EPS", "value": 1.0},
            {"date": date, "type": "Revenue", "value": 1000.0},
            {"date": date, "type": "GrossProfit", "value": 400.0},
            {"date": date, "type": "EquityAttributableToOwnersOfParent", "value": 100.0},
            {"date": date, "type": "IncomeAfterTaxes", "value": 100.0},
        ])
    return pd.DataFrame(rows)


def _balance_sheet_df():
    rows = []
    for date in ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]:
        rows.extend([
            {"date": date, "type": "EquityAttributableToOwnersOfParent", "value": 1000.0},
            {"date": date, "type": "Liabilities", "value": 300.0},
            {"date": date, "type": "TotalAssets", "value": 1000.0},
        ])
    return pd.DataFrame(rows)


def _revenue_df():
    dates = pd.date_range("2024-01-01", periods=24, freq="MS")
    revenue = [100.0] * 12 + [130.0] * 12
    return pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "revenue": revenue})


def _price_df():
    close = list(range(100, 160))
    return pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=len(close), freq="D").strftime("%Y-%m-%d"),
        "close": close,
        "max": close,
    })


def _inst_df():
    return pd.DataFrame([{
        "date": "2025-12-31", "name": "Foreign_Investor", "buy": 2000.0, "sell": 1000.0,
    }])


def _margin_df(values=None):
    values = values or [100.0, 100.0, 100.0, 100.0, 100.0, 105.0]
    return pd.DataFrame({
        "date": pd.date_range("2025-12-01", periods=len(values), freq="D").strftime("%Y-%m-%d"),
        "MarginPurchaseTodayBalance": values,
    })


class ScreenerCoreTests(unittest.TestCase):
    def test_fundamentals_pass_with_complete_positive_data(self):
        cfg = _cfg(NO_LOSS_YEARS=1, MIN_REVENUE_YOY=0.1, MIN_ROE=0.1, MAX_DEBT_RATIO=0.6)
        fund = screener.compute_fundamentals(_income_df(), _balance_sheet_df(), _revenue_df(), cfg=cfg)

        self.assertEqual([], screener.fundamental_fail_reasons(fund, cfg=cfg))
        self.assertTrue(screener.fundamentals_pass(fund, cfg=cfg))

    def test_technical_fail_reasons_are_explicit(self):
        cfg = _cfg(MA_SHORT=2, MA_MID=3, MA_LONG=4, MA_YEAR=5)
        price = pd.DataFrame({"close": [5.0, 4.0, 3.0, 2.0, 1.0], "max": [5.0, 4.0, 3.0, 2.0, 1.0]})
        tech = screener.compute_technical(price, cfg=cfg)

        self.assertIn("跌破年線", screener.technical_fail_reasons(tech, cfg=cfg))
        self.assertIn("長空排列", screener.technical_fail_reasons(tech, cfg=cfg))

    def test_csv_cache_fallback_is_loaded_when_parquet_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cache_dir = data_sources.config.CACHE_DIR
            data_sources.config.CACHE_DIR = tmp
            try:
                path = os.path.join(tmp, "fm_rev_1234.csv")
                pd.DataFrame({"stock_id": ["1234"], "revenue": [100]}).to_csv(path, index=False)

                loaded = data_sources._load_cache_df("fm_rev_1234.parquet")
            finally:
                data_sources.config.CACHE_DIR = old_cache_dir

        self.assertEqual(["1234"], loaded["stock_id"].tolist())
        self.assertEqual([100], loaded["revenue"].tolist())

    def test_schema_validation_reports_missing_columns(self):
        with self.assertRaisesRegex(ValueError, "測試資料 回傳欄位缺失：revenue"):
            data_sources._require_columns(
                pd.DataFrame({"date": ["2025-01-01"]}),
                ["date", "revenue"],
                "測試資料",
            )

    def test_invalid_cached_finmind_data_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cache_dir = data_sources.config.CACHE_DIR
            data_sources.config.CACHE_DIR = tmp
            try:
                path = os.path.join(tmp, "fm_rev_1234.csv")
                pd.DataFrame({"date": ["2025-01-01"]}).to_csv(path, index=False)

                with self.assertRaisesRegex(ValueError, "TaiwanStockMonthRevenue 1234 回傳欄位缺失：revenue"):
                    data_sources.fetch_month_revenue("1234")
            finally:
                data_sources.config.CACHE_DIR = old_cache_dir

    def test_run_screen_returns_stats_without_global_config_mutation(self):
        cfg = _cfg(MIN_DAILY_LOTS=10, MAX_DIST_FROM_52W_HIGH=1.0, NO_LOSS_YEARS=1)
        snapshot = pd.DataFrame([{
            "stock_id": "1234", "name": "測試股", "close": 159.0, "lots": 1000.0,
            "pe": 10.0, "pb": 1.0, "yield_pct": 2.0, "board": "上市",
        }])
        universe = pd.DataFrame([{
            "stock_id": "1234", "name": "測試股", "industry": "半導體業", "board": "上市",
        }])

        with patch.object(run_screener.ds, "get_market_snapshot", return_value=snapshot), \
             patch.object(run_screener.ds, "get_universe", return_value=universe), \
             patch.object(run_screener.ds, "fetch_price_history", return_value=_price_df()), \
             patch.object(run_screener.ds, "fetch_income_statement", return_value=_income_df()), \
             patch.object(run_screener.ds, "fetch_balance_sheet", return_value=_balance_sheet_df()), \
             patch.object(run_screener.ds, "fetch_month_revenue", return_value=_revenue_df()), \
             patch.object(run_screener.ds, "fetch_institutional", return_value=_inst_df()), \
             patch.object(run_screener.ds, "fetch_margin", return_value=_margin_df()):
            df, sublists, params, stats = run_screener.run_screen(
                max_deep=1, log=lambda _: None, include_stats=True, screen_config=cfg)

        self.assertEqual(1, stats["market_count"])
        self.assertEqual(1, stats["stage1_count"])
        self.assertEqual(1, stats["selected_count"])
        self.assertEqual("測試股", df.iloc[0]["股票名稱"])
        self.assertEqual(config.MIN_DAILY_LOTS, config.ScreenConfig.from_module().MIN_DAILY_LOTS)

    def test_finmind_200_without_data_is_not_treated_as_empty_data(self):
        cfg = _cfg(FINMIND_MIN_INTERVAL=0, FINMIND_MAX_RETRIES=0)
        response = FakeResponse(payload={"msg": "invalid dataset"})

        with patch.object(data_sources._session, "get", return_value=response):
            with self.assertRaises(RuntimeError):
                data_sources._finmind_raw("BadDataset", cfg=cfg)

    def test_require_pe_below_industry_filters_selected_stock(self):
        cfg = _cfg(
            MIN_DAILY_LOTS=10,
            MAX_DIST_FROM_52W_HIGH=1.0,
            NO_LOSS_YEARS=1,
            REQUIRE_PE_BELOW_INDUSTRY=True,
        )
        snapshot = pd.DataFrame([
            {
                "stock_id": "1234", "name": "測試股", "close": 159.0, "lots": 1000.0,
                "pe": 30.0, "pb": 1.0, "yield_pct": 2.0, "board": "上市",
            },
            {
                "stock_id": "5678", "name": "同業股", "close": 50.0, "lots": 1.0,
                "pe": 10.0, "pb": 1.0, "yield_pct": 2.0, "board": "上市",
            },
        ])
        universe = pd.DataFrame([
            {"stock_id": "1234", "name": "測試股", "industry": "半導體業", "board": "上市"},
            {"stock_id": "5678", "name": "同業股", "industry": "半導體業", "board": "上市"},
        ])

        with patch.object(run_screener.ds, "get_market_snapshot", return_value=snapshot), \
             patch.object(run_screener.ds, "get_universe", return_value=universe), \
             patch.object(run_screener.ds, "fetch_price_history", return_value=_price_df()), \
             patch.object(run_screener.ds, "fetch_income_statement", return_value=_income_df()), \
             patch.object(run_screener.ds, "fetch_balance_sheet", return_value=_balance_sheet_df()), \
             patch.object(run_screener.ds, "fetch_month_revenue", return_value=_revenue_df()), \
             patch.object(run_screener.ds, "fetch_institutional", return_value=_inst_df()):
            df, sublists, params, stats = run_screener.run_screen(
                max_deep=1, log=lambda _: None, include_stats=True, screen_config=cfg)

        self.assertIsNone(df)
        self.assertEqual(1, stats["skipped"]["PE未低於產業基準"])

    def test_margin_surge_can_exclude_stock(self):
        cfg = _cfg(
            MIN_DAILY_LOTS=10,
            MAX_DIST_FROM_52W_HIGH=1.0,
            NO_LOSS_YEARS=1,
            MARGIN_SURGE_5D=0.2,
            EXCLUDE_MARGIN_SURGE=True,
        )
        snapshot = pd.DataFrame([{
            "stock_id": "1234", "name": "測試股", "close": 159.0, "lots": 1000.0,
            "pe": 10.0, "pb": 1.0, "yield_pct": 2.0, "board": "上市",
        }])
        universe = pd.DataFrame([{
            "stock_id": "1234", "name": "測試股", "industry": "半導體業", "board": "上市",
        }])

        with patch.object(run_screener.ds, "get_market_snapshot", return_value=snapshot), \
             patch.object(run_screener.ds, "get_universe", return_value=universe), \
             patch.object(run_screener.ds, "fetch_price_history", return_value=_price_df()), \
             patch.object(run_screener.ds, "fetch_income_statement", return_value=_income_df()), \
             patch.object(run_screener.ds, "fetch_balance_sheet", return_value=_balance_sheet_df()), \
             patch.object(run_screener.ds, "fetch_month_revenue", return_value=_revenue_df()), \
             patch.object(run_screener.ds, "fetch_institutional", return_value=_inst_df()), \
             patch.object(run_screener.ds, "fetch_margin", return_value=_margin_df([100, 100, 100, 100, 100, 130])):
            df, sublists, params, stats = run_screener.run_screen(
                max_deep=1, log=lambda _: None, include_stats=True, screen_config=cfg)

        self.assertIsNone(df)
        self.assertEqual(1, stats["skipped"]["近5日融資暴增"])

    def test_reference_levels_use_stop_buffer(self):
        cfg = _cfg(STOP_BUFFER_BELOW_MID=0.1)
        _, stop = screener.reference_levels({"ma20": 110.0, "ma60": 100.0, "ma120": 90.0, "ma240": 80.0}, cfg=cfg)

        self.assertEqual("90.0（季線下方10%）", stop)

    def test_excel_report_can_be_built_in_memory(self):
        cfg = _cfg()
        df = pd.DataFrame([{col: "" for col in excel_report.COLUMNS}])
        params = {
            "eps": "是", "rev": 0.1, "gm": 3.0, "roe": 0.1, "debt": 0.6,
            "noloss": 1, "lots": 1000, "dist": 0.25,
        }

        payload = excel_report.build_report_bytes(df, {"測試榜": df}, params, 1, cfg=cfg)

        self.assertTrue(payload.startswith(b"PK"))


if __name__ == "__main__":
    unittest.main()
