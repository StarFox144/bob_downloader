import os
from pathlib import Path
import json
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# try .env
env_path = Path(__file__).with_name('.env')
token = None
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if line.startswith('TELEGRAM_TOKEN='):
            token = line.split('=', 1)[1].strip()
            break

if not token:
    token = os.environ.get('TELEGRAM_TOKEN')

if not token:
    print('TELEGRAM_TOKEN not found')
    raise SystemExit(2)

url = f'https://api.telegram.org/bot{token}/deleteWebhook'
req = Request(url, method='POST')
try:
    with urlopen(req, timeout=10) as resp:
        data = resp.read().decode('utf-8')
        print('Response:', data)
except HTTPError as e:
    print('HTTPError:', e.code, e.reason)
    print(e.read().decode('utf-8'))
    raise
except URLError as e:
    print('URLError:', e.reason)
    raise
