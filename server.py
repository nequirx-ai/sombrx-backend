"""SOMBRX SYSTEM 2.0 backend."""
import os
import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, APIRouter, HTTPException
from fastapi.responses import Response
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from storage import init_storage, get_object
from telegram_bot import build_application

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Mongo
mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]


# ---------- Models ----------
class Report(BaseModel):
    id: str
    alias: str
    description: str
    images: List[str] = Field(default_factory=list)
    created_at: str
    telegram_user_id: Optional[int] = None
    channel_message_ids: List[int] = Field(default_factory=list)


# ---------- Lifespan ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Storage
    try:
        init_storage()
    except Exception as e:
        logger.error("Storage init failed: %s", e)

    # Telegram bot
    tg_app = build_application(db)
    app.state.tg_app = tg_app
    try:
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "edited_message", "channel_post"],
        )
        logger.info("Telegram bot polling started")
    except Exception as e:
        logger.exception("Telegram bot startup failed: %s", e)

    try:
        yield
    finally:
        try:
            if tg_app.updater.running:
                await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception:
            pass
        client.close()


app = FastAPI(lifespan=lifespan)
api_router = APIRouter(prefix="/api")


# ---------- Routes ----------
@api_router.get("/")
async def root():
    return {"name": "SOMBRX SYSTEM 2.0", "status": "online"}


@api_router.get("/reports", response_model=List[Report])
async def list_reports(q: Optional[str] = None):
    query: dict = {}
    if q:
        # case-insensitive search across alias + description
        import re
        rx = re.compile(re.escape(q.strip()), re.IGNORECASE)
        query = {"$or": [{"alias": rx}, {"description": rx}]}
    cursor = db.reports.find(query, {"_id": 0}).sort("created_at", -1).limit(500)
    return await cursor.to_list(length=500)


@api_router.get("/reports/{report_id}", response_model=Report)
async def get_report(report_id: str):
    doc = await db.reports.find_one({"id": report_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Report not found")
    return doc


@api_router.get("/stats")
async def stats():
    total = await db.reports.count_documents({})
    last = await db.reports.find({}, {"_id": 0, "created_at": 1}) \
        .sort("created_at", -1).limit(1).to_list(length=1)
    return {
        "total": total,
        "last_updated": last[0]["created_at"] if last else None,
        "system": "SOMBRX SYSTEM 2.0",
        "now": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/files/{path:path}")
async def serve_file(path: str):
    try:
        data, content_type = get_object(path)
    except Exception as e:
        logger.warning("File fetch failed for %s: %s", path, e)
        raise HTTPException(status_code=404, detail="File not found")
    return Response(content=data, media_type=content_type)


app.include_router(api_router)


# Public download endpoint for the source ZIP (so the user can grab it via browser).
from fastapi.responses import FileResponse  # noqa: E402

@app.get("/download/sombrx-system.zip")
async def download_zip():
    zip_path = "/app/downloads/sombrx-system.zip"
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename="sombrx-system.zip",
    )

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
