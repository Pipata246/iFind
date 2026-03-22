import random
import re
import time
from time import sleep
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from browser_helpers import wait_for_document_ready
from config import (
  AVITO_BASE_URL,
  AVITO_MAX_PAGES_PER_RUN,
  DOCUMENT_READY_TIMEOUT,
  EXPLICIT_WAIT,
  VPS_LIGHT_MODE,
)


class AvitoBlockedError(RuntimeError):
  pass


# Перебор сотен элементов с elem.is_displayed() даёт тысячи round-trip к драйверу (10+ минут тишины в логах).
_QUICK_FILTER_COLLECT_CAP = 220
_QUICK_FILTER_SCAN_CAP = 80
_ROOT_CANDIDATES_CAP = 90


def _filters_to_excel_meta(filters, *, applied_mode: str = "", ui_applied_note: str = ""):
  """Запрос пользователя + как реально отобрали (UI / текст карточек)."""
  filters = filters or {}
  return {
    "avito_filter_memory": ", ".join(filters.get("memory", [])),
    "avito_filter_ram": ", ".join(filters.get("ram", [])),
    "avito_filter_sim": ", ".join(filters.get("sim", [])),
    "avito_filter_colors": ", ".join(filters.get("colors", [])),
    "avito_filter_condition": ", ".join(filters.get("condition", [])),
    "avito_filter_seller_type": (filters.get("seller_type") or "all"),
    "avito_filter_rating_4_plus": "yes" if filters.get("rating_4_plus") else "no",
    "avito_filter_applied_mode": applied_mode or "",
    "avito_ui_applied_note": ui_applied_note or "",
  }


def _item_search_blob(item: dict) -> str:
  parts = [item.get("title") or "", item.get("url") or ""]
  return " ".join(parts).lower()


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
  b = blob.lower()
  for v in values or []:
    raw = (v or "").strip().lower()
    if not raw:
      continue
    if raw in b:
      return True
    r2 = raw.replace("ё", "е")
    if r2 in b.replace("ё", "е"):
      return True
    if "зел" in raw and ("зел" in b or "green" in b):
      return True
    if "красн" in raw and "красн" in b:
      return True
    if "син" in raw and "син" in b:
      return True
    if "бел" in raw and ("бел" in b or "white" in b):
      return True
    if "черн" in raw and ("черн" in b or "black" in b):
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
  out = []
  for item in items:
    blob = _item_search_blob(item)
    ok = True
    if (filters.get("memory") or []) and u.get("memory", 0) == 0:
      ok = ok and _text_matches_capacity(blob, filters.get("memory"))
    if (filters.get("ram") or []) and u.get("ram", 0) == 0:
      ok = ok and _text_matches_capacity(blob, filters.get("ram"))
    if (filters.get("sim") or []) and u.get("sim", 0) == 0:
      ok = ok and _text_matches_sim(blob, filters.get("sim"))
    if (filters.get("colors") or []) and u.get("colors", 0) == 0:
      ok = ok and _text_matches_color(blob, filters.get("colors"))
    if (filters.get("condition") or []) and u.get("condition", 0) == 0:
      ok = ok and _text_matches_condition(blob, filters.get("condition"))
    st = str(filters.get("seller_type") or "all").lower()
    if st == "private" and u.get("seller_type", 0) == 0:
      ok = ok and ("частн" in blob or "private" in blob or "личн" in blob)
    if st == "company" and u.get("seller_type", 0) == 0:
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
    max_pages = min(max_pages, 5)
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


def _wait_for_avito_listing_shell(driver, timeout_sec=22, stop_event=None):
  """Дождаться карточек или блока фильтров (при page_load_strategy=none контент догружается после ready)."""
  deadline = time.monotonic() + float(timeout_sec)
  while time.monotonic() < deadline:
    if stop_event is not None and stop_event.is_set():
      return False
    try:
      if driver.find_elements(
        By.CSS_SELECTOR,
        "a[data-marker='item-title'], [data-marker='item'], [data-marker*='filter']",
      ):
        return True
    except Exception:
      pass
    sleep(0.35)
  return False


def build_avito_search_url(keyword, model, city, price_min, price_max, page=1):
  q_parts = []
  if keyword:
    q_parts.append(keyword)
  if model:
    q_parts.append(model)
  q = "+".join(q_parts) if q_parts else ""

  params = []
  if q:
    params.append(f"q={q}")
  if price_min is not None:
    params.append(f"pmin={price_min}")
  if price_max is not None:
    params.append(f"pmax={price_max}")
  if page > 1:
    params.append(f"p={page}")

  city_slug = _city_to_slug(city)
  base = AVITO_BASE_URL
  if city_slug:
    # Важно: для Avito город должен быть в пути URL, иначе выдача часто смешивается по регионам.
    base = f"{AVITO_BASE_URL}/{city_slug}"

  if params:
    return f"{base}/?{'&'.join(params)}"
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
  try:
    snippet = driver.execute_script(
      "return (document.documentElement && document.documentElement.outerHTML || '')"
      ".slice(0, 120000).toLowerCase();"
    )
  except Exception:
    return False, ""
  if not snippet:
    return False, ""
  blocked_markers = (
    "капча",
    "captcha",
    "подтвердите, что вы не робот",
    "доступ ограничен",
    "проблема с ip",
    "слишком много запросов",
    "подозрительная активность",
  )
  for marker in blocked_markers:
    if marker in snippet:
      return True, marker
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
    "[data-marker*='filter']",
    "[class*='SearchFilters']",
    "[class*='search-filters']",
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
    "[data-marker*='filter'] label",
    "[data-marker*='filter'] span",
    "[data-marker*='filter'] button",
    "[class*='SearchFilters'] label",
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
        document.querySelectorAll('[data-marker*="filter"],[data-marker*="params"]').forEach(function(n){ roots.push(n); });
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
          document.querySelectorAll('[data-marker*="filter"],[data-marker*="params"],[class*="SearchFilters"],[class*="search-filters"],[class*="styles-module-sidebar"]').forEach(add);
          if (list.length === 0) {
            document.querySelectorAll('section,div').forEach(function(n){
              var dm = n.getAttribute('data-marker') || '';
              if (dm.indexOf('filter') !== -1 || dm.indexOf('params') !== -1) add(n);
            });
          }
          return list;
        }
        function inFilterColumn(el){
          if (!el || !el.getBoundingClientRect) return false;
          if (document.querySelector('aside') && document.querySelector('aside').contains(el)) return true;
          var r = el.getBoundingClientRect();
          if (r.width < 2 || r.height < 2) return false;
          // Раньше было 44%/52% — на новой вёрстке колонка фильтров шире; узлы отсекались → «не найден фильтр».
          return r.left < window.innerWidth * 0.78;
        }
        function clickSmart(el){
          if (!el) return false;
          try { el.scrollIntoView({block:'center', behavior:'instant'}); } catch (e1) {}
          var p = el;
          for (var i = 0; i < 10 && p; i++) {
            var inp = p.querySelector && p.querySelector('input[type="checkbox"], input[type="radio"]');
            if (inp) {
              try { inp.click(); return true; } catch (e2) {}
            }
            p = p.parentElement;
          }
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
              var t = norm(el.innerText || el.textContent || '');
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
              var t = norm(el.innerText || el.textContent || '');
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
          for (var s = 0; s < nodes.length; s++) {
            var ts = nodes[s].t;
            if (ts.indexOf('sim') === -1 && ts.indexOf('сим') === -1 && ts.indexOf('nano') === -1) continue;
            if (needle.length >= 2 && (ts.indexOf(needle) !== -1 || needle.indexOf(ts) !== -1)) {
              if (clickSmart(nodes[s].el)) return true;
            }
          }
          for (var s2 = 0; s2 < nodes.length; s2++) {
            var ts2 = nodes[s2].t;
            if (ts2.indexOf(needle) !== -1 && ts2.length < 48) {
              if (clickSmart(nodes[s2].el)) return true;
            }
          }
          return false;
        }
        if (mode === 'color') {
          for (var c = 0; c < nodes.length; c++) {
            var tc = nodes[c].t;
            if (tc.indexOf(needle) !== -1 && tc.length < 56) {
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
  variants.extend(
    [
      raw.replace(" ", ""),
      raw.upper(),
      raw.lower(),
      "1 SIM",
      "1 sim",
      "1SIM",
      "SIM",
      "sim",
      "1 nano-SIM",
      "nano-SIM + eSIM",
      "2 SIM",
      "2 sim",
      "2SIM",
    ]
  )
  if "1" in low or re.search(r"\b1\b", raw):
    variants.extend(
      [
        "1 SIM",
        "1 sim",
        "1sim",
        "SIM",
        "1 SIM-карта",
        "1 SIM",
        "1 nano-SIM",
        "1 nano sim",
        "Nano-SIM",
        "одна SIM",
        "1 физическая SIM",
      ]
    )
  if "2" in low or re.search(r"\b2\b", raw):
    variants.extend(["2 SIM", "2 sim", "2SIM"])
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


def _click_show_results_button(driver):
  """Надежно нажимает кнопку применения фильтров («Показать N объявлений» и варианты)."""
  xpaths = [
    "//button[contains(normalize-space(), 'Показать') and contains(normalize-space(), 'объяв')]",
    "//a[contains(normalize-space(), 'Показать') and contains(normalize-space(), 'объяв')]",
    "//span[contains(normalize-space(), 'Показать') and contains(normalize-space(), 'объяв')]",
    "//*[@role='button' and contains(., 'Показать') and contains(., 'объяв')]",
    "//button[contains(., 'Показать')]",
    "//span[contains(., 'Показать') and contains(., 'объяв')]",
    "//*[contains(@class,'button') and contains(., 'Показать')]",
    "//*[contains(normalize-space(), 'Показать') and contains(normalize-space(), 'объяв')]",
  ]
  for xpath in xpaths:
    try:
      elems = driver.find_elements(By.XPATH, xpath)
      for elem in elems:
        try:
          driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
          sleep(0.25)
          try:
            elem.click()
          except Exception:
            driver.execute_script("arguments[0].click();", elem)
          return True
        except Exception:
          continue
    except Exception:
      continue
  return False


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
        return JSON.stringify({
          phase: arguments[0],
          innerW: window.innerWidth,
          innerH: window.innerHeight,
          url: String(location.href || "").slice(0, 220),
          aside: !!aside,
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


def _try_open_avito_filters_drawer(driver):
  """На части вёрсток колонка фильтров скрыта за кнопкой «Фильтры» / data-marker."""
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
        var labels = ["Фильтры", "Все фильтры", "Параметры", "Подобрать", "Настроить поиск", "Фильтр"];
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
  """Прокрутка aside — опции памяти/SIM часто подгружаются только после скролла."""
  try:
    driver.execute_script(
      """
      var aside = document.querySelector("aside");
      if (!aside) return;
      for (var i = 0; i < 30; i++) {
        aside.scrollTop += 280;
      }
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


def _detect_total_pages(driver):
  """Определяет число страниц в текущей выдаче Avito после применения фильтров."""
  max_page = 1
  try:
    page_links = driver.find_elements(By.XPATH, "//a[contains(@href, 'p=') or @data-marker]")
  except Exception:
    page_links = []
  for link in page_links:
    try:
      text = (link.text or "").strip()
      href = (link.get_attribute("href") or "").strip()
      if text.isdigit():
        max_page = max(max_page, int(text))
      m = re.search(r"[?&]p=(\d+)", href)
      if m:
        max_page = max(max_page, int(m.group(1)))
    except Exception:
      continue
  return max_page


def _apply_avito_ui_filters(driver, filters):
  if not filters:
    return {}

  print("[AVITO] Применяю расширенные фильтры в интерфейсе…")
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

  # На Avito после выбора фильтров часто требуется кнопка "Показать N объявлений".
  clicked_show = False
  for attempt in range(1, 4):
    clicked_show = _click_show_results_button(driver)
    if clicked_show:
      break
    sleep(0.9)
  if clicked_show:
    print("[AVITO] Нажал кнопку применения фильтров: 'Показать ... объявлений'.")
  else:
    print("[AVITO] Кнопка 'Показать ... объявлений' не найдена. Продолжаю без принудительного клика.")
  sleep(1.5)
  print(
    "[AVITO] Фильтры применены: "
    f"memory={applied['memory']}, ram={applied['ram']}, sim={applied['sim']}, colors={applied['colors']}, "
    f"condition={applied['condition']}, seller={applied['seller_type']}, "
    f"rating4+={applied['rating_4_plus']}"
  )
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


def _get_cards(driver, wait):
  card_selectors = [
    "[data-marker='item']",
    "div[data-marker='catalog-serp'] div[class*='iva-item-root-']",
    "div[class*='iva-item-root-']",
    "div[class*='iva-item-content-']",
    ".iva-item-content-fRmzq",
  ]
  cards = []
  for selector in card_selectors:
    try:
      wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector)))
      cards = driver.find_elements(By.CSS_SELECTOR, selector)
      if cards:
        return cards
    except Exception:
      continue
  try:
    wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "a[data-marker='item-title']")))
    title_links = driver.find_elements(By.CSS_SELECTOR, "a[data-marker='item-title']")
    for link in title_links:
      try:
        card = link.find_element(By.XPATH, "./ancestor::*[@data-marker='item'][1]")
        if card:
          cards.append(card)
          continue
      except Exception:
        pass
      try:
        card = link.find_element(By.XPATH, "./ancestor::div[contains(@class,'iva-item-root')][1]")
        if card:
          cards.append(card)
      except Exception:
        pass
    if cards:
      return cards
  except Exception:
    pass
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
  return ""


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
  if re.search(r"\d+\s*(час|часа|часов|мин|минут|минуты|сек|секунд|секунды)", s):
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

    items.append(
      {
        "platform": "avito",
        "title": title,
        "price": price,
        "url": href,
        "city": city_text or None,
        "date_text": date_text or None,
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
  for card in cards:
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
  filter_meta = _filters_to_excel_meta(filters)
  ui_applied = {}
  seen_item_keys = set()
  filtered_base_url = ""
  effective_max_pages = min(max_pages, AVITO_MAX_PAGES_PER_RUN)
  detected_pages = 1
  fallback_without_ui_filters_done = False
  parse_scope_announced = False

  # Небольшая пауза перед первым запросом, чтобы не бить сайт сразу
  if page == 1:
    first_delay = random.uniform(3.0, 6.0)
    print(f"[AVITO] Старт через {first_delay:.0f} сек…")
    _sleep_with_stop(stop_event, first_delay)

  while page <= max_pages:
    if stop_event is not None and stop_event.is_set():
      print("[AVITO] Остановка парсинга по запросу пользователя.")
      break
    if filtered_base_url:
      url = _build_page_url(filtered_base_url, page)
    else:
      url = build_avito_search_url(keyword, model, city, price_min, price_max, page=page)
    print(f"[AVITO] Страница {page}/{max_pages}: загрузка…")

    loaded = False
    for attempt in range(1, 4):
      try:
        driver.get(url)
        print(f"[AVITO] Ожидание готовности страницы (до {DOCUMENT_READY_TIMEOUT} сек, без ожидания полной загрузки ресурсов)…")
        if not wait_for_document_ready(driver, DOCUMENT_READY_TIMEOUT, stop_event):
          raise TimeoutException("document.readyState не достиг готовности")
        loaded = True
        break
      except TimeoutException:
        print(f"[AVITO] Таймаут загрузки (попытка {attempt}/3). Пауза 10 сек…")
        _sleep_with_stop(stop_event, 10)
      except (WebDriverException, OSError, Exception) as e:
        err = str(e).lower()
        if "connection" in err or "reset" in err or "10054" in err or "econnreset" in err or "tcp" in err:
          print(f"[AVITO] Обрыв соединения с прокси/сайтом (попытка {attempt}/3). Пауза 10 сек…")
        else:
          print(f"[AVITO] Ошибка загрузки: {e}")
        _sleep_with_stop(stop_event, 10)
    if not loaded:
      print("[AVITO] Не удалось загрузить страницу после 3 попыток. Проверьте прокси и сеть.")
      break

    # Если Avito не открыл нужную страницу p=..., дальше идти бессмысленно
    # (обычно это означает, что страниц больше нет и сайт вернул первую/последнюю).
    current_url = (driver.current_url or "").lower()
    if page > 1 and f"p={page}" not in current_url:
      print(
        f"[AVITO] Страница p={page} не открылась (URL: {driver.current_url}). "
        "Похоже, страниц больше нет."
      )
      break

    delay_after_load = random.uniform(load_delay * 0.8, load_delay * 1.2)
    print(f"[AVITO] Ожидание {delay_after_load:.0f} сек после загрузки…")
    _sleep_with_stop(stop_event, delay_after_load)

    if stop_event is not None and stop_event.is_set():
      print("[AVITO] Остановка парсинга после загрузки страницы.")
      break

    print("[AVITO] Проверка страницы на ограничение доступа…")
    blocked, reason = _is_avito_blocked(driver)
    if blocked:
      msg = f"[AVITO] Обнаружена блокировка/проверка ({reason})."
      print(msg)
      if raise_on_block:
        raise AvitoBlockedError(msg)
      break

    # Иначе фильтры кликаются по пустому DOM → «не найден фильтр», выдача пустая.
    if not _wait_for_avito_listing_shell(driver, timeout_sec=25, stop_event=stop_event):
      print("[AVITO] Долго нет карточек/фильтров в DOM — продолжаю, как есть (возможна медленная сеть).")

    wait = WebDriverWait(driver, EXPLICIT_WAIT)
    # КРИТИЧНО: после fallback «без UI» нельзя снова жать фильтры — иначе снова 0 карточек.
    if page == 1 and filters and not fallback_without_ui_filters_done and _has_meaningful_avito_ui_filters(filters):
      if status_callback:
        try:
          status_callback({"phase": "applying_filters"})
        except Exception as e:
          print(f"[AVITO] status_callback: {e}")
    if page == 1 and filters and not fallback_without_ui_filters_done:
      try:
        ui_applied = _apply_avito_ui_filters(driver, filters) or {}
        filtered_base_url = driver.current_url or ""
      except Exception as e:
        print(f"[AVITO] Не удалось применить часть фильтров: {e}")
        ui_applied = {}
    elif page == 1 and filters and fallback_without_ui_filters_done:
      print("[AVITO] Повтор после пустой выдачи: UI-фильтры отключены, парсинг по базовому запросу + текстовый отбор в конце.")
    if scroll_passes > 0:
      _scroll_page(driver, scroll_passes, scroll_delay, stop_event=stop_event)

    cards = _get_cards(driver, wait)
    used_html_fallback = False
    if cards:
      print(f"[AVITO] Страница {page}: найдено карточек {len(cards)}")
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
      detected_pages = _detect_total_pages(driver)
      effective_max_pages = min(max_pages, max(1, detected_pages), AVITO_MAX_PAGES_PER_RUN)
      print(
        f"[AVITO] Страниц в выдаче: {detected_pages}. "
        f"Буду парсить {effective_max_pages} стр. (макс. {AVITO_MAX_PAGES_PER_RUN} за запуск, precision ≤ {max_pages})."
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
      print("[AVITO] На следующей странице нет новых объявлений. Останавливаюсь.")
      break

    if today_only:
      before = len(unique_items)
      unique_items = [it for it in unique_items if _is_avito_today_text(it.get("date_text"))]
      dropped = before - len(unique_items)
      if dropped:
        print(f"[AVITO] Режим «только сегодня»: отфильтровано {dropped} объявлений (нет даты / не сегодня).")

    all_items.extend(_enrich_items_with_filter_meta(unique_items, filter_meta))
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
  if filters and _need_text_fallback(ui_applied, filters):
    before = len(all_items)
    narrowed = _post_filter_avito_items_by_text(all_items, filters, ui_applied)
    if len(narrowed) == 0 and before > 0:
      print(
        "[AVITO] Текстовый fallback дал 0 позиций — оставляю исходную выдачу "
        "(строгий отбор по title/URL отменён, чтобы не было пустого отчёта)."
      )
    else:
      all_items = narrowed
      text_fallback_ran = True
      print(
        f"[AVITO] Текстовый fallback (UI не применил часть фильтров): было {before} позиций, "
        f"после отбора по title/URL — {len(all_items)}."
      )

  mode, mode_note = _describe_applied_mode(filters, ui_applied, text_fallback_ran)
  final_meta = _filters_to_excel_meta(filters, applied_mode=mode, ui_applied_note=mode_note)
  for item in all_items:
    item.update(final_meta)

  print(f"[AVITO] Всего объявлений (с повторами): {len(all_items)}")
  if not all_items:
    print(
      "[AVITO] ⚠ Итог 0 объявлений. Частые причины: блокировка Avito, режим «только сегодня» "
      "(today_only), пустая выдача по запросу, обрыв после пустой страницы. "
      "Смотрите также [AVITO][diag:*] в логах при применении фильтров."
    )
  return all_items
