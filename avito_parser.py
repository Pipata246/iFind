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

from config import AVITO_BASE_URL, AVITO_MAX_PAGES_PER_RUN, EXPLICIT_WAIT, VPS_LIGHT_MODE


class AvitoBlockedError(RuntimeError):
  pass


def _filters_to_excel_meta(filters):
  filters = filters or {}
  return {
    "avito_filter_memory": ", ".join(filters.get("memory", [])),
    "avito_filter_ram": ", ".join(filters.get("ram", [])),
    "avito_filter_sim": ", ".join(filters.get("sim", [])),
    "avito_filter_colors": ", ".join(filters.get("colors", [])),
    "avito_filter_condition": ", ".join(filters.get("condition", [])),
    "avito_filter_seller_type": (filters.get("seller_type") or "all"),
    "avito_filter_rating_4_plus": "yes" if filters.get("rating_4_plus") else "no",
  }


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
  try:
    body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
  except Exception:
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
    if marker in body_text:
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
  """Где искать чекбоксы фильтров (не весь DOM — иначе десятки тысяч узлов и минуты на один клик)."""
  roots = []
  for sel in (
    "aside",
    "[class*='filter']",
    "[class*='Filter']",
    "[class*='search-form']",
    "[data-marker*='filter']",
    "form",
  ):
    try:
      for el in driver.find_elements(By.CSS_SELECTOR, sel):
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
  return uniq[:10]


def _click_text_option(driver, text, must_be_checkbox=False):
  """Клик по пункту фильтра. Раньше перебирались все label/div на странице — на Avito это зависало на минуты."""
  del must_be_checkbox  # на текущей вёрстке чипы фильтров часто <button>, их нельзя пропускать
  if not text:
    return False

  normalized_target = _norm_filter_text(text).replace("gb", "гб")
  variants = [text, text.replace("+", " + "), text.replace("ё", "е"), text.replace("е", "ё")]
  vseen = set()
  vlist = []
  for v in variants:
    k = (v or "").strip()
    if not k or k in vseen:
      continue
    vseen.add(k)
    vlist.append(k)

  for variant in vlist:
    if len(variant) > 100:
      continue
    # XPath: экранируем кавычки в строке
    if "'" not in variant:
      lit = f"'{variant}'"
    else:
      lit = '"' + variant.replace('"', '\\"') + '"'
    xps = [
      f"//label[contains(normalize-space(), {lit})]",
      f"//*[self::span or self::div or self::button][contains(normalize-space(), {lit})]",
      f"//*[@role='checkbox' or @role='switch'][contains(., {lit})]",
      f"//*[contains(@class,'Checkbox') or contains(@class,'checkbox')][contains(., {lit})]",
    ]
    for xp in xps:
      try:
        elems = driver.find_elements(By.XPATH, xp)
        for elem in elems[:30]:
          try:
            if not elem.is_displayed():
              continue
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
            sleep(0.12)
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
    try:
      candidates = root.find_elements(
        By.CSS_SELECTOR,
        "label, button, span[role], div[role], span, div[class*='Checkbox'], div[class*='checkbox']",
      )
    except Exception:
      continue
    for elem in candidates[:450]:
      try:
        if not elem.is_displayed():
          continue
        elem_text = elem.text or ""
        norm_elem = _norm_filter_text(elem_text).replace("gb", "гб")
        if not norm_elem:
          continue
        target_digits = re.sub(r"\D", "", normalized_target)
        elem_digits = re.sub(r"\D", "", norm_elem)
        match = (
          norm_elem == normalized_target
          or normalized_target in norm_elem
          or norm_elem in normalized_target
          or (len(norm_elem) > 2 and (norm_elem in normalized_target or normalized_target in norm_elem))
          or (
            target_digits
            and elem_digits == target_digits
            and ("гб" in normalized_target or "gb" in normalized_target)
          )
        )
        if not match:
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
        f"{digits}гб",
        f"{digits} GB",
        f"{digits}GB",
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
    variants.extend(["1 SIM", "1 sim", "1sim", "SIM", "1 SIM-карта", "1 SIM"])
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
    return

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
    ok = False
    for option in _capacity_variants(value):
      if _click_text_option(driver, option, must_be_checkbox=True):
        ok = True
        break
    if ok:
      applied["memory"] += 1
      sleep(0.2)
    else:
      print(f"[AVITO] Не найден фильтр памяти: {value}")

  for value in filters.get("ram", []):
    ok = False
    for option in _capacity_variants(value):
      if _click_text_option(driver, option, must_be_checkbox=True):
        ok = True
        break
    if ok:
      applied["ram"] += 1
      sleep(0.2)
    else:
      print(f"[AVITO] Не найден фильтр RAM: {value}")

  for value in filters.get("sim", []):
    ok = False
    for option in _sim_variants(value):
      if _click_text_option(driver, option, must_be_checkbox=True):
        ok = True
        break
    if ok:
      applied["sim"] += 1
      sleep(0.2)
    else:
      print(f"[AVITO] Не найден фильтр SIM: {value}")

  for value in filters.get("colors", []):
    ok = False
    for option in _color_variants(value):
      if _click_text_option(driver, option, must_be_checkbox=True):
        ok = True
        break
    if ok:
      applied["colors"] += 1
      sleep(0.2)
    else:
      print(f"[AVITO] Не найден фильтр цвета: {value}")

  for value in filters.get("condition", []):
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
    ok = False
    for label in _rating_variants():
      if _click_text_option(driver, label, must_be_checkbox=True):
        ok = True
        break
    if ok:
      applied["rating_4_plus"] += 1
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

    blocked, reason = _is_avito_blocked(driver)
    if blocked:
      msg = (
        f"[AVITO] Обнаружена блокировка/проверка ({reason}). "
        "Подождите 30–60 мин или смените IP/прокси."
      )
      print(msg)
      if raise_on_block:
        raise AvitoBlockedError(msg)
      break

    wait = WebDriverWait(driver, EXPLICIT_WAIT)
    if page == 1 and filters and _has_meaningful_avito_ui_filters(filters):
      if status_callback:
        try:
          status_callback({"phase": "applying_filters"})
        except Exception as e:
          print(f"[AVITO] status_callback: {e}")
    if page == 1 and filters:
      try:
        _apply_avito_ui_filters(driver, filters)
        filtered_base_url = driver.current_url or ""
      except Exception as e:
        print(f"[AVITO] Не удалось применить часть фильтров: {e}")
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

  print(f"[AVITO] Всего объявлений (с повторами): {len(all_items)}")
  return all_items
