import os
import re
import sys
import time
import shutil
import logging
import tempfile
import threading
import subprocess
import importlib.util
from pathlib import Path
from urllib.parse import urlparse

def ensure_package(module_name, package_name):
    if importlib.util.find_spec(module_name) is None:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check", package_name
        ])

ensure_package("telebot", "pyTelegramBotAPI")
ensure_package("yt_dlp", "yt-dlp")

import telebot
from telebot import types
import yt_dlp

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения облачного хостинга.")

MAX_UPLOAD_MB = max(5, int(os.getenv("MAX_UPLOAD_MB", "49")))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", tempfile.gettempdir())) / "mediaflow_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("mediaflow")
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=8)

MODE_VIDEO = "video"
MODE_AUDIO = "audio"
user_modes = {}
user_jobs = {}
jobs_lock = threading.Lock()

VIDEO_QUALITIES = {
    "360": ("360p", 360),
    "480": ("480p", 480),
    "720": ("720p HD", 720),
    "1080": ("1080p Full HD", 1080),
}
URL_PATTERN = re.compile(r"https?://[^\s<>()]+")

def main_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🎬 Скачать видео", callback_data="mode:video"),
        types.InlineKeyboardButton("🎵 Скачать аудио", callback_data="mode:audio"),
        types.InlineKeyboardButton("ℹ️ Возможности бота", callback_data="help"),
    )
    return kb

def quality_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("360p", callback_data="quality:360"),
        types.InlineKeyboardButton("480p", callback_data="quality:480"),
        types.InlineKeyboardButton("720p HD", callback_data="quality:720"),
        types.InlineKeyboardButton("1080p Full HD", callback_data="quality:1080"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="home"))
    return kb

def back_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="home"))
    return kb

def valid_url(text):
    match = URL_PATTERN.search((text or "").strip())
    if not match:
        return None
    url = match.group(0).rstrip(".,!?;:")
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        return url
    except ValueError:
        return None

def get_mode(chat_id):
    mode = user_modes.get(chat_id)
    return mode.get("mode") if isinstance(mode, dict) else mode

def ffmpeg_available():
    return shutil.which("ffmpeg") is not None

def safe_title(value):
    value = re.sub(r"[\r\n\t]+", " ", value or "media")
    value = re.sub(r'[\\/:*?"<>|]+', "_", value).strip(" ._")
    return (value or "media")[:100]

def progress_hook(chat_id, message_id):
    state = {"time": 0, "percent": None}

    def hook(data):
        if data.get("status") != "downloading":
            return
        now = time.time()
        if now - state["time"] < 2.5:
            return
        state["time"] = now
        downloaded = data.get("downloaded_bytes") or 0
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        percent = f"{downloaded * 100 / total:.0f}%" if total else ""
        if percent == state["percent"]:
            return
        state["percent"] = percent
        status = f"⏬ Скачиваю… {percent}" if percent else "⏬ Скачиваю…"
        try:
            bot.edit_message_text(status, chat_id, message_id)
        except Exception:
            pass
    return hook

def make_options(workdir, mode, quality, chat_id, status_message):
    common = {
        "outtmpl": str(workdir / "%(title).100B_%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "retries": 3,
        "fragment_retries": 3,
        "file_access_retries": 2,
        "overwrites": True,
        "progress_hooks": [progress_hook(chat_id, status_message.message_id)] if status_message else [],
        "max_filesize": MAX_UPLOAD_BYTES,
    }

    if mode == MODE_VIDEO:
        height = VIDEO_QUALITIES[quality][1]
        if ffmpeg_available():
            selector = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        else:
            # Without FFmpeg, choose a single combined file instead of separate video/audio streams.
            selector = f"best[height<={height}]/best"
        common.update({
            "format": selector,
            "merge_output_format": "mp4" if ffmpeg_available() else None,
        })
        if common.get("merge_output_format") is None:
            common.pop("merge_output_format", None)
    else:
        # Keep the source audio format when possible; FFmpeg is not required.
        common.update({
            "format": "bestaudio/best",
            "format_sort": ["abr", "acodec:opus", "acodec:m4a"],
        })
    return common

def get_result_file(workdir):
    candidates = [
        p for p in workdir.iterdir()
        if p.is_file() and p.stat().st_size > 0
        and not p.name.endswith((".part", ".ytdl", ".json", ".description", ".jpg", ".png", ".webp"))
    ]
    return max(candidates, key=lambda p: p.stat().st_size) if candidates else None

def download_media(chat_id, url, mode, quality=None, status_message=None):
    workdir = Path(tempfile.mkdtemp(prefix="mediaflow_", dir=str(DOWNLOAD_DIR)))
    try:
        options = make_options(workdir, mode, quality, chat_id, status_message)
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise RuntimeError("Сайт не вернул информацию о видео.")
            title = safe_title(info.get("title", "media"))
            duration = info.get("duration")
            if duration and duration > 4 * 60 * 60:
                raise ValueError("Ролики длиннее 4 часов не поддерживаются.")

        media_file = get_result_file(workdir)
        if media_file is None:
            raise RuntimeError("Загрузчик не создал файл. Проверь поддержку сайта и наличие места на сервере.")

        size = media_file.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"Файл весит {size / 1024 / 1024:.1f} МБ, а лимит отправки установлен на {MAX_UPLOAD_MB} МБ. "
                "Попробуй выбрать более низкое качество."
            )

        if status_message:
            try:
                bot.edit_message_text("📤 Отправляю файл в Telegram…", chat_id, status_message.message_id)
            except Exception:
                pass

        with media_file.open("rb") as file_obj:
            if mode == MODE_VIDEO:
                quality_name = VIDEO_QUALITIES[quality][0]
                caption = f"🎬 {title}\nКачество: до {quality_name}"
                # If Telegram rejects video parsing/codec, send it as a file instead.
                try:
                    bot.send_video(
                        chat_id, file_obj, caption=caption[:1000],
                        supports_streaming=True, timeout=180
                    )
                except Exception as send_error:
                    log.warning("send_video failed; trying document upload: %s", send_error)
                    file_obj.seek(0)
                    bot.send_document(chat_id, file_obj, caption=caption[:1000], timeout=180)
            else:
                caption = f"🎵 {title}\nФормат: {media_file.suffix.lstrip('.').upper() or 'аудио'}"
                try:
                    bot.send_audio(
                        chat_id, file_obj, caption=caption[:1000],
                        title=title[:64], timeout=180
                    )
                except Exception as send_error:
                    log.warning("send_audio failed; trying document upload: %s", send_error)
                    file_obj.seek(0)
                    bot.send_document(chat_id, file_obj, caption=caption[:1000], timeout=180)

        bot.send_message(chat_id, "✅ Готово. Можешь прислать следующую ссылку или выбрать другой режим.", reply_markup=main_keyboard())

    except yt_dlp.utils.DownloadError as exc:
        log.exception("yt-dlp download error")
        msg = (
            "Не удалось скачать эту ссылку. Причины могут быть такими:\n"
            "• сайт не поддерживается или изменил работу;\n"
            "• видео закрыто, удалено или требует входа;\n"
            "• на сервере устарела версия yt-dlp.\n\n"
            "Подробности ошибки записаны в журнал облачного хостинга."
        )
        try:
            bot.send_message(chat_id, msg, reply_markup=main_keyboard())
        except Exception:
            pass
    except Exception as exc:
        log.exception("Download/send failed")
        try:
            bot.send_message(chat_id, f"⚠️ Не удалось обработать файл: {str(exc)[:500]}", reply_markup=main_keyboard())
        except Exception:
            pass
    finally:
        if status_message:
            try:
                bot.delete_message(chat_id, status_message.message_id)
            except Exception:
                pass
        shutil.rmtree(workdir, ignore_errors=True)
        with jobs_lock:
            user_jobs.pop(chat_id, None)

def start_job(chat_id, url, mode, quality=None):
    with jobs_lock:
        if user_jobs.get(chat_id):
            bot.send_message(chat_id, "⏳ Предыдущая загрузка ещё выполняется. Дождись её окончания.")
            return
        user_jobs[chat_id] = True
    try:
        status = bot.send_message(chat_id, "🔎 Проверяю ссылку…")
    except Exception:
        with jobs_lock:
            user_jobs.pop(chat_id, None)
        raise
    threading.Thread(
        target=download_media,
        args=(chat_id, url, mode, quality, status),
        daemon=True,
    ).start()

@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    intro = (
        "Привет! Я MediaFlow136_Not.\n\n"
        "Отправь ссылку на видео, а я попробую скачать его и прислать сюда.\n\n"
        "🎬 Видео — выбор качества до 1080p.\n"
        "🎵 Аудио — скачивание звуковой дорожки в доступном формате.\n"
        "🌐 Поддерживаются многие сайты, совместимые с загрузчиком.\n\n"
        "Нажми нужную кнопку, отправь ссылку и дождись результата.\n\n"
        "Доступность зависит от сайта и лимита размера файла. Я не обхожу DRM или платный доступ. "
        "Скачивай материалы, которые разрешено сохранять."
    )
    bot.send_message(message.chat.id, intro, reply_markup=main_keyboard())

@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    bot.reply_to(
        message,
        "Остановить уже начавшуюся загрузку этой командой пока нельзя. Дождись её завершения.",
        reply_markup=main_keyboard(),
    )

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = call.message.chat.id
    data = call.data or ""
    try:
        if data == "home":
            user_modes.pop(chat_id, None)
            bot.answer_callback_query(call.id)
            bot.send_message(chat_id, "Главное меню. Что скачиваем?", reply_markup=main_keyboard())
        elif data == "help":
            bot.answer_callback_query(call.id)
            bot.send_message(
                chat_id,
                "🎬 Видео: отправь ссылку, затем выбери качество.\n"
                "🎵 Аудио: отправь ссылку — бот сохранит звуковую дорожку в исходном доступном формате.\n\n"
                "Если файл слишком большой, попробуй качество ниже. Для склейки отдельных видео- и аудиопотоков нужен FFmpeg.",
                reply_markup=back_keyboard(),
            )
        elif data == "mode:video":
            user_modes[chat_id] = MODE_VIDEO
            bot.answer_callback_query(call.id)
            bot.send_message(chat_id, "🎬 Пришли ссылку на видео.", reply_markup=back_keyboard())
        elif data == "mode:audio":
            user_modes[chat_id] = MODE_AUDIO
            bot.answer_callback_query(call.id)
            bot.send_message(chat_id, "🎵 Пришли ссылку на видео или аудио. Я попробую скачать звуковую дорожку.", reply_markup=back_keyboard())
        elif data.startswith("quality:"):
            quality = data.split(":", 1)[1]
            if quality not in VIDEO_QUALITIES:
                bot.answer_callback_query(call.id, "Неизвестное качество", show_alert=True)
                return
            pending = user_modes.get(chat_id)
            url = pending.get("url") if isinstance(pending, dict) else None
            if not url:
                bot.answer_callback_query(call.id, "Сначала отправь ссылку на видео", show_alert=True)
                return
            bot.answer_callback_query(call.id, f"Выбрано {VIDEO_QUALITIES[quality][0]}")
            user_modes[chat_id] = MODE_VIDEO
            start_job(chat_id, url, MODE_VIDEO, quality)
        else:
            bot.answer_callback_query(call.id)
    except Exception:
        log.exception("Callback handler failed")
        try:
            bot.answer_callback_query(call.id, "Ошибка обработки кнопки", show_alert=True)
        except Exception:
            pass

@bot.message_handler(content_types=["text"])
def handle_text(message):
    text = (message.text or "").strip()
    if text.startswith("/"):
        return
    url = valid_url(text)
    if not url:
        bot.send_message(message.chat.id, "Отправь ссылку, начинающуюся с http:// или https://, либо выбери режим.", reply_markup=main_keyboard())
        return

    mode = get_mode(message.chat.id)
    if not mode:
        bot.send_message(message.chat.id, "Сначала выбери режим скачивания.", reply_markup=main_keyboard())
        return

    if mode == MODE_VIDEO:
        user_modes[message.chat.id] = {"mode": MODE_VIDEO, "url": url}
        bot.send_message(
            message.chat.id,
            "Выбери максимальное качество. Если сайт не предоставляет такой вариант, будет выбран доступный формат.",
            reply_markup=quality_keyboard(),
        )
    elif mode == MODE_AUDIO:
        start_job(message.chat.id, url, MODE_AUDIO)
    else:
        bot.send_message(message.chat.id, "Выбери режим в главном меню.", reply_markup=main_keyboard())

def run_bot():
    log.info("MediaFlow136_Not запущен")
    while True:
        try:
            bot.infinity_polling(timeout=25, long_polling_timeout=25, skip_pending=True)
            log.warning("Telegram polling завершился; переподключение через 3 секунды")
        except KeyboardInterrupt:
            log.info("Бот остановлен")
            break
        except Exception:
            log.exception("Ошибка Telegram polling; повтор через 5 секунд")
            time.sleep(5)

if __name__ == "__main__":
    run_bot()
