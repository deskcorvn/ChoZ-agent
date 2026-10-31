# Sample data

## Products

Five instant-food products from **Khánh Thọ** (Hải Dương), an OCOP 4-star producer, published here
with the producer's permission. Each folder is one listing as a seller would make it:

```
samples/products/01-banh-da-ca-ro-goi/
  photo-1.png      product photo (1-3 photos; photo-2, photo-3 optional)
  note.txt         what the seller says, typed (or speech.wav: the seller saying it, 16 kHz mono WAV)
  expected.json    ground truth for `python -m app.batch`: price, stock, category
```

- Prices are the producer's retail prices from `supplier/price-list.jpg` (15,000 đ per pack, 22,000 đ per bowl).
- **Stock counts are made up for testing**; real stock is whatever the seller says.
- `03-banh-da-cua-goi` deliberately leaves out the stock, so the agent asks its one follow-up question.
- `05-chao-hau-hop` deliberately says "150 gam" (as the price list does) while the bowl's label reads
  120g, so the verification step has a real mismatch to report.

Run them all: `python -m app.batch samples/products`.

## Supplier material

`supplier/` holds the producer's price list and brand graphics (OCOP badge, slogan, frame, texture),
as received. The agent does not use them yet.

## Categories

`categories.json` maps the ids the model picks to the Vietnamese names sellers see. It is a
placeholder for Chợ Z's real top-level categories.
