// Service worker: shows the server's Web Push notice (fixed text) and opens the app when it is tapped. No caching.
const FALLBACK_TITLE = "SendPidge";
const APP_URL = "./index.html";

function readNoticeOrReportError(pushEvent) {
  try {
    return pushEvent.data ? pushEvent.data.json() : null;
  } catch (parseError) {
    console.error("SendPidge: unparseable push notice", parseError.name);
    return null;
  }
}

self.addEventListener("push", (pushEvent) => {
  const notice = readNoticeOrReportError(pushEvent);
  const title = typeof notice?.title === "string" ? notice.title : FALLBACK_TITLE;
  const body = typeof notice?.body === "string" ? notice.body : "Open SendPidge.";
  pushEvent.waitUntil(self.registration.showNotification(title, { body, icon: "assets/pidge-icon.png", tag: notice?.notice ?? "pidge" }));
});

self.addEventListener("notificationclick", (clickEvent) => {
  clickEvent.notification.close();
  clickEvent.waitUntil((async () => {
    const openWindows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    if (openWindows.length > 0) return openWindows[0].focus();
    return self.clients.openWindow(APP_URL);
  })());
});
