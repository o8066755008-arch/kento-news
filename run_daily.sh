#!/bin/sh
# 1時間おきに実行する。
#   毎時（軽い更新）: ニュース取得・翻訳 → サムネイル → サイト用データ更新 → 通知
#   3時間おき（日本時間 0, 3, 6 … 21 時）と手動実行（FULL=1）: 上に加えて 動画・STARTO のグループ別ニュース・トピックまとめ・イベント・FC会員数
# 費用（AI）がかかるのは主に3時間おきの処理なので、毎時にしても費用はほとんど増えない
cd "$(dirname "$0")"
H=$(date +%-H)
if [ "$FULL" = "1" ] || [ $((H % 3)) -eq 0 ]; then FULL=1; fi
echo "[更新] $(date '+%Y-%m-%d %H:%M') $( [ "$FULL" = "1" ] && echo すべて || echo 軽い更新)"
python3 crawler.py
python3 thumbnails.py
if [ "$FULL" = "1" ]; then
  python3 videos.py
  python3 starto.py
  python3 digest.py
  python3 events.py
  python3 fanclub.py
fi
python3 build_site.py
python3 notify.py
