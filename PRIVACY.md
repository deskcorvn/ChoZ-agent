# Where a seller's data goes

| Data | Sent to | Why | Kept |
|---|---|---|---|
| Photos (1–3) | Meta Model API: Muse Spark 1.3 (listing, check), SAM 3.1 (cut-out) | read the product, verify claims, cut it out | `data/runs/<id>/` on this server, deleted after `LISTING_RETENTION_DAYS` (default 7) |
| Voice (push-to-talk WAV) | Meta Model API: Muse Voice Transcribe | speech → text | **deleted right after transcription** (`LISTING_KEEP_AUDIO=1` keeps it, for debugging only) |
| Transcript, listing, check, log | not sent anywhere else | the preview and the step log | `data/runs/<id>/run.json`, same retention |
| Studio room prompt | Meta Model API: Muse Image | paint an empty room (no product, no person) | pooled rooms in `data/cache/neutral-rooms/` |
| Published listing + studio photo | the local sample catalog (`data/catalog.json`) | what a buyer sees | until the seller deletes it; contact data is masked before it is stored |
| Studio video (optional, preview) | Kling (render plug-in, its MCP server) | animate first frames painted by Muse Image | Kling keeps result URLs for 24 h; clips are downloaded to the run folder |

- **Contact data never reaches a listing.** Phone numbers, e-mails, links and account numbers in what the model writes or
  in what the seller types into the preview are masked before the preview and again on publish (`app/guard.py`).
- **The Meta key stays on the server.** It is sent only in the Authorization header and never logged.
- **The seller can take it all back.** The app says what is sent before upload. After a listing is made, "Tải về dữ liệu"
  (`GET /api/runs/<id>/export`) downloads a zip of everything kept for it, and "Xoá ảnh và dữ liệu" /
  "Gỡ tin và xoá dữ liệu" (`DELETE /api/runs/<id>`) removes the run folder and the catalog entry at once.
- **Each seller sees only their own data.** With seller tokens (`python -m app.sellers add <id>`, stored as hashes)
  every run, photo file, export, delete and shop entry is scoped to its seller; without any token the server only
  listens on `localhost`. The audit log (`data/audit/audit.jsonl`) keeps ids, verdicts and rule ids, never content. Chợ Z itself is not called; listings go to a local sample catalog.
- **Mock mode** (no key) sends nothing anywhere and says so in the log header.
