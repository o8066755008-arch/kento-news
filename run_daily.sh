#!/bin/sh
# 1日1回実行する: ニュース取得 → 翻訳 → FC会員数記録 → サイト用データ更新
cd "$(dirname "$0")"
python3 crawler.py
python3 fanclub.py
python3 build_site.py
