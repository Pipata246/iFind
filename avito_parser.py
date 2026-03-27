import json
import random
import re
import time
from time import sleep
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse, urlunparse
from urllib.request import ProxyHandler, build_opener

from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from browser_helpers import wait_for_document_ready
from config import (
  AVITO_BASE_URL,
  AVITO_BLOCK_MAX_RETRIES_PER_PAGE,
  AVITO_COOLDOWN_AFTER_NEW_IP_SEC,
  AVITO_DOM_RELOAD_TRIES,
  AVITO_DOM_WAIT_FILTERS_FIRST,
  AVITO_DOM_WAIT_FILTERS_NEXT,
  AVITO_DOM_WAIT_SHELL_FIRST,
  AVITO_DOM_WAIT_SHELL_NEXT,
  AVITO_DRIVER_RECYCLE_AFTER_ERRORS,
  AVITO_DRIVER_RECYCLE_AFTER_PAGES,
  AVITO_ENTER_THROTTLE_MAX_SEC,
  AVITO_ENTER_THROTTLE_MIN_SEC,
  AVITO_MAX_PAGES_PER_RUN,
  AVITO_PAGE_LOAD_MAX_RETRIES,
  AVITO_PAGES_BATCH_SIZE,
  AVITO_RETRY_BACKOFF_BASE_SEC,
  AVITO_RETRY_BACKOFF_MAX_SEC,
  AVITO_RETRY_JITTER_SEC,
  AVITO_WAIT_NEW_IP_POLL_SEC,
  AVITO_WAIT_NEW_IP_TIMEOUT_SEC,
  DOCUMENT_READY_TIMEOUT,
  EXPLICIT_WAIT,
  IMPLICIT_WAIT,
  MOBILE_PROXY_HOST,
  MOBILE_PROXY_PASS,
  MOBILE_PROXY_PORT,
  MOBILE_PROXY_USER,
  USE_MOBILE_PROXY,
  VPS_LIGHT_MODE,
)


class AvitoBlockedError(RuntimeError):
  pass


# Перебор сотен элементов с elem.is_displayed() даёт тысячи round-trip к драйверу (10+ минут тишины в логах).
_QUICK_FILTER_COLLECT_CAP = 220
_QUICK_FILTER_SCAN_CAP = 80
_ROOT_CANDIDATES_CAP = 90

# Сегмент пути сразу после .../mobilnye_telefony/apple/ (как в рабочей выдаче Avito).
# Хвост ASgBAg* — каталожный идентификатор линейки; при новых моделях добавьте ключ из URL на сайте.
AVITO_IPHONE_APPLE_SEGMENTS: dict[str, str] = {
  "iphone_15": "iphone_15-ASgBAgICA0SywA2SoO0RtMANzqs5sMENiPw3",
}


def _normalize_iphone_model_path_key(keyword: str, model: str) -> str | None:
  """iphone_15, iphone_15_pro_max, … для подстановки в каталожный URL /apple/…"""
  raw = f"{keyword or ''} {model or ''}".lower()
  raw = raw.replace("ё", "е").replace("айфон", "iphone")
  if "iphone" not in raw:
    return None
  if re.search(r"iphone\s*se\b", raw) or re.search(r"\bse\s*\(?20\d{2}", raw):
    return "iphone_se"
  m = re.search(r"iphone\D{0,12}(\d{1,2})\b", raw)
  if not m:
    return None
  n = m.group(1)
  rest = raw[m.end() : m.end() + 28]
  suf = ""
  if re.match(r"^\s*(pro\s*max|pro max)\b", rest):
    suf = "_pro_max"
  elif re.match(r"^\s*pro\b", rest):
    suf = "_pro"
  elif re.match(r"^\s*plus\b", rest):
    suf = "_plus"
  elif re.match(r"^\s*mini\b", rest):
    suf = "_mini"
  return f"iphone_{n}{suf}"


def _resolve_avito_iphone_apple_segment(keyword: str, model: str) -> str | None:
  key = _normalize_iphone_model_path_key(keyword, model)
  if not key:
    return None
  seg = AVITO_IPHONE_APPLE_SEGMENTS.get(key)
  if seg:
    return seg
  bare = re.sub(r"_(pro_max|pro|plus|mini)$", "", key)
  if bare != key:
    return AVITO_IPHONE_APPLE_SEGMENTS.get(bare)
  return None


def _filters_to_excel_meta(
  filters,
  *,
  applied_mode: str = "",
  ui_applied_note: str = "",
  today_only: bool = False,
  price_min=None,
  price_max=None,
):
  """Запрос пользователя + как реально отобрали (быстрый режим по выдаче)."""
  filters = filters or {}
  return {
    "avito_filter_price_min": price_min if price_min is not None else "",
    "avito_filter_price_max": price_max if price_max is not None else "",
    "avito_filter_today_only": "yes" if today_only else "no",
    "avito_filter_memory": ", ".join(filters.get("memory", [])),
    "avito_filter_colors": ", ".join(filters.get("colors", [])),
    "avito_filter_seller_type": (filters.get("seller_type") or "all"),
    "avito_filter_rating_4_plus": "yes" if filters.get("rating_4_plus") else "no",
    "avito_filter_applied_mode": applied_mode or "",
    "avito_ui_applied_note": ui_applied_note or "",
  }


def _item_search_blob(item: dict) -> str:
  parts = [item.get("title") or "", item.get("description") or "", item.get("url") or "", item.get("seller_text") or ""]
  return " ".join(parts).lower()


def _matches_memory_from_listing(item: dict, values) -> bool:
  if not values:
    return True
  blob = _item_search_blob(item)
  return _text_matches_capacity(blob, values)


def _text_matches_capacity(blob: str, values) -> bool:
  b = blob.replace("\u00a0", " ").lower()
  b_compact = re.sub(r"\s+", "", b)
  for v in values or []:
    d = re.sub(r"\D", "", str(v))
    if not d or len(d) > 4:
      continue
    if re.search(rf"\b{d}\s*(гб|gb)\b", b, re.I):
      return True
    if re.search(rf"{d}(гб|gb)", b_compact, re.I):
      return True
  return False


def _text_matches_sim(blob: str, values) -> bool:
  b = blob.replace("\u00a0", " ").lower()
  for v in values or []:
    s = str(v).lower()
    if re.search(r"\b1\b", s) and "sim" in s:
      if re.search(r"(\b1\s*sim\b|1\s*sim|nano|esim|одна\s*sim|one\s*sim|1\s*нано)", b, re.I):
        return True
    if re.search(r"\b2\b", s) and "sim" in s:
      if re.search(r"(\b2\s*sim\b|2\s*sim|dual|две\s*sim)", b, re.I):
        return True
    if "sim" in s and len(s) < 22:
      if re.sub(r"\s+", "", s) in re.sub(r"\s+", "", b):
        return True
  return False


def _text_matches_color(blob: str, values) -> bool:
  b = (blob or "").lower().replace("ё", "е")
  b_compact = re.sub(r"[^a-zа-я0-9]+", "", b)
  color_groups = {
    "blue": ["синий", "син", "blue", "blu", "siniy", "sinij", "ciniy", "golub", "голуб", "deepblue", "pacificblue"],
    "black": ["черный", "черн", "black", "blk", "cherniy", "chernyi", "grafit", "graphite"],
    "white": ["белый", "бел", "white", "silver", "серебрист", "serebro", "starlight"],
    "green": ["зеленый", "зелен", "green", "grn", "zelen", "mint", "teal"],
    "red": ["красный", "красн", "red", "korall", "coral", "burgundy"],
    "gold": ["золот", "gold", "desert", "champagne", "песоч", "titandesert"],
    "purple": ["фиолет", "purple", "violet", "lilac", "lavender"],
    "gray": ["серый", "сер", "gray", "grey", "spacegray", "spaceblack", "titanium"],
    "pink": ["розов", "pink", "rose"],
    "yellow": ["желт", "yellow"],
  }
  for v in values or []:
    raw = (v or "").strip().lower().replace("ё", "е")
    if not raw:
      continue
    raw_compact = re.sub(r"[^a-zа-я0-9]+", "", raw)
    if raw in b or (raw_compact and raw_compact in b_compact):
      return True
    for aliases in color_groups.values():
      if any(a in raw or a in raw_compact for a in aliases):
        if any(a in b or a in b_compact for a in aliases):
          return True
  return False


def _text_matches_condition(blob: str, values) -> bool:
  b = blob.lower()
  for v in values or []:
    t = (v or "").strip().lower()
    if t and t in b:
      return True
  return False


def _need_text_fallback(ui_applied: dict, filters: dict) -> bool:
  if not filters:
    return False
  u = ui_applied or {}
  # Если Avito не "зафиксировал" фильтры в выдаче (часто это видно по URL),
  # клики в UI могут оказаться ложноположительными — включаем text post-filter.
  if not bool(u.get("_url_committed", True)) and _has_meaningful_avito_ui_filters(filters or {}):
    return True
  if filters.get("memory") and u.get("memory", 0) == 0:
    return True
  if filters.get("ram") and u.get("ram", 0) == 0:
    return True
  if filters.get("sim") and u.get("sim", 0) == 0:
    return True
  if filters.get("colors") and u.get("colors", 0) == 0:
    return True
  if filters.get("condition") and u.get("condition", 0) == 0:
    return True
  st = str(filters.get("seller_type") or "all").lower()
  if st != "all" and u.get("seller_type", 0) == 0:
    return True
  if filters.get("rating_4_plus") and u.get("rating_4_plus", 0) == 0:
    return True
  return False


def _post_filter_avito_items_by_text(items: list, filters: dict, ui_applied: dict) -> list:
  u = ui_applied or {}
  force_text_filters = not bool(u.get("_url_committed", True))
  out = []
  for item in items:
    blob = _item_search_blob(item)
    ok = True
    if (filters.get("memory") or []) and (force_text_filters or u.get("memory", 0) == 0):
      ok = ok and _text_matches_capacity(blob, filters.get("memory"))
    if (filters.get("ram") or []) and (force_text_filters or u.get("ram", 0) == 0):
      ok = ok and _text_matches_capacity(blob, filters.get("ram"))
    if (filters.get("sim") or []) and (force_text_filters or u.get("sim", 0) == 0):
      ok = ok and _text_matches_sim(blob, filters.get("sim"))
    if (filters.get("colors") or []) and (force_text_filters or u.get("colors", 0) == 0):
      ok = ok and _text_matches_color(blob, filters.get("colors"))
    if (filters.get("condition") or []) and (force_text_filters or u.get("condition", 0) == 0):
      ok = ok and _text_matches_condition(blob, filters.get("condition"))
    st = str(filters.get("seller_type") or "all").lower()
    if st == "private" and (force_text_filters or u.get("seller_type", 0) == 0):
      ok = ok and ("частн" in blob or "private" in blob or "личн" in blob)
    if st == "company" and (force_text_filters or u.get("seller_type", 0) == 0):
      ok = ok and ("компани" in blob or "магазин" in blob or "shop" in blob)
    if ok:
      out.append(item)
  return out


def _enrich_items_with_filter_meta(items, filter_meta):
  for item in items:
    item.update(filter_meta)
  return items


def _city_to_slug(city):
  """Преобразует название города в slug для Avito URL (например, 'Самара' -> 'samara')."""
  if not city:
    return ""
  text = city.strip().lower().replace("ё", "е")
  translit = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
  }
  out = []
  for ch in text:
    if ch in translit:
      out.append(translit[ch])
    elif ch.isalnum():
      out.append(ch)
    else:
      out.append("-")
  slug = "".join(out)
  while "--" in slug:
    slug = slug.replace("--", "-")
  return slug.strip("-")


def _latin_slug(text: str) -> str:
  """Простой slug в латинице для model/color сегментов URL."""
  if not text:
    return ""
  src = str(text).strip().lower().replace("ё", "е")
  translit = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
  }
  out = []
  for ch in src:
    if ch in translit:
      out.append(translit[ch])
    elif ch.isalnum():
      out.append(ch)
    elif ch in (" ", "-", "_", "+"):
      out.append("_")
    else:
      out.append("_")
  slug = "".join(out)
  while "__" in slug:
    slug = slug.replace("__", "_")
  return slug.strip("_")


def _color_to_path_slug(color: str) -> str:
  raw = (color or "").strip().lower().replace("ё", "е")
  if not raw:
    return ""
  if "зелен" in raw:
    return "zelenyy"
  if "черн" in raw:
    return "chernyy"
  if "бел" in raw:
    return "belyy"
  if "син" in raw:
    return "siniy"
  if "голуб" in raw:
    return "goluboy"
  if "желт" in raw:
    return "zheltyy"
  if "розов" in raw:
    return "rozovyy"
  if "красн" in raw:
    return "krasnyy"
  if "фиолет" in raw:
    return "fioletovyy"
  return _latin_slug(raw)


def _precision_params(precision):
  """Поэтапно: 10 = очень медленно (обход лимитов), 1 = быстрее. Все задержки увеличены."""
  max_pages_map = [1, 2, 3, 5, 7, 10, 20, 50, 100, 999]
  max_pages = max_pages_map[min(precision - 1, 9)]
  scroll_passes = max(0, round((precision / 10) * 15))
  # Длинные паузы: скролл как человек (2–4 сек между прокрутками), между страницами 15–45 сек
  scroll_delay = 1.5 + (precision / 10) * 2.5
  page_delay = 15.0 + (precision / 10) * 30.0
  load_delay = 5.0 + (precision / 10) * 5.0
  if VPS_LIGHT_MODE:
    # Lower CPU/RAM pressure and overall runtime for weak VPS.
    scroll_passes = max(0, round(scroll_passes * 0.45))
    scroll_delay = max(0.7, scroll_delay * 0.45)
    page_delay = max(4.0, page_delay * 0.25)
    load_delay = max(2.0, load_delay * 0.55)
  return {"max_pages": max_pages, "scroll_passes": scroll_passes, "scroll_delay": scroll_delay, "page_delay": page_delay, "load_delay": load_delay}


def _sleep_with_stop(stop_event, seconds: float, step: float = 0.25):
  """Сон, который прерывается, как только stop_event установлен."""
  if stop_event is None:
    sleep(seconds)
    return
  end = time.time() + float(seconds)
  while True:
    if stop_event.is_set():
      return
    remaining = end - time.time()
    if remaining <= 0:
      return
    sleep(min(step, remaining))


_LAST_AVITO_ENTER_AT = 0.0


def _throttle_avito_enter(stop_event=None):
  """Запрет на слишком частые заходы на Avito в рамках одного процесса."""
  global _LAST_AVITO_ENTER_AT
  min_gap = random.uniform(float(AVITO_ENTER_THROTTLE_MIN_SEC), float(AVITO_ENTER_THROTTLE_MAX_SEC))
  now = time.time()
  wait_sec = (_LAST_AVITO_ENTER_AT + min_gap) - now
  if wait_sec > 0:
    print(f"[AVITO] THROTTLE: пауза {int(wait_sec)} сек перед новым заходом.")
    _sleep_with_stop(stop_event, wait_sec)
  _LAST_AVITO_ENTER_AT = time.time()


def _classify_open_exception(exc: BaseException) -> str:
  """Классификация ошибки загрузки страницы: timeout / transport / other."""
  if isinstance(exc, TimeoutException):
    return "timeout"
  err = str(exc).lower()
  if any(
    x in err
    for x in (
      "connection",
      "reset",
      "10054",
      "econnreset",
      "tcp",
      "refused",
      "broken pipe",
      "tunnel",
      "proxy",
      "net::",
      "err_",
      "target frame detached",
      "no such window",
      "invalid session",
    )
  ):
    return "transport"
  return "other"


def _retry_backoff_sleep(attempt: int, stop_event, reason: str) -> None:
  exp = float(AVITO_RETRY_BACKOFF_BASE_SEC) * (2 ** max(0, attempt - 1))
  delay = min(float(AVITO_RETRY_BACKOFF_MAX_SEC), exp) + random.uniform(0.0, float(AVITO_RETRY_JITTER_SEC))
  print(f"[AVITO] Backoff ({reason}, attempt {attempt}): {delay:.1f} sec")
  _sleep_with_stop(stop_event, delay)


def _call_checkpoint_callback(cb, items_snapshot: list, *, page: int, total_items: int):
  if not cb:
    return
  try:
    cb(items_snapshot, page=page, total_items=total_items)
  except TypeError:
    cb(items_snapshot)


def _get_public_ip_via_proxy(timeout_sec: float = 20.0) -> str:
  """Получаем внешний IP; при включенном мобильном прокси — через его же цепочку (httpbin / ipify)."""
  targets = [
    "https://api.ipify.org?format=json",
    "https://httpbin.org/ip",
    "https://api64.ipify.org?format=json",
  ]
  opener = None
  if USE_MOBILE_PROXY and MOBILE_PROXY_HOST and MOBILE_PROXY_PORT:
    auth = ""
    if MOBILE_PROXY_USER:
      user = quote(str(MOBILE_PROXY_USER), safe="")
      pwd = quote(str(MOBILE_PROXY_PASS or ""), safe="")
      auth = f"{user}:{pwd}@"
    proxy_url = f"http://{auth}{MOBILE_PROXY_HOST}:{MOBILE_PROXY_PORT}"
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
  for target in targets:
    try:
      resp = opener.open(target, timeout=timeout_sec) if opener else build_opener().open(target, timeout=timeout_sec)
      raw = resp.read().decode("utf-8", errors="ignore").strip()
      if not raw:
        continue
      payload = json.loads(raw)
      ip = (payload.get("origin") or payload.get("ip") or "").strip()
      # httpbin может вернуть "ip1, ip2"
      if "," in ip:
        ip = ip.split(",")[0].strip()
      if ip:
        return ip
    except Exception:
      continue
  return ""


def wait_for_new_ip(previous_ip: str, stop_event=None) -> str:
  """Ждём фактическую смену IP у мобильного прокси (проверка через тот же прокси, что и браузер)."""
  print("[AVITO] WAITING FOR NEW IP")
  deadline = time.time() + float(AVITO_WAIT_NEW_IP_TIMEOUT_SEC)
  prev = (previous_ip or "").strip()
  while time.time() < deadline:
    if stop_event is not None and stop_event.is_set():
      return ""
    now_ip = _get_public_ip_via_proxy()
    if now_ip and now_ip != prev:
      print(f"[AVITO] NEW IP DETECTED: {now_ip}")
      cooldown = random.uniform(float(AVITO_COOLDOWN_AFTER_NEW_IP_SEC), float(AVITO_COOLDOWN_AFTER_NEW_IP_SEC) + 8.0)
      if cooldown > 0:
        print(f"[AVITO] После смены IP охлаждение {int(cooldown)} сек…")
        _sleep_with_stop(stop_event, cooldown)
      return now_ip
    _sleep_with_stop(stop_event, float(AVITO_WAIT_NEW_IP_POLL_SEC))
  print(
    "[AVITO] WAITING FOR NEW IP: timeout — fallback, продолжаю с текущей сетью "
    f"(лимит {AVITO_WAIT_NEW_IP_TIMEOUT_SEC} сек)."
  )
  return ""


def _reset_avito_session_artifacts(driver):
  """Сброс следов сессии между раундами: куки + local/session storage."""
  try:
    driver.delete_all_cookies()
  except Exception:
    pass
  try:
    driver.execute_script(
      """
      try { localStorage.clear(); } catch (e) {}
      try { sessionStorage.clear(); } catch (e) {}
      """
    )
  except Exception:
    pass


def _try_accept_avito_consent(driver) -> bool:
  """Пытаемся принять cookie/consent баннер (если он всплывает на главной/поиске).

  Возвращает True если клик вероятно выполнен.
  """
  try:
    # Ставим short implicit wait, чтобы find/click не зависали.
    try:
      driver.implicitly_wait(0)
    except Exception:
      pass

    return bool(
      driver.execute_script(
        r"""
        try {
          var texts = [
            'принять', 'принять все', 'согласен', 'согласиться', 'согласие',
            'разрешить', 'разрешить все', 'ok', 'оk', 'ок', 'понял', 'понятно', 'продолжить',
            'cookies', 'cookie', 'cоглас', 'согласитесь'
          ];
          function norm(s){ return (s||'').toString().toLowerCase().replace(/\s+/g,' ').trim(); }
          function visible(el){
            if(!el || !el.getBoundingClientRect) return false;
            var r = el.getBoundingClientRect();
            if (r.width < 50 || r.height < 20) return false;
            if (el.offsetParent === null) return false;
            var st = window.getComputedStyle(el);
            if (st && (st.display==='none' || st.visibility==='hidden' || st.opacity==='0')) return false;
            return true;
          }
          var nodes = document.querySelectorAll('button, a, [role="button"], span, div');
          for (var i=0;i<nodes.length;i++){
            var el = nodes[i];
            if (!visible(el)) continue;
            var t = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
            if (!t) continue;
            // Делаем матчи более безопасными: только если есть явные ключи согласия.
            var hasConsent = false;
            var hasAnyText = false;
            for (var j=0;j<texts.length;j++){
              if (!texts[j]) continue;
              if (t.indexOf(texts[j]) !== -1) { hasAnyText = true; }
              if (texts[j] === 'принять' && t.indexOf('принять') !== -1) hasConsent = true;
              if (texts[j] === 'согласен' && t.indexOf('соглас') !== -1) hasConsent = true;
              if (texts[j] === 'ок' && (t === 'ок' || t.indexOf('ок') !== -1)) hasConsent = true;
              if (texts[j] === 'понятно' && t.indexOf('понятно') !== -1) hasConsent = true;
              if (texts[j] === 'разрешить' && t.indexOf('разреш') !== -1) hasConsent = true;
            }
            // Если есть только "cookies" без кнопки согласия — не трогаем.
            if (!hasConsent) continue;
            if (!hasAnyText) continue;
            try { el.scrollIntoView({block:'center', inline:'nearest'}); } catch(e1){}
            try { el.click(); return true; } catch(e2){}
          }
          return false;
        } catch (e) { return false; }
        """
      )
    )
  except Exception:
    return False


def _open_avito_with_soft_entry(driver, target_url: str, stop_event=None, include_home=False, reset_session=True):
  """Мягкий вход: обычно city -> category -> search (home только при необходимости)."""
  if reset_session and include_home:
    print("[AVITO] Мягкий вход: сброс сессии → главная Avito → город → категория → выдача.")
  elif reset_session:
    print("[AVITO] Мягкий вход: сброс сессии → город → категория → выдача (без главной).")
  elif include_home:
    print("[AVITO] Мягкий вход: главная Avito → город → категория → выдача.")
  else:
    print("[AVITO] Мягкий вход: город → категория → выдача (без сброса сессии).")
  if reset_session:
    _reset_avito_session_artifacts(driver)
  parsed = urlparse(target_url or "")
  city_slug = ""
  try:
    parts = [p for p in (parsed.path or "").split("/") if p]
    if parts:
      city_slug = parts[0]
  except Exception:
    city_slug = ""

  steps = [AVITO_BASE_URL] if include_home else []
  if city_slug:
    steps.append(f"{AVITO_BASE_URL}/{city_slug}")
    # «Голая» выдача /mobilnye_telefony без /apple/... в ряде городов даёт 404 — пропускаем, если цель уже apple/….
    tgt = target_url or ""
    if "/apple/" not in tgt:
      steps.append(f"{AVITO_BASE_URL}/{city_slug}/telefony/mobilnye_telefony")
  steps.append(target_url)

  seen = set()
  ordered_steps = []
  for u in [x for x in steps if x]:
    if u in seen:
      continue
    seen.add(u)
    ordered_steps.append(u)

  step_ready_timeout = min(45, int(DOCUMENT_READY_TIMEOUT))
  for idx, u in enumerate(ordered_steps):
    opened = False
    # Каждый шаг входа пробуем ограниченно, чтобы не зависать на одном URL.
    is_home_step = include_home and (u.rstrip("/") == (AVITO_BASE_URL or "").rstrip("/"))
    max_step_tries = 2
    for step_try in range(1, max_step_tries + 1):
      try:
        _sleep_with_stop(stop_event, random.uniform(0.25, 0.95))
        driver.get(u)
        if not wait_for_document_ready(driver, step_ready_timeout, stop_event):
          raise TimeoutException("document.readyState не достиг готовности")

        # Если открылся home — часто есть cookie/consent баннер, он мешает дальнейшим кликам.
        if is_home_step:
          _try_accept_avito_consent(driver)
          sleep(0.6)

        opened = True
        break
      except Exception as e:
        if step_try >= max_step_tries:
          raise

        print(
          f"[AVITO] Шаг мягкого входа не открылся ({u}) попытка {step_try}/{max_step_tries}: {e}"
        )
        # Лёгкий fallback: если шаг (не home) упал — возвращаемся на главную и пробуем снова.
        # Это полезно, когда SPA/сервер отдаёт разные редиректы.
        try:
          if not is_home_step:
            driver.get(AVITO_BASE_URL)
            wait_for_document_ready(driver, step_ready_timeout, stop_event)
            if include_home:
              _try_accept_avito_consent(driver)
              sleep(0.6)
        except Exception:
          pass

        # Подождём перед повтором: при ECONNRESET бывает "вторая попытка" успешнее.
        _sleep_with_stop(stop_event, random.uniform(3.0, 7.0))

    if not opened:
      raise TimeoutException(f"Не удалось открыть шаг мягкого входа: {u}")

    # Мини-скролл + пауза как у человека перед следующим шагом.
    try:
      driver.execute_script("window.scrollTo(0, Math.min(600, document.body.scrollHeight));")
    except Exception:
      pass
    if idx < len(ordered_steps) - 1:
      _sleep_with_stop(stop_event, random.uniform(2.4, 5.8))


def _avito_listing_shell_present(driver) -> bool:
  """Проверка, что в DOM появилась выдача или колонка фильтров (несколько вариантов вёрстки)."""
  try:
    if driver.find_elements(
      By.CSS_SELECTOR,
      ",".join(
        [
          "a[data-marker='item-title']",
          "[data-marker='item']",
          "[data-marker*='filter']",
          "[data-marker='catalog-serp']",
          "[class*='iva-item-root']",
          "[class*='iva-item-content']",
          "[class*='items-items']",
          "[class*='serp-item']",
        ]
      ),
    ):
      return True
  except Exception:
    pass
  try:
    return bool(
      driver.execute_script(
        """
        if (document.querySelector('a[data-marker="item-title"]')) return true;
        if (document.querySelector('[class*="iva-item-root"]')) return true;
        if (document.querySelector('[class*="iva-item-content"]')) return true;
        // Иногда data-marker почти нет, но выдача уже отрисована текстом.
        var txt = ((document.body && document.body.innerText) || '').toLowerCase();
        if (txt.indexOf('сортировка') !== -1 && txt.indexOf('уведомлять о новых') !== -1) return true;
        if (txt.indexOf('выбраны фильтры') !== -1 && txt.indexOf('iphone') !== -1) return true;
        if (txt.indexOf('мобильные телефоны') !== -1 && txt.indexOf('главная') !== -1) return true;
        var links = document.querySelectorAll('a[href*="/item/"]');
        return links.length >= 2;
        """
      )
    )
  except Exception:
    return False


def _looks_like_avito_home_or_service_page(driver) -> bool:
  """Похоже, что открылась главная/сервисная страница, а не поисковая выдача."""
  try:
    return bool(
      driver.execute_script(
        """
        try {
          var href = String(location.href || '').toLowerCase();
          var path = String(location.pathname || '').toLowerCase();
          var title = String(document.title || '').toLowerCase();
          var txt = ((document.body && document.body.innerText) || '').toLowerCase();
          var hasSearchCards = !!document.querySelector("a[data-marker='item-title'], [data-marker='catalog-serp']");
          if (hasSearchCards) return false;
          // Главная/каталог без выдачи.
          var looksHome = (
            title.indexOf('объявления на сайте авито') !== -1 ||
            txt.indexOf('каталог автомобилей') !== -1 ||
            txt.indexOf('разместить объявление') !== -1
          );
          // Сервисные окна/техработы.
          var looksService = (
            txt.indexOf('планово обновлять сайт') !== -1 ||
            txt.indexOf('сервисы могут работать чуть медленнее') !== -1
          );
          // Для поисковой страницы обычно есть более глубокий путь или q/f/pmin-параметры.
          var hasSearchQuery = (href.indexOf('?q=') !== -1 || href.indexOf('&q=') !== -1 || href.indexOf('?f=') !== -1 || href.indexOf('&f=') !== -1 || href.indexOf('pmin=') !== -1 || href.indexOf('pmax=') !== -1);
          var deepPath = path.split('/').filter(Boolean).length >= 3;
          if ((looksHome || looksService) && !hasSearchQuery && !deepPath) return true;
          return false;
        } catch (e) {
          return false;
        }
        """
      )
    )
  except Exception:
    return False


def _looks_like_avito_not_found_page(driver) -> bool:
  """Явная 404-страница Avito: 'Такой страницы не существует'."""
  try:
    return bool(
      driver.execute_script(
        """
        var t = (document.title || '').toLowerCase();
        var b = ((document.body && document.body.innerText) || '').toLowerCase();
        return (
          b.indexOf('такой страницы не существует') !== -1 ||
          b.indexOf('one of two') !== -1 ||
          t.indexOf('страница не существует') !== -1
        );
        """
      )
    )
  except Exception:
    return False


def _log_avito_empty_page_probe(driver):
  """Если выдачи нет в DOM — короткий срез текста/заголовка (капча, пустая страница, антибот)."""
  try:
    payload = driver.execute_script(
      """
      var t = (document.body && document.body.innerText) ? document.body.innerText : '';
      t = t.replace(/\\s+/g, ' ').trim().slice(0, 420);
      return JSON.stringify({
        title: (document.title || '').slice(0, 140),
        bodySample: t,
        dataMarkerCount: document.querySelectorAll('[data-marker]').length
      });
      """
    )
    print(f"[AVITO][probe] {payload}")
  except Exception as e:
    print(f"[AVITO][probe] ошибка: {e}")


def _wait_for_avito_listing_shell(driver, timeout_sec=45, stop_event=None):
  """Дождаться карточек или блока фильтров (при page_load_strategy=none контент догружается после ready)."""
  deadline = time.monotonic() + float(timeout_sec)
  while time.monotonic() < deadline:
    if stop_event is not None and stop_event.is_set():
      return False
    if _avito_listing_shell_present(driver):
      return True
    sleep(0.35)
  return False


def build_avito_search_url(keyword, model, city, price_min, price_max, page=1, filters=None):
  filters = filters or {}
  kw = (keyword or "").strip().lower()
  mdl = (model or "").strip().lower()
  is_iphone_flow = ("iphone" in kw) or ("iphone" in mdl) or ("apple" in kw)
  city_slug = _city_to_slug(city)
  base = AVITO_BASE_URL
  if city_slug:
    base = f"{AVITO_BASE_URL}/{city_slug}"

  # iPhone: в ссылке только линейка (модель) + pmin/pmax (+ cd). Цвет и прочее — через UI после загрузки.
  if is_iphone_flow:
    segment = _resolve_avito_iphone_apple_segment(keyword, model)
    params = []
    if price_min is not None:
      params.append(("pmin", str(price_min)))
    if price_max is not None:
      params.append(("pmax", str(price_max)))
    if page > 1:
      params.append(("p", str(page)))
    params.append(("cd", "1"))
    if segment:
      path = f"{base}/telefony/mobilnye_telefony/apple/{segment}"
      return f"{path}?{urlencode(params)}"
    q_parts = []
    if keyword:
      q_parts.append(keyword)
    if model:
      q_parts.append(model)
    params_q = []
    if q_parts:
      params_q.append(("q", " ".join(q_parts)))
    if price_min is not None:
      params_q.append(("pmin", str(price_min)))
    if price_max is not None:
      params_q.append(("pmax", str(price_max)))
    if page > 1:
      params_q.append(("p", str(page)))
    params_q.append(("cd", "1"))
    return f"{base}/?{urlencode(params_q)}" if params_q else base

  q_parts = []
  if keyword:
    q_parts.append(keyword)
  if model:
    q_parts.append(model)
  q = " ".join(q_parts) if q_parts else ""

  params = []
  if q:
    params.append(("q", q))
  if price_min is not None:
    params.append(("pmin", str(price_min)))
  if price_max is not None:
    params.append(("pmax", str(price_max)))
  if page > 1:
    params.append(("p", str(page)))
  params.append(("cd", "1"))

  if params:
    return f"{base}/?{urlencode(params)}"
  return base


def _scroll_page(driver, passes, delay, stop_event=None):
  """Скролл по одному шагу с паузой (поэтапно, как человек)."""
  for _ in range(passes):
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    d = random.uniform(delay * 0.8, delay * 1.5)
    _sleep_with_stop(stop_event, d)


def _parse_price_text(price_text):
  if not price_text:
    return None
  digits = re.sub(r"\D", "", price_text)
  return int(digits) if digits else None


def _is_avito_blocked(driver):
  """Проверка типовой блок-страницы Avito.

  Раньше использовали ``body.text``: на тяжёлой выдаче Selenium долго считает видимый текст
  (минуты без новых логов и без прогресса в боте). Берём короткий срез HTML в браузере.
  """
  # Если выдача уже есть, считаем страницу рабочей даже при наличии слова "captcha" в скриптах/ресурсах.
  try:
    if _avito_listing_shell_present(driver):
      return False, ""
  except Exception:
    pass

  try:
    snippet = driver.execute_script(
      "return (document.documentElement && document.documentElement.outerHTML || '')"
      ".slice(0, 120000).toLowerCase();"
    )
  except Exception:
    return False, ""
  if not snippet:
    return False, ""
  # "captcha" часто встречается в js/метриках даже на обычной странице; используем только явные признаки.
  blocked_markers = (
    "капча",
    "подтвердите, что вы не робот",
    "доступ ограничен",
    "проблема с ip",
    "слишком много запросов",
    "подозрительная активность",
    "доступ с вашего ip временно ограничен",
  )
  for marker in blocked_markers:
    if marker in snippet:
      return True, marker
  # Отдельно и строже: captcha только если есть challenge-формы/текст на странице.
  if "captcha" in snippet:
    try:
      hard = driver.execute_script(
        """
        var s = ((document.body && document.body.innerText) || '').toLowerCase();
        var hasText = s.indexOf('captcha') !== -1 || s.indexOf('капча') !== -1 || s.indexOf('не робот') !== -1;
        var hasChallenge = !!document.querySelector('form[action*="captcha"], iframe[src*="captcha"], [id*="captcha"], [class*="captcha"]');
        return !!(hasText || hasChallenge);
        """
      )
      if hard:
        return True, "captcha"
    except Exception:
      pass
  return False, ""


def _detect_avito_transport_issue(driver):
  """Отдельно детектим сетевые/TLS проблемы прокси-цепочки, не смешивая с антибот-блоком."""
  try:
    payload = driver.execute_script(
      """
      var t = (document.title || '').toLowerCase();
      var b = ((document.body && document.body.innerText) || '').toLowerCase();
      var h = ((document.documentElement && document.documentElement.outerHTML) || '').toLowerCase().slice(0, 80000);
      var s = [t, b, h].join(' ');
      return s.slice(0, 60000);
      """
    )
  except Exception:
    return False, ""
  if not payload:
    return False, ""
  markers = (
    "502 bad gateway",
    "this site can’t be reached",
    "this site can't be reached",
    "tlsprotocolexception",
    "ssl handshake error",
    "unexpected eof",
    "econnreset",
    "broken pipe",
    "tcpdisconnect",
    "epipe",
    "syscallerror",
  )
  for m in markers:
    if m in payload:
      return True, m
  return False, ""


def _norm_filter_text(s):
  if s is None:
    return ""
  return (
    str(s)
    .replace("\u00a0", " ")
    .replace("ё", "е")
    .replace("Ё", "Е")
    .strip()
    .lower()
    .replace(" ", "")
  )


def _filter_search_roots(driver):
  """Где искать чекбоксы фильтров. НЕ используем общий `form` — на Avito он часто = вся страница → минуты на обход."""
  roots = []
  for sel in (
    "aside",
    "[role='complementary']",
    "[data-marker*='filter']",
    "[class*='SearchFilters']",
    "[class*='search-filters']",
    "[class*='serp-filters']",
    "[class*='styles-module-sidebar']",
    "[class*='Sidebar']",
    "[class*='Filter']",
  ):
    try:
      # Без лимита find_elements по [class*='filter'] на всей странице — десятки секунд.
      for el in driver.find_elements(By.CSS_SELECTOR, sel)[:25]:
        try:
          if el.is_displayed():
            roots.append(el)
        except Exception:
          continue
    except Exception:
      continue
  seen = set()
  uniq = []
  for r in roots:
    rid = id(r)
    if rid in seen:
      continue
    seen.add(rid)
    uniq.append(r)
  if not uniq:
    try:
      return [driver.find_element(By.TAG_NAME, "body")]
    except Exception:
      return []
  return uniq[:6]


def _quick_filter_clickables(driver, deadline=None):
  """Чипы фильтров: узкие селекторы, ранний выход и лимит времени (иначе 10+ минут на find_elements)."""
  cap = _QUICK_FILTER_COLLECT_CAP
  end = deadline if deadline is not None else time.monotonic() + 5.0
  selectors = (
    "aside label",
    "aside button",
    "aside span",
    "aside div[role]",
    "[role='complementary'] label",
    "[role='complementary'] span",
    "[role='complementary'] button",
    "[class*='serp-filters'] label",
    "[class*='SearchFilters'] label",
    "[data-marker*='filter'] label",
    "[data-marker*='filter'] span",
    "[data-marker*='filter'] button",
    "[class*='SearchFilters'] span",
    "[class*='SearchFilters'] button",
    "[class*='search-filters'] label",
    "[class*='search-filters'] span",
  )
  seen = set()
  out = []
  for sel in selectors:
    if time.monotonic() > end or len(out) >= cap:
      break
    try:
      for el in driver.find_elements(By.CSS_SELECTOR, sel)[:35]:
        if time.monotonic() > end or len(out) >= cap:
          return out
        try:
          rid = id(el)
          if rid in seen:
            continue
          seen.add(rid)
          out.append(el)
        except Exception:
          continue
    except Exception:
      continue
  return out


def _js_expand_collapsed_filters(driver):
  """Раскрыть свёрнутые блоки фильтров (aria-expanded, иначе опции не в DOM / не кликаются)."""
  try:
    driver.execute_script(
      """
      (function(){
        var roots = [];
        var a = document.querySelector('aside');
        if (a) roots.push(a);
        document.querySelectorAll('[role="complementary"]').forEach(function(n){ roots.push(n); });
        document.querySelectorAll('[data-marker*="filter"],[data-marker*="params"]').forEach(function(n){ roots.push(n); });
        document.querySelectorAll('div[class],section[class]').forEach(function(n){
          var c = (n.className && n.className.toString()) || '';
          if (/sidebar|filters|serp-filters|catalog-filters|search-filters/i.test(c)) roots.push(n);
        });
        roots.forEach(function(root){
          if (!root) return;
          root.querySelectorAll('[aria-expanded="false"]').forEach(function(el){
            try {
              var r = el.getBoundingClientRect();
              if (r.width > 2 && r.height > 2) el.click();
            } catch (e) {}
          });
        });
      })();
      """
    )
  except Exception:
    pass


def _click_show_more_filter_options(driver, max_clicks=8):
  """На скринште Avito: «128 ГБ» под ссылкой «Показать ещё» — без клика чекбокса в DOM нет."""
  for _ in range(max_clicks):
    try:
      clicked = driver.execute_script(
        """
        var nodes = document.querySelectorAll('a,button,span,div[role="button"]');
        for (var i = 0; i < nodes.length; i++) {
          var t = (nodes[i].innerText || '').replace(/\\s+/g, ' ').trim();
          if (t !== 'Показать ещё' && t.indexOf('Показать ещё') !== 0) continue;
          var r = nodes[i].getBoundingClientRect();
          if (r.width < 2 || r.height < 2) continue;
          if (r.left > window.innerWidth * 0.55) continue;
          try { nodes[i].click(); return true; } catch (e1) {}
        }
        return false;
        """
      )
    except Exception:
      clicked = False
    if not clicked:
      break
    sleep(0.45)
  sleep(0.25)


def _js_click_filter_option(driver, raw_text: str, mode: str) -> bool:
  """Клик по значению фильтра в колонке (aside / data-marker / левая часть экрана).

  mode: memory | sim | color | rating | text | header
  На новой вёрстке Avito нет стабильного <aside>; ищем в контейнерах и кликаем input/label.
  """
  if not raw_text or not str(raw_text).strip():
    return False
  try:
    return bool(
      driver.execute_script(
        """
        var raw = arguments[0];
        var mode = arguments[1];
        function norm(s){ return (s||'').toLowerCase().replace(/\\s+/g,'').replace(/ё/g,'е'); }
        function collectRoots(){
          var list = [];
          var seen = new Set();
          function add(n){
            if (!n || n.nodeType !== 1 || seen.has(n)) return;
            try {
              var b = n.getBoundingClientRect();
              if (b.width < 30 || b.height < 8) return;
            } catch (e) { return; }
            seen.add(n);
            list.push(n);
          }
          var aside = document.querySelector('aside');
          if (aside) add(aside);
          document.querySelectorAll('[role="complementary"]').forEach(add);
          document.querySelectorAll('[data-marker*="filter"],[data-marker*="params"],[class*="SearchFilters"],[class*="search-filters"],[class*="styles-module-sidebar"]').forEach(add);
          // Новая вёрстка: колонка фильтров — div с Sidebar/sidebar/filters в class (без <aside> и без data-marker*=filter)
          document.querySelectorAll('div[class],section[class]').forEach(function(n){
            var c = (n.className && n.className.toString()) || '';
            if (!/sidebar|filters|serp-filters|catalog-filters|search-filters/i.test(c)) return;
            try {
              var b = n.getBoundingClientRect();
              if (b.left < window.innerWidth * 0.52 && b.width > 70 && b.height > 100) add(n);
            } catch (e2) {}
          });
          if (list.length === 0) {
            document.querySelectorAll('section,div').forEach(function(n){
              var dm = n.getAttribute('data-marker') || '';
              if (dm.indexOf('filter') !== -1 || dm.indexOf('params') !== -1) add(n);
            });
          }
          // Крайний fallback: ищем по всему body (новая вёрстка может быть без явного контейнера фильтров).
          if (list.length === 0 && document.body) add(document.body);
          return list;
        }
        function inFilterColumn(el){
          if (!el || !el.getBoundingClientRect) return false;
          if (document.querySelector('aside') && document.querySelector('aside').contains(el)) return true;
          var p = el;
          for (var d = 0; d < 14 && p; d++) {
            var cls = (p.className && p.className.toString()) || '';
            if (/sidebar|filters|serp-filters|catalog-filters|search-filters/i.test(cls)) return true;
            p = p.parentElement;
          }
          var r = el.getBoundingClientRect();
          if (r.width < 2 || r.height < 2) return false;
          return r.left < window.innerWidth * 0.78;
        }
        function clickSmart(el){
          if (!el) return false;
          try { el.scrollIntoView({block:'center', behavior:'instant'}); } catch (e1) {}
          var requireInput = (mode !== 'text' && mode !== 'header');
          var p = el;
          for (var i = 0; i < 10 && p; i++) {
            var inp = p.querySelector && p.querySelector('input[type="checkbox"], input[type="radio"]');
            if (inp) {
              try {
                var before = !!inp.checked;
                // Если уже отмечено — не переключаем в обратную сторону.
                if (before) return true;
                inp.click();
                var after = !!inp.checked;
                if (after) return true;
                // Иногда checked меняется после клика по label.
                try { p.click(); } catch (e2x) {}
                var after2 = !!inp.checked;
                if (after2) return true;
                // Если состояние не стало checked=true — считаем, что фильтр не применился.
              } catch (e2) {}
            }
            p = p.parentElement;
          }
          if (requireInput) return false;
          try { el.click(); return true; } catch (e3) {
            try {
              el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
              return true;
            } catch (e4) {}
          }
          return false;
        }
        function collectNodes(strictColumn){
          var roots = collectRoots();
          var out = [];
          if (!roots.length) return out;
          roots.forEach(function(root){
            root.querySelectorAll('label, li, div[role], span, button, a, p, div').forEach(function(el){
              if (strictColumn !== false && !inFilterColumn(el)) return;
              // У Avito цвет/чипы часто визуальные (текст может отсутствовать),
              // но подпись бывает в aria-label/title.
              var t = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
              if (!t || t.length > 140) return;
              out.push({el: el, t: t});
            });
          });
          return out;
        }
        var nodes = collectNodes(true);
        if (!nodes.length) nodes = collectNodes(false);
        if (!nodes.length) {
          var asOnly = document.querySelector('aside');
          if (asOnly) {
            asOnly.querySelectorAll('label, span, button, div, a').forEach(function(el){
              // У Avito цвет/чипы часто визуальные (текст может отсутствовать),
              // но подпись бывает в aria-label/title.
              var t = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
              if (t && t.length < 140) nodes.push({el: el, t: t});
            });
          }
        }
        if (!nodes.length) return false;
        var needle = norm(raw);
        var dg = String(raw).replace(/\\D/g, '');
        if (mode === 'memory' && dg) {
          for (var i = 0; i < nodes.length; i++) {
            var t = nodes[i].t;
            if (t.indexOf(dg) === -1) continue;
            if (t.indexOf('гб') === -1 && t.indexOf('gb') === -1) continue;
            if (clickSmart(nodes[i].el)) return true;
          }
          for (var j = 0; j < nodes.length; j++) {
            var t2 = nodes[j].t;
            if (t2 === dg || (t2.length <= 14 && t2.indexOf(dg) !== -1 && /гб|gb/.test(t2))) {
              if (clickSmart(nodes[j].el)) return true;
            }
          }
          return false;
        }
        if (mode === 'sim') {
          var want1 = /(^|[^0-9])1([^0-9]|$)/.test(needle);
          var want2 = /(^|[^0-9])2([^0-9]|$)/.test(needle);
          for (var s = 0; s < nodes.length; s++) {
            var ts = nodes[s].t;
            if (ts.indexOf('sim') === -1 && ts.indexOf('сим') === -1 && ts.indexOf('nano') === -1) continue;
            // Строгое соответствие количества SIM (1/2), если оно задано пользователем.
            if (want1) {
              if (!/(^|[^0-9])1([^0-9]|$)/.test(ts)) continue;
              if (ts.indexOf('esim') !== -1 || ts.indexOf('+') !== -1) continue;
            }
            if (want2) {
              if (!/(^|[^0-9])2([^0-9]|$)/.test(ts)) continue;
            }
            if (needle.length >= 2 && (ts.indexOf(needle) !== -1 || needle.indexOf(ts) !== -1)) {
              if (clickSmart(nodes[s].el)) return true;
            }
          }
          for (var s2 = 0; s2 < nodes.length; s2++) {
            var ts2 = nodes[s2].t;
            if (want1) {
              if (!/(^|[^0-9])1([^0-9]|$)/.test(ts2)) continue;
              if (ts2.indexOf('esim') !== -1 || ts2.indexOf('+') !== -1) continue;
            }
            if (want2) {
              if (!/(^|[^0-9])2([^0-9]|$)/.test(ts2)) continue;
            }
            if (ts2.indexOf(needle) !== -1 && ts2.length < 48) {
              if (clickSmart(nodes[s2].el)) return true;
            }
          }
          return false;
        }
        if (mode === 'color') {
          for (var c = 0; c < nodes.length; c++) {
            var tc = nodes[c].t;
            // Иногда aria-label содержит доп. сведения (не только сам цвет).
            if (tc.indexOf(needle) !== -1 && tc.length < 90) {
              if (clickSmart(nodes[c].el)) return true;
            }
          }
          return false;
        }
        if (mode === 'rating') {
          for (var r = 0; r < nodes.length; r++) {
            var tr = nodes[r].t;
            if ((tr.indexOf('звезд') !== -1 || tr.indexOf('рейтинг') !== -1) && tr.indexOf(needle.substring(0, Math.min(needle.length, 12))) !== -1) {
              if (clickSmart(nodes[r].el)) return true;
            }
          }
          for (var r2 = 0; r2 < nodes.length; r2++) {
            if (nodes[r2].t.indexOf(needle) !== -1) {
              if (clickSmart(nodes[r2].el)) return true;
            }
          }
          return false;
        }
        for (var k = 0; k < nodes.length; k++) {
          if (nodes[k].t.indexOf(needle) !== -1) {
            if (clickSmart(nodes[k].el)) return true;
          }
        }
        return false;
        """,
        str(raw_text).strip(),
        mode,
      )
    )
  except Exception:
    return False


def _try_expand_filter_sections(driver, filters=None):
  """Раскрыть секции фильтров (часто память/SIM скрыты до клика по заголовку или «Все фильтры»)."""
  for title in ("Все фильтры", "Ещё фильтры", "Показать все фильтры"):
    if _js_click_filter_option(driver, title, "text"):
      sleep(0.65)
      break
  mem_needed = bool(filters and (filters.get("memory") or filters.get("ram")))
  if mem_needed:
    for title in ("Память", "Встроенная память"):
      _js_click_filter_option(driver, title, "text")
      sleep(0.2)
  try:
    for aside in driver.find_elements(By.CSS_SELECTOR, "aside")[:1]:
      driver.execute_script("arguments[0].scrollTop += 450", aside)
      sleep(0.2)
  except Exception:
    pass


def _click_text_option(driver, text, must_be_checkbox=False, timeout_sec=22, js_mode=None):
  """Клик по одному тексту фильтра. timeout_sec — на весь вызов (не на каждый вариант списка)."""
  del must_be_checkbox
  if not text:
    return False
  return _click_text_option_multi(driver, [text], timeout_sec=timeout_sec, memory_style=False, js_mode=js_mode)


def _click_text_option_multi(driver, texts, timeout_sec=22, memory_style=False, js_mode=None):
  """Один проход по всем строкам-вариантам (128 ГБ, 128, …) — иначе 7×25 с ≈ 3 мин на один фильтр памяти."""
  if not texts:
    return False

  deadline = time.monotonic() + float(timeout_sec)

  def _timed_out():
    return time.monotonic() > deadline

  vseen = set()
  vlist = []
  for text in texts:
    for v in (text, text.replace("+", " + "), text.replace("ё", "е"), text.replace("е", "ё")):
      k = (v or "").strip()
      if not k or k in vseen:
        continue
      vseen.add(k)
      vlist.append(k)

  primary = (texts[0] or "").strip()
  normalized_target = _norm_filter_text(primary).replace("gb", "гб")
  target_digits = re.sub(r"\D", "", normalized_target)

  mode = js_mode
  if not mode:
    mode = "memory" if memory_style else "text"
    if not memory_style:
      joined = " ".join(vlist[:8]).lower()
      if "sim" in joined or "nano" in joined:
        mode = "sim"
      elif any(
        x in joined
        for x in (
          "зел",
          "красн",
          "син",
          "бел",
          "черн",
          "фиолет",
          "розов",
          "золот",
          "серебр",
          "серый",
          "оранж",
        )
      ):
        mode = "color"
      elif "звезд" in joined or "рейтинг" in joined:
        mode = "rating"
  for variant in vlist[:22]:
    if _timed_out():
      return False
    q = (variant or "").strip()
    if len(q) < 2 or len(q) > 90:
      continue
    if _js_click_filter_option(driver, q, mode):
      return True

  quick = _quick_filter_clickables(driver, deadline=deadline)
  for elem in quick[:_QUICK_FILTER_SCAN_CAP]:
    if _timed_out():
      return False
    try:
      if not elem.is_displayed():
        continue
      elem_text = elem.text or ""
      norm_elem = _norm_filter_text(elem_text).replace("gb", "гб")
      if not norm_elem:
        continue
      elem_digits = re.sub(r"\D", "", norm_elem)
      for variant in vlist:
        nt = _norm_filter_text(variant).replace("gb", "гб")
        match = (
          nt == norm_elem
          or nt in norm_elem
          or norm_elem in nt
          or (len(norm_elem) > 2 and (norm_elem in nt or nt in norm_elem))
          or (
            target_digits
            and elem_digits == target_digits
            and ("гб" in nt or "gb" in normalized_target)
          )
          or (
            memory_style
            and target_digits
            and elem_digits == target_digits
            and len(target_digits) >= 2
            and ("гб" in norm_elem or "gb" in norm_elem or len(norm_elem) <= 8)
          )
        )
        if not match:
          continue
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
        sleep(0.08)
        try:
          elem.click()
        except Exception:
          driver.execute_script("arguments[0].click();", elem)
        return True
    except Exception:
      continue

  for variant in vlist:
    if _timed_out():
      return False
    if len(variant) > 100:
      continue
    # XPath: экранируем кавычки в строке
    if "'" not in variant:
      lit = f"'{variant}'"
    else:
      lit = '"' + variant.replace('"', '\\"') + '"'
    # Сначала только в колонке фильтров (aside), без // по всему документу
    xps = [
      f"//aside//label[contains(normalize-space(), {lit})]",
      f"//aside//*[self::span or self::div or self::button][contains(normalize-space(), {lit})]",
      f"//aside//*[@role='checkbox' or @role='switch'][contains(., {lit})]",
      f"//*[contains(@data-marker,'filter')]//label[contains(normalize-space(), {lit})]",
      f"//*[contains(@data-marker,'filter')]//*[self::span or self::div][contains(normalize-space(), {lit})]",
      f"//*[contains(@class,'SearchFilters')]//label[contains(normalize-space(), {lit})]",
      f"//*[contains(@class,'search-filters')]//label[contains(normalize-space(), {lit})]",
      f"//*[contains(@class,'filters')]//span[contains(normalize-space(), {lit})]",
    ]
    for xp in xps:
      if _timed_out():
        return False
      try:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems[:12]:
          if _timed_out():
            return False
          try:
            if not elem.is_displayed():
              continue
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
            sleep(0.08)
            try:
              elem.click()
            except Exception:
              driver.execute_script("arguments[0].click();", elem)
            return True
          except Exception:
            continue
      except Exception:
        continue

  roots = _filter_search_roots(driver)
  for root in roots:
    if _timed_out():
      return False
    try:
      candidates = root.find_elements(
        By.CSS_SELECTOR,
        "label, button, span[role], div[role], span, div[class*='Checkbox'], div[class*='checkbox']",
      )
    except Exception:
      continue
    for elem in candidates[:_ROOT_CANDIDATES_CAP]:
      if _timed_out():
        return False
      try:
        if not elem.is_displayed():
          continue
        elem_text = elem.text or ""
        norm_elem = _norm_filter_text(elem_text).replace("gb", "гб")
        if not norm_elem:
          continue
        elem_digits = re.sub(r"\D", "", norm_elem)
        matched = False
        for variant in vlist:
          nt = _norm_filter_text(variant).replace("gb", "гб")
          if (
            nt == norm_elem
            or nt in norm_elem
            or norm_elem in nt
            or (len(norm_elem) > 2 and (norm_elem in nt or nt in norm_elem))
            or (
              target_digits
              and elem_digits == target_digits
              and ("гб" in nt or "gb" in normalized_target)
            )
            or (
              memory_style
              and target_digits
              and elem_digits == target_digits
              and len(target_digits) >= 2
              and ("гб" in norm_elem or "gb" in norm_elem or len(norm_elem) <= 8)
            )
          ):
            matched = True
            break
        if not matched:
          continue
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
        sleep(0.1)
        try:
          elem.click()
        except Exception:
          driver.execute_script("arguments[0].click();", elem)
        return True
      except Exception:
        continue

  return False


def _capacity_variants(value):
  """Generate robust textual variants for values like 128GB/128 ГБ/128."""
  raw = (value or "").strip()
  if not raw:
    return []
  variants = [raw]
  digits = re.sub(r"\D", "", raw)
  if digits:
    variants.extend(
      [
        digits,
        f"{digits} ГБ",
        f"{digits}\u00a0ГБ",  # неразрывный пробел как на сайте
        f"{digits} Гб",
        f"{digits}гб",
        f"{digits} GB",
        f"{digits}GB",
        f"{digits} Гб.",
      ]
    )
  # keep order, remove duplicates
  out = []
  seen = set()
  for v in variants:
    k = v.strip().lower()
    if not k or k in seen:
      continue
    seen.add(k)
    out.append(v)
  return out


def _uniq_strings(seq):
  out = []
  seen = set()
  for x in seq:
    if not x:
      continue
    k = str(x).strip().lower()
    if not k or k in seen:
      continue
    seen.add(k)
    out.append(x.strip() if isinstance(x, str) else x)
  return out


def _sim_variants(value):
  """Варианты подписи SIM на Avito (часто отличается от ввода пользователя)."""
  raw = (value or "").strip()
  if not raw:
    return []
  variants = [raw, raw.replace("ё", "е"), raw.replace("е", "ё")]
  low = raw.lower().replace("ё", "е")

  wants_1 = bool(re.search(r"\b1\b", low))
  wants_2 = bool(re.search(r"\b2\b", low))

  # Строгий режим: если пользователь просит 1 SIM/2 SIM, не подмешиваем
  # универсальные варианты (SIM, nano-SIM + eSIM), чтобы не кликнуть не то.
  if wants_1:
    variants.extend(
      [
        "1 SIM",
        "1 sim",
        "1SIM",
        "1 SIM-карта",
        "1 nano-SIM",
        "1 nano sim",
        "одна SIM",
        "1 физическая SIM",
      ]
    )
    return _uniq_strings(variants)

  if wants_2:
    variants.extend(
      [
        "2 SIM",
        "2 sim",
        "2SIM",
        "dual SIM",
        "две SIM",
      ]
    )
    return _uniq_strings(variants)

  # Нестрогий режим только если не задано количество SIM.
  variants.extend(
    [
      raw.replace(" ", ""),
      raw.upper(),
      raw.lower(),
      "SIM",
      "sim",
      "nano-SIM + eSIM",
      "eSIM",
    ]
  )
  return _uniq_strings(variants)


def _color_variants(value):
  """Ё/е и типичные варианты названия цвета на Avito."""
  raw = (value or "").strip()
  if not raw:
    return []
  variants = [raw, raw.replace("ё", "е"), raw.replace("е", "ё"), raw.title(), raw.lower(), raw.upper()]
  low = raw.lower().replace("ё", "е")
  if "зел" in low:
    variants.extend(
      [
        "Зелёный",
        "Зеленый",
        "зелёный",
        "зеленый",
        "Зеленый",
        "Green",
      ]
    )
  if "красн" in low:
    variants.extend(["Красный", "красный", "Red"])
  if "черн" in low:
    variants.extend(["Чёрный", "Черный", "черный", "Black"])
  if "бел" in low:
    variants.extend(["Белый", "белый", "White"])
  if "син" in low:
    variants.extend(["Синий", "синий", "Blue"])
  return _uniq_strings(variants)


def _rating_variants():
  return _uniq_strings(
    [
      "4 звезды и выше",
      "4 звезды",
      "4+",
      "от 4 звёзд",
      "от 4 звезд",
      "Рейтинг 4+",
      "4 звезды и выше",
    ]
  )


def _scroll_avito_filters_to_bottom(driver):
  """Кнопка «Показать N объявлений» внизу колонки фильтров — без прокрутки aside её не видно."""
  try:
    driver.execute_script(
      r"""
      (function() {
        var aside = document.querySelector("aside");
        if (aside) {
          aside.scrollTop = aside.scrollHeight + 80;
          try { aside.scrollTo(0, aside.scrollHeight + 80); } catch (e) {}
        }
        document.querySelectorAll("[class*='sidebar'],[class*='Sidebar'],[class*='filter']").forEach(function(el){
          try {
            if (el.scrollHeight > el.clientHeight + 30) el.scrollTop = el.scrollHeight;
          } catch (e) {}
        });
        document.querySelectorAll("div[style*='overflow']").forEach(function(el){
          try {
            var r = el.getBoundingClientRect();
            if (r.width > 100 && r.width < 560 && r.height > 180) el.scrollTop = el.scrollHeight;
          } catch (e) {}
        });
      })();
      """
    )
  except Exception:
    pass
  sleep(0.25)
  try:
    driver.execute_script("window.scrollTo(0, 0);")
  except Exception:
    pass


def _url_filters_fully_committed(url: str, filters) -> bool:
  """Полное применение в URL: localPriority или f= достаточной длины (короткий f= часто только цена)."""
  if not _has_meaningful_avito_ui_filters(filters):
    return True
  try:
    qs = parse_qs(urlparse(url or "").query, keep_blank_values=True)
    if "localPriority" in qs or "localpriority" in (url or "").lower():
      return True
    fv = (qs.get("f") or [""])[0].strip()
    if not fv:
      return False
    n = 0
    for k in ("memory", "ram", "sim", "colors", "condition"):
      n += len([x for x in (filters or {}).get(k) or [] if str(x).strip()])
    if (filters or {}).get("rating_4_plus"):
      n += 1
    if str((filters or {}).get("seller_type") or "all").lower() not in ("all", ""):
      n += 1
    if n == 0:
      return True
    need = 88 + n * 18
    return len(fv) >= min(need, 200)
  except Exception:
    return False


def _requested_ui_filters_satisfied(filters, ui_applied) -> bool:
  """По счётчикам кликов в UI: на каждый запрошенный пункт фильтра был успешный выбор."""
  if not filters:
    return True
  ua = ui_applied or {}
  for key in ("memory", "ram", "sim", "colors", "condition"):
    req = [x for x in (filters.get(key) or []) if str(x).strip()]
    if not req:
      continue
    if int(ua.get(key) or 0) < len(req):
      return False
  if filters.get("rating_4_plus") and int(ua.get("rating_4_plus") or 0) < 1:
    return False
  st = str(filters.get("seller_type") or "all").lower()
  if st != "all" and int(ua.get("seller_type") or 0) < 1:
    return False
  return True


def _wait_and_click_show_results_button(driver, stop_event=None, max_sec=40.0):
  """Ждём появления кнопки под фильтрами: прокрутка + повторные клики."""
  deadline = time.monotonic() + float(max_sec)
  step = 0
  while time.monotonic() < deadline:
    if stop_event is not None and stop_event.is_set():
      return False
    step += 1
    _scroll_avito_filters_to_bottom(driver)
    if step % 3 == 0:
      try:
        driver.execute_script(
          "window.scrollTo(0, Math.min(document.body.scrollHeight, 1200));"
        )
      except Exception:
        pass
    if step % 5 == 0:
      try:
        driver.execute_script(
          r"""
          var xp = "//*[self::button or self::a][contains(., 'Показать')][contains(., 'объяв')]";
          var r = document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
          var n = r.singleNodeValue;
          if (n) n.scrollIntoView({block:'center', inline:'nearest'});
          """
        )
      except Exception:
        pass
    if _click_show_results_button(driver):
      return True
    _sleep_with_stop(stop_event, 0.5)
  return False


def _click_show_results_button(driver):
  """Надежно нажимает кнопку применения фильтров («Показать N объявлений» и варианты).

  Без отключения implicit wait каждый ``find_elements`` по XPath может ждать до IMPLICIT_WAIT
  секунд на несовпадение; 8 xpath × 3 попытки ≈ минуты тишины в логах.
  """
  try:
    driver.implicitly_wait(0)
  except Exception:
    pass
  try:
    xpaths = [
      "//button[contains(normalize-space(.), 'Показать') and contains(normalize-space(.), 'объяв')]",
      "//a[contains(normalize-space(.), 'Показать') and contains(normalize-space(.), 'объяв')]",
      "//*[@role='button' and contains(normalize-space(.), 'Показать') and contains(normalize-space(.), 'объяв')]",
      "//*[contains(@data-marker,'filter') or contains(@data-marker,'submit') or contains(@data-marker,'apply')]"
      "[contains(., 'Показать')][contains(., 'объяв')]",
      "//span[contains(normalize-space(.), 'Показать') and contains(normalize-space(.), 'объяв')]",
      "//div[contains(normalize-space(.), 'Показать') and contains(normalize-space(.), 'объяв')]",
      "//button[contains(., 'Показать') and contains(., 'объяв')]",
      "//a[contains(., 'Показать') and contains(., 'объяв')]",
      "//button[contains(., 'Показать')]",
    ]
    for xpath in xpaths:
      try:
        elems = driver.find_elements(By.XPATH, xpath)
        for elem in elems:
          try:
            if not elem.is_displayed():
              continue
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
            sleep(0.2)
            try:
              elem.click()
            except Exception:
              driver.execute_script("arguments[0].click();", elem)
            return True
          except Exception:
            continue
      except Exception:
        continue

    for css_sel in (
      "aside button",
      "[data-marker*='filter'] button",
      "[data-marker*='serp'] button",
      "[class*='Filters'] button",
      "[class*='filters'] button",
      "div[class*='sticky'] button",
    ):
      try:
        for elem in driver.find_elements(By.CSS_SELECTOR, css_sel)[:45]:
          try:
            txt = (elem.text or "").replace("\n", " ").strip()
            low = txt.lower()
            if "показать" not in low or "объяв" not in low:
              continue
            if not elem.is_displayed():
              continue
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
            sleep(0.18)
            try:
              elem.click()
            except Exception:
              driver.execute_script("arguments[0].click();", elem)
            return True
          except Exception:
            continue
      except Exception:
        continue

    clicked = driver.execute_script(
      r"""
      var show = /показать/i;
      var ads = /объявл/i;
      var showNum = /показать\s+\d+/i;
      function visible(el) {
        if (!el || !el.getBoundingClientRect) return false;
        var r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return false;
        var st = window.getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
        return true;
      }
      function textOf(el) {
        return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
      }
      function tryClick(el) {
        if (!visible(el)) return false;
        try { el.scrollIntoView({block:'center', inline:'nearest'}); } catch (e) {}
        try { el.click(); return true; } catch (e1) {}
        try {
          var ev = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
          el.dispatchEvent(ev);
          return true;
        } catch (e2) {}
        return false;
      }
      var roots = document.querySelectorAll(
        'button, a[href], [role="button"], [data-marker*="filter"], [data-marker*="params"]'
      );
      var i, el, t, p, depth;
      for (i = 0; i < roots.length; i++) {
        el = roots[i];
        t = textOf(el);
        if (t.length < 6 || t.length > 140) continue;
        if ((!show.test(t) || !ads.test(t)) && !(showNum.test(t) && ads.test(t))) continue;
        if (tryClick(el)) return true;
      }
      var all = document.querySelectorAll('button a, button span, a span, div[role="button"] span');
      for (i = 0; i < all.length; i++) {
        el = all[i];
        t = textOf(el);
        if (t.length < 6 || t.length > 140) continue;
        if ((!show.test(t) || !ads.test(t)) && !(showNum.test(t) && ads.test(t))) continue;
        p = el;
        for (depth = 0; depth < 8 && p; depth++) {
          var tag = (p.tagName || '').toLowerCase();
          var role = p.getAttribute && p.getAttribute('role');
          if (tag === 'button' || tag === 'a' || role === 'button') {
            if (tryClick(p)) return true;
            break;
          }
          p = p.parentElement;
        }
      }
      return false;
      """
    )
    return bool(clicked)
  finally:
    try:
      driver.implicitly_wait(IMPLICIT_WAIT)
    except Exception:
      pass


def _log_avito_filters_diagnostics(driver, phase: str):
  """Почему не кликаются фильтры: снимок DOM (без body.text — быстро)."""
  try:
    payload = driver.execute_script(
      r"""
      try {
        var aside = document.querySelector("aside");
        var dm = document.querySelectorAll("[data-marker]");
        var dmF = 0;
        for (var i = 0; i < dm.length; i++) {
          var m = (dm[i].getAttribute("data-marker") || "");
          if (m.indexOf("filter") !== -1 || m.indexOf("params") !== -1) dmF++;
        }
        var inAside = function(sel) {
          if (!aside) return 0;
          return aside.querySelectorAll(sel).length;
        };
        var samples = [];
        if (aside) {
          aside.querySelectorAll("label, span, button, div[role], a").forEach(function(el){
            if (samples.length >= 12) return;
            var t = (el.innerText || "").replace(/\s+/g, " ").trim();
            if (!t || t.length > 90) return;
            if (/гб|gb|sim|сим|цвет|рейтинг|звезд|память|встроен/i.test(t)) samples.push(t.slice(0, 72));
          });
        }
        var ifr = document.querySelectorAll("iframe").length;
        var sb = 0;
        document.querySelectorAll("div[class],section[class]").forEach(function(n){
          var c = (n.className && n.className.toString()) || "";
          if (/sidebar|filters|serp-filters|catalog-filters|search-filters/i.test(c)) sb++;
        });
        var comp = document.querySelectorAll("[role='complementary']").length;
        return JSON.stringify({
          phase: arguments[0],
          innerW: window.innerWidth,
          innerH: window.innerHeight,
          url: String(location.href || "").slice(0, 220),
          aside: !!aside,
          complementary: comp,
          classSidebarLike: sb,
          asideLabels: inAside("label"),
          dataMarkerFilterish: dmF,
          iframeCount: ifr,
          samples: samples
        });
      } catch (e) {
        return JSON.stringify({ phase: arguments[0], error: String(e) });
      }
      """,
      phase,
    )
  except Exception as e:
    print(f"[AVITO][diag:{phase}] execute_script failed: {e}")
    return
  print(f"[AVITO][diag:{phase}] {payload}")


def _collect_selected_filters_from_dom(driver):
  """Собираем реальные выбранные фильтры из DOM.

  Цель: не доверять одному факту клика (бывают ложные True), а проверить наличие checked/selected state.
  """
  try:
    res = driver.execute_script(
      r"""
      try {
        function norm(t){ return (t||'').toString().replace(/\s+/g,' ').trim(); }
        function uniqPush(arr, v, seen){
          if (!v) return;
          v = norm(v);
          if (!v || v.length > 90) return;
          if (seen.has(v)) return;
          seen.add(v);
          arr.push(v);
        }
        var roots = [];
        var a = document.querySelector('aside');
        if (a) roots.push(a);
        document.querySelectorAll('[role="complementary"]').forEach(function(n){ roots.push(n); });
        document.querySelectorAll('[data-marker*="filter"],[data-marker*="params"],[class*="SearchFilters"],[class*="search-filters"],[class*="serp-filters"],[class*="catalog-filters"]').forEach(function(n){ roots.push(n); });
        // Если фильтры в оверлее/дровере — часто нет стабильного container. Добавляем body.
        if (document.body) roots.push(document.body);
        // дедуп roots
        var seenR = new Set();
        var r2 = [];
        for (var i=0;i<roots.length;i++){
          var rr = roots[i];
          if (!rr) continue;
          var id = rr.id || (rr.tagName+":"+rr.className+":"+i);
          if (seenR.has(id)) continue;
          seenR.add(id);
          r2.push(rr);
        }
        roots = r2;

        function collectChecked(doc){
          var checked = [];
          try {
            doc.querySelectorAll('input[type="checkbox"]:checked, input[type="radio"]:checked').forEach(function(inp){ checked.push(inp); });
            doc.querySelectorAll('[aria-selected="true"],[aria-checked="true"],[aria-pressed="true"]').forEach(function(n){ checked.push(n); });
          } catch(e){}
          return checked;
        }

        // 1) собираем из основного документа (самое надежное)
        var checked = collectChecked(document);
        // 2) + если есть same-origin iframes — попробуем и их (часть вёрсток прячет фильтры внутрь iframe)
        try {
          var ifs = document.querySelectorAll('iframe');
          for (var ii=0; ii<ifs.length && ii<6; ii++){
            try {
              var idoc = ifs[ii].contentDocument;
              if (idoc) {
                var more = collectChecked(idoc);
                for (var j=0;j<more.length;j++) checked.push(more[j]);
              }
            } catch(e2){}
          }
        } catch(e3){}

        var out = [];
        var seen = new Set();
        for (var i=0;i<checked.length;i++){
          var el = checked[i];
          if (!el) continue;
          // input -> label
          try {
            if (el.tagName && el.tagName.toLowerCase() === 'input') {
              var id = el.id || '';
              if (id) {
                var lab = document.querySelector('label[for="'+id+'"]');
                if (lab) { uniqPush(out, lab.innerText, seen); continue; }
              }
              var pLab = el.closest ? el.closest('label') : null;
              if (pLab && pLab.innerText) { uniqPush(out, pLab.innerText, seen); continue; }
              if (el.getAttribute('aria-label')) { uniqPush(out, el.getAttribute('aria-label'), seen); continue; }
              if (el.value) { uniqPush(out, el.value, seen); continue; }
            }
          } catch(e){}
          // non-input: take text
          try {
            if (el.innerText) { uniqPush(out, el.innerText, seen); continue; }
            if (el.getAttribute && el.getAttribute('aria-label')) { uniqPush(out, el.getAttribute('aria-label'), seen); continue; }
          } catch(e){}
        }
        // иногда selected отображается чипами без checkbox
        // собираем дополнительно видимые тексты с aria-selected=true
        if (out.length < 3) {
          document.querySelectorAll('[aria-selected="true"],[aria-pressed="true"],[aria-checked="true"]').forEach(function(n){
            try { if (n && n.offsetParent !== null) uniqPush(out, n.innerText, seen); } catch(e){}
          });
        }
        return out;
      } catch(e){
        return [];
      }
      """,
      )
    if not isinstance(res, list):
      return ""
    blob = " | ".join(str(x) for x in res if x)
    return blob
  except Exception:
    return ""


def _wait_for_avito_filters_panel(driver, timeout_sec=45, stop_event=None):
  """Ждём колонку фильтров: иначе клики идут в пустой DOM → «не найден фильтр»."""
  deadline = time.monotonic() + float(timeout_sec)
  while time.monotonic() < deadline:
    if stop_event is not None and stop_event.is_set():
      return False
    try:
      ok = driver.execute_script(
        """
        var a = document.querySelector('aside');
        if (a) {
          var r = a.getBoundingClientRect();
          if (r.height > 50 && r.width > 70) return true;
        }
        var dm = document.querySelector('[data-marker*="filter"],[data-marker*="params"]');
        if (dm) {
          var r2 = dm.getBoundingClientRect();
          if (r2.height > 40) return true;
        }
        var nodes = document.querySelectorAll('div[class],section[class]');
        for (var i = 0; i < nodes.length && i < 500; i++) {
          var c = (nodes[i].className && nodes[i].className.toString()) || '';
          if (!/sidebar|filters|search-filters|serp-filters|catalog-filters|Filter/i.test(c)) continue;
          var b = nodes[i].getBoundingClientRect();
          if (b.height > 85 && b.left < window.innerWidth * 0.58) return true;
        }
        var rc = document.querySelector('[role="complementary"]');
        if (rc && rc.getBoundingClientRect().height > 80) return true;
        // Текстовые маркеры левой колонки (бывают без стабильных data-marker/class).
        var txt = ((document.body && document.body.innerText) || '').toLowerCase();
        // Некоторые вёрстки не содержат точную строку "SIM-карты" / "показать ещё"
        // (возможны пробелы, разные тире и ё/е), поэтому делаем детект мягче.
        if (txt.indexOf('память') !== -1 && (txt.indexOf('sim') !== -1 || txt.indexOf('сим') !== -1)) return true;
        if (txt.indexOf('оперативная память') !== -1 && (txt.indexOf('показать ещё') !== -1 || txt.indexOf('показать еще') !== -1)) return true;
        return false;
        """
      )
      if ok:
        return True
    except Exception:
      pass
    sleep(0.45)
  return False


def _try_open_avito_filters_drawer(driver):
  """На части вёрсток колонка фильтров скрыта за кнопкой «Фильтры» / data-marker."""
  for xp in (
    "//button[contains(normalize-space(),'Фильтры')]",
    "//span[contains(normalize-space(),'Фильтры')]",
    "//a[contains(normalize-space(),'Все параметры')]",
    "//button[contains(normalize-space(),'Все параметры')]",
  ):
    try:
      for el in driver.find_elements(By.XPATH, xp)[:5]:
        try:
          if el.is_displayed():
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            sleep(0.15)
            driver.execute_script("arguments[0].click();", el)
            sleep(1.1)
            print(f"[AVITO] Открытие панели: XPath {xp[:50]}…")
            return True
        except Exception:
          continue
    except Exception:
      continue
  for css in (
    "button[aria-label*='Фильтр']",
    "button[aria-label*='фильтр']",
    "[data-marker*='open-filter']",
    "[data-marker*='filters-button']",
    "[data-marker='catalog-filters']",
    "button[data-marker*='filter']",
    "[data-marker*='Filters']",
  ):
    try:
      for el in driver.find_elements(By.CSS_SELECTOR, css)[:8]:
        try:
          if el.is_displayed():
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            sleep(0.12)
            driver.execute_script("arguments[0].click();", el)
            sleep(1.0)
            print(f"[AVITO] Открытие панели: клик по селектору {css!r}")
            return True
        except Exception:
          continue
    except Exception:
      continue
  clicked = False
  try:
    clicked = bool(
      driver.execute_script(
        """
        var labels = ["Фильтры", "Все фильтры", "Все параметры", "Параметры", "Подобрать", "Настроить поиск", "Фильтр", "Ещё фильтры"];
        var nodes = document.querySelectorAll("button, a, [role='button'], span, div");
        for (var i = 0; i < nodes.length; i++) {
          var el = nodes[i];
          var t = (el.innerText || "").replace(/\\s+/g, " ").trim();
          if (!t || t.length > 48) continue;
          for (var j = 0; j < labels.length; j++) {
            if (t === labels[j] || t.indexOf(labels[j]) === 0) {
              var r = el.getBoundingClientRect();
              if (r.width < 2 || r.height < 2) continue;
              try { el.click(); return true; } catch (e1) {}
              try {
                el.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true, view: window}));
                return true;
              } catch (e2) {}
            }
          }
        }
        return false;
        """
      )
    )
  except Exception:
    clicked = False
  if clicked:
    sleep(1.0)
    print("[AVITO] Открытие панели: клик по кнопке «Фильтры»/«Параметры»/… (по тексту)")
  return clicked


def _scroll_aside_filters_deep(driver):
  """Прокрутка колонки фильтров (aside или div.sidebar) — опции подгружаются после скролла."""
  try:
    driver.execute_script(
      """
      function scrollEl(el){
        if (!el) return;
        for (var i = 0; i < 30; i++) { el.scrollTop += 280; }
      }
      var aside = document.querySelector("aside");
      scrollEl(aside);
      document.querySelectorAll("[role='complementary']").forEach(scrollEl);
      document.querySelectorAll("div[class],section[class]").forEach(function(n){
        var c = (n.className && n.className.toString()) || "";
        if (/sidebar|filters|serp-filters|catalog-filters|search-filters/i.test(c)) {
          try {
            var b = n.getBoundingClientRect();
            if (b.left < window.innerWidth * 0.52 && b.width > 60) scrollEl(n);
          } catch (e) {}
        }
      });
      """
    )
    sleep(0.45)
  except Exception:
    pass


def _build_page_url(base_url, page):
  if not base_url:
    return ""
  parsed = urlparse(base_url)
  query = dict(parse_qsl(parsed.query, keep_blank_values=True))
  if page <= 1:
    query.pop("p", None)
  else:
    query["p"] = str(page)
  new_query = urlencode(query, doseq=True)
  return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


def _normalize_avito_listing_base_url(url: str) -> str:
  """База выдачи: без p= (страница задаётся отдельно)."""
  if not url:
    return url
  try:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q.pop("p", None)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q, doseq=True), p.fragment))
  except Exception:
    return url


def _f_param_length(url: str) -> int:
  try:
    qs = parse_qs(urlparse(url or "").query, keep_blank_values=True)
    return len((qs.get("f") or [""])[0].strip())
  except Exception:
    return 0


def _enrich_listing_base_url_from_dom(driver, current_url: str) -> str:
  """В адресной строке часто нет f=, тогда как в canonical/ссылках пагинации — полный f= (нужен для driver.get на стр.2+)."""
  if _f_param_length(current_url) >= 60:
    return _normalize_avito_listing_base_url(current_url)
  cur_path = ""
  try:
    cur_path = urlparse(current_url or "").path
  except Exception:
    pass
  try:
    href = driver.execute_script(
      """
      var best = '';
      var bestLen = 0;
      var c = document.querySelector('link[rel="canonical"]');
      if (c && c.href && c.href.indexOf('f=') > -1) { best = c.href; }
      if (!best) {
        var nodes = document.querySelectorAll('a[href*="f="]');
        for (var i = 0; i < Math.min(nodes.length, 150); i++) {
          var h = nodes[i].href || '';
          if (h.indexOf('avito.ru') === -1) continue;
          var m = h.match(/[?&]f=([^&]+)/);
          var L = m ? m[1].length : 0;
          if (L > bestLen) { bestLen = L; best = h; }
        }
      }
      return best || '';
      """
    )
    href = (href or "").strip()
    if not href or _f_param_length(href) < 60:
      return current_url
    try:
      hp = urlparse(href).path.rstrip("/")
      cp = (cur_path or "").rstrip("/")
      if cp and hp and hp != cp:
        return current_url
    except Exception:
      pass
    out = _normalize_avito_listing_base_url(href)
    print(f"[AVITO] URL выдачи дополнен из DOM (canonical/ссылка с f=), длина f≈{_f_param_length(out)}.")
    return out
  except Exception:
    return current_url


def _try_click_avito_pagination_page(driver, target_page: int, stop_event=None) -> bool:
  """Переход на страницу N кликом по пагинации — сохраняет SPA-состояние, если GET по «голому» URL сбрасывает фильтры."""
  if target_page <= 1:
    return True
  try:
    driver.implicitly_wait(0)
  except Exception:
    pass
  try:
    clicked = driver.execute_script(
      """
      var want = arguments[0];
      var roots = document.querySelectorAll(
        '[data-marker*="pagination"], nav[aria-label*="Страниц"], [class*="Pagination"], [data-marker="pagination"]'
      );
      var candidates = [];
      for (var r = 0; r < roots.length; r++) {
        var as = roots[r].querySelectorAll('a[href]');
        for (var j = 0; j < as.length; j++) candidates.push(as[j]);
      }
      if (candidates.length < 1) {
        document.querySelectorAll('a[href*="avito.ru"][href*="&p="], a[href*="avito.ru"][href*="?p="]').forEach(
          function(a){ candidates.push(a); }
        );
      }
      function pnum(h) {
        try {
          var u = new URL(h, location.href);
          var p = u.searchParams.get('p');
          return p ? parseInt(p, 10) : 0;
        } catch (e) { return 0; }
      }
      for (var i = 0; i < candidates.length; i++) {
        var a = candidates[i];
        var h = a.getAttribute('href') || '';
        var t = (a.textContent || '').replace(/\\s+/g, ' ').trim();
        if (t === String(want) || pnum(h) === want) {
          try {
            a.scrollIntoView({block:'center', inline:'nearest'});
            a.click();
            return true;
          } catch (e) {}
        }
      }
      return false;
      """,
      int(target_page),
    )
    if not clicked:
      return False
    _sleep_with_stop(stop_event, random.uniform(0.8, 1.6))
    deadline = time.monotonic() + 35.0
    while time.monotonic() < deadline:
      if stop_event is not None and stop_event.is_set():
        return False
      try:
        u = (driver.current_url or "").lower()
        if f"p={target_page}" in u or f"&p={target_page}" in u:
          return True
      except Exception:
        pass
      _sleep_with_stop(stop_event, 0.35)
    return True
  except Exception:
    return False


def _ensure_price_bounds_in_url(url: str, price_min, price_max) -> str:
  """Гарантирует, что pmin/pmax не потеряются после UI-кликов/редиректов Avito."""
  if not url:
    return url
  try:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    if price_min is not None:
      q["pmin"] = str(price_min)
    if price_max is not None:
      q["pmax"] = str(price_max)
    new_query = urlencode(q, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, p.fragment))
  except Exception:
    return url


def _has_filter_signature(url: str) -> bool:
  """UI-фильтры Avito считаем применёнными только при реальном query-параметре f."""
  try:
    p = urlparse(url or "")
    qs = parse_qs(p.query, keep_blank_values=True)
    return "f" in qs and bool((qs.get("f") or [""])[0].strip())
  except Exception:
    return False


def _url_has_expected_price_bounds(url: str, price_min, price_max) -> bool:
  """Проверка, что pmin/pmax присутствуют и совпадают с ожидаемыми (если заданы)."""
  try:
    p = urlparse(url or "")
    qs = parse_qs(p.query, keep_blank_values=True)
    if price_min is not None and str(price_min) != str((qs.get("pmin") or [""])[0]).strip():
      return False
    if price_max is not None and str(price_max) != str((qs.get("pmax") or [""])[0]).strip():
      return False
    return True
  except Exception:
    return False


def _color_path_tokens(value: str):
  """Ожидаемые slug-токены цвета в path URL Avito."""
  raw = (value or "").strip().lower().replace("ё", "е")
  if not raw:
    return []
  tokens = [raw]
  mapping = {
    "зелен": ["zelenyy", "zeleniy", "green"],
    "черн": ["chernyy", "cherniy", "black"],
    "бел": ["belyy", "beliy", "white"],
    "син": ["siniy", "sinij", "blue"],
    "голуб": ["goluboy", "blue"],
    "желт": ["zheltyy", "yellow"],
    "розов": ["rozovyy", "pink"],
    "красн": ["krasnyy", "red"],
    "фиолет": ["fioletovyy", "purple"],
  }
  for k, vals in mapping.items():
    if k in raw:
      tokens.extend(vals)
  out = []
  seen = set()
  for t in tokens:
    tt = (t or "").strip().lower()
    if not tt or tt in seen:
      continue
    seen.add(tt)
    out.append(tt)
  return out


def _url_has_expected_color_path(url: str, colors) -> bool:
  """Если цвет запрошен, проверяем что path URL содержит цветовой slug."""
  if not colors:
    return True
  try:
    path = (urlparse(url or "").path or "").lower()
  except Exception:
    return False
  # Допускаем совпадение любого из запрошенных цветов.
  for c in colors or []:
    for token in _color_path_tokens(str(c)):
      if token and token in path:
        return True
  return False


def _color_filter_accepted(url: str, filters, ui_applied) -> bool:
  """Цвет: slug в path (как у ручной ссылки) ИЛИ реально выбран в UI (часто цвет только в f=, без /zelenyy/)."""
  colors = (filters or {}).get("colors") or []
  if not colors:
    return True
  if _url_has_expected_color_path(url, colors):
    return True
  n_req = len([c for c in colors if str(c).strip()])
  if not n_req:
    return True
  n_ap = int((ui_applied or {}).get("colors") or 0)
  return n_ap >= n_req


def _has_meaningful_avito_ui_filters(filters):
  """Есть ли смысловые фильтры в интерфейсе (не только значения по умолчанию)."""
  if not filters:
    return False
  if filters.get("memory") or filters.get("ram") or filters.get("sim"):
    return True
  if filters.get("colors") or filters.get("condition"):
    return True
  if str(filters.get("seller_type") or "all").lower() != "all":
    return True
  if filters.get("rating_4_plus"):
    return True
  return False


def _describe_applied_mode(filters, ui_applied, text_fallback_ran: bool):
  """Режим для Excel: ui / ui+text / none + короткая сводка по счётчикам UI."""
  if not filters or not _has_meaningful_avito_ui_filters(filters):
    return ("none", "")
  u = ui_applied or {}
  bits = []
  if filters.get("memory"):
    bits.append(f"memory={u.get('memory', 0)}")
  if filters.get("ram"):
    bits.append(f"ram={u.get('ram', 0)}")
  if filters.get("sim"):
    bits.append(f"sim={u.get('sim', 0)}")
  if filters.get("colors"):
    bits.append(f"colors={u.get('colors', 0)}")
  if filters.get("condition"):
    bits.append(f"condition={u.get('condition', 0)}")
  st = str(filters.get("seller_type") or "all").lower()
  if st != "all":
    bits.append(f"seller={u.get('seller_type', 0)}")
  if filters.get("rating_4_plus"):
    bits.append(f"rating4+={u.get('rating_4_plus', 0)}")
  note = "UI: " + ", ".join(bits)
  if text_fallback_ran:
    return ("ui+text", note + " | доп. отбор по тексту карточки (title/URL)")
  return ("ui", note)


def _page_from_href(href: str):
  """Номер страницы только из query-параметра p (не подстрока 'p=' внутри f=)."""
  if not href:
    return None
  try:
    qs = parse_qs(urlparse(href).query)
    if "p" in qs and qs["p"]:
      return int(qs["p"][0])
  except Exception:
    pass
  return None


def _detect_total_pages(driver):
  """Определяет число страниц в блоке пагинации (иначе в f= и в левой колонке ловятся ложные p=)."""
  try:
    n = driver.execute_script(
      """
      var max = 1;
      var roots = document.querySelectorAll(
        '[data-marker*="pagination"], [class*="Pagination"], nav[aria-label*="Страниц"], [class*="pagination"]'
      );
      for (var r = 0; r < roots.length; r++) {
        var nav = roots[r];
        nav.querySelectorAll('a[href]').forEach(function(a){
          try {
            var u = new URL(a.href, location.href);
            var p = u.searchParams.get('p');
            if (p !== null && p !== '') {
              var n = parseInt(p, 10);
              if (!isNaN(n)) max = Math.max(max, n);
            }
          } catch (e) {}
        });
        nav.querySelectorAll('a,button,span').forEach(function(el){
          var t = (el.innerText || '').trim();
          if (/^\\d{1,4}$/.test(t)) {
            var n2 = parseInt(t, 10);
            if (!isNaN(n2) && n2 < 5000) max = Math.max(max, n2);
          }
        });
      }
      return max;
      """
    )
    if isinstance(n, int) and n >= 1:
      return min(n, 500)
  except Exception:
    pass

  max_page = 1
  try:
    page_links = driver.find_elements(
      By.CSS_SELECTOR,
      "[data-marker*='pagination'] a[href], nav[aria-label*='Страниц'] a[href], [class*='Pagination'] a[href]",
    )
  except Exception:
    page_links = []
  if not page_links:
    try:
      page_links = driver.find_elements(By.XPATH, "//a[contains(@href, 'avito.ru') and contains(@href, '&p=')]")
    except Exception:
      page_links = []
  for link in page_links[:80]:
    try:
      text = (link.text or "").strip()
      href = (link.get_attribute("href") or "").strip()
      if text.isdigit() and len(text) <= 4:
        max_page = max(max_page, int(text))
      pn = _page_from_href(href)
      if pn is not None:
        max_page = max(max_page, pn)
    except Exception:
      continue
  return max_page


def _apply_avito_ui_filters(driver, filters, stop_event=None):
  if not filters:
    return {}

  print("[AVITO] Применяю расширенные фильтры в интерфейсе…")
  pre_apply_url = driver.current_url or ""
  print(
    "[AVITO] Запрошенные фильтры: "
    f"memory={filters.get('memory') or []}, "
    f"ram={filters.get('ram') or []}, "
    f"sim={filters.get('sim') or []}, "
    f"colors={filters.get('colors') or []}, "
    f"condition={filters.get('condition') or []}, "
    f"seller_type={filters.get('seller_type') or 'all'}, "
    f"rating_4_plus={bool(filters.get('rating_4_plus'))}"
  )

  print("[AVITO] Жду появления колонки/блока фильтров в DOM (до 45 с)…")
  panel_ok = _wait_for_avito_filters_panel(driver, timeout_sec=45, stop_event=stop_event)
  if not panel_ok:
    print(
      "[AVITO] Колонка фильтров не появилась по таймауту — клики могут не сработать. "
      "Проверьте [AVITO][diag:*] и скорость сети."
    )
    # Попробуем ещё раз открыть панель и быстро перепроверить наличие фильтров в DOM.
    _try_open_avito_filters_drawer(driver)
    sleep(0.8)
    _wait_for_avito_filters_panel(driver, timeout_sec=20, stop_event=stop_event)
  sleep(1.0)

  _log_avito_filters_diagnostics(driver, "before_drawer")
  _try_open_avito_filters_drawer(driver)
  _log_avito_filters_diagnostics(driver, "after_drawer")

  # Дождаться отрисовки и прокрутить к колонке фильтров (левый aside)
  sleep(1.8)
  try:
    driver.execute_script("window.scrollTo(0, 0);")
    for aside in driver.find_elements(By.CSS_SELECTOR, "aside")[:2]:
      try:
        driver.execute_script("arguments[0].scrollIntoView({block:'start'});", aside)
        break
      except Exception:
        continue
  except Exception:
    pass
  sleep(0.5)

  print("[AVITO] Раскрываю секции фильтров (если нужно)…")
  _js_expand_collapsed_filters(driver)
  sleep(0.45)
  _try_expand_filter_sections(driver, filters)
  _scroll_aside_filters_deep(driver)
  if filters.get("memory") or filters.get("ram") or filters.get("sim") or filters.get("colors"):
    print("[AVITO] Раскрываю «Показать ещё» в левой колонке (память/SIM/цвет и т.д.)…")
    _click_show_more_filter_options(driver, max_clicks=10)
  _scroll_aside_filters_deep(driver)
  _log_avito_filters_diagnostics(driver, "after_expand")
  applied = {
    "memory": 0,
    "ram": 0,
    "sim": 0,
    "colors": 0,
    "condition": 0,
    "seller_type": 0,
    "rating_4_plus": 0,
  }

  for value in filters.get("memory", []):
    print(f"[AVITO] Память: ищу «{value}»…")
    if _click_text_option_multi(driver, _capacity_variants(value), timeout_sec=24, memory_style=True):
      applied["memory"] += 1
      print(f"[AVITO] Память: выбрано «{value}»")
      sleep(0.2)
    else:
      print(f"[AVITO] Не найден фильтр памяти: {value}")

  for value in filters.get("ram", []):
    print(f"[AVITO] RAM: ищу «{value}»…")
    if _click_text_option_multi(driver, _capacity_variants(value), timeout_sec=24, memory_style=True):
      applied["ram"] += 1
      print(f"[AVITO] RAM: выбрано «{value}»")
      sleep(0.2)
    else:
      print(f"[AVITO] Не найден фильтр RAM: {value}")

  for value in filters.get("sim", []):
    print(f"[AVITO] SIM: ищу «{value}»…")
    if _click_text_option_multi(driver, _sim_variants(value), timeout_sec=24, memory_style=False):
      applied["sim"] += 1
      print(f"[AVITO] SIM: выбрано «{value}»")
      sleep(0.2)
    else:
      print(f"[AVITO] Не найден фильтр SIM: {value}")

  for value in filters.get("colors", []):
    print(f"[AVITO] Цвет: ищу «{value}»…")
    if _click_text_option_multi(driver, _color_variants(value), timeout_sec=24, memory_style=False):
      applied["colors"] += 1
      print(f"[AVITO] Цвет: выбрано «{value}»")
      sleep(0.2)
    else:
      print(f"[AVITO] Не найден фильтр цвета: {value}")

  for value in filters.get("condition", []):
    print(f"[AVITO] Состояние: ищу «{value}»…")
    if _click_text_option(driver, value, must_be_checkbox=True):
      applied["condition"] += 1
      sleep(0.2)
    else:
      print(f"[AVITO] Не найден фильтр состояния: {value}")

  seller_type = (filters.get("seller_type") or "all").lower()
  seller_label = {"all": "Все", "private": "Частные", "company": "Компании"}.get(seller_type, "Все")
  if seller_type == "all":
    # "Все" обычно состояние по умолчанию; клик не обязателен.
    applied["seller_type"] += 1
  elif _click_text_option(driver, seller_label, must_be_checkbox=False):
    applied["seller_type"] += 1
  else:
    print(f"[AVITO] Не найден фильтр продавца: {seller_label}")

  if filters.get("rating_4_plus"):
    print("[AVITO] Рейтинг: ищу «4 звезды и выше»…")
    if _click_text_option_multi(driver, _rating_variants(), timeout_sec=20, memory_style=False):
      applied["rating_4_plus"] += 1
      print("[AVITO] Рейтинг: применён")
    else:
      print("[AVITO] Не найден фильтр рейтинга: 4 звезды и выше")

  # Пред-проверка: перед кликом "Показать объявления" убеждаемся,
  # что в DOM действительно отмечены нужные UI-фильтры.
  # Это критично для сценария "1 в 1": иногда Avito после части кликов
  # возвращает дефолтные значения (например memory/colors).
  if _has_meaningful_avito_ui_filters(filters):
    def _calc_verified_from_blob(blob: str) -> dict:
      verified = {
        "memory": 0,
        "ram": 0,
        "sim": 0,
        "colors": 0,
        "condition": 0,
        "seller_type": 0,
        "rating_4_plus": 0,
      }
      if not blob:
        return verified
      # memory/ram/capacity
      for v in filters.get("memory", []) or []:
        if _text_matches_capacity(blob, [v]):
          verified["memory"] += 1
      for v in filters.get("ram", []) or []:
        if _text_matches_capacity(blob, [v]):
          verified["ram"] += 1
      for v in filters.get("sim", []) or []:
        if _text_matches_sim(blob, [v]):
          verified["sim"] += 1
      for v in filters.get("colors", []) or []:
        if _text_matches_color(blob, [v]):
          verified["colors"] += 1
      for v in filters.get("condition", []) or []:
        if _text_matches_condition(blob, [v]):
          verified["condition"] += 1

      seller_req = str(filters.get("seller_type") or "all").lower()
      if seller_req == "all":
        verified["seller_type"] = 1
      elif seller_req == "private":
        verified["seller_type"] = (
          1
          if ("частн" in blob.lower() or "private" in blob.lower() or "личн" in blob.lower())
          else 0
        )
      elif seller_req == "company":
        verified["seller_type"] = (
          1
          if ("компан" in blob.lower() or "магазин" in blob.lower() or "company" in blob.lower() or "shop" in blob.lower())
          else 0
        )
      else:
        verified["seller_type"] = 0

      if filters.get("rating_4_plus"):
        sb = blob.lower()
        verified["rating_4_plus"] = 1 if ("звезд" in sb or "rating" in sb) and re.search(r"\b4\b", sb) else 0
      return verified

    # Делаем максимум 2 цикла "если не совпало -> повторить клики недостающего".
    for pre_try in range(1, 3):
      selected_blob_pre = _collect_selected_filters_from_dom(driver)
      if not selected_blob_pre:
        break
      ui_pre = _calc_verified_from_blob(selected_blob_pre)
      print(
        f"[AVITO] Pre-check UI filters (try {pre_try}/2): "
        f"{ui_pre} (selected_blob={selected_blob_pre[:120]!r})"
      )
      # Решение о том, "нужно ли" повторять клики — делаем по факту кликов (applied),
      # а не по selected_blob (он иногда детектит memory/colors неверно).
      if _requested_ui_filters_satisfied(filters or {}, applied or {}):
        break

      # Реприм недостающие группы
      print("[AVITO] UI pre-check не совпал — повторно кликаю недостающие фильтры…")
      _js_expand_collapsed_filters(driver)
      sleep(0.3)
      _try_expand_filter_sections(driver, filters)
      _scroll_aside_filters_deep(driver)
      if filters.get("memory") or filters.get("ram") or filters.get("sim") or filters.get("colors"):
        _click_show_more_filter_options(driver, max_clicks=10)
      _scroll_aside_filters_deep(driver)
      sleep(0.25)

      # capacity
      for value in filters.get("memory", []) or []:
        if ui_pre.get("memory", 0) < len([x for x in filters.get("memory", []) or [] if str(x).strip()]):
          _click_text_option_multi(driver, _capacity_variants(value), timeout_sec=18, memory_style=True)
          sleep(0.15)
      for value in filters.get("ram", []) or []:
        if ui_pre.get("ram", 0) < len([x for x in filters.get("ram", []) or [] if str(x).strip()]):
          _click_text_option_multi(driver, _capacity_variants(value), timeout_sec=18, memory_style=True)
          sleep(0.15)

      # sim/colors/condition
      for value in filters.get("sim", []) or []:
        if ui_pre.get("sim", 0) < len([x for x in filters.get("sim", []) or [] if str(x).strip()]):
          _click_text_option_multi(driver, _sim_variants(value), timeout_sec=18, memory_style=False)
          sleep(0.15)
      for value in filters.get("colors", []) or []:
        if ui_pre.get("colors", 0) < len([x for x in filters.get("colors", []) or [] if str(x).strip()]):
          _click_text_option_multi(driver, _color_variants(value), timeout_sec=18, memory_style=False)
          sleep(0.15)
      for value in filters.get("condition", []) or []:
        if ui_pre.get("condition", 0) < len([x for x in filters.get("condition", []) or [] if str(x).strip()]):
          _click_text_option(driver, value, must_be_checkbox=True)
          sleep(0.15)

      seller_req = str(filters.get("seller_type") or "all").lower()
      if seller_req != "all" and ui_pre.get("seller_type", 0) < 1:
        seller_label = {"private": "Частные", "company": "Компании"}.get(seller_req, "Все")
        _click_text_option(driver, seller_label, must_be_checkbox=False)
        sleep(0.2)

      if filters.get("rating_4_plus") and ui_pre.get("rating_4_plus", 0) < 1:
        _click_text_option_multi(driver, _rating_variants(), timeout_sec=15, memory_style=False)
        sleep(0.2)

  # Кнопка внизу колонки фильтров — без прокрутки и ожидания Avito не фиксирует фильтры в f=.
  _scroll_avito_filters_to_bottom(driver)
  sleep(0.45)
  clicked_show = _wait_and_click_show_results_button(driver, stop_event=stop_event, max_sec=44.0)
  if clicked_show:
    print("[AVITO] Нажал кнопку применения фильтров: «Показать … объявлений».")
  else:
    print(
      "[AVITO] Кнопка «Показать … объявлений» не найдена за отведённое время. "
      "Фильтры могут не попасть в URL — будет повтор попытки на уровне парсера."
    )
  # Avito SPA часто обновляет f=/localPriority с задержкой после «Показать объявления».
  for _ in range(48):
    cur = driver.current_url or ""
    if cur and _url_filters_fully_committed(cur, filters):
      break
    sleep(0.42)
  if _has_meaningful_avito_ui_filters(filters) and not _url_filters_fully_committed(
    driver.current_url or "", filters
  ):
    print(
      "[AVITO] В адресе нет localPriority/длинного f= после клика (у части вёрсток Avito так бывает); "
      "ниже парсер примет выдачу по факту UI, если цена/цвет и клики совпали с запросом."
    )
  sleep(0.8)
  applied["_show_clicked"] = 1 if clicked_show else 0

  # Верификация: какие фильтры реально выбраны в DOM.
  # Это фиксит кейс "кликнул -> считал применённым -> парсит всё подряд".
  selected_blob = _collect_selected_filters_from_dom(driver)
  if selected_blob:
    verified = {
      "memory": 0,
      "ram": 0,
      "sim": 0,
      "colors": 0,
      "condition": 0,
      "seller_type": 0,
      "rating_4_plus": 0,
    }
    for v in filters.get("memory", []) or []:
      if _text_matches_capacity(selected_blob, [v]):
        verified["memory"] += 1
    for v in filters.get("ram", []) or []:
      if _text_matches_capacity(selected_blob, [v]):
        verified["ram"] += 1
    for v in filters.get("sim", []) or []:
      if _text_matches_sim(selected_blob, [v]):
        verified["sim"] += 1
    for v in filters.get("colors", []) or []:
      if _text_matches_color(selected_blob, [v]):
        verified["colors"] += 1
    for v in filters.get("condition", []) or []:
      if _text_matches_condition(selected_blob, [v]):
        verified["condition"] += 1

    seller_req = str(filters.get("seller_type") or "all").lower()
    if seller_req == "all":
      verified["seller_type"] = 1
    elif seller_req == "private":
      verified["seller_type"] = 1 if ("частн" in selected_blob.lower() or "private" in selected_blob.lower() or "личн" in selected_blob.lower()) else 0
    elif seller_req == "company":
      verified["seller_type"] = 1 if ("компан" in selected_blob.lower() or "магазин" in selected_blob.lower() or "company" in selected_blob.lower() or "shop" in selected_blob.lower()) else 0
    else:
      verified["seller_type"] = 0

    if filters.get("rating_4_plus"):
      sb = selected_blob.lower()
      verified["rating_4_plus"] = 1 if ("звезд" in sb or "rating" in sb) and re.search(r"\b4\b", sb) else 0

    # Не "обнуляем" успешные клики из-за того, что selected_blob детектит memory/colors
    # (и иногда другие блоки) неточно. Берём максимум: клики <= verify.
    applied["memory"] = max(int(applied.get("memory") or 0), int(verified.get("memory") or 0))
    applied["ram"] = max(int(applied.get("ram") or 0), int(verified.get("ram") or 0))
    applied["sim"] = max(int(applied.get("sim") or 0), int(verified.get("sim") or 0))
    applied["colors"] = max(int(applied.get("colors") or 0), int(verified.get("colors") or 0))
    applied["condition"] = max(int(applied.get("condition") or 0), int(verified.get("condition") or 0))
    applied["seller_type"] = max(int(applied.get("seller_type") or 0), int(verified.get("seller_type") or 0))
    applied["rating_4_plus"] = max(int(applied.get("rating_4_plus") or 0), int(verified.get("rating_4_plus") or 0))
    print(f"[AVITO] UI verified selected filters: {verified} (selected_blob={selected_blob[:120]!r})")
  else:
    print("[AVITO] UI verified: не удалось собрать выбранные фильтры из DOM (selected_blob пустой).")

  print(
    "[AVITO] Фильтры применены: "
    f"memory={applied['memory']}, ram={applied['ram']}, sim={applied['sim']}, colors={applied['colors']}, "
    f"condition={applied['condition']}, seller={applied['seller_type']}, "
    f"rating4+={applied['rating_4_plus']}, show_btn={applied['_show_clicked']}, "
    f"url_committed={_url_filters_fully_committed(driver.current_url or '', filters)}"
  )
  applied["_url_committed"] = 1 if _url_filters_fully_committed(driver.current_url or '', filters) else 0
  applied["_url_has_f"] = 1 if _has_filter_signature(driver.current_url or '') else 0
  _log_avito_filters_diagnostics(driver, "after_filter_clicks")
  if (
    applied["memory"] + applied["ram"] + applied["sim"] + applied["colors"]
    + applied["condition"] + applied["rating_4_plus"] == 0
    and _has_meaningful_avito_ui_filters(filters)
  ):
    print(
      "[AVITO] Подсказка: смотрите JSON выше — если aside=false или samples=[], "
      "фильтры в DOM не видны (другая вёрстка/модалка/iframe). Проверьте окно браузера и прокси."
    )
  return applied


def _normalize_avito_item_href(href: str) -> str:
  """Один URL на объявление — убираем дубли DOM (два [data-marker=item] на карточку и т.п.)."""
  h = (href or "").strip()
  if not h:
    return ""
  if h.startswith("/"):
    h = f"{AVITO_BASE_URL}{h}"
  # Без query — достаточно для дедупа карточки
  return h.split("?")[0].rstrip("/")


def _avito_item_numeric_id(href: str):
  """ID объявления в конце пути (для дедупа при разных query/path)."""
  try:
    path = urlparse(href).path
  except Exception:
    return None
  m = re.search(r"-(\d{8,})\s*$", path)
  return m.group(1) if m else None


def _collect_cards_from_title_links(driver, title_links, *, source: str):
  """Собрать корневые карточки из ссылок заголовков с дедупом по URL и по числовому id."""
  cards = []
  seen_key = set()
  seen_ids = set()
  for link in title_links[:220]:
    try:
      raw_href = link.get_attribute("href") or ""
      href = _normalize_avito_item_href(raw_href)
      if not href:
        continue
      nid = _avito_item_numeric_id(href)
      if nid and nid in seen_ids:
        continue
      key = href.lower()
      if key in seen_key:
        continue
      seen_key.add(key)
      if nid:
        seen_ids.add(nid)
      try:
        card = link.find_element(By.XPATH, "./ancestor::*[@data-marker='item'][1]")
      except Exception:
        card = link.find_element(By.XPATH, "./ancestor::div[contains(@class,'iva-item-root')][1]")
      cards.append(card)
    except Exception:
      continue
  if cards:
    print(f"[AVITO] Карточек из ленты ({source}): {len(cards)} уникальных объявлений.")
  return cards


def _get_cards(driver, wait):
  """Только основная выдача: item-title вне catalog-serp — рекомендации/виджеты (лишние 25 ссылок)."""
  cards = []
  serp_selectors = (
    "[data-marker='catalog-serp'] a[data-marker='item-title']",
    "[data-marker='items'] a[data-marker='item-title']",
    "[data-marker='items-list'] a[data-marker='item-title']",
    "div[class*='items-items'] a[data-marker='item-title']",
  )

  try:
    wait.until(
      EC.presence_of_element_located(
        (By.CSS_SELECTOR, "[data-marker='catalog-serp'] a[data-marker='item-title'], a[data-marker='item-title']")
      )
    )
  except Exception:
    pass

  for sel in serp_selectors:
    try:
      title_links = driver.find_elements(By.CSS_SELECTOR, sel)
      if len(title_links) < 1:
        continue
      cards = _collect_cards_from_title_links(driver, title_links, source=f"селектор {sel!r}")
      if cards:
        return cards
    except Exception:
      continue

  try:
    print(
      "[AVITO] В контейнере выдачи мало ссылок — fallback по всей странице "
      "(возможны лишние карточки из рекомендаций)."
    )
    title_links = driver.find_elements(By.CSS_SELECTOR, "a[data-marker='item-title']")
    cards = _collect_cards_from_title_links(driver, title_links, source="вся страница (fallback)")
    if cards:
      return cards
  except Exception:
    pass

  card_selectors = [
    "div[data-marker='catalog-serp'] [data-marker='item']",
    "[data-marker='catalog-serp'] [data-marker='item']",
    "[data-marker='item']",
    "div[data-marker='catalog-serp'] div[class*='iva-item-root-']",
    "div[class*='iva-item-root-']",
  ]
  for selector in card_selectors:
    try:
      wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
      raw = driver.find_elements(By.CSS_SELECTOR, selector)
      if not raw:
        continue
      seen = set()
      out = []
      for el in raw[:200]:
        try:
          rid = id(el)
          if rid in seen:
            continue
          seen.add(rid)
          out.append(el)
        except Exception:
          continue
      if out:
        return out
    except Exception:
      continue
  return []


def _extract_card_date_text_selenium(card):
  """Текст даты размещения с карточки Avito (если есть)."""
  selectors = [
    "[data-marker='item-date']",
    "[class*='item-date']",
    "[class*='iva-item-date']",
  ]
  for sel in selectors:
    try:
      el = card.find_element(By.CSS_SELECTOR, sel)
      t = (el.text or "").strip()
      if t:
        return t
    except Exception:
      continue
  # Fallback: на части вёрсток дата без явного data-marker, но есть в тексте карточки.
  try:
    blob = (card.text or "").strip()
    t2 = _extract_relative_date_from_text(blob)
    if t2:
      return t2
  except Exception:
    pass
  return ""


def _extract_relative_date_from_text(text: str) -> str:
  s = (text or "").strip().lower().replace("\u00a0", " ")
  if not s:
    return ""
  # Ищем подпись времени так, как она обычно видна под описанием карточки.
  patterns = [
    r"\bсегодня\b",
    r"\b\d+\s*(?:ч|ч\.|час|часа|часов)\s*назад\b",
    r"\b\d+\s*(?:м|м\.|мин|мин\.|минута|минуты|минут)\s*назад\b",
    r"\b\d+\s*(?:с|с\.|сек|сек\.|секунда|секунды|секунд)\s*назад\b",
    r"\bвчера\b",
  ]
  for p in patterns:
    m = re.search(p, s, re.I)
    if m:
      return m.group(0).strip()
  return ""


def _extract_card_seller_text_selenium(card):
  """Имя/блок продавца прямо из карточки выдачи."""
  selectors = [
    "[data-marker*='seller']",
    "[class*='seller']",
    "[class*='iva-seller']",
    "[data-marker*='profile']",
  ]
  for sel in selectors:
    try:
      els = card.find_elements(By.CSS_SELECTOR, sel)
      for el in els[:5]:
        t = (el.text or "").strip()
        if t and len(t) <= 120:
          return t
    except Exception:
      continue
  return ""


def _extract_card_rating_text_selenium(card):
  """Рейтинг продавца (если показан в карточке выдачи)."""
  selectors = [
    "[data-marker*='rating']",
    "[class*='rating']",
    "[class*='stars']",
  ]
  for sel in selectors:
    try:
      els = card.find_elements(By.CSS_SELECTOR, sel)
      for el in els[:6]:
        t = (el.text or "").strip()
        if t and re.search(r"\b[1-5][\.,]?\d?\b", t):
          return t
    except Exception:
      continue
  return ""


def _is_private_seller_text(seller_text: str) -> bool:
  s = (seller_text or "").strip().lower()
  if not s:
    return False
  if any(x in s for x in ("магазин", "официальн", "компан", "company", "shop")):
    return False
  # Для частников обычно есть человеческое имя.
  return bool(re.search(r"[a-zа-я]{2,}", s, re.I))


def _extract_rating_value(text: str) -> float | None:
  s = (text or "").replace(",", ".")
  m = re.search(r"\b([1-5](?:\.\d)?)\b", s)
  if not m:
    return None
  try:
    v = float(m.group(1))
  except Exception:
    return None
  if 0.0 <= v <= 5.0:
    return v
  return None


def _is_avito_today_text(text):
  """Объявление «за сегодня» по подписи времени на карточке."""
  s = (text or "").strip().lower()
  if not s:
    return False
  if "вчера" in s or "yesterday" in s:
    return False
  if "сегодня" in s or "today" in s:
    return True
  # «N часов/минут/секунд назад» на Avito обычно означает сегодня
  if re.search(r"\d+\s*(ч|ч\.|час|часа|часов|м|м\.|мин|мин\.|минут|минуты|с|с\.|сек|сек\.|секунд|секунды)\s*назад?", s):
    return True
  return False


def _parse_cards_from_html(driver):
  try:
    html = driver.page_source
  except Exception:
    return []
  soup = BeautifulSoup(html, "html.parser")
  items = []
  seen_urls = set()
  for link in soup.select("a[data-marker='item-title'][href]"):
    title = link.get_text(" ", strip=True)
    href = (link.get("href") or "").strip()
    if not href:
      continue
    if href.startswith("/"):
      href = f"{AVITO_BASE_URL}{href}"
    if href in seen_urls:
      continue

    container = link.find_parent(attrs={"data-marker": "item"})
    if container is None:
      container = link.find_parent("div")
    price = None
    city_text = ""
    date_text = ""
    seller_text = ""
    description_text = ""
    rating_text = ""
    rating_value = None
    if container is not None:
      price_tag = container.select_one(
        "[data-marker='item-price'] [data-marker='item-price-value'], "
        "[data-marker='item-price'] span, [data-marker='item-price']"
      )
      if price_tag:
        price = _parse_price_text(price_tag.get_text(" ", strip=True))
      city_tag = container.select_one("[data-marker='item-location']")
      if city_tag:
        city_text = city_tag.get_text(" ", strip=True)
      date_tag = container.select_one("[data-marker='item-date']") or container.select_one(
        "[class*='item-date']"
      )
      date_text = (date_tag.get_text(" ", strip=True) if date_tag else "") or ""
      if not date_text:
        try:
          date_text = _extract_relative_date_from_text(container.get_text(" ", strip=True))
        except Exception:
          date_text = ""
      seller_tag = container.select_one("[data-marker*='seller'], [class*='seller'], [class*='iva-seller']")
      seller_text = (seller_tag.get_text(" ", strip=True) if seller_tag else "") or ""
      description_tag = container.select_one("[data-marker='item-description'], [class*='item-description'], [class*='iva-item-text']")
      description_text = (description_tag.get_text(" ", strip=True) if description_tag else "") or ""
      rating_tag = container.select_one("[data-marker*='rating'], [class*='rating'], [class*='stars']")
      rating_text = (rating_tag.get_text(" ", strip=True) if rating_tag else "") or ""
      rating_value = _extract_rating_value(rating_text)

    items.append(
      {
        "platform": "avito",
        "title": title,
        "price": price,
        "url": href,
        "city": city_text or None,
        "date_text": date_text or None,
        "description": description_text or None,
        "seller_text": seller_text or None,
        "is_private_seller": bool(_is_private_seller_text(seller_text)),
        "rating_text": rating_text or None,
        "seller_rating": rating_value,
      }
    )
    seen_urls.add(href)
  return items


def _parse_cards_to_items(cards, city, price_min, price_max):
  items = []
  stats = {
    "cards_total": len(cards),
    "parsed_ok": 0,
    "skipped_no_title_or_url": 0,
    "skipped_city": 0,
    "skipped_price": 0,
    "skipped_error": 0,
  }
  total_cards = len(cards)
  for idx, card in enumerate(cards, start=1):
    if total_cards > 12 and (idx == 1 or idx % 10 == 0 or idx == total_cards):
      print(f"[AVITO] Разбор карточек DOM: {idx}/{total_cards}…")
    try:
      title_el = card.find_element(By.CSS_SELECTOR, "a[data-marker='item-title']")
      title = title_el.text.strip()
      href = title_el.get_attribute("href") or ""
      if not title and not href:
        stats["skipped_no_title_or_url"] += 1
        continue

      price = None
      try:
        price_el = card.find_element(
          By.CSS_SELECTOR,
          "[data-marker='item-price'] [data-marker='item-price-value'], "
          "[data-marker='item-price'] span, [data-marker='item-price']",
        )
        price = _parse_price_text(price_el.text)
      except Exception:
        pass

      city_text = ""
      try:
        city_el = card.find_element(By.CSS_SELECTOR, "[data-marker='item-location']")
        city_text = city_el.text.strip()
      except Exception:
        pass

      date_text = _extract_card_date_text_selenium(card)
      seller_text = _extract_card_seller_text_selenium(card)
      description_text = ""
      try:
        desc_el = card.find_element(
          By.CSS_SELECTOR,
          "[data-marker='item-description'], [class*='item-description'], [class*='iva-item-text']",
        )
        description_text = (desc_el.text or "").strip()
      except Exception:
        pass
      rating_text = _extract_card_rating_text_selenium(card)
      rating_value = _extract_rating_value(rating_text)

      # Фильтр по городу делаем на уровне URL (/samara/...), а не по тексту карточки.
      # В карточках город может быть указан как район/пригород и давать ложные отсеивания.
      if price_min is not None and price is not None and price < price_min:
        stats["skipped_price"] += 1
        continue
      if price_max is not None and price is not None and price > price_max:
        stats["skipped_price"] += 1
        continue

      items.append({
        "platform": "avito",
        "title": title,
        "price": price,
        "url": href,
        "city": city_text or None,
        "date_text": date_text or None,
        "description": description_text or None,
        "seller_text": seller_text or None,
        "is_private_seller": bool(_is_private_seller_text(seller_text)),
        "rating_text": rating_text or None,
        "seller_rating": rating_value,
      })
      stats["parsed_ok"] += 1
    except Exception:
      stats["skipped_error"] += 1
      continue
  return items, stats


def parse_avito(
  driver,
  keyword,
  model,
  city,
  price_min,
  price_max,
  precision=7,
  filters=None,
  stop_event=None,
  raise_on_block=False,
  today_only=False,
  status_callback=None,
  driver_recreate_callback=None,
  checkpoint_callback=None,
):
  """Поэтапно: одна страница → пауза → скролл по шагам → сбор → длинная пауза → следующая страница."""
  params = _precision_params(precision)
  max_pages = params["max_pages"]
  scroll_passes = params["scroll_passes"]
  scroll_delay = params["scroll_delay"]
  page_delay = params["page_delay"]
  load_delay = params["load_delay"]

  all_items = []
  page = 1
  print("[AVITO] PARSE RESUMED")
  pages_since_driver_recycle = 0
  session_errors_since_recycle = 0
  filter_meta = _filters_to_excel_meta(
    filters,
    today_only=bool(today_only),
    price_min=price_min,
    price_max=price_max,
  )
  ui_applied = {}
  seen_item_keys = set()
  filtered_base_url = ""
  effective_max_pages = min(max_pages, AVITO_MAX_PAGES_PER_RUN)
  detected_pages = 1
  fallback_without_ui_filters_done = True
  parse_scope_announced = False
  ui_filters_temporarily_disabled = True

  # Пауза перед первым запросом (даём сети/прокси «остыть» перед заходом)
  if page == 1:
    first_delay = random.uniform(6.0, 12.5)
    print(f"[AVITO] Старт через {first_delay:.0f} сек…")
    _sleep_with_stop(stop_event, first_delay)
  last_known_ip = _get_public_ip_via_proxy()
  if last_known_ip:
    print(f"[AVITO] Текущий внешний IP: {last_known_ip}")

  while True:
    if stop_event is not None and stop_event.is_set():
      print("[AVITO] Остановка парсинга по запросу пользователя.")
      break
    if page > effective_max_pages:
      print("[AVITO] Достиг конец страниц по примененным фильтрам.")
      break
    if filtered_base_url:
      url = _build_page_url(filtered_base_url, page)
      # Без длинного f= в базе GET на ?p=2 часто отдаёт выдачу без UI-фильтров — переходим кликом.
      prefer_ui_pagination = (
        page > 1
        and _has_meaningful_avito_ui_filters(filters or {})
        and _f_param_length(filtered_base_url) < 60
      )
    else:
      url = build_avito_search_url(keyword, model, city, price_min, price_max, page=page, filters=filters)
      prefer_ui_pagination = False
      if page == 1:
        print(f"[AVITO] Seed URL (модель/цвет/цена): {url[:320]}")

    # Защита: капча/блокировка — до 3 раз на КАЖДУЮ страницу (счётчик сбрасывается на новой странице),
    # пауза между раундами — смена IP у мобильного прокси.
    abort_page_loop = False
    skip_current_page = False
    transport_failures_on_page = 0
    unlimited_entry_mode = (page == 1 and not all_items)
    max_rounds_for_page = None if unlimited_entry_mode else AVITO_BLOCK_MAX_RETRIES_PER_PAGE
    block_round = 0
    while True:
      block_round += 1
      rounds_label = "∞" if max_rounds_for_page is None else str(max_rounds_for_page)
      if block_round > 1:
        print(
          f"[AVITO] Страница {page}: повтор после блокировки — раунд {block_round}/"
          f"{rounds_label} (ожидание смены IP)…"
        )
      else:
        print(f"[AVITO] Страница {page}/{max_pages}: загрузка…")
      if status_callback:
        try:
          status_callback(
            {
              "phase": "page_loading",
              "page": int(page),
              "pages_to_parse": int(max_pages),
              "block_round": int(block_round),
              "block_round_max": int(max_rounds_for_page or 0),
            }
          )
        except Exception as e:
          print(f"[AVITO] status_callback: {e}")

      loaded = False
      open_try_max = int(AVITO_PAGE_LOAD_MAX_RETRIES)
      t_open0 = time.perf_counter()
      for attempt in range(1, open_try_max + 1):
        try:
          _throttle_avito_enter(stop_event)
          print("[AVITO] ENTER AVITO")
          if page == 1:
            # Чередуем мягкие сценарии входа, чтобы не стучаться всегда одним паттерном.
            strategy = (block_round + attempt) % 3
            if strategy == 0:
              print("[AVITO] Вход на выдачу: прямой переход на целевой URL…")
              _sleep_with_stop(stop_event, random.uniform(2.0, 5.0))
              driver.get(url)
            elif strategy == 1:
              print("[AVITO] Вход на выдачу: прогрев через главную, затем целевой URL…")
              _sleep_with_stop(stop_event, random.uniform(3.0, 7.0))
              driver.get("https://www.avito.ru/")
              if wait_for_document_ready(driver, DOCUMENT_READY_TIMEOUT, stop_event):
                _sleep_with_stop(stop_event, random.uniform(4.0, 9.0))
              driver.get(url)
            else:
              print("[AVITO] Вход на выдачу: прогрев через безопасный поиск, затем целевой URL…")
              _sleep_with_stop(stop_event, random.uniform(3.0, 7.0))
              warmup_url = build_avito_search_url(keyword, model, city, price_min, price_max, page=1, filters={})
              driver.get(warmup_url)
              if wait_for_document_ready(driver, DOCUMENT_READY_TIMEOUT, stop_event):
                _sleep_with_stop(stop_event, random.uniform(4.0, 8.0))
              driver.get(url)
          else:
            # Мягче открываем следующие страницы, чтобы снизить частоту блокировок.
            _sleep_with_stop(stop_event, random.uniform(1.8, 4.2))
            if prefer_ui_pagination and _try_click_avito_pagination_page(driver, page, stop_event):
              print(
                f"[AVITO] Страница {page}: открыта кликом по пагинации "
                "(в базовом URL нет длинного f= — прямой GET мог бы сбросить фильтры)."
              )
            else:
              driver.get(url)
          print(
            f"[AVITO] Ожидание готовности страницы (до {DOCUMENT_READY_TIMEOUT} сек, "
            "без ожидания полной загрузки ресурсов)…"
          )
          if not wait_for_document_ready(driver, DOCUMENT_READY_TIMEOUT, stop_event):
            raise TimeoutException("document.readyState не достиг готовности")
          loaded = True
          dt_open = (time.perf_counter() - t_open0) * 1000.0
          print(f"[AVITO] metric open_ms={dt_open:.0f} stage=ok")
          break
        except TimeoutException:
          print(f"[AVITO] Таймаут загрузки (попытка {attempt}/{open_try_max}).")
          session_errors_since_recycle += 1
          _retry_backoff_sleep(attempt, stop_event, "timeout")
        except (WebDriverException, OSError, Exception) as e:
          kind = _classify_open_exception(e)
          if kind == "transport":
            print(f"[AVITO] Обрыв соединения с прокси/сайтом (попытка {attempt}/{open_try_max}): {e}")
          else:
            print(f"[AVITO] Ошибка загрузки ({kind}, попытка {attempt}/{open_try_max}): {e}")
          session_errors_since_recycle += 1
          _retry_backoff_sleep(attempt, stop_event, kind)
      if not loaded:
        dt_open = (time.perf_counter() - t_open0) * 1000.0
        print(f"[AVITO] metric open_ms={dt_open:.0f} stage=failed")
        print(f"[AVITO] Не удалось загрузить страницу после {open_try_max} попыток. Проверьте прокси и сеть.")
        if unlimited_entry_mode:
          cooldown_sec = random.uniform(90.0, 180.0)
          print(
            f"[AVITO] Первая страница всё ещё не открылась. "
            f"Делаю охлаждение {int(cooldown_sec)} сек и пробую следующий сценарий…"
          )
          _sleep_with_stop(stop_event, cooldown_sec)
          continue
        if page > 1:
          print(
            f"[AVITO] PAGE SKIPPED: page={page} reason=load_failed "
            f"(после {open_try_max} попыток), продолжаю со следующей страницы."
          )
          skip_current_page = True
          break
        abort_page_loop = True
        break

      # Если Avito не открыл нужную страницу p=..., дальше идти бессмысленно
      current_url = (driver.current_url or "").lower()
      if page > 1 and f"p={page}" not in current_url:
        print(
          f"[AVITO] Страница p={page} не открылась (URL: {driver.current_url}). "
          "Пробую открыть страницу повторно."
        )
        try:
          driver.get(url)
          wait_for_document_ready(driver, DOCUMENT_READY_TIMEOUT, stop_event)
          _sleep_with_stop(stop_event, random.uniform(2.0, 4.0))
        except Exception as e:
          print(f"[AVITO] Повторный переход на p={page} не удался: {e}")
        current_url = (driver.current_url or "").lower()
        if f"p={page}" not in current_url:
          can_retry_round = (max_rounds_for_page is None) or (block_round < max_rounds_for_page)
          if can_retry_round:
            print(
              f"[AVITO] p={page} всё ещё не открылась. Перехожу к следующему раунду "
              f"{block_round + 1}/{rounds_label}."
            )
            continue
          print("[AVITO] Не удалось открыть нужную страницу после всех раундов.")
          abort_page_loop = True
          break

      delay_after_load = (
        random.uniform(12.0, 24.0) if page == 1 else random.uniform(max(7.0, load_delay * 0.9), max(12.0, load_delay * 1.4))
      )
      print(f"[AVITO] Ожидание {delay_after_load:.0f} сек после загрузки…")
      _sleep_with_stop(stop_event, delay_after_load)

      if stop_event is not None and stop_event.is_set():
        print("[AVITO] Остановка парсинга после загрузки страницы.")
        abort_page_loop = True
        break

      print("[AVITO] Проверка страницы на ограничение доступа…")
      if _looks_like_avito_not_found_page(driver):
        print("[AVITO] Открылась 404-страница Avito (битая ссылка). Перехожу на безопасный URL поиска…")
        safe_url = build_avito_search_url(keyword, model, city, price_min, price_max, page=page, filters={})
        try:
          driver.get(safe_url)
          if not wait_for_document_ready(driver, DOCUMENT_READY_TIMEOUT, stop_event):
            print("[AVITO] После перехода на безопасный URL document.readyState не готов.")
          _sleep_with_stop(stop_event, random.uniform(2.0, 4.0))
          url = safe_url
        except Exception as e:
          print(f"[AVITO] Ошибка перехода на безопасный URL: {e}")
      blocked, reason = _is_avito_blocked(driver)
      if not blocked:
        t_fail, t_reason = _detect_avito_transport_issue(driver)
        if not t_fail:
          break
        transport_failures_on_page += 1
        # Транспортный сбой != блокировка Avito: не ждём слишком долго, пробуем быстрее восстановиться.
        transport_wait = random.uniform(90.0, 130.0) if page == 1 else random.uniform(8.0, 20.0)
        print(
          f"[AVITO] Транспортная ошибка сети/прокси ({t_reason}). "
          f"Жду {int(transport_wait)} сек и пробую на текущей сессии…"
        )
        if status_callback:
          try:
            status_callback(
              {
                "phase": "transport_issue",
                "page": int(page),
                "reason": str(t_reason or ""),
                "wait_sec": int(transport_wait),
              }
            )
          except Exception as e:
            print(f"[AVITO] status_callback: {e}")
        _sleep_with_stop(stop_event, transport_wait)
        # Не пересоздаём сессию сразу: сохраняем рабочий IP/сессию, если она ещё жива.
        if transport_failures_on_page >= 2 and driver_recreate_callback is not None:
          try:
            new_ip = wait_for_new_ip(last_known_ip, stop_event)
            if new_ip:
              last_known_ip = new_ip
            print("[AVITO] START NEW SESSION")
            new_driver = driver_recreate_callback()
            if new_driver is not None:
              driver = new_driver
              pages_since_driver_recycle = 0
              session_errors_since_recycle = 0
              if status_callback:
                try:
                  status_callback({"phase": "driver_recreated", "page": int(page)})
                except Exception as e:
                  print(f"[AVITO] status_callback: {e}")
          except Exception as e:
            print(f"[AVITO] Ошибка пересоздания driver: {e}")
        # Если транспорт разваливается подряд, не тратим 20+ минут на бесполезные циклы.
        if transport_failures_on_page >= 3:
          if unlimited_entry_mode:
            cooldown_sec = random.uniform(140.0, 260.0)
            print(
              "[AVITO] Много транспортных сбоев подряд на первой странице. "
              f"Делаю длинное охлаждение {int(cooldown_sec)} сек и продолжаю попытки."
            )
            transport_failures_on_page = 0
            _sleep_with_stop(stop_event, cooldown_sec)
            continue
          if page > 1:
            print(
              "[AVITO] PAGE SKIPPED: page=%s reason=transport_failures — продолжаю со следующей страницы."
              % (page,)
            )
            skip_current_page = True
            break
          print(
            "[AVITO] Много транспортных сбоев подряд на странице. "
            "Прерываю страницу раньше, чтобы запуск завершался быстрее и стабильнее."
          )
          abort_page_loop = True
          break
        continue

      msg = f"[AVITO] Обнаружена блокировка/проверка ({reason})."
      print(msg)
      print("[AVITO] BLOCK DETECTED")
      if status_callback:
        try:
          status_callback(
            {
              "phase": "block_detected",
              "page": int(page),
              "reason": str(reason or ""),
              "block_round": int(block_round),
              "block_round_max": int(max_rounds_for_page or 0),
            }
          )
        except Exception as e:
          print(f"[AVITO] status_callback: {e}")
      # Каждые 3 неудачных раунда добавляем длинное охлаждение, чтобы дождаться новой IP-сессии.
      wait_block_sec = random.uniform(120.0, 160.0)
      if block_round % 3 == 0:
        wait_block_sec = random.uniform(190.0, 320.0)
      print(
        f"[AVITO] Попытка {block_round}/{rounds_label} на странице {page}. "
        f"Пауза {int(wait_block_sec)} сек (новый IP у прокси), затем новый сценарий входа…"
      )
      if max_rounds_for_page is not None and block_round >= max_rounds_for_page:
        if status_callback:
          try:
            status_callback(
              {
                "phase": "block_give_up",
                "page": int(page),
                "reason": str(reason or ""),
                "collected_items": int(len(all_items)),
              }
            )
          except Exception as e:
            print(f"[AVITO] status_callback: {e}")
        # Не сбрасываем весь прогон, если уже есть данные с предыдущих страниц.
        # Фатально падаем только если блок на первой странице и ещё нечего отдавать.
        if raise_on_block and page <= 1 and not all_items:
          raise AvitoBlockedError(msg)
        if page > 1:
          print(
            "[AVITO] PAGE SKIPPED: page=%s reason=block_retries_exhausted — продолжаю парсинг дальше."
            % (page,)
          )
          skip_current_page = True
        else:
          print(
            "[AVITO] Лимит попыток на первой странице — завершаю текущий прогон с уже собранными объявлениями."
          )
          abort_page_loop = True
          break
        break
      if status_callback:
        try:
          status_callback(
            {
              "phase": "block_retry_wait",
              "page": int(page),
                "wait_sec": int(wait_block_sec),
              "next_round": int(block_round + 1),
              "block_round_max": int(max_rounds_for_page or 0),
            }
          )
        except Exception as e:
          print(f"[AVITO] status_callback: {e}")
      if driver_recreate_callback is not None:
        try:
          new_ip = wait_for_new_ip(last_known_ip, stop_event)
          if new_ip:
            last_known_ip = new_ip
          print("[AVITO] START NEW SESSION")
          new_driver = driver_recreate_callback()
          if new_driver is not None:
            driver = new_driver
            transport_failures_on_page = 0
            pages_since_driver_recycle = 0
            session_errors_since_recycle = 0
        except Exception as e:
          print(f"[AVITO] Ошибка создания новой сессии после блока: {e}")
      _sleep_with_stop(stop_event, wait_block_sec)

    if abort_page_loop:
      break
    if skip_current_page:
      page += 1
      if page > effective_max_pages:
        print("[AVITO] Достиг конец страниц по примененным фильтрам.")
        break
      delay = random.uniform(page_delay * 0.8, page_delay * 1.3)
      print(f"[AVITO] Пауза {delay:.0f} сек перед следующей страницей (после пропуска)…")
      _sleep_with_stop(stop_event, delay)
      continue

    # Критично: НЕ продолжаем, пока не появились карточки и (для стр.1 с фильтрами) панель фильтров.
    # Иначе клики по фильтрам идут в пустой DOM и фильтры «не находятся».
    need_filters_panel = False
    if status_callback:
      try:
        status_callback(
          {
            "phase": "dom_wait",
            "page": int(page),
            "need_filters_panel": bool(need_filters_panel),
            "dom_try_max": int(AVITO_DOM_RELOAD_TRIES),
          }
        )
      except Exception as e:
        print(f"[AVITO] status_callback: {e}")
    ui_filters_temporarily_disabled = True
    dom_ready = False
    t_dom0 = time.perf_counter()
    # По требованию: DOM-перезаходы ограничиваем тремя попытками, чтобы не зависать слишком долго.
    dom_reload_max = min(int(AVITO_DOM_RELOAD_TRIES), 3)
    for dom_try in range(1, dom_reload_max + 1):
      blocked_dom, blocked_reason_dom = _is_avito_blocked(driver)
      if blocked_dom:
        soft_wait = random.uniform(120.0, 140.0)
        print(
          f"[AVITO] Во время ожидания DOM обнаружена блокировка ({blocked_reason_dom}). "
          f"Жду {int(soft_wait)} сек и мягко перезахожу на страницу…"
        )
        if status_callback:
          try:
            status_callback(
              {
                "phase": "block_detected",
                "page": int(page),
                "reason": str(blocked_reason_dom or ""),
                "block_round": int(dom_try),
                "block_round_max": int(AVITO_DOM_RELOAD_TRIES),
              }
            )
          except Exception as e:
            print(f"[AVITO] status_callback: {e}")
          try:
            status_callback(
              {
                "phase": "block_retry_wait",
                "page": int(page),
                "wait_sec": int(soft_wait),
                "next_round": int(min(dom_try + 1, AVITO_DOM_RELOAD_TRIES)),
                "block_round_max": int(AVITO_DOM_RELOAD_TRIES),
              }
            )
          except Exception as e:
            print(f"[AVITO] status_callback: {e}")
        _sleep_with_stop(stop_event, soft_wait)
        if stop_event is not None and stop_event.is_set():
          abort_page_loop = True
          break
        try:
          if page == 1:
            driver.get(url)
          else:
            driver.get(url)
          if not wait_for_document_ready(driver, DOCUMENT_READY_TIMEOUT, stop_event):
            print("[AVITO] После перезахода при блокировке document.readyState не готов — пробую дальше.")
          _sleep_with_stop(stop_event, random.uniform(3.0, 7.0))
        except Exception as e:
          print(f"[AVITO] Ошибка перезахода после блокировки: {e}")
        continue

      transport_dom, transport_reason_dom = _detect_avito_transport_issue(driver)
      if transport_dom:
        transport_failures_on_page += 1
        transport_wait = random.uniform(90.0, 130.0) if page == 1 else random.uniform(8.0, 20.0)
        print(
          f"[AVITO] В DOM-цикле транспортная ошибка ({transport_reason_dom}). "
          f"Жду {int(transport_wait)} сек и пробую на текущей сессии…"
        )
        if status_callback:
          try:
            status_callback(
              {
                "phase": "transport_issue",
                "page": int(page),
                "reason": str(transport_reason_dom or ""),
                "wait_sec": int(transport_wait),
              }
            )
          except Exception as e:
            print(f"[AVITO] status_callback: {e}")
        _sleep_with_stop(stop_event, transport_wait)
        if page > 1 and transport_failures_on_page >= 2 and driver_recreate_callback is not None:
          try:
            print("[AVITO] START NEW SESSION")
            new_driver = driver_recreate_callback()
            if new_driver is not None:
              driver = new_driver
              pages_since_driver_recycle = 0
              session_errors_since_recycle = 0
              if status_callback:
                try:
                  status_callback({"phase": "driver_recreated", "page": int(page)})
                except Exception as e:
                  print(f"[AVITO] status_callback: {e}")
          except Exception as e:
            print(f"[AVITO] Ошибка пересоздания driver в DOM-цикле: {e}")
        if transport_failures_on_page >= 3:
          if page > 1:
            print(
              "[AVITO] PAGE SKIPPED: page=%s reason=dom_transport_failures — продолжаю со следующей страницы."
              % (page,)
            )
            skip_current_page = True
            break
          print(
            "[AVITO] Много транспортных сбоев подряд в DOM-цикле. "
            "Прерываю страницу раньше, чтобы не зависать на длинных ожиданиях."
          )
          abort_page_loop = True
          break
        continue

      # Частый кейс: после "успешной" загрузки открывается главная Avito/сервисная страница,
      # а не SERP. Не ждём долгий DOM-таймаут, сразу мягко перезаходим в целевой URL.
      if _looks_like_avito_home_or_service_page(driver):
        print("[AVITO] Открылась главная/сервисная страница вместо выдачи. Мягко перехожу снова на поиск…")
        if status_callback:
          try:
            status_callback(
              {
                "phase": "dom_retry",
                "page": int(page),
                "dom_try": int(dom_try),
                "dom_try_max": int(AVITO_DOM_RELOAD_TRIES),
                "cards_ok": False,
                "filters_panel_ok": False,
              }
            )
          except Exception as e:
            print(f"[AVITO] status_callback: {e}")
        _sleep_with_stop(stop_event, random.uniform(8.0, 18.0))
        try:
          if page == 1:
            driver.get(url)
          else:
            driver.get(url)
          if not wait_for_document_ready(driver, DOCUMENT_READY_TIMEOUT, stop_event):
            print("[AVITO] После возврата из главной страницы document.readyState не готов — пробую дальше.")
          _sleep_with_stop(stop_event, random.uniform(2.0, 5.0))
        except Exception as e:
          print(f"[AVITO] Ошибка возврата из главной/сервисной страницы: {e}")
        continue

      shell_timeout = AVITO_DOM_WAIT_SHELL_FIRST if dom_try == 1 else AVITO_DOM_WAIT_SHELL_NEXT
      shell_ok = _wait_for_avito_listing_shell(driver, timeout_sec=shell_timeout, stop_event=stop_event)
      filters_panel_ok = True
      if need_filters_panel:
        filters_timeout = AVITO_DOM_WAIT_FILTERS_FIRST if dom_try == 1 else AVITO_DOM_WAIT_FILTERS_NEXT
        filters_panel_ok = _wait_for_avito_filters_panel(
          driver, timeout_sec=filters_timeout, stop_event=stop_event
        )
      # Карточки уже есть, а панель фильтров не детектится селекторами:
      # пробуем UI-фильтры всё равно (поиск по body/text), без перезаходов.
      if shell_ok and need_filters_panel and not filters_panel_ok:
        dom_ready = True
        ui_filters_temporarily_disabled = False
        print(
          "[AVITO] Карточки есть, но панель фильтров не появилась. "
          "Пробую применить UI-фильтры через расширенный поиск по DOM."
        )
        if status_callback:
          try:
            status_callback({"phase": "ui_filters_unavailable", "page": int(page)})
          except Exception as e:
            print(f"[AVITO] status_callback: {e}")
        break
      if shell_ok and filters_panel_ok:
        dom_ready = True
        if dom_try > 1:
          print(f"[AVITO] DOM готов после перезагрузки (попытка {dom_try}/3).")
        break
      _log_avito_empty_page_probe(driver)
      blocked_after_probe, blocked_reason_after_probe = _is_avito_blocked(driver)
      if blocked_after_probe:
        soft_wait = random.uniform(120.0, 140.0)
        print(
          f"[AVITO] После probe обнаружена блокировка ({blocked_reason_after_probe}). "
          f"Жду {int(soft_wait)} сек и мягко перезахожу на страницу…"
        )
        if status_callback:
          try:
            status_callback(
              {
                "phase": "block_detected",
                "page": int(page),
                "reason": str(blocked_reason_after_probe or ""),
                "block_round": int(dom_try),
                "block_round_max": int(AVITO_DOM_RELOAD_TRIES),
              }
            )
          except Exception as e:
            print(f"[AVITO] status_callback: {e}")
          try:
            status_callback(
              {
                "phase": "block_retry_wait",
                "page": int(page),
                "wait_sec": int(soft_wait),
                "next_round": int(min(dom_try + 1, AVITO_DOM_RELOAD_TRIES)),
                "block_round_max": int(AVITO_DOM_RELOAD_TRIES),
              }
            )
          except Exception as e:
            print(f"[AVITO] status_callback: {e}")
        _sleep_with_stop(stop_event, soft_wait)
        if stop_event is not None and stop_event.is_set():
          abort_page_loop = True
          break
        try:
          if page == 1:
            driver.get(url)
          else:
            driver.get(url)
          if not wait_for_document_ready(driver, DOCUMENT_READY_TIMEOUT, stop_event):
            print("[AVITO] После перезахода после probe document.readyState не готов — пробую дальше.")
          _sleep_with_stop(stop_event, random.uniform(3.0, 7.0))
        except Exception as e:
          print(f"[AVITO] Ошибка перезахода после probe-блокировки: {e}")
        continue
      print(
        f"[AVITO] DOM не готов (попытка {dom_try}/{dom_reload_max}): "
        f"cards={'ok' if shell_ok else 'none'}, filters_panel={'ok' if filters_panel_ok else 'none'}."
      )
      if status_callback:
        try:
          status_callback(
            {
              "phase": "dom_retry",
              "page": int(page),
              "dom_try": int(dom_try),
                "dom_try_max": int(dom_reload_max),
              "cards_ok": bool(shell_ok),
              "filters_panel_ok": bool(filters_panel_ok),
            }
          )
        except Exception as e:
          print(f"[AVITO] status_callback: {e}")
      if dom_try >= dom_reload_max:
        break
      # Часто сначала грузится только шапка, а карточки догружаются позже.
      # Даем короткое дополнительное ожидание перед перезаходом.
      _sleep_with_stop(stop_event, random.uniform(8.0, 16.0))
      if _avito_listing_shell_present(driver):
        print("[AVITO] После доп. ожидания контент догрузился — продолжаю без перезахода.")
        dom_ready = True
        break
      print("[AVITO] Перезагружаю страницу и жду DOM снова…")
      try:
        # Полный перезаход надёжнее refresh при сетевых ERR_TUNNEL/прокси-сбоях.
        driver.get(url)
        if not wait_for_document_ready(driver, DOCUMENT_READY_TIMEOUT, stop_event):
          print("[AVITO] После перезахода document.readyState не готов — пробую дальше.")
        _sleep_with_stop(stop_event, 2.5)
      except Exception as e:
        print(f"[AVITO] Ошибка перезахода на страницу: {e}")

    if skip_current_page:
      page += 1
      if page > effective_max_pages:
        print("[AVITO] Достиг конец страниц по примененным фильтрам.")
        break
      delay = random.uniform(page_delay * 0.8, page_delay * 1.3)
      print(f"[AVITO] Пауза {delay:.0f} сек перед следующей страницей (после пропуска)…")
      _sleep_with_stop(stop_event, delay)
      continue

    if dom_ready:
      print(f"[AVITO] metric dom_wait_ms={(time.perf_counter() - t_dom0) * 1000.0:.0f} stage=ok")
    else:
      print(f"[AVITO] metric dom_wait_ms={(time.perf_counter() - t_dom0) * 1000.0:.0f} stage=failed")
      print(
        f"[AVITO] Не удалось получить карточки/панель фильтров после {dom_reload_max} перезаходов. "
        "На этой странице пропускаю сбор и перехожу дальше."
      )
      if page > 1:
        print("[AVITO] PAGE SKIPPED: page=%s reason=dom_not_ready" % (page,))
      if page <= 1 and not all_items:
        abort_page_loop = True
        break
      page += 1
      if page > effective_max_pages:
        print("[AVITO] Достиг конец страниц по примененным фильтрам.")
        break
      delay = random.uniform(page_delay * 0.8, page_delay * 1.3)
      print(f"[AVITO] Пауза {delay:.0f} сек перед следующей страницей (после пропуска)…")
      _sleep_with_stop(stop_event, delay)
      continue

    wait = WebDriverWait(driver, EXPLICIT_WAIT)
    # КРИТИЧНО: после fallback «без UI» нельзя снова жать фильтры — иначе снова 0 карточек.
    if False:
      if status_callback:
        try:
          status_callback({"phase": "applying_filters"})
        except Exception as e:
          print(f"[AVITO] status_callback: {e}")
    if False:
      try:
        meaningful = _has_meaningful_avito_ui_filters(filters or {})
        max_apply_attempts = 3 if meaningful else 1
        apply_ok = False
        last_url = ""
        for apply_try in range(1, max_apply_attempts + 1):
          if apply_try > 1:
            print(f"[AVITO] Повторное применение UI-фильтров: попытка {apply_try}/{max_apply_attempts}…")
            if status_callback:
              try:
                status_callback(
                  {
                    "phase": "filters_retry",
                    "page": int(page),
                    "apply_try": int(apply_try),
                    "apply_try_max": int(max_apply_attempts),
                  }
                )
              except Exception as e:
                print(f"[AVITO] status_callback: {e}")
            # Нельзя открывать seed url без f=: сбросит выдачу. Берём URL после прошлой попытки.
            reload_target = (
              last_url
              if (last_url and _has_filter_signature(last_url))
              else _ensure_price_bounds_in_url(url, price_min, price_max)
            )
            if reload_target != url:
              print(
                f"[AVITO] Повтор: перезаход по URL с параметром f= (не по начальному seed), "
                f"{len(reload_target)} симв."
              )
            try:
              driver.get(reload_target)
            except Exception:
              _open_avito_with_soft_entry(
                driver, reload_target, stop_event=stop_event, include_home=False, reset_session=False
              )
            if not wait_for_document_ready(driver, DOCUMENT_READY_TIMEOUT, stop_event):
              raise TimeoutException("document.readyState не достиг готовности при повторном применении фильтров")
            _sleep_with_stop(stop_event, random.uniform(1.2, 2.2))

          ui_applied = _apply_avito_ui_filters(driver, filters, stop_event=stop_event) or {}
          current_after_filters = driver.current_url or ""
          current_after_filters = _ensure_price_bounds_in_url(current_after_filters, price_min, price_max)
          last_url = current_after_filters
          raw_ui_sum = sum(
            int(ui_applied.get(k, 0)) for k in ("memory", "ram", "sim", "colors", "condition", "rating_4_plus")
          )
          # Важно: короткий f= часто означает "только цена", это НЕ подтверждение UI-фильтров.
          has_filter_signature = _f_param_length(current_after_filters) >= 60
          has_price_signature = _url_has_expected_price_bounds(current_after_filters, price_min, price_max)
          has_color_ok = _color_filter_accepted(current_after_filters, filters, ui_applied)
          has_color_in_path = _url_has_expected_color_path(
            current_after_filters, (filters or {}).get("colors")
          )
          has_f_commit = _url_filters_fully_committed(current_after_filters, filters)
          strict_url_ok = (
            has_filter_signature
            and has_price_signature
            and has_color_ok
            and has_f_commit
            and _requested_ui_filters_satisfied(filters or {}, ui_applied or {})
          )
          # Частый случай (новый фронт Avito): фильтры реально применены в колонке и по выдаче,
          # но в адресе нет длинного f= / localPriority — только pmin/pmax/cd и т.д.
          ui_evidence_ok = (
            meaningful
            and has_price_signature
            and has_color_ok
            and int((ui_applied or {}).get("_show_clicked") or 0) == 1
            and _requested_ui_filters_satisfied(filters or {}, ui_applied or {})
          )
          print(
            f"[AVITO] Проверка URL после фильтров (попытка {apply_try}): "
            f"has_f={has_filter_signature}, has_price={has_price_signature}, "
            f"has_color_ok={has_color_ok} (path_slug={has_color_in_path}), "
            f"f_commit={has_f_commit}, ui_sum={raw_ui_sum}, strict_url={strict_url_ok}, "
            f"ui_evidence={ui_evidence_ok}, url={current_after_filters[:320]}"
          )
          if not meaningful:
            apply_ok = True
            break
          if strict_url_ok or ui_evidence_ok:
            if ui_evidence_ok and not strict_url_ok:
              print(
                "[AVITO] В URL нет полного f=/localPriority, но фильтры подтверждены UI "
                "(«Показать объявления» + клики по всем запрошенным блокам) — продолжаем парсинг."
              )
            if status_callback:
              try:
                status_callback(
                  {
                    "phase": "filters_applied_url",
                    "page": int(page),
                    "url": str(current_after_filters or "")[:320],
                  }
                )
              except Exception as e:
                print(f"[AVITO] status_callback: {e}")
            apply_ok = True
            break

        if meaningful and not apply_ok:
          raise RuntimeError(
            "Avito не подтвердил фильтры: в URL нет полного f=/localPriority, "
            "и не сработал запасной критерий (кнопка «Показать объявления», цена в URL, цвет, "
            "успешные клики по всем запрошенным фильтрам). Парсинг остановлен."
          )

        last_url = _enrich_listing_base_url_from_dom(driver, last_url)
        last_url = _ensure_price_bounds_in_url(last_url, price_min, price_max)
        filtered_base_url = last_url or _ensure_price_bounds_in_url(url, price_min, price_max)
        if filtered_base_url:
          print(f"[AVITO] URL после фильтров: {filtered_base_url[:320]}")
      except Exception as e:
        print(f"[AVITO] Не удалось корректно применить фильтры: {e}")
        raise
    elif page == 1:
      print("[AVITO] Быстрый режим: без UI-фильтров, отбор по выдаче (цена/память/сегодня/частник).")
    t_parse0 = time.perf_counter()
    if scroll_passes > 0:
      _scroll_page(driver, scroll_passes, scroll_delay, stop_event=stop_event)

    cards = _get_cards(driver, wait)
    used_html_fallback = False
    if cards:
      print(f"[AVITO] Страница {page}: найдено карточек {len(cards)}")
    # Основной быстрый путь: парсинг page_source через BS4.
    items = _parse_cards_from_html(driver)
    if items:
      used_html_fallback = True
      print(f"[AVITO] Страница {page}: HTML-разбор, найдено {len(items)} карточек")
    elif cards:
      # Редкий fallback: если HTML не дал карточек, пробуем DOM-парсер Selenium.
      items, page_stats = _parse_cards_to_items(cards, city, price_min, price_max)
      print(
        f"[AVITO] Страница {page}: parsed={page_stats['parsed_ok']}/{page_stats['cards_total']}, "
        f"added={len(items)}, "
        f"skip_city={page_stats['skipped_city']}, skip_price={page_stats['skipped_price']}, "
        f"skip_other={page_stats['skipped_no_title_or_url'] + page_stats['skipped_error']}"
      )
    else:
      items = _parse_cards_from_html(driver)
      used_html_fallback = True
      if page == 1 and not items:
        # Avito sometimes returns a broken/empty state after applying UI filters.
        # Fallback: retry from base query without UI filters to avoid empty result runs.
        if filters and not fallback_without_ui_filters_done:
          if _has_meaningful_avito_ui_filters(filters):
            print(
              "[AVITO] После применения UI-фильтров карточки не найдены на странице 1. "
              "Безопасный режим: НЕ переключаюсь на парсинг без фильтров, "
              "чтобы не вернуть неверную широкую выдачу."
            )
            break
          print(
            "[AVITO] После UI-фильтров карточки не найдены на странице 1. "
            "Повторяю поиск без UI-фильтров как fallback."
          )
          fallback_without_ui_filters_done = True
          filtered_base_url = ""
          page = 1
          continue
        print("[AVITO] Не удалось найти объявления ни селекторами, ни через HTML fallback.")
        break
      print(f"[AVITO] Страница {page}: fallback HTML, найдено {len(items)} карточек")

    # После успешной первой страницы (есть карточки): число страниц в выдаче + лимит за запуск.
    # Не вызываем до fallback «без UI-фильтров», чтобы не слать в бот неверные цифры.
    if page == 1 and not parse_scope_announced:
      print("[AVITO] Определяю число страниц пагинации…")
      detected_pages = _detect_total_pages(driver)
      # Узкая выдача по фильтрам: на 1-й странице <50 карточек → обычно одна страница (не доверяем ложным «40 стр.» из старых ссылок)
      try:
        fb = filtered_base_url or ""
        has_f = "f=" in fb or "&f=" in fb or "?f=" in fb
        if has_f and _has_meaningful_avito_ui_filters(filters or {}):
          nt = len(driver.find_elements(By.CSS_SELECTOR, "[data-marker='catalog-serp'] a[data-marker='item-title']"))
          if nt < 1:
            nt = len(driver.find_elements(By.CSS_SELECTOR, "a[data-marker='item-title']"))
          if 0 < nt < 50:
            old_dp = detected_pages
            detected_pages = min(detected_pages, 1)
            if old_dp != detected_pages:
              print(
                f"[AVITO] На стр.1 только {nt} объявлений при активных фильтрах — "
                f"страниц для обхода: {detected_pages} (было бы {old_dp} без ограничения)."
              )
      except Exception:
        pass
      # Идем батчами страниц за один прогон: так стабильнее на слабом VPS и при мобильном прокси.
      total_pages_cap = min(max(1, detected_pages), AVITO_MAX_PAGES_PER_RUN)
      effective_max_pages = min(total_pages_cap, page - 1 + AVITO_PAGES_BATCH_SIZE)
      print(
        f"[AVITO] Страниц в выдаче: {detected_pages}. "
        f"Буду парсить {effective_max_pages} стр. подряд (батч {AVITO_PAGES_BATCH_SIZE})."
      )
      if status_callback:
        try:
          status_callback(
            {
              "phase": "ready",
              "detected_pages": int(detected_pages),
              "pages_to_parse": int(effective_max_pages),
              "filters_applied": _has_meaningful_avito_ui_filters(filters),
            }
          )
        except Exception as e:
          print(f"[AVITO] status_callback: {e}")
      parse_scope_announced = True

    if price_min is not None or price_max is not None:
      filtered = []
      for item in items:
        price = item.get("price")
        if price_min is not None and price is not None and price < price_min:
          continue
        if price_max is not None and price is not None and price > price_max:
          continue
        filtered.append(item)
      items = filtered

    unique_items = []
    for item in items:
      key = ((item.get("url") or "").strip(), (item.get("title") or "").strip())
      if key in seen_item_keys:
        continue
      seen_item_keys.add(key)
      unique_items.append(item)

    if page > 1 and not unique_items:
      print("[AVITO] На странице нет новых объявлений. Перехожу к следующей странице.")

    if today_only:
      before = len(unique_items)
      unique_items = [it for it in unique_items if _is_avito_today_text(it.get("date_text"))]
      dropped = before - len(unique_items)
      if dropped:
        print(f"[AVITO] Режим «только сегодня»: отфильтровано {dropped} объявлений (нет даты / не сегодня).")

    # Быстрая фильтрация по карточке выдачи (без захода в объявление).
    if filters.get("memory"):
      before = len(unique_items)
      unique_items = [it for it in unique_items if _matches_memory_from_listing(it, filters.get("memory"))]
      if before != len(unique_items):
        print(f"[AVITO] Фильтр памяти (выдача): отфильтровано {before - len(unique_items)} объявлений.")
    if filters.get("colors"):
      before = len(unique_items)
      unique_items = [it for it in unique_items if _text_matches_color(_item_search_blob(it), filters.get("colors"))]
      if before != len(unique_items):
        print(f"[AVITO] Фильтр цвета (описание/заголовок): отфильтровано {before - len(unique_items)} объявлений.")
    seller_mode = str(filters.get("seller_type") or "all").lower()
    if seller_mode == "private":
      before = len(unique_items)
      unique_items = [it for it in unique_items if bool(it.get("is_private_seller"))]
      if before != len(unique_items):
        print(f"[AVITO] Фильтр «только частные»: отфильтровано {before - len(unique_items)} объявлений.")
    elif seller_mode == "company":
      before = len(unique_items)
      unique_items = [it for it in unique_items if not bool(it.get("is_private_seller"))]
      if before != len(unique_items):
        print(f"[AVITO] Фильтр «только компании»: отфильтровано {before - len(unique_items)} объявлений.")

    if bool(filters.get("rating_4_plus")):
      before = len(unique_items)
      unique_items = [
        it
        for it in unique_items
        if (it.get("seller_rating") is not None and float(it.get("seller_rating")) >= 4.0)
      ]
      if before != len(unique_items):
        print(f"[AVITO] Фильтр рейтинга 4+: отфильтровано {before - len(unique_items)} объявлений.")

    all_items.extend(_enrich_items_with_filter_meta(unique_items, filter_meta))
    t_parse_ms = (time.perf_counter() - t_parse0) * 1000.0
    print(f"[AVITO] metric parse_ms={t_parse_ms:.0f} stage=ok")
    session_errors_since_recycle = 0
    t_ckpt0 = time.perf_counter()
    try:
      _call_checkpoint_callback(checkpoint_callback, list(all_items), page=page, total_items=len(all_items))
    except Exception as e:
      print(f"[AVITO] checkpoint_callback: {e}")
    print(f"[AVITO] metric checkpoint_ms={(time.perf_counter() - t_ckpt0) * 1000.0:.0f} stage=ok")
    if driver_recreate_callback is not None:
      if AVITO_DRIVER_RECYCLE_AFTER_PAGES > 0:
        pages_since_driver_recycle += 1
      if AVITO_DRIVER_RECYCLE_AFTER_PAGES > 0 and pages_since_driver_recycle >= AVITO_DRIVER_RECYCLE_AFTER_PAGES:
        try:
          print("[AVITO] START NEW SESSION (recycle: page budget)")
          nd = driver_recreate_callback()
          if nd is not None:
            driver = nd
            pages_since_driver_recycle = 0
            session_errors_since_recycle = 0
        except Exception as e:
          print(f"[AVITO] Ошибка recycle driver: {e}")
      elif AVITO_DRIVER_RECYCLE_AFTER_ERRORS > 0 and session_errors_since_recycle >= AVITO_DRIVER_RECYCLE_AFTER_ERRORS:
        try:
          print("[AVITO] START NEW SESSION (recycle: error budget)")
          nd = driver_recreate_callback()
          if nd is not None:
            driver = nd
            session_errors_since_recycle = 0
        except Exception as e:
          print(f"[AVITO] Ошибка recycle driver: {e}")
    if status_callback:
      try:
        status_callback(
          {
            "phase": "page_parsed",
            "page": int(page),
            "added": int(len(unique_items)),
            "total_collected": int(len(all_items)),
            "cards_seen": int(len(cards) if cards else 0),
          }
        )
      except Exception as e:
        print(f"[AVITO] status_callback: {e}")
    # Не выходим из-за «мало карточек», если ещё есть страницы по пагинации (иначе обрыв на 1-й странице).
    if not used_html_fallback and len(cards) < 20:
      if page >= effective_max_pages:
        break
    if used_html_fallback and len(items) < 15:
      if page >= effective_max_pages:
        break
    page += 1
    if page > effective_max_pages:
      print("[AVITO] Достиг конец страниц по примененным фильтрам.")
      break
    if page <= effective_max_pages:
      delay = random.uniform(page_delay * 0.8, page_delay * 1.3)
      print(f"[AVITO] Пауза {delay:.0f} сек перед следующей страницей (обход лимитов)…")
      _sleep_with_stop(stop_event, delay)

  text_fallback_ran = False
  mode, mode_note = ("fast_listing", "Отбор по выдаче: price + memory + colors + today_only + seller + rating")
  final_meta = _filters_to_excel_meta(
    filters,
    applied_mode=mode,
    ui_applied_note=mode_note,
    today_only=bool(today_only),
    price_min=price_min,
    price_max=price_max,
  )
  for item in all_items:
    item.update(final_meta)

  print(f"[AVITO] Всего объявлений (с повторами): {len(all_items)}")
  print("[AVITO] PARSE FINISHED")
  if status_callback and (stop_event is None or not stop_event.is_set()):
    try:
      status_callback({"phase": "parse_finished", "total_items": int(len(all_items))})
    except Exception as e:
      print(f"[AVITO] status_callback: {e}")
  if not all_items:
    print(
      "[AVITO] ⚠ Итог 0 объявлений. Частые причины: блокировка Avito, режим «только сегодня» "
      "(today_only), пустая выдача по запросу, обрыв после пустой страницы. "
      "Смотрите также [AVITO][diag:*] в логах при применении фильтров."
    )
  return all_items
