import asyncio
import csv
import json

from app.batch import run_folder, summary
from tests.conftest import product_photo


def test_batch_reports_status_cost_and_accuracy(settings, tmp_path):
    products = tmp_path / "products"
    for name, note, expected in (
        ("01-ao", "Áo thun nam, giá 120 nghìn, còn 50 cái", {"price_vnd": 120000, "stock": 50}),
        ("02-ao-thieu-gia", "Áo thun nam, còn 10 cái", {"price_vnd": 90000}),
    ):
        folder = products / name
        folder.mkdir(parents=True)
        (folder / "photo-1.jpg").write_bytes(product_photo())
        (folder / "note.txt").write_text(note, encoding="utf-8")
        (folder / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
    (products / "03-empty").mkdir()

    path, rows = asyncio.run(run_folder(products, settings))

    assert [r["status"] for r in rows] == ["ready", "needs_answer", "skipped"]
    assert rows[0]["price_vnd_ok"] and rows[0]["stock_ok"] and not rows[1]["price_vnd_ok"]
    with path.open(encoding="utf-8-sig") as handle:
        assert len(list(csv.DictReader(handle))) == 3
    text = summary(rows)
    assert "price_vnd: 1/2 correct" in text and "stock: 1/1 correct" in text


def test_wilson_interval_reads_small_samples_honestly():
    from app.results import wilson

    lo, hi = wilson(5, 5)
    assert round(lo, 2) == 0.57 and hi == 1.0
    lo, hi = wilson(30, 30)
    assert round(lo, 2) == 0.89
