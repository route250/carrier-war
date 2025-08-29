from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn

# Resolve project root (two levels up from this file: server/main.py -> project root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"

app = FastAPI()

# Serve all files in ./static at the root path.
# With html=True, a request to "/" returns index.html if present.
app.mount(
    "/",
    StaticFiles(directory=str(STATIC_DIR), html=True),
    name="static",
)


# If you later need API routes, consider mounting static at '/static'
# and adding an explicit route for '/'.

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)