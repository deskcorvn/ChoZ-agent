"""`python -m app` → http://127.0.0.1:8000"""
import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("LISTING_ACCESS_TOKEN"):
        raise SystemExit("Refusing to listen on " + host + " without LISTING_ACCESS_TOKEN (anyone could spend your credits).")
    uvicorn.run("app.server:create_app", factory=True,
                host=host, port=int(os.environ.get("PORT", "8000")))
