from fastapi import FastAPI

from app.api.workflows import router as workflows_router

app = FastAPI(title="Axon")
app.include_router(workflows_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
