// オフラインでも前回のデータを見られるようにする（データは常にネット優先）
const CACHE = "kento-v1";
self.addEventListener("install", e => e.waitUntil(
  caches.open(CACHE).then(c => c.addAll(["./", "index.html", "manifest.json", "icon.svg"]))));
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  e.respondWith(fetch(e.request).then(res => {
    const copy = res.clone();
    caches.open(CACHE).then(c => c.put(e.request, copy));
    return res;
  }).catch(() => caches.match(e.request)));
});
