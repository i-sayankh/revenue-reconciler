from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import close_db, connect_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await connect_db()
    yield
    await close_db()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}
