from fastapi import FastAPI

from central.app.probe_api import router as probe_router


app = FastAPI(title="PING Central", version="0.1.0")
app.include_router(probe_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "central"}

