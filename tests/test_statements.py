import math

import pandas as pd
import pytest

from tw_fundamental.statements import decumulate_ytd, first_col, pivot_statement, roc_fiscal_year


def long_rows(rows):
    return pd.DataFrame(rows, columns=["date", "type", "value"])


def test_pivot_drops_percent_rows_and_fills_missing_quarters():
    df = long_rows([
        ("2024-03-31", "Revenue", 100.0),
        ("2024-03-31", "Revenue_per", 50.0),
        ("2024-09-30", "Revenue", 120.0),  # 2024Q2 missing
    ])
    wide = pivot_statement(df)
    assert list(wide.columns) == ["Revenue"]
    assert [str(p) for p in wide.index] == ["2024Q1", "2024Q2", "2024Q3"]
    assert math.isnan(wide.loc[pd.Period("2024Q2"), "Revenue"])


def test_decumulate_ytd_cash_flow():
    df = long_rows([
        ("2023-12-31", "OCF", 400.0),
        ("2024-03-31", "OCF", 100.0),
        ("2024-06-30", "OCF", 250.0),
        ("2024-09-30", "OCF", 300.0),
        ("2024-12-31", "OCF", 500.0),
    ])
    q = decumulate_ytd(pivot_statement(df))["OCF"]
    # 2023Q4 has no 2023Q3 to subtract, so it must not masquerade as a single quarter
    assert math.isnan(q[pd.Period("2023Q4")])
    assert q[pd.Period("2024Q1")] == 100.0  # Q1 is not reduced by the prior year's Q4
    assert q[pd.Period("2024Q2")] == 150.0
    assert q[pd.Period("2024Q3")] == 50.0
    assert q[pd.Period("2024Q4")] == 200.0


def test_decumulate_gap_yields_nan():
    df = long_rows([("2024-03-31", "OCF", 100.0), ("2024-09-30", "OCF", 300.0)])
    q = decumulate_ytd(pivot_statement(df))["OCF"]
    assert math.isnan(q[pd.Period("2024Q3")])


def test_first_col_fallback():
    df = pd.DataFrame({"IncomeAfterTax": [1.0]})
    assert first_col(df, "IncomeAfterTaxes", "IncomeAfterTax").tolist() == [1.0]
    assert math.isnan(first_col(df, "Missing", "AlsoMissing").iloc[0])


@pytest.mark.parametrize("label,year", [("113年第3季", 2024), ("112年", 2023), ("", None)])
def test_roc_fiscal_year(label, year):
    assert roc_fiscal_year(label) == year
