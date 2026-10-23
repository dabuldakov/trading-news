from __future__ import annotations

from dataclasses import dataclass

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import settings

SEC_COLUMNS = "SECID,SHORTNAME,SECNAME,LOTSIZE,ISIN,LISTLEVEL,CURRENCYID"


@dataclass(frozen=True)
class Security:
    ticker: str
    short_name: str
    name: str
    isin: str
    lot: int
    currency: str
    list_level: int | None


@dataclass(frozen=True)
class Issuer:
    title: str
    inn: str
    okpo: str


def parse_securities(payload: dict) -> list[Security]:
    block = payload.get("securities") or {}
    columns = block.get("columns") or []
    rows = block.get("data") or []
    index = {name: i for i, name in enumerate(columns)}
    out: list[Security] = []
    for row in rows:
        ticker = str(row[index["SECID"]] or "").strip().upper()
        if not ticker:
            continue

        def value(name: str, row=row, index=index):
            position = index.get(name)
            if position is None or position >= len(row):
                return None
            return row[position]

        list_level = value("LISTLEVEL")
        out.append(
            Security(
                ticker=ticker,
                short_name=str(value("SHORTNAME") or "").strip(),
                name=str(value("SECNAME") or "").strip(),
                isin=str(value("ISIN") or "").strip(),
                lot=max(1, int(value("LOTSIZE") or 1)),
                currency=str(value("CURRENCYID") or "RUB").strip(),
                list_level=int(list_level) if list_level is not None else None,
            )
        )
    return out


def parse_description(payload: dict) -> Issuer:
    block = payload.get("description") or {}
    columns = block.get("columns") or []
    rows = block.get("data") or []
    index = {name: i for i, name in enumerate(columns)}
    data: dict[str, object] = {}
    for row in rows:
        name = row[index["name"]] if "name" in index else None
        value = row[index["value"]] if "value" in index else None
        if name:
            data[str(name)] = value
    return Issuer(
        title=str(data.get("emitent_title") or "").strip(),
        inn=str(data.get("emitent_inn") or "").strip(),
        okpo=str(data.get("emitent_okpo") or "").strip(),
    )


class MoexClient:
    """Thin MOEX ISS client (public, no auth)."""

    def __init__(
        self,
        base: str | None = None,
        board: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base = (base or settings.moex_iss_base).rstrip("/")
        self.board = board or settings.moex_board
        self.timeout = timeout or settings.http_timeout

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _get(self, url: str, params: dict) -> dict:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    def securities(self) -> list[Security]:
        url = (
            f"{self.base}/engines/stock/markets/shares/boards/"
            f"{self.board}/securities.json"
        )
        payload = self._get(
            url,
            {
                "iss.meta": "off",
                "iss.only": "securities",
                "securities.columns": SEC_COLUMNS,
            },
        )
        return parse_securities(payload)

    def description(self, secid: str) -> Issuer:
        url = f"{self.base}/securities/{secid}.json"
        payload = self._get(url, {"iss.meta": "off", "iss.only": "description"})
        return parse_description(payload)
