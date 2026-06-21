from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from central.app.dashboard import STATIC_DIR, router as dashboard_router
from central.app.probe_api import router as probe_router


app = FastAPI(title="PING Central", version="0.1.0")
app.include_router(dashboard_router)
app.include_router(probe_router)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "central"}

