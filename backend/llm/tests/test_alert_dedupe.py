"""Q&A / summary alerts: a repeated alert is listed once with a count, and no raw [LEVEL] tags."""
from datetime import datetime, timedelta, timezone

from backend.contracts import Alert, AlertLevel
from backend.llm import templates

T0 = datetime(2026, 10, 11, 5, 0, tzinfo=timezone.utc)
MSG = "BUY NVDAB not executed: Blocked (max_open_positions): 3 open position(s), limit 3 - limit reached"


def _a(i, level=AlertLevel.INFO, code="RISK_BLOCKED", msg=MSG, sym="NVDAB"):
    return Alert(level=level, code=code, message=msg, symbol=sym, timestamp=T0 + timedelta(minutes=i))


def test_repeated_alert_listed_once_with_count():
    line = templates.alerts_line([_a(i) for i in range(5)])
    assert line.count("risk blocked a trade (NVDAB)") == 1
    assert "(5 times)" in line and "more)" not in line


def test_no_bracket_level_tags_and_severity_words():
    line = templates.alerts_line([_a(0), _a(1, AlertLevel.WARNING, "QUOTE_EXPIRED", "quote expired"),
                                  _a(2, AlertLevel.CRITICAL, "DAILY_LOSS", "daily loss limit reached")])
    assert "[" not in line
    assert "Critical: " in line and "Warning: " in line
    assert "Info" not in line


def test_distinct_alerts_still_capped_with_more_count():
    alerts = [_a(i, msg=f"blocked {i}") for i in range(5)] + [_a(10 + i) for i in range(4)]
    line = templates.alerts_line(alerts)
    assert line.startswith("Recent alerts (+3 more): ")
    assert "(4 times)" in line  # most recent group comes first


def test_answer_uses_deduped_alerts():
    from backend.llm.tests.test_explain_fixes import _status  # reuse the shared status builder
    from backend.contracts import TradingMode
    txt = templates.answer("any alerts?", _status(TradingMode.SIM, False), None, [_a(i) for i in range(3)])
    assert txt.count("risk blocked a trade") == 1 and "(3 times)" in txt and "[INFO]" not in txt
