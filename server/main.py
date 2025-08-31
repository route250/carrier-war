from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# Resolve project root (two levels up from this file: server/main.py -> project root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Ensure project root on sys.path so `import server.*` works when running as a script
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
STATIC_DIR = PROJECT_ROOT / "static"

app = FastAPI()

# Mount static files at /static
app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR), html=False),
    name="static",
)


# Root serves index.html
@app.get("/")
def read_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# Health check
@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# API routers
try:
    from server.routers.ai_router import router as ai_router
    app.include_router(ai_router, prefix="/v1/ai", tags=["ai"])
except Exception as e:
    print(f"Failed to load AI router: {e}")

try:
    from server.routers.session_router import router as session_router
    app.include_router(session_router, prefix="/v1/session", tags=["session"])
except Exception as e:
    print(f"Failed to load Session router: {e}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
