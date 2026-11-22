from kbo_ai_platform.ingestion.fa_contracts import parse_foreign, parse_krw, parse_years


def test_parse_krw():
    assert parse_krw("4년 50억") == 5_000_000_000
    assert parse_krw("2억 5,000만 원") == 250_000_000
    assert parse_krw("1년 1억 1천만원") == 110_000_000
    assert parse_krw("0.75억") == 75_000_000
    assert parse_krw("3+1년 20억(보장 18억)") == 2_000_000_000
    assert parse_krw("연간 3천") is None
    assert parse_krw("5억 엔") is None


def test_parse_years():
    assert parse_years("4년") == 4
    assert parse_years("2+2년 21억") == 4
    assert parse_years("비공개") is None


def test_parse_foreign():
    assert parse_foreign("7억 6천만 엔") == (760_000_000, "JPY")
    assert parse_foreign("310만불") == (3_100_000, "USD")
