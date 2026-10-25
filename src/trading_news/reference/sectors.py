"""Отраслевая разметка MOEX-тикеров для новостного research.

MOEX ISS не отдаёт отрасль эмитента, поэтому держим компактную карту для
ликвидных бумаг, которые реально попадают в новостные связи. Неизвестные
тикеры остаются без отрасли — лучше пропуск, чем неверная классификация.
"""

from __future__ import annotations

import logging

from ..db import pool

logger = logging.getLogger(__name__)

SECTOR_BY_TICKER: dict[str, str] = {
    # oil & gas
    "GAZP": "oil_gas",
    "LKOH": "oil_gas",
    "ROSN": "oil_gas",
    "NVTK": "oil_gas",
    "TATN": "oil_gas",
    "SNGS": "oil_gas",
    "SNGSP": "oil_gas",
    "SIBN": "oil_gas",
    "TRNFP": "oil_gas",
    # metals & mining
    "GMKN": "metals",
    "RUAL": "metals",
    "ALRS": "metals",
    "NLMK": "metals",
    "MAGN": "metals",
    "CHMF": "metals",
    "MTLR": "metals",
    "EVRZ": "metals",
    # gold
    "PLZL": "gold",
    "SELG": "gold",
    # banks
    "SBER": "banks",
    "SBERP": "banks",
    "VTBR": "banks",
    "SVCB": "banks",
    "BSPB": "banks",
    "PRMB": "banks",
    "MBNK": "banks",
    "T": "banks",
    # financials / exchanges / leasing / insurance
    "MOEX": "financial",
    "AFKS": "financial",
    "DOMRF": "financial",
    "LEAS": "financial",
    "SPBE": "financial",
    "RENI": "financial",
    "SFIN": "financial",
    "ZAYM": "financial",
    # real estate / construction
    "SMLT": "real_estate",
    "KFBA": "real_estate",
    "APRI": "real_estate",
    # retail / consumer
    "MGNT": "retail",
    "OZON": "retail",
    "LENT": "retail",
    "MVID": "retail",
    "SVET": "retail",
    "BELU": "consumer",
    "ROST": "consumer",
    "AQUA": "consumer",
    # tech / IT
    "YDEX": "tech",
    "ELMT": "tech",
    "DIAS": "tech",
    "DATA": "tech",
    "ASTR": "tech",
    "HEAD": "tech",
    "POSI": "tech",
    "SOFL": "tech",
    # telecom
    "CNTL": "telecom",
    "RTKM": "telecom",
    "MTSS": "telecom",
    # transport
    "AFLT": "transport",
    "FLOT": "transport",
    "DELI": "transport",
    # utilities / power
    "MAGE": "utilities",
    "TORS": "utilities",
    "FEES": "utilities",
    "HYDR": "utilities",
    # chemicals / fertilizers
    "AKRN": "chemicals",
    "PHOR": "chemicals",
    # industrials
    "ZVEZ": "industrials",
    "IVAT": "industrials",
    "KMAZ": "industrials",
    # pharma
    "LIFE": "pharma",
    "OZPH": "pharma",
    # agriculture
    "RAGR": "agriculture",
}


def sector_for(ticker: str | None) -> str:
    return SECTOR_BY_TICKER.get((ticker or "").strip().upper(), "")


def _apply(conn) -> int:
    updated = 0
    for ticker, sector in SECTOR_BY_TICKER.items():
        cursor = conn.execute(
            """
            UPDATE companies c
            SET sector = %s, updated_at = now()
            FROM instruments i
            WHERE i.company_id = c.id
              AND i.ticker = %s
              AND (c.sector IS NULL OR c.sector = '')
            """,
            (sector, ticker),
        )
        updated += cursor.rowcount or 0
    return updated


def backfill_sectors(conn=None) -> int:
    """Заполнить ``companies.sector`` по известным тикерам (идемпотентно)."""
    if conn is not None:
        return _apply(conn)
    with pool().connection() as own:
        updated = _apply(own)
        own.commit()
    logger.info("sectors: updated %s companies", updated)
    return updated


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print({"sectors": backfill_sectors()})
