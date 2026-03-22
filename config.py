import os


def _load_dotenv_file(path=".env"):
  if not os.path.exists(path):
    return
  try:
    with open(path, "r", encoding="utf-8") as fh:
      for raw in fh:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
          continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
          os.environ[key] = value
  except Exception:
    pass


_load_dotenv_file(".env")


def _env_bool(name, default=False):
  raw = os.getenv(name)
  if raw is None:
    return default
  return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name, default):
  raw = os.getenv(name)
  if raw is None or not str(raw).strip():
    return default
  try:
    return int(raw)
  except ValueError:
    return default


USE_MOBILE_PROXY = _env_bool("USE_MOBILE_PROXY", True)

MOBILE_PROXY_HOST = os.getenv("MOBILE_PROXY_HOST", "91.221.70.204")
MOBILE_PROXY_PORT = _env_int("MOBILE_PROXY_PORT", 10237)
MOBILE_PROXY_USER = os.getenv("MOBILE_PROXY_USER", "OU9tLKqk63")
MOBILE_PROXY_PASS = os.getenv("MOBILE_PROXY_PASS", "t8cfLDzi55")

IMPLICIT_WAIT = _env_int("IMPLICIT_WAIT", 10)
EXPLICIT_WAIT = _env_int("EXPLICIT_WAIT", 45)

# После page_load_strategy=none: сколько секунд ждать body + readyState (без ожидания «load»).
DOCUMENT_READY_TIMEOUT = max(15, _env_int("DOCUMENT_READY_TIMEOUT", 120))

AVITO_BASE_URL = os.getenv("AVITO_BASE_URL", "https://www.avito.ru").rstrip("/")
WB_BASE_URL = os.getenv("WB_BASE_URL", "https://www.wildberries.ru").rstrip("/")

# Light mode for low-resource VPS (1 vCPU / 1GB RAM): less pages and shorter pauses.
VPS_LIGHT_MODE = _env_bool("VPS_LIGHT_MODE", False)

# Максимум страниц выдачи Avito за один запуск (выдача может быть на десятках страниц).
AVITO_MAX_PAGES_PER_RUN = max(1, _env_int("AVITO_MAX_PAGES_PER_RUN", 5))

# При капче/блокировке на странице: столько раз ждём (смена IP у моб. прокси) и перезагружаем URL.
# Счётчик сбрасывается на каждой новой странице выдачи.
AVITO_BLOCK_MAX_RETRIES_PER_PAGE = max(1, _env_int("AVITO_BLOCK_MAX_RETRIES_PER_PAGE", 3))
AVITO_BLOCK_RETRY_WAIT_SEC = max(10, _env_int("AVITO_BLOCK_RETRY_WAIT_SEC", 130))

