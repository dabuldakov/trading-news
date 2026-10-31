"""Small audited benchmark for ticker-link precision.

Labels are manual: 1 = company is the subject/important mention, 0 = false link.
Run after relevance_llm predictions to compare rules vs LLM gate.
"""
from __future__ import annotations

import json

from ..db import pool

CASES = [
    (218261, "HEAD", 0, "UPS≠HeadHunter / Ahead"),
    (228016, "AKRN", 0, "Акрон внутри Макрона"),
    (228016, "MOEX", 0, "Французская политика"),
    (228016, "SBER", 0, "Французская политика"),
    (228016, "VTBR", 0, "Французская политика"),
    (228017, "SMLT", 0, "самолет≠Самолет"),
    (229233, "MGNT", 1, "Дикси/Магнит"),
    (229233, "ALRS", 0, "Дикси/Магнит"),
    (229233, "CHMF", 0, "Дикси/Магнит"),
    (229233, "MAGN", 0, "Дикси/Магнит"),
    (229233, "NLMK", 0, "Дикси/Магнит"),
    (229397, "PHOR", 1, "PhosAgro"),
    (229397, "MAGE", 0, "PhosAgro≠Магаданэнерго"),
    (230009, "POSI", 0, "литий≠Positive"),
    (230009, "GMKN", 0, "литий"),
    (230011, "CARM", 0, "carmakers"),
    (230011, "TORS", 0, "carmakers"),
]


def evaluate() -> dict:
    rows = []
    with pool().connection() as conn:
        for article_id, ticker, label, note in CASES:
            row = conn.execute(
                """
                SELECT confidence, method, relevance
                FROM news_tickers
                WHERE article_id = %s AND ticker = %s
                """,
                (article_id, ticker),
            ).fetchone()
            rules = row is not None
            relevance = None if row is None else row[2]
            keep = relevance is not None and float(relevance) >= 1.0
            rows.append(
                {
                    "article_id": article_id,
                    "ticker": ticker,
                    "label": label,
                    "rules": rules,
                    "relevance": relevance,
                    "llm": keep,
                    "note": note,
                }
            )

    def score(key: str) -> dict:
        tp = sum(1 for r in rows if r["label"] and r[key])
        fp = sum(1 for r in rows if not r["label"] and r[key])
        fn = sum(1 for r in rows if r["label"] and not r[key])
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        return {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(precision, 3),
            "recall": round(recall, 3),
        }

    return {
        "n": len(rows),
        "rules": score("rules"),
        "llm": score("llm"),
        "cases": rows,
    }


if __name__ == "__main__":
    result = evaluate()
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, indent=2))
    for row in result["cases"]:
        print(row)
