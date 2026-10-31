"""Minimal crypto asset reference for the isolated crypto-news profile."""

from __future__ import annotations

import argparse
import logging

from .db import migrate, pool

COINS = [
    ("BTC", "Bitcoin", ["Bitcoin", "BTC", "XBT", "Биткоин", "Биткойн"]),
    ("ETH", "Ethereum", ["Ethereum", "ETH", "Ether", "Эфириум", "Эфир"]),
    ("SOL", "Solana", ["Solana", "SOL", "Солана"]),
    ("XRP", "XRP", ["XRP", "Ripple", "Рипл"]),
    ("BNB", "BNB", ["BNB", "Binance Coin", "Бинанс"]),
    ("DOGE", "Dogecoin", ["Dogecoin", "DOGE", "Догикоин"]),
    ("ADA", "Cardano", ["Cardano", "ADA", "Кардано"]),
    ("AVAX", "Avalanche", ["Avalanche", "AVAX", "Аваланч"]),
    ("LINK", "Chainlink", ["Chainlink", "LINK", "Чейнлинк"]),
    ("DOT", "Polkadot", ["Polkadot", "DOT", "Полкадот"]),
    ("LTC", "Litecoin", ["Litecoin", "LTC", "Лайткоин"]),
    ("TON", "Toncoin", ["Toncoin", "TON", "Тонкоин"]),
    ("TRX", "TRON", ["TRON", "TRX", "Трон"]),
    ("SHIB", "Shiba Inu", ["Shiba Inu", "SHIB", "Шиба-ину"]),
    ("UNI", "Uniswap", ["Uniswap", "UNI", "Юнисвоп"]),
    ("ATOM", "Cosmos", ["Cosmos", "ATOM", "Космос"]),
    ("ETC", "Ethereum Classic", ["Ethereum Classic", "ETC", "Эфириум Классик"]),
    ("FIL", "Filecoin", ["Filecoin", "FIL", "Файлкоин"]),
    ("NEAR", "NEAR Protocol", ["NEAR Protocol", "NEAR", "Нир"]),
    ("APT", "Aptos", ["Aptos", "APT", "Аптос"]),
]


def seed() -> int:
    migrate()
    n = 0
    with pool().connection() as conn:
        for base, name, aliases in COINS:
            ticker = f"{base}:USDT"
            company = conn.execute(
                """
                INSERT INTO companies (name, short_name)
                VALUES (%s, %s)
                ON CONFLICT (name) DO UPDATE SET updated_at = now()
                RETURNING id
                """,
                (name, name),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO instruments
                    (ticker, secid, board, type, name, company_id, lot, currency,
                     status, source, last_seen_at, updated_at)
                VALUES (%s, %s, 'CRYPTO', 'crypto', %s, %s, 1, 'USD',
                        'active', 'crypto_ref', now(), now())
                ON CONFLICT (ticker) DO UPDATE SET
                    name = EXCLUDED.name, company_id = EXCLUDED.company_id,
                    status = 'active', updated_at = now()
                """,
                (ticker, ticker, name, company),
            )
            for alias in aliases:
                conn.execute(
                    """
                    INSERT INTO aliases (company_id, alias, kind, lang, weight)
                    VALUES (%s, %s, 'crypto_name', 'en', 1.0)
                    ON CONFLICT (company_id, alias, kind) DO UPDATE SET weight = 1.0
                    """,
                    (company, alias),
                )
            n += 1
        conn.commit()
    return n


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.parse_args()
    print({"assets": seed()})


if __name__ == "__main__":
    main()
