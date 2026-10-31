"""`python -m app` → http://127.0.0.1:8000"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.server:create_app", factory=True,
                host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
