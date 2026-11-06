"""Regenerate OCR fixtures (tests/fixtures/ocr/FX-2xx.json).

Source PDFs are the files the agent itself downloaded through the portal UI
into ./downloads (run the agent first). The seeded portal/data/faxes folder is
intentionally NOT read.

Usage:  python tests/fixtures/regenerate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from linda_agent.ocr.rapid_ocr import RapidOCREngine  # noqa: E402

OUT = Path(__file__).resolve().parent / "ocr"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    engine = RapidOCREngine()
    rows = []
    for pdf in sorted((ROOT / "downloads").glob("FX-*.pdf")):
        pages = engine.extract_pages(pdf)
        data = {
            "fax_id": pdf.stem,
            "pages": [
                {
                    "text": p.text,
                    "min_conf": p.min_conf,
                    "mean_conf": p.mean_conf,
                    "line_count": p.line_count,
                }
                for p in pages
            ],
        }
        (OUT / f"{pdf.stem}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        for i, p in enumerate(pages, 1):
            rows.append((pdf.stem, i, p.min_conf, p.mean_conf, p.line_count))
        print(f"wrote {pdf.stem}: {len(pages)} page(s)")

    print("\nfax    page  min_conf  mean_conf  lines")
    for fax, pg, mn, mean, n in rows:
        print(f"{fax}  {pg:>4}  {mn:8.3f}  {mean:9.3f}  {n:>5}")
    print(f"\nlowest page min_conf: {min(r[2] for r in rows):.3f}")
    print(f"lowest page mean_conf: {min(r[3] for r in rows):.3f}")


if __name__ == "__main__":
    main()
