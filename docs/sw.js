// Lettura offline: prima si prova la rete (così si vede sempre l'edizione più recente),
// se manca la connessione si mostra l'ultima copia salvata.
const CACHE = "ai-news-v1";
const BASE = ["./", "./index.html", "./manifest.webmanifest", "./icona-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(BASE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((k) => Promise.all(k.filter((n) => n !== CACHE).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET" || new URL(req.url).origin !== location.origin) return;
  if (req.url.endsWith(".mp3")) return; // l'audio non si salva: occupa troppo spazio
  e.respondWith(
    fetch(req)
      .then((r) => {
        if (r.ok) {
          const copia = r.clone();
          caches.open(CACHE).then((c) => c.put(req, copia));
        }
        return r;
      })
      .catch(() => caches.match(req).then((r) => r || caches.match("./index.html")))
  );
});
