"""The executive report's charts, rendered by the real plotly.

exec_report.py is the only plotly consumer, and nothing else in the suite
imports it: a plotly upgrade that renamed a keyword, changed `to_html`, or
stopped bundling the JS would surface only when someone opened a report. These
tests render each chart section from a small ReportData and check the
markup a browser needs: the Plotly.newPlot call, the trace types and labels,
and the inlined library build_html embeds.

Numeric arrays are deliberately not asserted from the HTML — plotly 6 encodes
them as base64 typed arrays, so a value check would test the encoding.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd

import exec_report


def _report(**overrides) -> exec_report.ReportData:
    base = dict(
        as_of=datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc),
        positions=pd.DataFrame([
            {"symbol": "NVDA", "qty": 3.0, "market_value": 600.0},
            {"symbol": "AMD", "qty": 2.0, "market_value": 400.0},
            {"symbol": "GONE", "qty": 0.0, "market_value": 0.0},
        ]),
        totals={},
        cash_by_account={},
        account_allocation=pd.DataFrame([
            {"account": "IBKR", "value": 700.0, "weight": 70.0},
            {"account": "Schwab", "value": 300.0, "weight": 30.0},
        ]),
        equity_curve=pd.DataFrame({
            "date": pd.to_datetime([date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19)]),
            "value": [950.0, 975.5, 1000.0],
        }),
    )
    base.update(overrides)
    return exec_report.ReportData(**base)


def _plots(html: str) -> int:
    return html.count("Plotly.newPlot(")


def test_allocation_renders_a_donut_and_an_account_bar():
    html = exec_report._section_allocation(_report())

    assert _plots(html) == 2
    assert '"type":"pie"' in html and '"type":"bar"' in html
    assert '"hole":0.5' in html
    # Labels are strings, so they survive in plain text; the closed position
    # is filtered out before plotting.
    assert '"NVDA"' in html and '"AMD"' in html and '"GONE"' not in html
    assert '"IBKR"' in html and '"Schwab"' in html
    assert "%{text:.1f}%" in html
    # include_plotlyjs=False: the library is inlined once by build_html, not
    # per chart.
    assert "cdn.plot.ly" not in html


def test_allocation_without_account_data_keeps_the_donut():
    html = exec_report._section_allocation(_report(account_allocation=pd.DataFrame()))
    assert _plots(html) == 1
    assert "No account-level data." in html


def test_equity_curve_renders_a_filled_line():
    html = exec_report._section_equity_curve(_report())
    assert _plots(html) == 1
    assert '"type":"scatter"' in html
    assert '"fill":"tozeroy"' in html
    assert "%{x|%Y-%m-%d}" in html


def test_empty_inputs_render_no_chart():
    empty = _report(
        positions=pd.DataFrame(columns=["symbol", "qty", "market_value"]),
        equity_curve=pd.DataFrame(),
    )
    assert exec_report._section_allocation(empty) == ""
    assert exec_report._section_equity_curve(empty) == ""


def test_the_inlined_library_is_plotly():
    js = exec_report.get_plotlyjs()
    assert len(js) > 1_000_000
    assert "newPlot" in js
