"""Start Ms Tanya's gateway (and, in dev mode, the developer console at http://localhost:8000).

    python app.py

Server mode (RUN_MODE=server in .env) also needs the workers:
    python -m tanya.workers turn 0-7
    python -m tanya.workers persist
    python -m tanya.workers loader
    python -m tanya.workers sessions
"""
import uvicorn

from tanya.settings import S

if __name__ == "__main__":
    uvicorn.run("tanya.gateway:app", host=S.env("HOST", "127.0.0.1"), port=int(S.env("PORT", "8000")))
