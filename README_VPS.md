# iFind on VPS

## 1) Prepare VPS

Ubuntu example:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip unzip curl
```

Install Chrome (or Chromium) and chromedriver.

**1 GB RAM:** Chrome часто падает по нехватке памяти. Добавь swap (1–2 GB) и проверяй OOM:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo dmesg -T | grep -i -E 'oom|killed process' | tail
```

## 2) Setup project

```bash
cd /opt
git clone <your-repo-url> ifind
cd ifind
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip setuptools
pip install -r requirements.txt
```

Copy env template:

```bash
cp .env.vps.example .env
```

Then set variables in `.env`:
- `BOT_TOKEN`
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- proxy on/off, waits, base urls
- `VPS_LIGHT_MODE=true` for 1 vCPU / 1GB VPS (faster, lighter parsing)

## 3) Run in non-interactive VPS mode

Wildberries:

```bash
python3 main.py --mode wb --keyword "iPhone" --model "13" --price-min 22000 --price-max 24000 --precision 7 --headless
```

Avito:

```bash
python3 main.py --mode avito --keyword "iPhone" --model "13" --city "Самара" --price-min 22000 --price-max 24000 --precision 7 --headless
```

Both:

```bash
python3 main.py --mode both --keyword "iPhone" --model "13" --city "Самара" --price-min 22000 --price-max 24000 --precision 7 --headless
```

Use direct WB URL:

```bash
python3 main.py --mode wb --wb-url "https://www.wildberries.ru/catalog/0/search.aspx?page=1&sort=popular&search=iphone+13" --precision 7 --headless
```

## 4) Run Telegram bot as systemd service

1. Copy `ifind-parser.service.example` to `/etc/systemd/system/ifind-parser.service`
2. Verify `ExecStart` points to `telegram_bot.py`
3. Start service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable ifind-parser
sudo systemctl start ifind-parser
sudo systemctl status ifind-parser
```

## 5) Optional helper script

```bash
chmod +x run_vps.sh
./run_vps.sh --mode wb --keyword "iPhone" --model "13" --price-min 22000 --price-max 24000 --precision 7 --headless
```
