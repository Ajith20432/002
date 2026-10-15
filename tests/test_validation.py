import validation


def test_native_protection_contract_is_two_sided():
    result = validation.native_protection_contract()
    assert result["passed"] is True
    assert result["orders"] == 2


def test_long_validation_has_trades_and_is_deterministic(monkeypatch):
    report1 = validation.run_all(days=2)
    report2 = validation.run_all(days=2)
    assert report1 == report2
    assert all(c["candles"] == 2 * 24 * 4 for c in report1["cases"])
    assert all(c["trades"] >= 1 for c in report1["cases"])


def test_report_flags_an_edge_that_vanishes_under_high_costs():
    report = validation.run_all(days=10)
    assert "warnings" in report
    hc = next(c for c in report["cases"] if c["name"] == "high_cost")
    assert (hc["return_pct"] <= 0) == any("high-cost" in w for w in report["warnings"])
