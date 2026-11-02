async function checks() {
  const { state, acquireController, loseControl, requestJson } = client;
  const sent = [];
  transport = async (path) => {
    sent.push(path);
    if (path === "/api/controller/claim") return reply({ ok: true, controller: { owned: true, token: "first-token" } });
    return reply({ ok: true, mangas: [], snapshot_id: "first-snapshot" });
  };
  await acquireController();
  assert(state.ownsControl, "First page claims control automatically");
  assert(element("state-screen").hidden && !element("home-screen").hidden, "Claim opens home");
  assert(sent.join(",") === "/api/controller/claim,/api/library", "No polling or extra requests on entry");

  sent.length = 0;
  transport = async (path, options) => {
    sent.push(path);
    assert(options.headers.get("X-Page-Id") === state.pageId, "Protected requests use the common page header");
    assert(options.headers.get("X-Controller-Token") === "first-token", "Existing token protects requests");
    return reply({ ok: true, mangas: [], snapshot_id: "first-snapshot" });
  };
  await requestJson("/api/library");
  assert(sent.join(",") === "/api/library", "Protected request carries ownership headers");

  state.positionQueue.set("unsure", { imageId: "unsure-image" });
  loseControl();
  assert(!state.ownsControl && element("library-screen").hidden, "Transferred control hides the whole app");
  assert(!element("state-screen").hidden && element("state-action").textContent === "Take Control", "Transfer shows the waiting screen");
  assert(state.positionQueue.size === 1, "Transfer does not silently lose an unconfirmed change");
  transport = async (path) => {
    if (path === "/api/controller/takeover") return reply({ ok: true, controller: { owned: true, token: "replacement-token" } });
    assert(path === "/api/library", "Takeover never replays the uncertain position");
    return reply({ ok: true, mangas: [], snapshot_id: "new-snapshot" });
  };
  await acquireController({ takeover: true });
  assert(state.ownsControl && state.controllerToken === "replacement-token", "Explicit takeover uses a fresh grant");
  assert(state.positionQueue.size === 0, "Takeover refreshes authoritative data instead of replaying uncertain changes");
  assert(element("action-error-title").textContent === "Review unconfirmed changes", "User is told which changes need review");

  let complete;
  transport = async () => new Promise((resolve) => { complete = resolve; });
  const pending = requestJson("/api/library");
  state.controllerEpoch += 1;
  complete(reply({ ok: true, mangas: [] }));
  let staleRejected = false;
  try { await pending; } catch (error) { staleRejected = error.code === "controller_changed"; }
  assert(staleRejected, "A late response from an older owner cannot replace current state");
  assert(intervals === 0, "Ownership never creates background polling");

  await openMangaChecks();
  await recommendationChecks();
  await recommendedReaderChecks();
  await transientSaveChecks();
}

async function recommendedReaderChecks() {
  const { state, selectRecommendationFilter, requestRecommendation, readRecommendation, navigateBackAfterMutations } = client;
  let remembered;
  window.localStorage.setItem = (key, value) => { remembered = [key, value]; };
  selectRecommendationFilter("adult");
  assert(state.recommendationFilter === "adult" && remembered[1] === "adult", "One tap chooses and remembers the pool");
  assert(element("filter-adult").getAttribute("aria-pressed") === "true" && element("filter-all").getAttribute("aria-pressed") === "false", "The pool exposes exactly one active option");

  const pending = { id: "direct-offer", manga: "[Author] Direct", manga_id: "m_direct", filter: "adult" };
  const requests = [];
  transport = async (path, options) => {
    requests.push(path);
    if (path === "/api/recommendations/offer") {
      assert(JSON.parse(options.body).filter === "adult", "Recommend submits the selected pool");
      return reply({ ok: true, snapshot_id: "direct-snapshot", mangas: [{ id: "m_direct", name: pending.manga }], recommendations: { pending, preferences: {} } });
    }
    if (path === "/api/manga/m_direct?activity=read") {
      return reply({ ok: true, activity: "read", manga: { id: "m_direct", name: pending.manga, current_folder_id: "f_direct", current_image_id: "i_saved", folders: [{ id: "f_direct", name: "Chapter 3" }] } });
    }
    if (path === "/api/folder/f_direct?activity=read") {
      return reply({ ok: true, activity: "read", folder: { id: "f_direct", current_image_id: "i_saved", name: "Chapter 3", images: [{ id: "i_first", name: "1.jpg" }, { id: "i_saved", name: "2.jpg" }] } });
    }
    if (path === "/api/image/i_saved") {
      return { ok: true, headers: { get: () => "image/jpeg" }, blob: async () => ({}) };
    }
    throw new Error(`Unexpected request: ${path}`);
  };
  const serveReader = transport;
  await requestRecommendation();
  assert(!element("reader-screen").hidden && element("recommendation-screen").hidden, "Recommend goes straight into the reader");
  assert(state.activity === "read" && state.currentImageIndex === 1, "Recommended reading resumes its saved Read position");
  assert(requests.slice(0, 3).join(",") === "/api/recommendations/offer,/api/manga/m_direct?activity=read,/api/folder/f_direct?activity=read", "Recommend opens Read without a separate click or redundant scan");
  assert(state.recommendations.pending && element("back-to-activities").getAttribute("aria-label") === "Back to recommendation feedback", "Opening the reader keeps feedback pending");

  await navigateBackAfterMutations();
  assert(element("reader-screen").hidden && !element("recommendation-screen").hidden, "The reader back arrow opens feedback");
  await readRecommendation();
  assert(!element("reader-screen").hidden, "Read on feedback can reopen the reader");
  await navigateBackAfterMutations();

  // A failed initial reader request must still leave a usable feedback screen.
  transport = async (path) => {
    if (path === "/api/recommendations/offer") return reply({ ok: true, mangas: [{ id: "m_direct", name: pending.manga }], recommendations: { pending, preferences: {} } });
    throw new Error("Wi-Fi interrupted while opening Read");
  };
  await requestRecommendation();
  assert(!element("recommendation-screen").hidden && !element("action-error").hidden, "A failed reader launch preserves feedback and offers retry");
  assert(!state.recommendationBusy && state.recommendations.pending.id === pending.id, "A failed reader launch does not resolve feedback");

  transport = async (path, options) => {
    if (path === "/api/folder/f_direct?activity=read") throw new Error("Folder request interrupted");
    return serveReader(path, options);
  };
  await requestRecommendation();
  assert(!element("recommendation-screen").hidden && typeof state.actionRetry === "function", "A failed initial folder request also leaves a usable retry");
  transport = serveReader;
  await state.actionRetry();
  assert(!element("reader-screen").hidden, "Retry after a folder failure reopens Read successfully");
  await navigateBackAfterMutations();
  client.applyLibraryPayload({ mangas: [], recommendations: { pending: null, preferences: {} } });
}

async function recommendationChecks() {
  const { state, recommendationAction, applyLibraryPayload, showLibrary, showHome, historyChanged } = client;
  const pending = { id: "offer", manga: "[Author] Recommended", manga_id: "m_rec", filter: "all" };
  const preferences = { "[Author] Recommended": { favorite: false } };
  applyLibraryPayload({ snapshot_id: "recommendation-snapshot", mangas: [{ id: "m_rec", name: pending.manga }], recommendations: { pending, preferences } });
  assert(!element("recommendation-screen").hidden && element("home-screen").hidden, "Pending feedback is restored instead of home");
  assert(element("recommendation-title").textContent === pending.manga, "Pending feedback uses the stored title");
  showLibrary();
  assert(element("library-screen").hidden, "Pending feedback blocks Explore");
  await historyChanged({ state: { view: "library" } });
  assert(!element("recommendation-screen").hidden, "Browser Back cannot bypass feedback");

  transport = async () => reply({ ok: true, recommendations: { pending, preferences: { [pending.manga]: { favorite: true } } } });
  await recommendationAction("preference", { manga: pending.manga, flag: "favorite", value: true }, client.showRecommendation);
  assert(state.recommendations.pending && state.recommendations.preferences[pending.manga].favorite, "Favorite does not resolve the offer");

  const attempts = [];
  let online = false;
  transport = async (path, options) => {
    attempts.push([path, options.body]);
    if (!online) throw new Error("Lost response");
    return reply({ ok: true, recommendations: { pending: null, preferences } });
  };
  await recommendationAction("feedback", { id: "offer", feedback: "no_feedback" }, () => showHome());
  assert(state.recommendationUnconfirmed && !element("recommendation-screen").hidden, "Unconfirmed feedback keeps its screen and retry");
  showHome();
  assert(element("home-screen").hidden, "Unconfirmed feedback cannot be bypassed");
  assert(!element("action-error").hidden && element("action-error-retry").textContent === "Retry", "Blocked navigation keeps the retry available");
  const retry = state.recommendationUnconfirmed;
  online = true;
  await retry();
  assert(attempts.length === 2 && attempts[0][1] === attempts[1][1], "Retry resends the same offer ID and outcome");
  assert(!state.recommendations.pending && !state.recommendationUnconfirmed && !element("home-screen").hidden, "Confirmed No feedback returns home");

  applyLibraryPayload({ mangas: [], recommendations: { pending: { ...pending, manga_id: null }, preferences } });
  assert(!element("recommendation-screen").hidden && element("recommendation-read").hidden, "Removed pending title still offers feedback without Read");
  assert(client.readRecommendationFilter() === "all", "The filter defaults to All");
  window.localStorage.getItem = () => "adult";
  assert(client.readRecommendationFilter() === "adult", "The filter is browser-local");
  window.localStorage.getItem = () => { throw new Error("Storage disabled"); };
  assert(client.readRecommendationFilter() === "all", "Unavailable browser storage does not block startup");
  applyLibraryPayload({ mangas: [], recommendations: { pending: null, preferences: {} } });
}

async function openMangaChecks() {
  const { state, openManga } = client;
  const manga = { id: "m_one", name: "Series One" };
  state.library = [manga];
  const scans = [];
  transport = async (path, options) => {
    scans.push([path, options.method, options.body]);
    return reply({ ok: true, manga: { id: "m_one", name: "Series One" } });
  };
  await openManga(manga);
  assert(scans.length === 1, "Tapping a manga sends exactly one request");
  assert(scans[0].join(" ") === "/api/manga/m_one/scan POST {}", "Tapping a manga scans it on the PC");
  assert(!element("activity-screen").hidden && element("library-screen").hidden, "A scanned manga opens its Read/Edit choice");
  assert(element("activity-title").textContent === "Series One", "The choice is titled with the manga name");

  transport = async () => reply({ ok: false, error: { code: "no_images", message: "None of the folders in 'Series One' contain JPG or PNG images." } }, 409);
  await openManga(manga);
  assert(element("action-error-title").textContent === "Can’t open Series One", "An unreadable manga names itself");
  assert(element("action-error-message").textContent.includes("JPG or PNG"), "The server explains why it cannot be opened");
  assert(!state.mangaOpening, "A failed open can be retried");
  element("action-error-title").textContent = "";
}

async function transientSaveChecks() {
  const { state, ensurePositionFlush, drainPendingMutations, setSelection } = client;
  // Retry delays are short; request timeouts (5s+) are never fired here.
  const timers = [];
  window.setTimeout = (callback, delay) => { timers.push({ callback, delay }); return timers.length; };
  const settle = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };
  const runRetryTimers = async () => {
    const due = timers.splice(0).filter((timer) => timer.delay < 5_000);
    due.forEach((timer) => timer.callback());
    await settle();
    await state.positionFlushPromise;
    await state.selectionRequestTail;
    await settle();
  };
  const banner = () => element("action-error-title").textContent;
  const position = () => ({ activity: "read", folderId: "folder", imageId: "page-19", epoch: state.activityEpoch, ownershipEpoch: state.controllerEpoch });
  state.ownsControl = true;
  state.controllerToken = "replacement-token";
  element("action-error-title").textContent = "";

  let online = false;
  let sent = 0;
  transport = async () => {
    sent += 1;
    if (!online) throw new Error("The network connection was lost.");
    return reply({ ok: true, position: {} });
  };
  state.positionQueue.set("read:manga", position());
  await ensurePositionFlush();
  assert(!state.connectionInterrupted && banner() === "", "A missed position save does not interrupt reading");
  assert(state.positionQueue.size === 1, "The latest position is kept for a quiet retry");
  online = true;
  await runRetryTimers();
  assert(sent === 2 && state.positionQueue.size === 0, "The position is saved by the quiet retry");

  online = false;
  state.positionQueue.set("read:manga", position());
  assert(await drainPendingMutations(), "Leaving a view does not wait on a missed position");
  assert(state.positionQueue.size === 0 && banner() === "", "The missed position is dropped silently");
  timers.length = 0;

  state.activity = "edit";
  const folder = { id: "folder", revision: 0, images: [], selectedCount: 0 };
  const blip = { id: "blip-page", selected: false };
  let failures = 1;
  transport = async () => {
    if (failures > 0) {
      failures -= 1;
      throw new Error("The network connection was lost.");
    }
    return reply({ ok: true, selection: { selected: true, revision: 1 } });
  };
  await setSelection(folder, blip, true, state.activityEpoch);
  assert(state.selectionPending.has("blip-page"), "A selection stays pending during its quiet retry");
  assert(!state.connectionInterrupted && banner() === "", "One failed selection attempt shows no banner");
  await runRetryTimers();
  assert(!state.selectionPending.has("blip-page") && blip.selected, "The quiet retry confirms the selection");
  assert(banner() === "", "A recovered selection never showed a banner");

  const down = { id: "down-page", selected: false };
  let attempts = 0;
  transport = async () => {
    attempts += 1;
    throw new Error("The network connection was lost.");
  };
  await setSelection(folder, down, true, state.activityEpoch);
  await runRetryTimers();
  assert(!state.connectionInterrupted, "Quiet retries continue before reporting");
  await runRetryTimers();
  assert(attempts === 3, "A selection is attempted three times in total");
  assert(state.connectionInterrupted && banner() === "Connection interrupted", "Persistent failure asks the user to retry");
  assert(state.selectionPending.has("down-page") && !down.selected, "An unconfirmed selection is kept, not assumed");

  state.selectionPending.clear();
  state.connectionInterrupted = false;
  transport = async () => reply({ ok: false, error: { code: "controller_transferred", message: "Moved." } }, 409);
  state.positionQueue.set("read:manga", position());
  await ensurePositionFlush();
  assert(!state.ownsControl && element("state-action").textContent === "Take Control", "Losing control is still reported immediately");
}
checks().then(
  () => { if (typeof print === "function") print("Frontend behavior checks passed"); else console.log("Frontend behavior checks passed"); },
  (error) => { if (typeof print === "function") { print(error.stack); throw error; } else { console.error(error); process.exitCode = 1; } },
);
