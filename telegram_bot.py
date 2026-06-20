"""Telegram bot for SOMBRX SYSTEM 2.0."""
import os
import io
import uuid
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv(Path(__file__).parent / ".env")

from storage import put_object, APP_NAME  # noqa: E402

logger = logging.getLogger(__name__)

# In-memory buffer for media groups (albums sent from Telegram).
_media_groups: dict[str, dict] = {}
_MG_FLUSH_DELAY = 2.0  # seconds — wait for all photos of an album


def _env():
    return (
        os.environ.get("TELEGRAM_TOKEN"),
        int(os.environ.get("OWNER_ID", "0") or 0),
        os.environ.get("TELEGRAM_CHANNEL", ""),
    )


WELCOME = (
    "👁️‍🗨️  *SOMBRX SYSTEM 2.0*  👁️‍🗨️\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "🩸 _Terminal de exposición activado_\n\n"
    "📡 Envíame *fotos* (puedes enviar un álbum) con texto "
    "(caption) o un mensaje de texto para registrar una nueva *RATA*.\n\n"
    "🗂  Formato sugerido del caption:\n"
    "`ALIAS | descripción completa del sujeto`\n"
    "o simplemente:\n"
    "`ALIAS`\n"
    "`descripción en líneas siguientes`\n\n"
    "🕳  El registro será publicado en el canal "
    "y aparecerá en el sitio.\n"
    "⛓  Acceso restringido al *OWNER*."
)

HELP = (
    "🛰 *COMANDOS DISPONIBLES*\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "/start  – Activar terminal 👁\n"
    "/help   – Mostrar ayuda 🗝\n"
    "/ping   – Comprobar enlace 📡\n"
    "/list   – Últimos 10 expedientes 📂\n"
    "/borrar `<id>` – Eliminar expediente 🗑\n\n"
    "📎 *Envío de reporte:* foto(s) + caption con\n"
    "`ALIAS | descripción`\n"
    "Soporta *álbumes* (varias fotos a la vez)."
)


def _parse_caption(text: str):
    text = (text or "").strip()
    if not text:
        return "ANÓNIMO", "(sin descripción)"
    if "|" in text:
        alias, _, desc = text.partition("|")
        return alias.strip() or "ANÓNIMO", desc.strip() or "(sin descripción)"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) == 1:
        return lines[0][:60].strip(), lines[0]
    return lines[0].strip(), "\n".join(lines[1:]).strip()


def _is_owner(update: Update) -> bool:
    user = update.effective_user
    _, owner_id, _ = _env()
    return bool(user and user.id == owner_id)


async def _download_and_store(bot, file_id: str) -> str | None:
    """Download a Telegram photo and push it to object storage. Returns path."""
    try:
        file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await file.download_to_memory(out=buf)
        path = f"{APP_NAME}/evidence/{uuid.uuid4()}.jpg"
        result = put_object(path, buf.getvalue(), "image/jpeg")
        return result["path"]
    except Exception as e:
        logger.exception("Storage upload failed: %s", e)
        return None


async def _publish_report(context, db, photos_file_ids, caption_raw, reply_msg=None):
    """Persist a report and post it to the channel."""
    alias, description = _parse_caption(caption_raw or "")
    image_paths: list[str] = []
    for fid in photos_file_ids:
        p = await _download_and_store(context.bot, fid)
        if p:
            image_paths.append(p)

    report_id = str(uuid.uuid4())
    doc = {
        "id": report_id,
        "alias": alias,
        "description": description,
        "images": image_paths,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "telegram_user_id": reply_msg.from_user.id if reply_msg else None,
        "channel_message_ids": [],
    }

    header = "🩸 *NUEVA RATA EXPUESTA* 🩸\n👁‍🗨 SOMBRX SYSTEM 2.0\n━━━━━━━━━━━━━━━━━━━\n"
    body = (
        f"🗂 *ALIAS:* `{alias}`\n📝 *REPORTE:*\n{description}\n"
        f"🆔 `{report_id[:8]}`\n\n⛓ _Archivo agregado al expediente público_"
    )
    caption = header + body

    posted = False
    _, _, channel = _env()
    if channel:
        try:
            if len(photos_file_ids) > 1:
                media = [
                    InputMediaPhoto(
                        media=fid,
                        caption=caption if i == 0 else None,
                        parse_mode=ParseMode.MARKDOWN if i == 0 else None,
                    )
                    for i, fid in enumerate(photos_file_ids)
                ]
                sent = await context.bot.send_media_group(chat_id=channel, media=media)
                doc["channel_message_ids"] = [m.message_id for m in sent]
            elif len(photos_file_ids) == 1:
                sent = await context.bot.send_photo(
                    chat_id=channel,
                    photo=photos_file_ids[0],
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                )
                doc["channel_message_ids"] = [sent.message_id]
            else:
                sent = await context.bot.send_message(
                    chat_id=channel,
                    text=caption,
                    parse_mode=ParseMode.MARKDOWN,
                )
                doc["channel_message_ids"] = [sent.message_id]
            posted = True
        except Exception as e:
            logger.exception("Channel post failed: %s", e)

    await db.reports.insert_one(doc)

    if reply_msg is not None:
        confirm = (
            f"✅ *Reporte registrado*\n"
            f"🆔 `{report_id[:8]}`\n"
            f"🗂 Alias: `{alias}`\n"
            f"🖼 Evidencias: {len(image_paths)}\n"
            f"{'📡 Publicado en el canal' if posted else '⚠️ No se pudo publicar en el canal'}"
        )
        await reply_msg.reply_text(confirm, parse_mode=ParseMode.MARKDOWN)


async def _flush_media_group(group_id: str, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(_MG_FLUSH_DELAY)
    buf = _media_groups.pop(group_id, None)
    if not buf:
        return
    db = context.application.bot_data["db"]
    await _publish_report(
        context,
        db,
        photos_file_ids=buf["photos"],
        caption_raw=buf["caption"],
        reply_msg=buf["reply_msg"],
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await update.message.reply_text(
            "⛔ Acceso restringido. Esta terminal pertenece al OWNER."
        )
        return
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await update.message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await update.message.reply_text("📡 PONG · enlace estable 🩸")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    db = context.application.bot_data["db"]
    docs = await db.reports.find({}, {"_id": 0, "id": 1, "alias": 1, "created_at": 1}) \
        .sort("created_at", -1).limit(10).to_list(length=10)
    if not docs:
        await update.message.reply_text("📂 Archivo vacío. Aún no hay ratas registradas.")
        return
    lines = ["📂 *ÚLTIMOS EXPEDIENTES*", "━━━━━━━━━━━━━━━━━━━━━"]
    for d in docs:
        ts = (d.get("created_at") or "")[:16].replace("T", " ")
        lines.append(f"🆔 `{d['id'][:8]}` · *{d.get('alias','?')}* · _{ts}_")
    lines.append("\n🗑 Borrar: `/borrar <id>` (primeros 8 caracteres)")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "❓ Uso: `/borrar <id>` (8 caracteres del id, o id completo)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    prefix = args[0].strip().lower()
    if len(prefix) < 4:
        await update.message.reply_text("⚠️ Indica al menos 4 caracteres del id.")
        return

    db = context.application.bot_data["db"]
    import re as _re
    # match by exact id or by prefix (escape for regex safety)
    doc = await db.reports.find_one(
        {"$or": [{"id": prefix}, {"id": {"$regex": f"^{_re.escape(prefix)}"}}]},
        {"_id": 0},
    )
    if not doc:
        await update.message.reply_text("❌ No se encontró ningún expediente con ese id.")
        return

    # Try to delete from channel
    _, _, channel = _env()
    deleted_channel = 0
    for mid in doc.get("channel_message_ids", []) or []:
        try:
            await context.bot.delete_message(chat_id=channel, message_id=mid)
            deleted_channel += 1
        except Exception as e:
            logger.warning("Could not delete channel message %s: %s", mid, e)

    await db.reports.delete_one({"id": doc["id"]})
    await update.message.reply_text(
        f"🗑 *Expediente eliminado*\n"
        f"🆔 `{doc['id'][:8]}`\n"
        f"🗂 Alias: `{doc.get('alias','?')}`\n"
        f"📡 Mensajes borrados del canal: {deleted_channel}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await update.message.reply_text("⛔ No autorizado.")
        return

    msg = update.message
    db = context.application.bot_data["db"]

    # Telegram media groups: collect photos sharing the same media_group_id.
    if msg.media_group_id and msg.photo:
        gid = msg.media_group_id
        buf = _media_groups.setdefault(
            gid,
            {"photos": [], "caption": None, "reply_msg": msg},
        )
        buf["photos"].append(msg.photo[-1].file_id)
        if msg.caption and not buf["caption"]:
            buf["caption"] = msg.caption
        # First photo schedules the flush task
        if "task" not in buf:
            buf["task"] = asyncio.create_task(_flush_media_group(gid, context))
        return

    # Single photo or text-only
    photos = [msg.photo[-1].file_id] if msg.photo else []
    caption_raw = msg.caption if msg.photo else msg.text
    await _publish_report(
        context,
        db,
        photos_file_ids=photos,
        caption_raw=caption_raw or "",
        reply_msg=msg,
    )


def build_application(db) -> Application:
    token, _, _ = _env()
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN not configured")
    app = ApplicationBuilder().token(token).build()
    app.bot_data["db"] = db

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.TEXT) & ~filters.COMMAND,
            handle_message,
        )
    )
    return app
