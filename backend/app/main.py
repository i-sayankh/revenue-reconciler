from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings
from app.db import close_db, connect_db
from app.errors import register_exception_handlers
from app.routers import ingest, reconcile, whoami

settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await connect_db()
    yield
    await close_db()


app = FastAPI(lifespan=lifespan)

# Allows the frontend origin(s) in ALLOWED_ORIGINS (comma-separated) to call
# this API with credentials (the Supabase bearer token). Locally this is
# just http://localhost:3000; Step 16 adds the deployed Vercel origin to the
# same env var, no code change needed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

app.include_router(whoami.router, prefix="/api")
app.include_router(ingest.router, prefix="/api")
app.include_router(reconcile.router, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok"}
