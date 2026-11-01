import pandas as pd
import pytest

from tw_fundamental.analysis import StockData, annual_dividends, checks, quarterly_metrics


def make_stock(quarters=8):
    idx = pd.period_range("2023Q1", periods=quarters, freq="Q")
    n = range(quarters)
    income = pd.DataFrame({
        "Revenue": [100.0 + 10 * i for i in n],
        "GrossProfit": [50.0 + 5 * i for i in n],
        "OperatingIncome": [30.0] * quarters,
        "IncomeAfterTaxes": [20.0] * quarters,
        "EquityAttributableToOwnersOfParent": [20.0] * quarters,
        "EPS": [2.0] * quarters,
    }, index=idx)
    balance = pd.DataFrame({
        "EquityAttributableToOwnersOfParent": [400.0] * quarters,
        "TotalAssets": [1000.0] * quarters,
        "Liabilities": [600.0] * quarters,
        "CurrentAssets": [300.0] * quarters,
        "CurrentLiabilities": [150.0] * quarters,
    }, index=idx)
    cashflow = pd.DataFrame({
        "CashFlowsFromOperatingActivities": [30.0] * quarters,
        "PropertyAndPlantAndEquipment": [-10.0] * quarters,
    }, index=idx)
    dividends = pd.DataFrame({
        "year": ["112年", "113年"],
        "CashEarningsDistribution": [4.0, 5.0],
        "CashStatutorySurplus": [0.0, 1.0],
        "StockEarningsDistribution": [0.0, 0.5],
        "StockStatutorySurplus": [0.0, 0.0],
    })
    return StockData("0000", "Test", "測試業", "twse", income, balance, cashflow,
                     pd.DataFrame(columns=["revenue"]), pd.DataFrame(), dividends)


def test_quarterly_metrics():
    q = quarterly_metrics(make_stock())
    last = q.iloc[-1]
    assert last["gross_margin"] == pytest.approx(85 / 170)
    assert last["revenue_yoy"] == pytest.approx(170 / 130 - 1)
    assert last["eps_ttm"] == pytest.approx(8.0)
    assert last["roe_ttm"] == pytest.approx(80 / 400)
    assert last["debt_ratio"] == pytest.approx(0.6)
    assert last["current_ratio"] == pytest.approx(2.0)
    assert last["fcf"] == pytest.approx(20.0)
    assert last["ocf_to_net_income_ttm"] == pytest.approx(1.5)
    # The first three quarters lack a full TTM window
    assert q["eps_ttm"].iloc[:3].isna().all()


def test_annual_dividends_payout():
    d = make_stock()
    divs = annual_dividends(d, quarterly_metrics(d))
    assert divs.loc[2024, "cash"] == pytest.approx(6.0)
    assert divs.loc[2024, "stock"] == pytest.approx(0.5)
    assert divs.loc[2024, "payout"] == pytest.approx(6.0 / 8.0)


def test_financial_checks_skip_leverage():
    d = make_stock()
    q = quarterly_metrics(d)
    texts = [t for _, t in checks(q, pd.DataFrame(), pd.DataFrame(), financial=True)]
    assert not any("Debt ratio" in t for t in texts)
    assert any("Debt ratio" in t for _, t in checks(q, pd.DataFrame(), pd.DataFrame()))


def test_dividend_check_counts_completed_years():
    d = make_stock()
    q = quarterly_metrics(d)
    divs = annual_dividends(d, q)
    texts = dict((t, ok) for ok, t in checks(q, pd.DataFrame(), divs))
    # 2023 and 2024 both have four quarters of EPS and a cash dividend
    assert texts["Paid cash dividends in 2 of 2 completed fiscal years"] is True


def test_roe_for_standalone_filer_without_parent_split():
    d = make_stock()
    d.income = d.income.drop(columns=["EquityAttributableToOwnersOfParent"])
    d.balance = d.balance.drop(columns=["EquityAttributableToOwnersOfParent"]).assign(Equity=400.0)
    q = quarterly_metrics(d)
    assert q["roe_ttm"].iloc[-1] == pytest.approx(80 / 400)
