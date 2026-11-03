"""检查后端 API；开发版还需检查 Vite 页面，避免单侧故障被标记为健康。"""

import os
from urllib.request import urlopen

urls = ["http://127.0.0.1:8765/api/v1/sessions"]
if os.environ.get("DOTCLAW_DEV") == "1":
    urls.append("http://127.0.0.1:5173/")
for url in urls:
    with urlopen(url, timeout=2) as response:
        if response.status != 200:
            raise SystemExit(1)
