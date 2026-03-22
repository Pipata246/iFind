import argparse
import os
import random
import time

from seleniumwire import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from avito_parser import parse_avito
from config import (
  IMPLICIT_WAIT,
  MOBILE_PROXY_HOST,
  MOBILE_PROXY_PASS,
  MOBILE_PROXY_PORT,
  MOBILE_PROXY_USER,
  USE_MOBILE_PROXY,
  VPS_LIGHT_MODE,
)
from excel_export import export_to_excel
from wb_parser import parse_wb


def _parse_multi_values(raw):
  if not raw:
    return []
  return [p.strip() for p in str(raw).split(",") if p.strip()]


def _build_cli():
  parser = argparse.ArgumentParser(description="iFind parser (VPS-friendly)")
  parser.add_argument("--mode", choices=("avito", "wb", "both"), default="both")
  parser.add_argument("--keyword", default="")
  parser.add_argument("--model", default="")
  parser.add_argument("--city", default="")
  parser.add_argument("--price-min", type=int, default=None)
  parser.add_argument("--price-max", type=int, default=None)
  parser.add_argument("--precision", type=int, default=7)
  parser.add_argument("--headless", dest="headless", action="store_true", default=True)
  parser.add_argument("--no-headless", dest="headless", action="store_false")
  parser.add_argument("--wb-url", default="")
  parser.add_argument("--wb-today-only", action="store_true", help="WB: keep only items marked as today")
  parser.add_argument(
    "--avito-today-only",
    action="store_true",
    help="Avito: keep only listings whose card date looks like today",
  )
  parser.add_argument("--output-prefix", default="")

  # Avito advanced filters for non-interactive VPS runs
  parser.add_argument("--avito-memory", default="")
  parser.add_argument("--avito-ram", default="")
  parser.add_argument("--avito-sim", default="")
  parser.add_argument("--avito-colors", default="")
  parser.add_argument("--avito-condition", default="")
  parser.add_argument("--avito-seller-type", choices=("all", "private", "company"), default="all")
  parser.add_argument("--avito-rating-4-plus", action="store_true")

  # Local fallback mode only
  parser.add_argument("--interactive", action="store_true", help="Use interactive prompts")
  return parser


def _interactive_args(args):
  print("=== Selenium-парсер ===")
  args.mode = input("Что парсить? (avito / wb / both) [both]: ").strip().lower() or "both"
  args.keyword = input("Ключевое слово (например: iPhone): ").strip()
  args.model = input("Модель (например: 13, 15 Pro Max) [можно пусто]: ").strip()
  if args.mode in ("wb", "both"):
    args.wb_url = input("Ссылка WB для прямого парсинга [можно пусто]: ").strip()
  if args.mode in ("avito", "both"):
    args.city = input("Город (например: Самара) [можно пусто, только для Avito]: ").strip()
    pmin = input("Минимальная цена [пусто = нет]: ").strip()
    pmax = input("Максимальная цена [пусто = нет]: ").strip()
    args.price_min = int(pmin) if pmin.isdigit() else None
    args.price_max = int(pmax) if pmax.isdigit() else None
  while True:
    raw = input("Точность парсинга (1-10) [7]: ").strip() or "7"
    if raw.isdigit() and 1 <= int(raw) <= 10:
      args.precision = int(raw)
      break
    print("Введите число от 1 до 10.")
  args.headless = (input("Запускать браузер в фоне? (y/n) [y]: ").strip().lower() or "y") != "n"
  return args


def _validate_args(args):
  if not (1 <= args.precision <= 10):
    raise ValueError("precision must be in range 1..10")
  if args.mode in ("wb", "both") and not args.keyword and not args.wb_url:
    raise ValueError("for mode wb/both use --keyword or --wb-url")
  if args.mode in ("avito", "both") and not args.keyword:
    raise ValueError("for mode avito/both use --keyword")


def build_driver(headless=True):
  chrome_options = Options()
  chrome_options.page_load_strategy = "eager"
  chrome_options.add_argument("--disable-blink-features=AutomationControlled")
  chrome_options.add_argument("--no-sandbox")
  chrome_options.add_argument("--disable-dev-shm-usage")
  if VPS_LIGHT_MODE:
    # Меньше памяти и нагрузки на 1 GB RAM VPS.
    chrome_options.add_argument("--window-size=1280,720")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--memory-pressure-off")
  else:
    chrome_options.add_argument("--window-size=1920,1080")
  chrome_options.add_argument("--disable-infobars")
  chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
  chrome_options.add_experimental_option("useAutomationExtension", False)

  if headless:
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")

  use_proxy = USE_MOBILE_PROXY and MOBILE_PROXY_HOST and str(MOBILE_PROXY_HOST).strip()

  chromedriver_env_path = os.getenv("CHROMEDRIVER_PATH")
  if chromedriver_env_path:
    service = Service(chromedriver_env_path)
  else:
    # webdriver-manager can fail on some VPS setups (e.g. browser version detection).
    # Fallback to Selenium Manager to auto-resolve the driver.
    try:
      service = Service(ChromeDriverManager().install())
    except Exception as e:
      print(f"[Driver] webdriver-manager failed: {e}. Fallback to Selenium Manager.")
      service = Service()

  seleniumwire_options = {}
  if use_proxy:
    seleniumwire_options = {
      "proxy": {
        "http": f"http://{MOBILE_PROXY_USER}:{MOBILE_PROXY_PASS}@{MOBILE_PROXY_HOST}:{MOBILE_PROXY_PORT}",
        "https": f"http://{MOBILE_PROXY_USER}:{MOBILE_PROXY_PASS}@{MOBILE_PROXY_HOST}:{MOBILE_PROXY_PORT}",
      }
    }

  driver = webdriver.Chrome(service=service, options=chrome_options, seleniumwire_options=seleniumwire_options)
  try:
    driver.execute_cdp_cmd(
      "Page.addScriptToEvaluateOnNewDocument",
      {
        "source": """
          Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
          Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5], configurable: true });
          Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU','ru','en-US','en'], configurable: true });
          window.chrome = { runtime: {} };
        """
      },
    )
  except Exception:
    pass
  driver.set_page_load_timeout(120)
  driver.implicitly_wait(IMPLICIT_WAIT)
  if use_proxy:
    print(f"[Прокси] Используется мобильный прокси: {MOBILE_PROXY_HOST}:{MOBILE_PROXY_PORT}")
  else:
    print("[Прокси] Прокси отключен: используется IP VPS.")
  return driver


def main():
  parser = _build_cli()
  args = parser.parse_args()

  if args.interactive:
    args = _interactive_args(args)
  _validate_args(args)

  avito_filters = {
    "memory": _parse_multi_values(args.avito_memory),
    "ram": _parse_multi_values(args.avito_ram),
    "sim": _parse_multi_values(args.avito_sim),
    "colors": _parse_multi_values(args.avito_colors),
    "condition": _parse_multi_values(args.avito_condition),
    "seller_type": args.avito_seller_type,
    "rating_4_plus": bool(args.avito_rating_4_plus),
  }

  driver = build_driver(headless=args.headless)
  all_items = []
  try:
    if args.mode in ("avito", "both"):
      print("\n--- Парсим Avito ---")
      avito_items = parse_avito(
        driver,
        args.keyword,
        args.model,
        args.city,
        args.price_min,
        args.price_max,
        precision=args.precision,
        filters=avito_filters,
        today_only=bool(args.avito_today_only),
      )
      all_items.extend(avito_items)

    if args.mode in ("wb", "both"):
      if args.mode == "both":
        pause = random.uniform(8, 16)
        print(f"\n[Пауза] {pause:.0f} сек перед парсингом WB…")
        time.sleep(pause)
      print("\n--- Парсим Wildberries ---")
      wb_items = parse_wb(
        driver,
        args.keyword,
        args.model,
        args.price_min,
        args.price_max,
        precision=args.precision,
        wb_url=args.wb_url or None,
        wb_today_only=bool(args.wb_today_only),
      )
      all_items.extend(wb_items)
  finally:
    driver.quit()

  print(f"\nВсего найдено объявлений: {len(all_items)}")
  prefix = args.output_prefix or (
    "avito_results" if args.mode == "avito" else "wb_results" if args.mode == "wb" else "avito_wb_results"
  )
  export_to_excel(all_items, filename_prefix=prefix)


if __name__ == "__main__":
  main()
