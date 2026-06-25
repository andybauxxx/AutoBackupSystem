"""Development helper: download the official Blender 4.2 Windows portable build."""

import re
import shutil
import tempfile
import urllib.request
from pathlib import Path


url = "https://download.blender.org/release/Blender4.2/"
request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
html = urllib.request.urlopen(request, timeout=30).read().decode("utf-8")
links = re.findall(r'href="([^"]+windows-x64\.zip)"', html)
versions = []
for link in links:
    match = re.search(r"blender-(4\.2\.\d+)-windows-x64\.zip", link)
    if match:
        versions.append((tuple(map(int, match.group(1).split("."))), link))

_version, filename = max(versions)
download_url = url + filename
target = Path(tempfile.gettempdir()) / filename
if not target.exists():
    download_request = urllib.request.Request(
        download_url, headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(download_request, timeout=60) as source:
        with target.open("wb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
print(f"BLENDER_42_ARCHIVE={target}")
