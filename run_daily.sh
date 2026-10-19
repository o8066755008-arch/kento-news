#!/bin/sh
# 3時間おきに実行する: ニュース取得 → 動画 → 翻訳 → サムネイル → トピックまとめ → イベント → FC会員数記録 → サイト用データ更新 → 通知
cd "$(dirname "$0")"
python3 crawler.py
python3 videos.py
python3 thumbnails.py
python3 digest.py
python3 events.py
python3 fanclub.py
python3 build_site.py
python3 notify.py
