// オフラインでも前回のデータを見られるようにする（データは常にネット優先）
const CACHE = "kento-v2";
self.addEventListener("install", e => e.waitUntil(
  caches.open(CACHE).then(c => c.addAll(["./", "index.html", "manifest.json", "icon.svg", "icon-192.png"]))));
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  e.respondWith(fetch(e.request).then(res => {
    const copy = res.clone();
    caches.open(CACHE).then(c => c.put(e.request, copy));
    return res;
  }).catch(() => caches.match(e.request)));
});

// 通知を受け取って表示する
self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = {body: e.data && e.data.text()}; }
  e.waitUntil(self.registration.showNotification(d.title || "中島健人ニュース", {
    body: d.body || "", icon: "icon-192.png", badge: "icon-192.png", data: {url: d.url || "./"},
  }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({type: "window"}).then(list => {
    const open = list.find(c => "focus" in c);
    return open ? open.focus() : clients.openWindow(e.notification.data.url);
  }));
});
