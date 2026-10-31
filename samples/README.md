# Sample data

`categories.json` is the category list the listing must pick from. It is a placeholder: replace it with
Chợ Z's real top-level categories.

## Products for testing and the demo

Put each real product in its own folder under `samples/products/`:

```
samples/products/01-ao-thun-nam/
  photo-1.jpg        1–3 phone photos, as a seller would take them (photo-2.jpg, photo-3.jpg optional)
  note.txt           what the seller says, typed (optional if speech.wav exists)
  speech.wav         the seller saying it (optional; 16 kHz mono WAV is what the web UI records)
  expected.json      optional ground truth: {"price_vnd": 120000, "stock": 50, "category": "Thời trang nam"}
```

Then run `python -m app.batch samples/products`. Only add photos the seller agreed to publish: this
repository is public.
