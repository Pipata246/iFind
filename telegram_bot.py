import asyncio
import base64
import io
import os
import re
import threading
from datetime import datetime, timezone

from supabase import Client, create_client
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.error import TimedOut, NetworkError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import time


BOT_TOKEN = "8388606268:AAGZytFu6t2i6oEiaHgJFHwCbFJoCGbPSpA"
SUPABASE_URL = "https://jfydcvornxzwuzjexiqb.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImpmeWRjdm9ybnh6d3V6amV4aXFiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM3NTM1MTgsImV4cCI6MjA4OTMyOTUxOH0.XmagPVxGHkAYqr_hSSlSrQ4nubOaTCUZlyzT0FbUgo4"
SUPABASE_USERS_TABLE = "bot_users"
SUPABASE_SETTINGS_TABLE = "bot_settings"
SUPABASE_MANUAL_SETTINGS_TABLE = "bot_manual_settings"
SUPABASE_EXCEL_FILES_TABLE = "bot_excel_files"

BTN_MANUAL_RUN = "🚀 Ручной запуск"
BTN_HELP = "📘 Инструкция"
BTN_AUTO_SETTINGS = "⚙️ Настройки автопарсинга"
BTN_EXCEL = "📄 Excel файлы"
BTN_EXCEL_SHOW = "📥 Показать Excel файлы"
BTN_EXCEL_DELETE = "🗑 Удалить Excel файл"
BTN_AVITO = "🇦🇺 Авито"
BTN_WB = "🛒 ВБ"
BTN_CANCEL = "Отмена"
BTN_EDIT = "Изменить"
BTN_MANUAL_AVITO_MY = "✅ Парсинг с моими настройками"
BTN_MANUAL_AVITO_MANUAL = "✍️ Задать вручную"
BTN_STOP_PARSING = "⛔ Остановить парсинг"
# Выбор сохраняется в bot_manual_settings (колонка today_only)
BTN_PARSE_SCOPE_TODAY = "📅 Только сегодняшние объявления"
BTN_PARSE_SCOPE_ALL = "📋 Все объявления"


def build_supabase_client() -> Client:
  return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


def upsert_user_to_supabase(client: Client, update: Update):
  user = update.effective_user
  if not user:
    return

  now_iso = datetime.now(timezone.utc).isoformat()
  payload = {
    "telegram_id": user.id,
    "username": user.username,
    "first_name": user.first_name,
    "last_name": user.last_name,
    "language_code": user.language_code,
    "is_bot": user.is_bot,
    "last_seen_at": now_iso,
    # На апдейте поле останется прежним, если уже было записано.
    "started_at": now_iso,
  }
  client.table(SUPABASE_USERS_TABLE).upsert(payload, on_conflict="telegram_id").execute()


def build_main_keyboard():
  keyboard = [
    [KeyboardButton(BTN_MANUAL_RUN), KeyboardButton(BTN_HELP)],
    [KeyboardButton(BTN_AUTO_SETTINGS), KeyboardButton(BTN_EXCEL)],
  ]
  return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def build_cancel_keyboard():
  keyboard = [[KeyboardButton(BTN_CANCEL)]]
  return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def build_platform_keyboard():
  keyboard = [
    [KeyboardButton(BTN_AVITO), KeyboardButton(BTN_WB)],
    [KeyboardButton(BTN_CANCEL)],
  ]
  return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def build_edit_keyboard():
  keyboard = [
    [KeyboardButton(BTN_EDIT)],
    [KeyboardButton(BTN_CANCEL)],
  ]
  return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def build_stop_keyboard():
  keyboard = [[KeyboardButton(BTN_STOP_PARSING)]]
  return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def build_parse_scope_keyboard():
  keyboard = [
    [KeyboardButton(BTN_PARSE_SCOPE_TODAY), KeyboardButton(BTN_PARSE_SCOPE_ALL)],
    [KeyboardButton(BTN_CANCEL)],
  ]
  return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def build_manual_avito_keyboard():
  keyboard = [
    [KeyboardButton(BTN_MANUAL_AVITO_MY), KeyboardButton(BTN_MANUAL_AVITO_MANUAL)],
    [KeyboardButton(BTN_CANCEL)],
  ]
  return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def build_excel_menu_keyboard():
  keyboard = [
    [KeyboardButton(BTN_EXCEL_SHOW), KeyboardButton(BTN_EXCEL_DELETE)],
    [KeyboardButton(BTN_CANCEL)],
  ]
  return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def build_excel_delete_keyboard(labels: list[str]):
  keyboard = [[KeyboardButton(x)] for x in labels]
  keyboard.append([KeyboardButton(BTN_CANCEL)])
  return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def parse_csv_list(text: str):
  if not text:
    return []
  parts = [p.strip() for p in text.split(",")]
  return [p for p in parts if p]


def _is_stop_parsing_command(text: str) -> bool:
  """Распознаем команду стопа устойчиво (с/без emoji, лишние пробелы)."""
  t = (text or "").strip().lower()
  if not t:
    return False
  if t == BTN_STOP_PARSING.lower():
    return True
  # Иногда Telegram-кнопка может прийти с другим emoji/вариацией.
  compact = re.sub(r"\s+", " ", t)
  return "останов" in compact and "парсинг" in compact


def normalize_capacity_values(values):
  """Normalize values like 128, 128gb, 128 ГБ into '128 ГБ'."""
  out = []
  for v in (values or []):
    s = (str(v) if v is not None else "").strip()
    if not s:
      continue
    digits = re.sub(r"\D", "", s)
    if digits:
      out.append(f"{int(digits)} ГБ")
    else:
      out.append(s)
  # keep order, unique
  uniq = []
  seen = set()
  for x in out:
    k = x.lower()
    if k in seen:
      continue
    seen.add(k)
    uniq.append(x)
  return uniq


def normalize_seller_type(text: str):
  s = (text or "").strip().lower()
  mapping = {
    "all": "all",
    "все": "all",
    "частные": "private",
    "private": "private",
    "компании": "company",
    "company": "company",
    "юрлица": "company",
  }
  return mapping.get(s)


def _format_list(val):
  if not val:
    return "-"
  if isinstance(val, list):
    return ", ".join([str(x) for x in val if x])
  return str(val)


def _ru_pages_phrase(n: int) -> str:
  """Склонение «N страниц» для фразы «Буду парсить …»."""
  n = abs(int(n))
  if n % 10 == 1 and n % 100 != 11:
    return f"{n} страницу"
  if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
    return f"{n} страницы"
  return f"{n} страниц"


def _format_avito_ready_bot_message(payload: dict) -> str:
  filters_ok = bool(payload.get("filters_applied"))
  d = int(payload.get("detected_pages") or 1)
  n = int(payload.get("pages_to_parse") or 1)
  head = "Открыл Avito, фильтры применены." if filters_ok else "Открыл Avito."
  return (
    f"{head}\n"
    f"Найдено страниц в выдаче: {d}.\n"
    f"Буду парсить {_ru_pages_phrase(n)}.\n"
    "Начало парсинга."
  )


async def _emit_avito_parse_status(
  update: Update, payload: dict, stop_event: threading.Event | None = None
):
  """Минимальные статусы парсинга для Telegram (без спама)."""
  if stop_event is not None and stop_event.is_set():
    return
  phase = payload.get("phase")
  if phase == "driver_ready":
    await update.message.reply_text(
      "🚀 Запуск парсинга…",
      reply_markup=build_stop_keyboard(),
    )
  elif phase == "page_loading":
    page = int(payload.get("page") or 1)
    pages = int(payload.get("pages_to_parse") or 1)
    await update.message.reply_text(
      f"📄 Открываю страницу {page}/{pages}.",
      reply_markup=build_stop_keyboard(),
    )
  elif phase == "dom_wait":
    await update.message.reply_text(
      "🔎 Собираю данные…",
      reply_markup=build_stop_keyboard(),
    )
  elif phase == "page_parsed":
    page = int(payload.get("page") or 1)
    added = int(payload.get("added") or 0)
    total = int(payload.get("total_collected") or 0)
    await update.message.reply_text(
      f"✅ Страница {page}: собрал данных {added}; данных всего {total}.",
      reply_markup=build_stop_keyboard(),
    )
  elif phase == "parse_finished":
    total = int(payload.get("total_items") or 0)
    await update.message.reply_text(
      f"📦 Парсинг завершён. Всего объявлений: {total}. Готовлю Excel…",
      reply_markup=build_stop_keyboard(),
    )


def format_settings_for_user(settings: dict):
  if not settings:
    return "Настройки не заданы."
  return (
    "Ваши настройки Avito:\n"
    f"• Название: {settings.get('keyword') or '-'}\n"
    f"• Модель: {settings.get('model') or '-'}\n"
    f"• Город: {settings.get('city') or '-'}\n"
    f"• Цена: {settings.get('price_min') or '-'} — {settings.get('price_max') or '-'}\n"
    f"• Память: {_format_list(settings.get('memory'))}\n"
    f"• Цвета: {_format_list(settings.get('colors'))}\n"
    f"• Продавцы: {'-' if not settings.get('seller_type') or settings.get('seller_type') == 'all' else settings.get('seller_type')}\n"
    f"• Только 4 звезды и выше: {'да' if settings.get('rating_4_plus') is True else '-'}\n"
    f"• Точность парсинга: {settings.get('precision') or '-'}"
  )


def _excel_file_to_base64(filepath: str) -> str:
  with open(filepath, "rb") as f:
    raw = f.read()
  return base64.b64encode(raw).decode("utf-8")


def upload_excel_file_to_supabase(supabase: Client, telegram_id: int, filepath: str):
  if not filepath or not os.path.exists(filepath):
    raise FileNotFoundError(filepath)
  filename = os.path.basename(filepath)
  content_b64 = _excel_file_to_base64(filepath)
  payload = {
    "telegram_id": telegram_id,
    "filename": filename,
    "content_base64": content_b64,
    "created_at": datetime.now(timezone.utc).isoformat(),
  }
  # Каждый запуск создаёт новый файл. Защита от дублей — опционально.
  return supabase.table(SUPABASE_EXCEL_FILES_TABLE).insert(payload).execute()


def list_excel_files_for_user(supabase: Client, telegram_id: int, limit: int = 20):
  res = (
    supabase.table(SUPABASE_EXCEL_FILES_TABLE)
    .select("id, filename, content_base64, created_at")
    .eq("telegram_id", telegram_id)
    .order("created_at", desc=True)
    .limit(limit)
    .execute()
  )
  return res.data or []


def delete_excel_file_from_supabase(supabase: Client, telegram_id: int, file_id):
  return (
    supabase.table(SUPABASE_EXCEL_FILES_TABLE)
    .delete()
    .eq("telegram_id", telegram_id)
    .eq("id", file_id)
    .execute()
  )


def _try_delete_excel_from_disk(filename: str):
  """Пробуем удалить физический xlsx с диска (если сохранился локально)."""
  if not filename:
    return False
  candidates = []
  try:
    candidates.append(os.path.abspath(filename))
  except Exception:
    pass
  try:
    candidates.append(os.path.join(os.getcwd(), filename))
  except Exception:
    pass
  # На сервере сервис чаще всего работает из /opt/iFind
  candidates.append(os.path.join("/opt/iFind", filename))

  seen = set()
  for p in candidates:
    if not p:
      continue
    ap = os.path.abspath(p)
    if ap in seen:
      continue
    seen.add(ap)
    try:
      if os.path.exists(ap):
        os.remove(ap)
        return True
    except Exception:
      continue
  return False


async def send_excel_files_from_supabase(update: Update, context: ContextTypes.DEFAULT_TYPE):
  supabase: Client = context.bot_data.get("supabase_client")
  telegram_id = update.effective_user.id
  files = list_excel_files_for_user(supabase, telegram_id, limit=10)
  if not files:
    await update.message.reply_text("Пока нет сохраненных Excel файлов.")
    return

  await update.message.reply_text(f"Найдено файлов: {len(files)}. Загружаю…")
  for f in files:
    try:
      b = base64.b64decode(f.get("content_base64") or "")
      bio = io.BytesIO(b)
      bio.name = f.get("filename") or "file.xlsx"
      await update.message.reply_document(document=bio, filename=bio.name)
    except Exception as e:
      print(f"[Supabase] Ошибка отправки файла: {e}")


async def ask_excel_file_to_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
  supabase: Client = context.bot_data.get("supabase_client")
  telegram_id = update.effective_user.id
  files = list_excel_files_for_user(supabase, telegram_id, limit=15)
  if not files:
    await update.message.reply_text("Пока нет Excel файлов для удаления.", reply_markup=build_main_keyboard())
    return

  labels = []
  mapping = {}
  for i, f in enumerate(files, start=1):
    filename = str(f.get("filename") or "file.xlsx")
    short = filename if len(filename) <= 42 else (filename[:39] + "...")
    label = f"🗑 {i}. {short}"
    labels.append(label)
    mapping[label] = {"id": f.get("id"), "filename": filename}
    mapping[str(i)] = {"id": f.get("id"), "filename": filename}

  context.user_data["awaiting_excel_delete"] = True
  context.user_data["excel_delete_map"] = mapping
  await update.message.reply_text(
    "Выберите файл для удаления:",
    reply_markup=build_excel_delete_keyboard(labels),
  )


async def ask_parse_scope_before_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """После сохранения строки в bot_manual_settings спрашиваем «сегодня / все» и пишем в колонку today_only."""
  context.user_data["awaiting_parse_scope"] = True
  await update.message.reply_text(
    "Какие объявления собрать в этом запуске?\n"
    f"• «{BTN_PARSE_SCOPE_TODAY}» — на карточке должна быть дата «сегодня» или «N часов/минут назад».\n"
    f"• «{BTN_PARSE_SCOPE_ALL}» — без фильтра по дате.",
    reply_markup=build_parse_scope_keyboard(),
  )


async def run_avito_parsing_and_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
  # Импортируем локально, чтобы не тянуть Selenium при старте бота
  from main import build_driver
  from avito_parser import parse_avito
  from excel_export import export_to_excel

  supabase: Client = context.bot_data.get("supabase_client")
  telegram_id = update.effective_user.id

  manual_row = get_manual_settings(supabase, telegram_id)
  auto_row = get_user_settings(supabase, telegram_id)
  if not manual_row and not auto_row:
    await update.message.reply_text(
      "Нет настроек: ни ручного запуска (bot_manual_settings), ни автопарсинга (bot_settings). "
      "Задайте их в «⚙️ Настройки автопарсинга» или в мастере ручного запуска.",
      reply_markup=build_main_keyboard(),
    )
    return
  settings, filled_from_auto = merge_manual_settings_with_autoparse(manual_row, auto_row)
  if filled_from_auto:
    print(f"[bot] Настройки: дополнено из bot_settings (как при автопарсинге): {', '.join(filled_from_auto)}")

  keyword = settings.get("keyword")
  model = settings.get("model")
  city = settings.get("city")
  price_min = settings.get("price_min")
  price_max = settings.get("price_max")
  precision = settings.get("precision") or 7
  today_only = bool(settings.get("today_only"))

  filters = {
    "memory": normalize_capacity_values(settings.get("memory") or []),
    "colors": settings.get("colors") or [],
    # Быстрый режим: фильтры, видимые прямо в выдаче.
    "seller_type": settings.get("seller_type") or "all",
    "rating_4_plus": bool(settings.get("rating_4_plus")),
  }

  stop_event = threading.Event()
  context.user_data["active_parse"] = {"platform": "avito", "stop_event": stop_event, "driver": None}

  start_msg = await update.message.reply_text("Запуск…", reply_markup=build_stop_keyboard())
  context.user_data["avito_parse_start_msg_id"] = start_msg.message_id

  loop = asyncio.get_running_loop()

  def _sync_once():
    driver = None
    partial_items: list[dict] = []
    try:
      driver = build_driver(headless=True)
      context.user_data["active_parse"]["driver"] = driver
      status_state = {
        "last_page_loading_key": None,
        "last_dom_wait_page": None,
        "last_dom_retry_page": None,
        "last_block_wait_key": None,
        "last_block_detected_key": None,
        "last_filters_retry_key": None,
        # Не спамить про блокировки/IP: не больше 5 сообщений за один запуск парсинга.
        "block_telegram_sent": 0,
        "block_telegram_cap": 5,
      }

      def _notify(payload: dict):
        if stop_event.is_set():
          return
        fut = asyncio.run_coroutine_threadsafe(
          _emit_avito_parse_status(update, payload, stop_event), loop
        )
        try:
          fut.result(timeout=120)
        except Exception as e:
          print(f"[bot] Статус парсинга: {e}")

      _notify({"phase": "driver_ready"})

      def _status_callback(payload: dict):
        ph = payload.get("phase")
        if ph not in ("page_loading", "dom_wait", "page_parsed", "parse_finished"):
          return
        # Снижаем шум: отправляем только ключевые этапы и редкие обновления.
        if ph == "page_loading":
          page = int(payload.get("page") or 1)
          round_no = int(payload.get("block_round") or 1)
          key = (page, round_no)
          # Для обычного хода страницы достаточно первого раунда.
          if round_no > 1:
            return
          if status_state["last_page_loading_key"] == key:
            return
          status_state["last_page_loading_key"] = key
        elif ph == "dom_wait":
          page = int(payload.get("page") or 1)
          if status_state["last_dom_wait_page"] == page:
            return
          status_state["last_dom_wait_page"] = page
        _notify(payload)

      def _recreate_driver():
        nonlocal driver
        try:
          if driver:
            try:
              driver.quit()
            except Exception:
              pass
          driver = build_driver(headless=True)
          context.user_data["active_parse"]["driver"] = driver
          return driver
        except Exception as e:
          print(f"[bot] Ошибка recreate driver: {e}")
          return None

      def _checkpoint_cb(items_snapshot: list[dict]):
        nonlocal partial_items
        try:
          partial_items = list(items_snapshot or [])
        except Exception:
          partial_items = []

      items = parse_avito(
        driver,
        keyword,
        model,
        city,
        price_min,
        price_max,
        precision=precision,
        filters=filters,
        stop_event=stop_event,
        raise_on_block=True,
        today_only=today_only,
        status_callback=_status_callback,
        driver_recreate_callback=_recreate_driver,
        checkpoint_callback=_checkpoint_cb,
      )
      if stop_event.is_set():
        return None
      filepath = export_to_excel(items, filename_prefix="parsing_avito")
      return filepath
    except Exception:
      # Чекпоинт: если парсинг упал, но что-то уже собрано — сохраняем.
      if partial_items:
        try:
          return export_to_excel(partial_items, filename_prefix="parsing_avito_partial")
        except Exception:
          pass
      raise
    finally:
      try:
        if driver:
          # На всякий случай чистим ссылку на драйвер
          if context.user_data.get("active_parse"):
            context.user_data["active_parse"]["driver"] = None
          driver.quit()
      except Exception:
        pass

  # Повторы при капче — внутри parse_avito (до AVITO_BLOCK_MAX_RETRIES_PER_PAGE на каждую страницу).
  filepath = None
  try:
    if not stop_event.is_set():
      filepath = await asyncio.to_thread(_sync_once)
  except Exception as e:
    context.user_data.pop("active_parse", None)
    context.user_data.pop("avito_parse_start_msg_id", None)
    if context.user_data.get("stop_notified"):
      context.user_data.pop("stop_notified", None)
      return
    err_text = str(e)
    hint = ""
    if any(
      x in err_text
      for x in ("Connection refused", "Errno 111", "Max retries exceeded", "Failed to establish a new connection")
    ):
      hint = (
        "\n\nПодсказка: Chrome/драйвер оборвались (часто на 1 GB RAM — OOM). "
        "На сервере: `sudo dmesg | grep -i oom` и добавь swap 1–2 GB."
      )
    await update.message.reply_text(
      f"Ошибка запуска парсинга: {e}{hint}",
      reply_markup=build_main_keyboard(),
    )
    return
  context.user_data.pop("active_parse", None)
  context.user_data.pop("avito_parse_start_msg_id", None)

  # Сообщение об остановке уже отправлено кнопкой. Не дублируем.
  if stop_event.is_set() and context.user_data.get("stop_notified"):
    context.user_data.pop("stop_notified", None)
    return

  if not filepath:
    if stop_event.is_set():
      await update.message.reply_text(
        "Парсинг остановлен пользователем.",
        reply_markup=build_main_keyboard(),
      )
    else:
      await update.message.reply_text(
        "Парсинг завершен, но Excel не сформирован (нет данных).",
        reply_markup=build_main_keyboard(),
      )
    return

  if stop_event.is_set():
    await update.message.reply_text(
      "Парсинг остановлен. Загружаю Excel в БД…",
      reply_markup=build_main_keyboard(),
    )
  else:
    await update.message.reply_text("Парсинг завершен.", reply_markup=build_main_keyboard())
  await asyncio.to_thread(upload_excel_file_to_supabase, supabase, telegram_id, filepath)
  await update.message.reply_text("Excel сохранен ✅ «📄 Excel файлы».", reply_markup=build_main_keyboard())


def get_user_settings(client: Client, telegram_id: int):
  try:
    res = (
      client.table(SUPABASE_SETTINGS_TABLE)
      .select("*")
      .eq("telegram_id", telegram_id)
      .maybe_single()
      .execute()
    )
    return res.data
  except Exception as e:
    print(f"[Supabase] Ошибка get_user_settings: {e}")
    return None


def upsert_user_settings(client: Client, telegram_id: int, settings: dict):
  now_iso = datetime.now(timezone.utc).isoformat()
  payload = {
    "telegram_id": telegram_id,
    "platform": "avito",
    "keyword": settings.get("keyword"),
    "model": settings.get("model"),
    "city": settings.get("city"),
    "price_min": settings.get("price_min"),
    "price_max": settings.get("price_max"),
    "memory": normalize_capacity_values(settings.get("memory") or []),
    "colors": settings.get("colors") or [],
    "seller_type": settings.get("seller_type"),
    "rating_4_plus": settings.get("rating_4_plus"),
    "precision": settings.get("precision") or 7,
    "updated_at": now_iso,
  }
  return client.table(SUPABASE_SETTINGS_TABLE).upsert(payload, on_conflict="telegram_id").execute()


def get_manual_settings(client: Client, telegram_id: int):
  try:
    res = (
      client.table(SUPABASE_MANUAL_SETTINGS_TABLE)
      .select("*")
      .eq("telegram_id", telegram_id)
      .maybe_single()
      .execute()
    )
    return res.data
  except Exception as e:
    print(f"[Supabase] Ошибка get_manual_settings: {e}")
    return None


def merge_manual_settings_with_autoparse(manual: dict | None, auto: dict | None) -> tuple[dict, list]:
  """Ручная строка (bot_manual_settings) в приоритете; пустые поля добираем из bot_settings.

  Раньше автопарсинг читал только bot_settings — там часто полный набор фильтров.
  Ручной запуск мог сохранить неполную строку → фильтры «пропадали». Слияние выравнивает поведение.
  """
  manual = dict(manual or {})
  auto = dict(auto or {})
  filled: list[str] = []
  out = {**auto, **manual}
  for k in ("memory", "colors"):
    if not out.get(k) and auto.get(k):
      out[k] = auto[k]
      filled.append(k)
  for k in ("keyword", "model", "city", "price_min", "price_max", "precision"):
    if out.get(k) in (None, "") and auto.get(k) not in (None, ""):
      out[k] = auto[k]
      filled.append(k)
  if out.get("seller_type") in (None, "") and auto.get("seller_type") not in (None, ""):
    out["seller_type"] = auto["seller_type"]
    filled.append("seller_type")
  if manual:
    out["today_only"] = bool(manual.get("today_only"))
  else:
    out["today_only"] = False
  try:
    out["precision"] = int(out.get("precision") or 7)
  except (TypeError, ValueError):
    out["precision"] = 7
  return out, filled


def bot_settings_to_manual_dict(bot: dict) -> dict:
  """Копия настроек автопарсинга для строки ручного запуска (без today_only)."""
  if not bot:
    return {}
  return {
    "keyword": bot.get("keyword"),
    "model": bot.get("model"),
    "city": bot.get("city"),
    "price_min": bot.get("price_min"),
    "price_max": bot.get("price_max"),
    "memory": normalize_capacity_values(bot.get("memory") or []),
    "ram": [],
    "sim": [],
    "colors": bot.get("colors") or [],
    "condition": [],
    "seller_type": bot.get("seller_type"),
    "rating_4_plus": bot.get("rating_4_plus"),
    "precision": int(bot.get("precision") or 7),
  }


def upsert_manual_settings(client: Client, telegram_id: int, settings: dict, today_only=None):
  """Одна строка на пользователя: полный upsert настроек ручного запуска."""
  now_iso = datetime.now(timezone.utc).isoformat()
  if today_only is not None:
    t = bool(today_only)
  else:
    t = bool(settings.get("today_only"))
  payload = {
    "telegram_id": telegram_id,
    "platform": "avito",
    "keyword": settings.get("keyword"),
    "model": settings.get("model"),
    "city": settings.get("city"),
    "price_min": settings.get("price_min"),
    "price_max": settings.get("price_max"),
    "memory": normalize_capacity_values(settings.get("memory") or []),
    "colors": settings.get("colors") or [],
    "seller_type": settings.get("seller_type"),
    "rating_4_plus": settings.get("rating_4_plus"),
    "precision": int(settings.get("precision") or 7),
    "today_only": t,
    "updated_at": now_iso,
  }
  return client.table(SUPABASE_MANUAL_SETTINGS_TABLE).upsert(payload, on_conflict="telegram_id").execute()


def set_manual_today_only(client: Client, telegram_id: int, today_only: bool):
  """Обновить только флаг today_only в сохранённой строке ручного запуска."""
  now_iso = datetime.now(timezone.utc).isoformat()
  return (
    client.table(SUPABASE_MANUAL_SETTINGS_TABLE)
    .update({"today_only": bool(today_only), "updated_at": now_iso})
    .eq("telegram_id", telegram_id)
    .execute()
  )


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
  try:
    supabase: Client = context.bot_data["supabase_client"]
    upsert_user_to_supabase(supabase, update)
  except Exception as e:
    # Не ломаем бота, если таблица в Supabase еще не создана/неверная схема.
    print(f"[Supabase] Не удалось сохранить пользователя: {e}")

  text = (
    "Привет! 👋\n\n"
    "Я бот для управления вашим парсером объявлений.\n"
    "Здесь можно быстро запускать парсинг и работать с результатами.\n\n"
    "Выберите действие в нижнем меню:\n"
    "🚀 Ручной запуск\n"
    "📘 Инструкция\n"
    "⚙️ Настройки автопарсинга\n"
    "📄 Excel файлы"
  )
  await update.message.reply_text(text, reply_markup=build_main_keyboard())


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
  try:
    supabase: Client = context.bot_data["supabase_client"]
    upsert_user_to_supabase(supabase, update)
  except Exception as e:
    print(f"[Supabase] Не удалось обновить last_seen: {e}")

  user_text = (update.message.text or "").strip()

  # Удаление Excel: ожидаем выбор файла из списка.
  if context.user_data.get("awaiting_excel_delete"):
    if user_text == BTN_CANCEL:
      context.user_data.pop("awaiting_excel_delete", None)
      context.user_data.pop("excel_delete_map", None)
      await update.message.reply_text("Удаление отменено.", reply_markup=build_main_keyboard())
      return
    mapping = context.user_data.get("excel_delete_map") or {}
    row = mapping.get(user_text)
    if not row:
      # fallback: если пользователь отправил только число
      m = re.search(r"\d+", user_text or "")
      if m:
        row = mapping.get(m.group(0))
    if not row:
      labels = [k for k in mapping.keys() if isinstance(k, str) and k.startswith("🗑 ")]
      await update.message.reply_text(
        "Выберите файл кнопкой из списка или нажмите «Отмена».",
        reply_markup=build_excel_delete_keyboard(labels) if labels else build_main_keyboard(),
      )
      return
    supabase: Client = context.bot_data.get("supabase_client")
    telegram_id = update.effective_user.id
    file_id = row.get("id")
    filename = row.get("filename") or "file.xlsx"
    try:
      delete_excel_file_from_supabase(supabase, telegram_id, file_id)
    except Exception as e:
      await update.message.reply_text(f"Ошибка удаления из БД: {e}", reply_markup=build_main_keyboard())
      return
    deleted_disk = _try_delete_excel_from_disk(filename)
    context.user_data.pop("awaiting_excel_delete", None)
    context.user_data.pop("excel_delete_map", None)
    await update.message.reply_text(
      f"Удалено из БД: {filename}\n"
      f"Файл на диске: {'удален' if deleted_disk else 'не найден/не удален'}",
      reply_markup=build_main_keyboard(),
    )
    return

  # Остановка текущего парсинга (если идет)
  active_parse = context.user_data.get("active_parse")
  if _is_stop_parsing_command(user_text):
    if not active_parse:
      # Не уводим в общий fallback «Используйте кнопки внизу.»
      await update.message.reply_text("Остановка уже выполняется или парсинг не запущен.", reply_markup=build_main_keyboard())
      return
    stop_event = active_parse.get("stop_event")
    if stop_event is not None:
      stop_event.set()
    # Жестко прерываем Selenium, чтобы ожидания/скролл оборвались моментально.
    try:
      drv = active_parse.get("driver")
      if drv is not None:
        try:
          drv.quit()
        except Exception:
          pass
    except Exception:
      pass

    context.user_data["stop_notified"] = True
    mid = context.user_data.pop("avito_parse_start_msg_id", None)
    cid = update.effective_chat.id
    stop_text = "⛔ Парсинг остановлен."
    if mid is not None:
      try:
        await context.bot.edit_message_text(
          chat_id=cid,
          message_id=mid,
          text=stop_text,
          reply_markup=build_main_keyboard(),
        )
      except Exception:
        try:
          await update.message.reply_text(stop_text, reply_markup=build_main_keyboard())
        except Exception:
          pass
    else:
      try:
        await update.message.reply_text(stop_text, reply_markup=build_main_keyboard())
      except Exception:
        pass
    return

  # Выбор «только сегодня / все» → колонка today_only в bot_manual_settings, затем парсинг по этой таблице
  if context.user_data.get("awaiting_parse_scope"):
    supabase: Client = context.bot_data.get("supabase_client")
    telegram_id = update.effective_user.id
    if user_text == BTN_CANCEL:
      context.user_data.pop("awaiting_parse_scope", None)
      await update.message.reply_text("Запуск парсинга отменён.", reply_markup=build_main_keyboard())
      return
    if user_text == BTN_PARSE_SCOPE_TODAY:
      context.user_data.pop("awaiting_parse_scope", None)
      if not get_manual_settings(supabase, telegram_id):
        await update.message.reply_text(
          "Нет сохранённых настроек ручного запуска.",
          reply_markup=build_main_keyboard(),
        )
        return
      try:
        set_manual_today_only(supabase, telegram_id, True)
      except Exception as e:
        await update.message.reply_text(f"Ошибка сохранения в Supabase: {e}", reply_markup=build_main_keyboard())
        return
      await run_avito_parsing_and_store(update, context)
      return
    if user_text == BTN_PARSE_SCOPE_ALL:
      context.user_data.pop("awaiting_parse_scope", None)
      if not get_manual_settings(supabase, telegram_id):
        await update.message.reply_text(
          "Нет сохранённых настроек ручного запуска.",
          reply_markup=build_main_keyboard(),
        )
        return
      try:
        set_manual_today_only(supabase, telegram_id, False)
      except Exception as e:
        await update.message.reply_text(f"Ошибка сохранения в Supabase: {e}", reply_markup=build_main_keyboard())
        return
      await run_avito_parsing_and_store(update, context)
      return
    await update.message.reply_text(
      f"Нажмите «{BTN_PARSE_SCOPE_TODAY}», «{BTN_PARSE_SCOPE_ALL}» или «{BTN_CANCEL}».",
      reply_markup=build_parse_scope_keyboard(),
    )
    return

  # Мастер-настройки (без inline-кнопок): хранится в context.user_data
  wizard = context.user_data.get("wizard")
  if wizard:
    state = wizard.get("state")
    draft = wizard.get("draft") or {}

    if user_text == BTN_CANCEL:
      context.user_data.pop("wizard", None)
      await update.message.reply_text("Ок, отменено.", reply_markup=build_main_keyboard())
      return

    supabase: Client = context.bot_data.get("supabase_client")
    telegram_id = update.effective_user.id

    if state == "platform":
      if user_text == BTN_WB:
        await update.message.reply_text(
          "Парсер Wildberries пока в разработке. Используйте Авито.",
          reply_markup=build_main_keyboard(),
        )
        context.user_data.pop("wizard", None)
        return
      if user_text != BTN_AVITO:
        await update.message.reply_text("Нажмите “Авито” или “ВБ”.", reply_markup=build_platform_keyboard())
        return

      settings = get_user_settings(supabase, telegram_id)
      if not settings:
        settings = {
          "keyword": None,
          "model": None,
          "city": None,
          "price_min": None,
          "price_max": None,
          "memory": [],
          "ram": [],
          "sim": [],
          "colors": [],
          "condition": [],
          "seller_type": None,
          "rating_4_plus": None,
          "precision": 7,
        }
      else:
        # Нормализуем старые/дефолтные значения.
        if settings.get("seller_type") == "all":
          settings["seller_type"] = None
        if settings.get("rating_4_plus") is not True:
          settings["rating_4_plus"] = None

      await update.message.reply_text(
        f"{format_settings_for_user(settings)}\n\nНажмите “{BTN_EDIT}”, чтобы изменить настройки.",
        reply_markup=build_edit_keyboard(),
      )
      # Чтобы пользователь мог пропускать шаги через "-", храним текущие настройки
      # и на "Изменить" показываем их как значения по умолчанию.
      wizard["base_settings"] = settings
      wizard["draft"] = dict(settings)
      wizard["state"] = "edit_decision"
      context.user_data["wizard"] = wizard
      return

    if state == "edit_decision":
      if user_text != BTN_EDIT:
        await update.message.reply_text("Ничего не меняем. Ок.", reply_markup=build_main_keyboard())
        context.user_data.pop("wizard", None)
        return
      base = wizard.get("base_settings") or {}
      if not base:
        base = {"rating_4_plus": None, "seller_type": None, "precision": 7}
      # Копируем base в draft, чтобы шаги со знаком "-" могли оставить текущие значения.
      wizard["draft"] = dict(base)
      wizard["state"] = "keyword"
      await update.message.reply_text(
        "Шаг 1/10. Введите Название (например: iPhone, Samsung).\n"
        "Если не нужно менять — вводи '-'.",
        reply_markup=build_cancel_keyboard(),
      )
      return

    def parse_price_int(t):
      s = (t or "").strip().replace(" ", "")
      s = re.sub(r"[^\d]", "", s)
      if not s:
        return None
      return int(s)

    if state == "keyword":
      if user_text == "-":
        if draft.get("keyword"):
          wizard["draft"] = draft
          wizard["state"] = "model"
          await update.message.reply_text(
            "Пропускаем название. Берем текущее значение.\n"
            "Шаг 2/10. Введите модель (например: 17 pro max, galaxy se):",
            reply_markup=build_cancel_keyboard(),
          )
          return
        await update.message.reply_text("Название нельзя пропустить, если его еще нет. Введите значение или используйте “Изменить” позже.", reply_markup=build_cancel_keyboard())
        return
      if not user_text:
        await update.message.reply_text("Введите непустое название.", reply_markup=build_cancel_keyboard())
        return
      draft["keyword"] = user_text
      wizard["draft"] = draft
      wizard["state"] = "model"
      await update.message.reply_text(
        "Шаг 2/10. Введите модель (например: 17 pro max, galaxy se):",
        reply_markup=build_cancel_keyboard(),
      )
      return

    if state == "model":
      if user_text == "-":
        if draft.get("model"):
          wizard["draft"] = draft
          wizard["state"] = "city"
          await update.message.reply_text(
            "Пропускаем модель. Берем текущее значение.\n"
            "Шаг 3/10. Введите город (например: Самара):",
            reply_markup=build_cancel_keyboard(),
          )
          return
        await update.message.reply_text("Модель нельзя пропустить, если ее еще нет. Введите значение или используйте “Изменить” позже.", reply_markup=build_cancel_keyboard())
        return
      if not user_text:
        await update.message.reply_text("Введите непустую модель.", reply_markup=build_cancel_keyboard())
        return
      draft["model"] = user_text
      wizard["draft"] = draft
      wizard["state"] = "city"
      await update.message.reply_text(
        "Шаг 3/10. Введите город (например: Самара):",
        reply_markup=build_cancel_keyboard(),
      )
      return

    if state == "city":
      if user_text == "-":
        if draft.get("city"):
          wizard["draft"] = draft
          wizard["state"] = "price_min"
          await update.message.reply_text(
            "Пропускаем город. Берем текущее значение.\n"
            "Шаг 4/10. Цена от (число, пример: 15000):",
            reply_markup=build_cancel_keyboard(),
          )
          return
        await update.message.reply_text("Город нельзя пропустить, если его еще нет. Введите значение.", reply_markup=build_cancel_keyboard())
        return
      if not user_text:
        await update.message.reply_text("Введите непустой город.", reply_markup=build_cancel_keyboard())
        return
      draft["city"] = user_text
      wizard["draft"] = draft
      wizard["state"] = "price_min"
      await update.message.reply_text(
        "Шаг 4/10. Цена от (число, пример: 15000):",
        reply_markup=build_cancel_keyboard(),
      )
      return

    if state == "price_min":
      if user_text == "-":
        draft["price_min"] = None
        wizard["draft"] = draft
        wizard["state"] = "price_max"
        await update.message.reply_text(
          "Пропускаем цену от. Берем текущее значение (если есть) или None.\n"
          "Шаг 5/10. Цена до (число, пример: 35000):",
          reply_markup=build_cancel_keyboard(),
        )
        return
      v = parse_price_int(user_text)
      if v is None:
        await update.message.reply_text("Введите цену от числом. Пример: 15000", reply_markup=build_cancel_keyboard())
        return
      draft["price_min"] = v
      wizard["draft"] = draft
      wizard["state"] = "price_max"
      await update.message.reply_text(
        "Шаг 5/10. Цена до (число, пример: 35000):",
        reply_markup=build_cancel_keyboard(),
      )
      return

    if state == "price_max":
      if user_text == "-":
        draft["price_max"] = None
        wizard["draft"] = draft
        wizard["state"] = "memory"
        await update.message.reply_text(
          "Пропускаем цену до. Берем None.\n"
          "Шаг 6/10. Память (через запятую, пример: 128 ГБ,256 ГБ):",
          reply_markup=build_cancel_keyboard(),
        )
        return
      v = parse_price_int(user_text)
      if v is None:
        await update.message.reply_text("Введите цену до числом. Пример: 35000", reply_markup=build_cancel_keyboard())
        return
      if draft.get("price_min") is not None and v < draft["price_min"]:
        await update.message.reply_text("Цена до должна быть >= цене от.", reply_markup=build_cancel_keyboard())
        return
      draft["price_max"] = v
      wizard["draft"] = draft
      wizard["state"] = "memory"
      await update.message.reply_text(
        "Шаг 6/10. Память (через запятую, пример: 128 ГБ,256 ГБ):",
        reply_markup=build_cancel_keyboard(),
      )
      return

    if state == "memory":
      if user_text == "-":
        draft["memory"] = []
        wizard["draft"] = draft
        wizard["state"] = "colors"
        await update.message.reply_text(
          "Пропускаем память. Фильтр не применяется.\n"
          "Шаг 7/10. Цвета (пример: Синий,Белый,Черный):",
          reply_markup=build_cancel_keyboard(),
        )
        return
      items = parse_csv_list(user_text)
      if not items:
        await update.message.reply_text("Введите хотя бы одно значение памяти. Пример: 128 ГБ,256 ГБ", reply_markup=build_cancel_keyboard())
        return
      draft["memory"] = normalize_capacity_values(items)
      wizard["draft"] = draft
      wizard["state"] = "colors"
      await update.message.reply_text(
        "Шаг 7/10. Цвета (пример: Синий,Белый,Черный):",
        reply_markup=build_cancel_keyboard(),
      )
      return

    if state == "colors":
      if user_text == "-":
        draft["colors"] = []
        wizard["draft"] = draft
        wizard["state"] = "seller_type"
        await update.message.reply_text(
          "Пропускаем цвета. Фильтр не применяется.\n"
          "Шаг 8/10. Продавцы: all / private / company (или Все / Частные / Компании) (или '-' по умолчанию):",
          reply_markup=build_cancel_keyboard(),
        )
        return
      items = parse_csv_list(user_text)
      if not items:
        await update.message.reply_text("Введите хотя бы один цвет. Пример: Синий,Белый", reply_markup=build_cancel_keyboard())
        return
      draft["colors"] = items
      wizard["draft"] = draft
      wizard["state"] = "seller_type"
      await update.message.reply_text(
        "Шаг 8/10. Продавцы: all / private / company (или Все / Частные / Компании) (или '-' по умолчанию):",
        reply_markup=build_cancel_keyboard(),
      )
      return

    if state == "ram":
      if user_text == "-":
        draft["ram"] = []
        wizard["draft"] = draft
        wizard["state"] = "sim"
        await update.message.reply_text(
          "Пропускаем оперативную память. Фильтр не применяется.\n"
          "Шаг 8/14. SIM (пример: SIM + eSIM,2 SIM,1 SIM,eSIM):",
          reply_markup=build_cancel_keyboard(),
        )
        return
      items = parse_csv_list(user_text)
      if not items:
        await update.message.reply_text("Введите хотя бы одно значение оперативной памяти. Пример: 4 ГБ,6 ГБ", reply_markup=build_cancel_keyboard())
        return
      draft["ram"] = normalize_capacity_values(items)
      wizard["draft"] = draft
      wizard["state"] = "sim"
      await update.message.reply_text(
        "Шаг 8/14. SIM (пример: SIM + eSIM,2 SIM,1 SIM,eSIM):",
        reply_markup=build_cancel_keyboard(),
      )
      return

    if state == "sim":
      if user_text == "-":
        draft["sim"] = []
        wizard["draft"] = draft
        wizard["state"] = "colors"
        await update.message.reply_text(
          "Пропускаем SIM. Фильтр не применяется.\n"
          "Шаг 9/14. Цвета (пример: Белый,Зелёный):",
          reply_markup=build_cancel_keyboard(),
        )
        return
      items = parse_csv_list(user_text)
      if not items:
        await update.message.reply_text("Введите хотя бы одно значение SIM. Пример: SIM + eSIM,2 SIM", reply_markup=build_cancel_keyboard())
        return
      draft["sim"] = items
      wizard["draft"] = draft
      wizard["state"] = "colors"
      await update.message.reply_text(
        "Шаг 9/14. Цвета (пример: Белый,Зелёный):",
        reply_markup=build_cancel_keyboard(),
      )
      return

    if state == "colors":
      if user_text == "-":
        draft["colors"] = []
        wizard["draft"] = draft
        wizard["state"] = "condition"
        await update.message.reply_text(
          "Пропускаем цвета. Фильтр не применяется.\n"
          "Шаг 10/14. Состояние (пример: Отличное,Хорошее):",
          reply_markup=build_cancel_keyboard(),
        )
        return
      items = parse_csv_list(user_text)
      if not items:
        await update.message.reply_text("Введите хотя бы один цвет. Пример: Белый,Зелёный", reply_markup=build_cancel_keyboard())
        return
      draft["colors"] = items
      wizard["draft"] = draft
      wizard["state"] = "condition"
      await update.message.reply_text(
        "Шаг 10/14. Состояние (пример: Отличное,Хорошее):",
        reply_markup=build_cancel_keyboard(),
      )
      return

    if state == "condition":
      if user_text == "-":
        draft["condition"] = []
        wizard["draft"] = draft
        wizard["state"] = "seller_type"
        await update.message.reply_text(
          "Пропускаем состояние. Фильтр не применяется.\n"
          "Шаг 11/14. Продавцы: all / private / company (или Все / Частные / Компании) (или '-' по умолчанию):",
          reply_markup=build_cancel_keyboard(),
        )
        return
      items = parse_csv_list(user_text)
      if not items:
        await update.message.reply_text("Введите хотя бы одно состояние. Пример: Отличное,Хорошее", reply_markup=build_cancel_keyboard())
        return
      draft["condition"] = items
      wizard["draft"] = draft
      wizard["state"] = "seller_type"
      await update.message.reply_text(
        "Шаг 11/14. Продавцы: all / private / company (или Все / Частные / Компании) (или '-' по умолчанию):",
        reply_markup=build_cancel_keyboard(),
      )
      return

    if state == "seller_type":
      if user_text == "-":
        draft["seller_type"] = draft.get("seller_type")
        wizard["draft"] = draft
        wizard["state"] = "rating_4_plus"
        await update.message.reply_text(
          "Пропускаем продавцов. Берем текущее значение.\n"
          "Шаг 9/10. Только 4 звезды и выше? y/n (или '-' по умолчанию):",
          reply_markup=build_cancel_keyboard(),
        )
        return
      v = normalize_seller_type(user_text)
      if not v:
        await update.message.reply_text("Не понял. Введите all/private/company или Все/Частные/Компании.", reply_markup=build_cancel_keyboard())
        return
      draft["seller_type"] = None if v == "all" else v
      wizard["draft"] = draft
      wizard["state"] = "rating_4_plus"
      await update.message.reply_text(
        "Шаг 9/10. Только 4 звезды и выше? y/n (или '-' по умолчанию):",
        reply_markup=build_cancel_keyboard(),
      )
      return

    if state == "rating_4_plus":
      if user_text == "-":
        draft["rating_4_plus"] = draft.get("rating_4_plus")
        wizard["draft"] = draft
        wizard["state"] = "precision"
        await update.message.reply_text(
          "Пропускаем 4 звезды и выше. Берем текущее значение.\n"
          "Шаг 10/10. Точность парсинга (1–10):",
          reply_markup=build_cancel_keyboard(),
        )
        return
      s = (user_text or "").strip().lower()
      if s not in ("y", "yes", "да", "n", "no", "нет"):
        await update.message.reply_text("Введите y/n (да/нет). Пример: y", reply_markup=build_cancel_keyboard())
        return
      draft["rating_4_plus"] = True if s in ("y", "yes", "да") else None
      wizard["draft"] = draft
      wizard["state"] = "precision"
      await update.message.reply_text(
        "Шаг 10/10. Точность парсинга (1–10):",
        reply_markup=build_cancel_keyboard(),
      )
      return

    if state == "precision":
      persist = wizard.get("persist_settings", True)
      if user_text == "-":
        draft["precision"] = int(draft.get("precision") or 7)
        wizard["draft"] = draft
        # Финал: сохраняем в Supabase только если persist_settings=True
        if persist:
          try:
            upsert_user_settings(supabase, telegram_id, draft)
          except Exception as e:
            await update.message.reply_text(
              f"Ошибка сохранения в Supabase: {e}",
              reply_markup=build_main_keyboard(),
            )
            context.user_data.pop("wizard", None)
            return
        await update.message.reply_text(
          "Готово! Настройки сохранены ✅\n\n" + format_settings_for_user(draft)
          if persist
          else "Готово! Настройки применены для текущего запуска.\n\n" + format_settings_for_user(draft),
          reply_markup=build_main_keyboard(),
        )
        context.user_data.pop("wizard", None)
        # Если настройка была запрошена из ручного режима — сохраняем в bot_manual_settings и спрашиваем «сегодня».
        if context.user_data.get("after_wizard_action") == "run_avito":
          context.user_data.pop("after_wizard_action", None)
          try:
            upsert_manual_settings(supabase, telegram_id, draft, today_only=False)
          except Exception as e:
            await update.message.reply_text(
              f"Ошибка сохранения настроек ручного запуска: {e}",
              reply_markup=build_main_keyboard(),
            )
            return
          await ask_parse_scope_before_run(update, context)
          return
        return
      try:
        v = int(user_text.strip())
      except Exception:
        v = None
      if v is None or v < 1 or v > 10:
        await update.message.reply_text("Введите число 1–10.", reply_markup=build_cancel_keyboard())
        return
      draft["precision"] = v
      # Финал: сохраняем в Supabase только если persist_settings=True
      if persist:
        try:
          upsert_user_settings(supabase, telegram_id, draft)
        except Exception as e:
          await update.message.reply_text(
            f"Ошибка сохранения в Supabase: {e}",
            reply_markup=build_main_keyboard(),
          )
          context.user_data.pop("wizard", None)
          return

      await update.message.reply_text(
        "Готово! Настройки сохранены ✅\n\n" + format_settings_for_user(draft)
        if persist
        else "Готово! Настройки применены для текущего запуска.\n\n" + format_settings_for_user(draft),
        reply_markup=build_main_keyboard(),
      )
      context.user_data.pop("wizard", None)

      # Если настройка была запрошена из ручного режима — сохраняем в bot_manual_settings и спрашиваем «сегодня».
      if context.user_data.get("after_wizard_action") == "run_avito":
        context.user_data.pop("after_wizard_action", None)
        try:
          upsert_manual_settings(supabase, telegram_id, draft, today_only=False)
        except Exception as e:
          await update.message.reply_text(
            f"Ошибка сохранения настроек ручного запуска: {e}",
            reply_markup=build_main_keyboard(),
          )
          return
        await ask_parse_scope_before_run(update, context)
        return

      return

    await update.message.reply_text("Неизвестный шаг. Нажмите “Отмена”.", reply_markup=build_cancel_keyboard())
    return

  # Если мы не в режиме настройки — управляем ручным меню и Excel
  manual = context.user_data.get("manual")
  if manual:
    state = manual.get("state")
    if user_text == BTN_CANCEL:
      context.user_data.pop("manual", None)
      await update.message.reply_text("Ок, отменено.", reply_markup=build_main_keyboard())
      return

    supabase: Client = context.bot_data.get("supabase_client")
    telegram_id = update.effective_user.id

    if state == "platform":
      if user_text == BTN_WB:
        await update.message.reply_text(
          "Парсер Wildberries пока в разработке.",
          reply_markup=build_main_keyboard(),
        )
        context.user_data.pop("manual", None)
        return
      if user_text != BTN_AVITO:
        await update.message.reply_text("Нажмите “Авито” или “ВБ”.", reply_markup=build_platform_keyboard())
        return
      context.user_data["manual"] = {"state": "avito_menu"}
      await update.message.reply_text(
        "Авито: выберите режим:",
        reply_markup=build_manual_avito_keyboard(),
      )
      return

    if state == "avito_menu":
      if user_text == BTN_MANUAL_AVITO_MY:
        settings = get_user_settings(supabase, telegram_id)
        if not settings:
          await update.message.reply_text(
            "Настройки не найдены. Выберите “Задать вручную”.",
            reply_markup=build_manual_avito_keyboard(),
          )
          return
        manual_src = bot_settings_to_manual_dict(settings)
        try:
          upsert_manual_settings(supabase, telegram_id, manual_src, today_only=False)
        except Exception as e:
          await update.message.reply_text(
            f"Ошибка сохранения настроек ручного запуска: {e}",
            reply_markup=build_manual_avito_keyboard(),
          )
          return
        context.user_data.pop("manual", None)
        await ask_parse_scope_before_run(update, context)
        return
      if user_text == BTN_MANUAL_AVITO_MANUAL:
        base = get_user_settings(supabase, telegram_id) or {}
        if base.get("seller_type") == "all":
          base["seller_type"] = None
        if base.get("rating_4_plus") is not True:
          base["rating_4_plus"] = None
        draft = {
          "keyword": base.get("keyword"),
          "model": base.get("model"),
          "city": base.get("city"),
          "price_min": base.get("price_min"),
          "price_max": base.get("price_max"),
          "memory": normalize_capacity_values(base.get("memory") or []),
          "ram": normalize_capacity_values(base.get("ram") or []),
          "sim": base.get("sim") or [],
          "colors": base.get("colors") or [],
          "condition": base.get("condition") or [],
          "seller_type": base.get("seller_type"),
          "rating_4_plus": base.get("rating_4_plus"),
          "precision": int(base.get("precision") or 7),
        }
        context.user_data.pop("manual", None)
        context.user_data["after_wizard_action"] = "run_avito"
        # Стартуем мастер для разового запуска на базе текущих настроек пользователя.
        # Поэтому '-' на шагах действительно означает "оставить текущее значение".
        context.user_data["wizard"] = {
          "persist_settings": False,
          "state": "keyword",
          "draft": draft,
        }
        await update.message.reply_text(
          "Шаг 1/10. Введите Название (например: iPhone, Samsung).\n"
          "Если не нужно менять — вводи '-'.",
          reply_markup=build_cancel_keyboard(),
        )
        return
      await update.message.reply_text("Нажмите одну из кнопок режима.", reply_markup=build_manual_avito_keyboard())
      return

  # Если мы не в режиме ручного меню
  if user_text == BTN_AUTO_SETTINGS:
    context.user_data["wizard"] = {"state": "platform", "draft": {}}
    await update.message.reply_text(
      "Выберите площадку для автопарсинга:",
      reply_markup=build_platform_keyboard(),
    )
    return

  if user_text == BTN_EXCEL:
    await update.message.reply_text(
      "Управление Excel файлами:",
      reply_markup=build_excel_menu_keyboard(),
    )
    return

  if user_text == BTN_EXCEL_SHOW:
    await send_excel_files_from_supabase(update, context)
    await update.message.reply_text("Готово.", reply_markup=build_main_keyboard())
    return

  if user_text == BTN_EXCEL_DELETE:
    await ask_excel_file_to_delete(update, context)
    return

  if user_text == BTN_MANUAL_RUN:
    context.user_data["manual"] = {"state": "platform"}
    await update.message.reply_text(
      "Ручной парсинг. Выберите площадку:",
      reply_markup=build_platform_keyboard(),
    )
    return

  if user_text == BTN_HELP:
    await update.message.reply_text("Все работает", reply_markup=build_main_keyboard())
    return

  await update.message.reply_text(
    "Используйте кнопки внизу.",
    reply_markup=build_main_keyboard(),
  )


def main():
  # Если Telegram медленный/частично недоступен (особенно в РФ),
  # то на старте бот не должен крашиться — он будет повторять bootstrap.
  retry_delay_seconds = 20
  while True:
    try:
      app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        # Длиннее таймауты для медленных подключений к Telegram.
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(10)
        .build()
      )
      app.bot_data["supabase_client"] = build_supabase_client()
      app.add_handler(CommandHandler("start", start_handler))
      app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))
      print("Telegram bot started...")
      app.run_polling(drop_pending_updates=True, bootstrap_retries=10)
      return
    except (TimedOut, NetworkError) as e:
      print(f"[Telegram] Недоступен/медленный канал: {e}. Повтор через {retry_delay_seconds}s...")
      time.sleep(retry_delay_seconds)
      continue


if __name__ == "__main__":
  main()
