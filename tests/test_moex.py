from __future__ import annotations

from trading_news.reference.moex import parse_description, parse_securities

SECURITIES = {
    "securities": {
        "columns": [
            "SECID",
            "SHORTNAME",
            "SECNAME",
            "LOTSIZE",
            "ISIN",
            "LISTLEVEL",
            "CURRENCYID",
        ],
        "data": [
            ["SBER", "Сбербанк", "Сбербанк ПАО", "10", "RU0009029540", "1", "RUB"],
            ["GAZP", "Газпром", "Газпром ПАО", "10", "RU0007661625", "1", "RUB"],
            ["", "skip", "no ticker", "1", "", "3", "RUB"],
        ],
    }
}

DESCRIPTION = {
    "description": {
        "columns": ["name", "title", "value"],
        "data": [
            ["emitent_title", "Эмитент", "ПАО Сбербанк"],
            ["emitent_inn", "ИНН", "7707083893"],
            ["emitent_okpo", "ОКПО", "00032537"],
        ],
    }
}


def test_parse_securities_skips_empty_ticker() -> None:
    rows = parse_securities(SECURITIES)
    assert [row.ticker for row in rows] == ["SBER", "GAZP"]
    assert rows[0].lot == 10
    assert rows[0].isin == "RU0009029540"
    assert rows[0].list_level == 1


def test_parse_description_extracts_issuer() -> None:
    issuer = parse_description(DESCRIPTION)
    assert issuer.title == "ПАО Сбербанк"
    assert issuer.inn == "7707083893"
    assert issuer.okpo == "00032537"
