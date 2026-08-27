"use strict";

(() => {
  const HEARTBEAT_INTERVAL_MS = 12_000;
  const STATUS_POLL_INTERVAL_MS = 4_000;
  const REQUEST_TIMEOUT_MS = 15_000;
  const READ = "read";
  const EDIT = "edit";

  const ROUTES = Object.freeze({
    status: "/api/status",
    pair: "/api/pair",
    claim: "/api/controller/claim",
    heartbeat: "/api/controller/heartbeat",
    release: "/api/controller/release",
    library: "/api/library",
    manga: (id, activity) => (
      `/api/manga/${encodeURIComponent(id)}?activity=${encodeURIComponent(activity)}`
    ),
    folder: (id, activity) => (
      `/api/folder/${encodeURIComponent(id)}?activity=${encodeURIComponent(activity)}`
    ),
    image: (id) => `/api/image/${encodeURIComponent(id)}`,
    readPosition: (id) => `/api/read/folder/${encodeURIComponent(id)}/position`,
    editPosition: (id) => `/api/edit/folder/${encodeURIComponent(id)}/position`,
    editSelection: (id) => `/api/edit/folder/${encodeURIComponent(id)}/selection`,
  });

  const element = (id) => document.getElementById(id);
  const elements = Object.freeze({
    stateScreen: element("state-screen"),
    stateSpinner: element("state-spinner"),
    stateKicker: element("state-kicker"),
    stateTitle: element("state-title"),
    stateMessage: element("state-message"),
    stateAction: element("state-action"),
    stateDetail: element("state-detail"),
    pairForm: element("pair-form"),
    pairCode: element("pair-code"),
    pairHint: element("pair-hint"),
    pairSubmit: element("pair-submit"),
    libraryScreen: element("library-screen"),
    libraryRefresh: element("library-refresh"),
    librarySummary: element("library-summary"),
    libraryNotice: element("library-notice"),
    libraryList: element("library-list"),
    libraryEmpty: element("library-empty"),
    emptyRetry: element("empty-retry"),
    activityScreen: element("activity-screen"),
    activityTitle: element("activity-title"),
    backToLibrary: element("back-to-library"),
    chooseRead: element("choose-read"),
    chooseEdit: element("choose-edit"),
    readerScreen: element("reader-screen"),
    readerStage: element("reader-stage"),
    imageDisplay: element("image-display"),
    selectionFrame: element("selection-frame"),
    selectionTab: element("selection-tab"),
    previousZone: element("previous-zone"),
    selectionZone: element("selection-zone"),
    nextZone: element("next-zone"),
    chromeToggle: element("chrome-toggle"),
    topChrome: element("top-chrome"),
    backToActivities: element("back-to-activities"),
    folderPicker: element("folder-picker"),
    selectedPickerShell: element("selected-picker-shell"),
    selectedPicker: element("selected-picker"),
    imagePicker: element("image-picker"),
    bottomFolder: element("bottom-folder"),
    bottomPosition: element("bottom-position"),
    imageLoading: element("image-loading"),
    imageError: element("image-error"),
    imageRetry: element("image-retry"),
    boundaryCue: element("boundary-cue"),
    readerFeedback: element("reader-feedback"),
    actionError: element("action-error"),
    actionErrorTitle: element("action-error-title"),
    actionErrorMessage: element("action-error-message"),
    actionErrorRetry: element("action-error-retry"),
    actionErrorDismiss: element("action-error-dismiss"),
    toast: element("toast"),
    connectionAnnouncer: element("connection-announcer"),
  });

  class ApiError extends Error {
    constructor(code, message, status = 0, payload = null) {
      super(message);
      this.name = "ApiError";
      this.code = String(code || "request_failed").toLowerCase();
      this.status = status;
      this.payload = payload;
    }
  }

  const state = {
    clientId: getClientId(),
    pageInstanceId: createOpaqueId(),
    snapshotId: null,
    leaseClaimed: false,
    heartbeatTimer: 0,
    heartbeatInFlight: false,
    statusPollTimer: 0,
    stateAction: null,
    library: [],
    libraryIssueCount: 0,
    activityManga: null,
    activity: null,
    currentManga: null,
    currentFolder: null,
    currentImageIndex: 0,
    viewRequestToken: 0,
    activityEpoch: 0,
    chromeVisible: true,
    selectionPending: new Map(),
    selectionRequestTail: Promise.resolve(),
    actionRetry: null,
    toastTimer: 0,
    boundaryTimer: 0,
    feedbackTimer: 0,
    animationTimer: 0,
    imageRequestToken: 0,
    imageController: null,
    currentImageUrl: null,
    prefetchController: null,
    prefetchedImages: new Map(),
    positionQueue: new Map(),
    positionFlushActive: false,
    positionFlushPromise: Promise.resolve(),
    mutationBarrierTail: Promise.resolve(),
    activityOpening: false,
    entryNavigationPending: false,
    entryNavigationToken: 0,
    historyNavigationPending: false,
    historyChangeToken: 0,
  };

  function getClientId() {
    const storageKey = "pocket-manga-companion-client";
    try {
      const existing = window.sessionStorage.getItem(storageKey);
      if (existing && /^[A-Za-z0-9._~-]{1,128}$/.test(existing)) {
        return existing;
      }
    } catch (_error) {
      // An in-memory identity remains valid when storage is unavailable.
    }
    const id = createOpaqueId();
    try {
      window.sessionStorage.setItem(storageKey, id);
    } catch (_error) {
      // An in-memory identity remains valid for this document lifetime.
    }
    return id;
  }

  function createOpaqueId() {
    if (typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }

  function bindEvents() {
    elements.pairForm.addEventListener("submit", pairDevice);
    elements.stateAction.addEventListener("click", () => {
      if (typeof state.stateAction === "function") {
        void state.stateAction();
      }
    });
    elements.libraryRefresh.addEventListener("click", () => void loadLibrary());
    elements.emptyRetry.addEventListener("click", () => void loadLibrary());
    elements.backToLibrary.addEventListener("click", navigateBackAfterMutations);
    elements.chooseRead.addEventListener("click", () => void chooseActivity(READ));
    elements.chooseEdit.addEventListener("click", () => void chooseActivity(EDIT));
    elements.backToActivities.addEventListener("click", navigateBackAfterMutations);
    elements.folderPicker.addEventListener("change", (event) => {
      const folderId = event.currentTarget.value;
      if (folderId) {
        void openFolder(folderId, "", { persist: true, entryEdge: "first" });
      }
    });
    elements.selectedPicker.addEventListener("change", (event) => {
      const imageId = event.currentTarget.value;
      event.currentTarget.value = "";
      if (imageId) {
        goToImageId(imageId);
      }
    });
    elements.imagePicker.addEventListener("change", (event) => {
      goToImageId(event.currentTarget.value);
    });
    elements.previousZone.addEventListener("click", () => navigateImage(-1));
    elements.nextZone.addEventListener("click", () => navigateImage(1));
    elements.selectionZone.addEventListener("click", toggleCurrentSelection);
    elements.chromeToggle.addEventListener("click", toggleChrome);
    elements.imageRetry.addEventListener("click", loadCurrentImage);
    elements.actionErrorRetry.addEventListener("click", () => {
      const retry = state.actionRetry;
      hideActionError();
      if (typeof retry === "function") {
        void retry();
      }
    });
    elements.actionErrorDismiss.addEventListener("click", hideActionError);

    for (const control of [elements.topChrome, elements.backToActivities]) {
      for (const eventName of ["click", "pointerup", "touchend"]) {
        control.addEventListener(eventName, (event) => event.stopPropagation());
      }
    }
    for (const eventName of ["contextmenu", "dragstart", "dblclick", "selectstart"]) {
      elements.readerStage.addEventListener(eventName, (event) => event.preventDefault());
    }

    elements.imageDisplay.addEventListener("load", imageLoaded);
    elements.imageDisplay.addEventListener("error", imageFailed);
    window.addEventListener("resize", syncSelectionFrame, { passive: true });
    window.addEventListener("orientationchange", () => window.setTimeout(syncSelectionFrame, 80));
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", syncSelectionFrame, { passive: true });
    }
    window.addEventListener("popstate", historyChanged);
    document.addEventListener("visibilitychange", visibilityChanged);
    window.addEventListener("pagehide", (event) => {
      if (!event.persisted) {
        releaseController();
      }
    });
    window.addEventListener("online", () => void bootstrap());
    window.addEventListener("offline", () => {
      showUnavailable("This iPhone is offline. Reconnect to the same Wi-Fi as the PC.");
    });
  }

  async function requestJson(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.controller !== false) {
      headers.set("X-Companion-Instance", state.clientId);
      headers.set("X-Companion-Page", state.pageInstanceId);
    }
    let body;
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(options.body);
    }
    try {
      const response = await fetch(path, {
        method: options.method || "GET",
        headers,
        body,
        credentials: "same-origin",
        cache: "no-store",
        signal: options.signal || controller.signal,
      });
      const contentType = response.headers.get("content-type") || "";
      const payload = contentType.includes("application/json")
        ? await response.json()
        : null;
      if (!response.ok || !payload || payload.ok === false) {
        const detail = payload && typeof payload.error === "object" ? payload.error : {};
        throw new ApiError(
          detail.code || `http_${response.status}`,
          detail.message || `The PC returned an unexpected response (${response.status}).`,
          response.status,
          payload,
        );
      }
      return payload;
    } catch (error) {
      if (error instanceof ApiError || error.name === "AbortError") {
        throw error;
      }
      throw new ApiError("network_error", "Could not reach Pocket Manga Editor on this network.");
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function requestImage(imageId, signal) {
    let response;
    try {
      response = await fetch(ROUTES.image(imageId), {
        method: "GET",
        headers: {
          Accept: "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.5",
          "X-Companion-Instance": state.clientId,
          "X-Companion-Page": state.pageInstanceId,
        },
        credentials: "same-origin",
        cache: "default",
        signal,
      });
    } catch (error) {
      if (error.name === "AbortError") {
        throw error;
      }
      throw new ApiError("network_error", "The image could not be reached.");
    }
    if (!response.ok) {
      let detail = null;
      try {
        detail = await response.json();
      } catch (_error) {
        // Image failures are allowed to have an empty body.
      }
      const apiDetail = detail && typeof detail.error === "object" ? detail.error : {};
      throw new ApiError(
        apiDetail.code || `http_${response.status}`,
        apiDetail.message || "The image is unavailable.",
        response.status,
        detail,
      );
    }
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.startsWith("image/")) {
      throw new ApiError("invalid_image", "The PC returned an invalid image.");
    }
    return response.blob();
  }

  async function bootstrap() {
    clearStatusPoll();
    stopHeartbeat();
    showState({
      kicker: "Companion",
      title: "Connecting to your PC",
      message: "Checking the local Companion service…",
      loading: true,
    });
    try {
      const payload = await requestJson(ROUTES.status, { controller: false });
      const status = payload.status;
      if (!status || status.server !== "available") {
        showUnavailable("Pocket Manga Editor reports that Companion is unavailable.");
        return;
      }
      if (!status.paired) {
        showPairing(Boolean(status.pairing_open));
        return;
      }
      if (!status.companion_active) {
        showInactive();
        return;
      }
      await claimController();
    } catch (error) {
      if (error.name === "AbortError") {
        showUnavailable("The PC did not respond in time.");
      } else {
        handleSessionError(error, bootstrap);
      }
    }
  }

  async function pairDevice(event) {
    event.preventDefault();
    const code = elements.pairCode.value.trim();
    if (!code) {
      elements.pairCode.focus();
      elements.pairHint.textContent = "Enter the code shown on the PC.";
      return;
    }
    elements.pairSubmit.disabled = true;
    elements.pairSubmit.textContent = "Pairing…";
    elements.pairHint.textContent = "Confirming this iPhone with the PC…";
    try {
      await requestJson(ROUTES.pair, {
        method: "POST",
        body: { code },
        controller: false,
      });
      elements.pairCode.value = "";
      announce("This iPhone is paired.");
      await bootstrap();
    } catch (error) {
      if (
        error instanceof ApiError
        && (error.code === "pairing_closed" || error.code === "pairing_rate_limited")
      ) {
        showPairing(false, friendlyMessage(error));
      } else {
        elements.pairHint.textContent = friendlyMessage(error, "The pairing code was not accepted.");
        elements.pairCode.select();
      }
    } finally {
      elements.pairSubmit.disabled = false;
      elements.pairSubmit.textContent = "Pair this iPhone";
    }
  }

  async function claimController() {
    showState({
      kicker: "Paired",
      title: "Opening your library",
      message: "Claiming the Companion controller lease…",
      loading: true,
    });
    try {
      const payload = await requestJson(ROUTES.claim, {
        method: "POST",
        body: { client_id: state.clientId, page_id: state.pageInstanceId },
      });
      state.leaseClaimed = Boolean(payload.controller && payload.controller.claimed);
      state.snapshotId = payload.snapshot_id || null;
      startHeartbeat();
      await loadLibrary();
    } catch (error) {
      handleSessionError(error, bootstrap);
    }
  }

  function startHeartbeat() {
    stopHeartbeat();
    state.heartbeatTimer = window.setInterval(sendHeartbeat, HEARTBEAT_INTERVAL_MS);
  }

  function stopHeartbeat() {
    if (state.heartbeatTimer) {
      window.clearInterval(state.heartbeatTimer);
      state.heartbeatTimer = 0;
    }
    state.heartbeatInFlight = false;
  }

  async function sendHeartbeat() {
    if (state.heartbeatInFlight || document.visibilityState === "hidden") {
      return;
    }
    state.heartbeatInFlight = true;
    try {
      const payload = await requestJson(ROUTES.heartbeat, {
        method: "POST",
        body: { client_id: state.clientId, page_id: state.pageInstanceId },
      });
      if (payload.snapshot_id && state.snapshotId && payload.snapshot_id !== state.snapshotId) {
        await loadLibrary();
      }
    } catch (error) {
      handleSessionError(error, bootstrap);
    } finally {
      state.heartbeatInFlight = false;
    }
  }

  function releaseController() {
    if (
      !state.leaseClaimed
      || state.positionFlushActive
      || state.positionQueue.size
      || state.selectionPending.size
    ) {
      return;
    }
    state.leaseClaimed = false;
    stopHeartbeat();
    void fetch(ROUTES.release, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Companion-Instance": state.clientId,
        "X-Companion-Page": state.pageInstanceId,
      },
      body: JSON.stringify({
        client_id: state.clientId,
        page_id: state.pageInstanceId,
      }),
      credentials: "same-origin",
      cache: "no-store",
      keepalive: true,
    }).catch(() => {});
  }

  async function visibilityChanged() {
    if (document.visibilityState === "hidden") {
      stopHeartbeat();
      return;
    }
    if (!navigator.onLine) {
      showUnavailable("This iPhone is offline. Reconnect to the same Wi-Fi as the PC.");
      return;
    }
    if (state.leaseClaimed && state.snapshotId) {
      await resumeController();
    } else {
      await bootstrap();
    }
  }

  async function resumeController() {
    try {
      const previousSnapshotId = state.snapshotId;
      const payload = await requestJson(ROUTES.claim, {
        method: "POST",
        body: { client_id: state.clientId, page_id: state.pageInstanceId },
      });
      state.leaseClaimed = Boolean(payload.controller && payload.controller.claimed);
      state.snapshotId = payload.snapshot_id || null;
      startHeartbeat();
      if (!state.snapshotId || state.snapshotId !== previousSnapshotId) {
        await loadLibrary();
        return;
      }
      await drainPendingMutations();
      if (
        state.activity
        && state.currentFolder
        && state.currentManga
        && state.activityManga
      ) {
        const activity = state.activity;
        showActivityChoice(state.activityManga, { historyMode: "none" });
        await chooseActivity(activity, { historyMode: "none" });
      } else if (state.activityManga) {
        showActivityChoice(state.activityManga, { historyMode: "none" });
      } else {
        showLibrary({ historyMode: "none" });
      }
      announce("Companion reconnected.");
    } catch (error) {
      handleSessionError(error, bootstrap);
    }
  }

  function handleSessionError(error, retry) {
    const code = error instanceof ApiError ? error.code : "network_error";
    if (code === "unauthorized" || code === "unpaired") {
      showPairing(false, "This browser is not authorized. Open pairing on the PC to pair again.");
      return;
    }
    if (code === "inactive_mode") {
      showInactive();
      return;
    }
    if (code === "lease_conflict") {
      showOccupied();
      return;
    }
    if (code === "stale_snapshot" || code === "invalid_snapshot") {
      state.snapshotId = null;
      showUnavailable("The PC library changed. Reconnect to load the current snapshot.", bootstrap);
      return;
    }
    showUnavailable(friendlyMessage(error, "Pocket Manga Editor could not be reached."), retry);
  }

  function showPairing(pairingOpen, overrideMessage = "") {
    state.leaseClaimed = false;
    const message = overrideMessage || (pairingOpen
      ? "Enter the one-time code shown by Pocket Manga Editor. Pairing does not enable Companion Mode by itself."
      : "Open the pairing flow in Pocket Manga Editor on the PC, then check again.");
    showState({
      kicker: "Pair this iPhone",
      title: pairingOpen ? "Enter your pairing code" : "Pairing isn’t open yet",
      message,
      loading: false,
      pair: pairingOpen,
      actionLabel: pairingOpen ? "" : "Check again",
      action: bootstrap,
      detail: "Your PC and iPhone must be on the same trusted Wi-Fi network.",
    });
    if (!pairingOpen) {
      scheduleStatusPoll();
    } else {
      window.setTimeout(() => elements.pairCode.focus(), 50);
    }
  }

  function showInactive() {
    state.leaseClaimed = false;
    showState({
      kicker: "Paired",
      title: "Companion Mode is inactive",
      message: "Enable Companion Mode in Pocket Manga Editor on the PC. Your library stays private until then.",
      loading: false,
      actionLabel: "Check again",
      action: bootstrap,
      detail: "Keep Pocket Manga Editor open while reading.",
    });
    scheduleStatusPoll();
  }

  function showOccupied() {
    state.leaseClaimed = false;
    showState({
      kicker: "Controller in use",
      title: "Companion is open elsewhere",
      message: "Another tab or device currently controls this library. Close it or disconnect it from the PC before retrying.",
      loading: false,
      actionLabel: "Try again",
      action: bootstrap,
      detail: "Only one mobile controller can be active at a time.",
    });
  }

  function showUnavailable(message, retry = bootstrap) {
    state.leaseClaimed = false;
    stopHeartbeat();
    showState({
      kicker: "Connection unavailable",
      title: "Can’t reach your PC",
      message,
      loading: false,
      actionLabel: "Try again",
      action: retry,
      detail: "Confirm the PC is awake, Pocket Manga Editor is open, and both devices use the same Wi-Fi.",
    });
    announce("Pocket Manga Editor is unavailable.");
  }

  function showState({ kicker, title, message, loading, pair = false, actionLabel = "", action = null, detail = "" }) {
    hideActionError();
    clearReaderMedia();
    elements.stateScreen.hidden = false;
    elements.libraryScreen.hidden = true;
    elements.activityScreen.hidden = true;
    elements.readerScreen.hidden = true;
    elements.stateKicker.textContent = kicker;
    elements.stateTitle.textContent = title;
    elements.stateMessage.textContent = message;
    elements.stateSpinner.classList.toggle("is-paused", !loading);
    elements.pairForm.hidden = !pair;
    elements.stateAction.hidden = !actionLabel;
    elements.stateAction.textContent = actionLabel || "Try again";
    elements.stateDetail.hidden = !detail;
    elements.stateDetail.textContent = detail;
    state.stateAction = action;
  }

  function scheduleStatusPoll() {
    clearStatusPoll();
    state.statusPollTimer = window.setTimeout(() => void bootstrap(), STATUS_POLL_INTERVAL_MS);
  }

  function clearStatusPoll() {
    if (state.statusPollTimer) {
      window.clearTimeout(state.statusPollTimer);
      state.statusPollTimer = 0;
    }
  }

  async function loadLibrary() {
    clearStatusPoll();
    await drainPendingMutations();
    const requestToken = ++state.viewRequestToken;
    const epoch = ++state.activityEpoch;
    try {
      const payload = await requestJson(ROUTES.library);
      if (
        requestToken !== state.viewRequestToken
        || epoch !== state.activityEpoch
      ) {
        return;
      }
      if (payload.snapshot_id) {
        state.snapshotId = payload.snapshot_id;
      }
      state.library = Array.isArray(payload.mangas)
        ? payload.mangas.map(normalizeMangaSummary).filter((manga) => manga.id)
        : [];
      state.libraryIssueCount = nonNegativeInteger(payload.issue_count);
      state.activityManga = null;
      state.activity = null;
      state.currentManga = null;
      state.currentFolder = null;
      showLibrary({ historyMode: "replace" });
    } catch (error) {
      if (
        requestToken === state.viewRequestToken
        && epoch === state.activityEpoch
      ) {
        handleSessionError(error, loadLibrary);
      }
    }
  }

  function normalizeMangaSummary(manga) {
    return {
      id: String(manga && manga.id || ""),
      name: String(manga && manga.name || "Untitled manga"),
      folderCount: nonNegativeInteger(manga && manga.folder_count),
    };
  }

  function renderLibrary() {
    elements.libraryList.replaceChildren();
    let folderCount = 0;
    for (const manga of state.library) {
      folderCount += manga.folderCount;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "manga-card";
      button.disabled = manga.folderCount === 0;
      button.setAttribute("aria-label", mangaCardLabel(manga));
      button.addEventListener("click", () => {
        showActivityChoice(manga, { historyMode: "push" });
      });

      const text = document.createElement("span");
      const title = document.createElement("span");
      title.className = "manga-card-title";
      title.textContent = manga.name;
      const meta = document.createElement("span");
      meta.className = "manga-card-meta";
      meta.textContent = mangaCardMeta(manga);
      text.append(title, meta);

      const arrow = document.createElement("span");
      arrow.className = "manga-card-arrow";
      arrow.setAttribute("aria-hidden", "true");
      arrow.textContent = "›";
      button.append(text, arrow);
      const item = document.createElement("div");
      item.setAttribute("role", "listitem");
      item.append(button);
      elements.libraryList.append(item);
    }

    const mangaText = `${state.library.length} manga`;
    const folderText = `${folderCount} ${folderCount === 1 ? "folder" : "folders"}`;
    elements.librarySummary.textContent = `${mangaText} · ${folderText}`;
    elements.libraryEmpty.hidden = state.library.length > 0;
    elements.libraryList.hidden = state.library.length === 0;
    elements.libraryNotice.hidden = state.libraryIssueCount === 0;
    if (state.libraryIssueCount) {
      elements.libraryNotice.textContent = `${state.libraryIssueCount} source ${state.libraryIssueCount === 1 ? "item was" : "items were"} skipped by the PC scan.`;
    }
  }

  function mangaCardMeta(manga) {
    if (!manga.folderCount) {
      return "No readable image folders";
    }
    return plural(manga.folderCount, "1 image folder", `${manga.folderCount} image folders`);
  }

  function mangaCardLabel(manga) {
    return `${manga.name}, ${mangaCardMeta(manga)}`;
  }

  function showLibrary({ historyMode = "none" } = {}) {
    hideActionError();
    clearReaderFeedback();
    state.viewRequestToken += 1;
    state.activityEpoch += 1;
    state.activityManga = null;
    state.activity = null;
    state.currentManga = null;
    state.currentFolder = null;
    state.entryNavigationPending = false;
    state.entryNavigationToken += 1;
    renderLibrary();
    elements.stateScreen.hidden = true;
    elements.activityScreen.hidden = true;
    elements.readerScreen.hidden = true;
    elements.libraryScreen.hidden = false;
    document.title = "Library · Pocket Manga";
    clearReaderMedia();
    setHistory({ view: "library" }, historyMode);
  }

  function showActivityChoice(manga, { historyMode = "none" } = {}) {
    if (!manga) {
      showLibrary({ historyMode });
      return;
    }
    hideActionError();
    clearReaderFeedback();
    clearReaderMedia();
    state.viewRequestToken += 1;
    state.activityEpoch += 1;
    state.activityManga = manga;
    state.activity = null;
    state.currentManga = null;
    state.currentFolder = null;
    state.entryNavigationPending = false;
    state.entryNavigationToken += 1;
    elements.activityTitle.textContent = manga.name;
    elements.stateScreen.hidden = true;
    elements.libraryScreen.hidden = true;
    elements.readerScreen.hidden = true;
    elements.activityScreen.hidden = false;
    document.title = `${manga.name} · Pocket Manga`;
    setHistory({ view: "activity", manga_id: manga.id }, historyMode);
    window.setTimeout(() => elements.activityTitle.focus(), 20);
  }

  async function chooseActivity(activity, { historyMode = "push" } = {}) {
    const manga = state.activityManga;
    if (
      !manga
      || (activity !== READ && activity !== EDIT)
      || state.activityOpening
    ) {
      return;
    }
    state.activityOpening = true;
    elements.chooseRead.disabled = true;
    elements.chooseEdit.disabled = true;
    await drainPendingMutations();
    if (state.activityManga !== manga) {
      state.activityOpening = false;
      elements.chooseRead.disabled = false;
      elements.chooseEdit.disabled = false;
      return;
    }
    const requestToken = ++state.viewRequestToken;
    const epoch = ++state.activityEpoch;
    showToast(activity === READ ? "Opening reader…" : "Opening editor…", 1_200);
    try {
      const payload = await requestJson(ROUTES.manga(manga.id, activity));
      if (requestToken !== state.viewRequestToken || epoch !== state.activityEpoch) {
        return;
      }
      if (payload.activity !== activity) {
        throw new ApiError("wrong_activity", "The PC opened a different activity.");
      }
      if (payload.snapshot_id) {
        state.snapshotId = payload.snapshot_id;
      }
      const raw = payload.manga || {};
      const folders = Array.isArray(raw.folders)
        ? raw.folders.map((folder) => normalizeFolderSummary(folder, activity)).filter((folder) => folder.id)
        : [];
      state.activity = activity;
      const currentManga = {
        id: String(raw.id || manga.id),
        name: String(raw.name || manga.name),
        currentFolderId: String(raw.current_folder_id || ""),
        currentImageId: String(raw.current_image_id || ""),
        folders,
      };
      if (activity === EDIT) {
        currentManga.selectedCount = nonNegativeInteger(raw.selected_count);
      }
      state.currentManga = currentManga;
      if (!folders.length) {
        showActionError(
          "No readable image folder",
          "This manga no longer has a readable image folder in the active snapshot.",
          () => chooseActivity(activity, { historyMode: "none" }),
        );
        return;
      }
      populateFolderPicker(folders);
      const preferred = folders.find((folder) => folder.id === state.currentManga.currentFolderId)
        || folders[0];
      await openFolder(preferred.id, state.currentManga.currentImageId, {
        persist: false,
        historyMode,
        requestEpoch: epoch,
      });
      showWarnings(payload.warnings);
    } catch (error) {
      if (requestToken === state.viewRequestToken && epoch === state.activityEpoch) {
        if (isSessionGateError(error)) {
          handleSessionError(error, () => chooseActivity(activity, { historyMode: "none" }));
        } else {
          showActionError(
            `Couldn’t open ${activity === READ ? "reader" : "editor"}`,
            friendlyMessage(error),
            () => chooseActivity(activity, { historyMode: "none" }),
          );
        }
      }
    } finally {
      state.activityOpening = false;
      elements.chooseRead.disabled = false;
      elements.chooseEdit.disabled = false;
    }
  }

  function normalizeFolderSummary(folder, activity) {
    const normalized = {
      id: String(folder && folder.id || ""),
      name: String(folder && folder.name || "Untitled folder"),
      imageCount: nonNegativeInteger(folder && folder.image_count),
    };
    if (activity === EDIT) {
      normalized.selectedCount = nonNegativeInteger(folder && folder.selected_count);
    }
    return normalized;
  }

  async function openFolder(
    folderId,
    preferredImageId = "",
    {
      persist = false,
      historyMode = "none",
      requestEpoch = state.activityEpoch,
      entryEdge = "",
    } = {},
  ) {
    const activity = state.activity;
    if (!activity) {
      return;
    }
    const requestToken = ++state.viewRequestToken;
    try {
      const payload = await requestJson(ROUTES.folder(folderId, activity));
      if (
        requestToken !== state.viewRequestToken
        || requestEpoch !== state.activityEpoch
        || state.activity !== activity
      ) {
        return;
      }
      if (payload.activity !== activity) {
        throw new ApiError("wrong_activity", "The PC returned the wrong activity.");
      }
      const raw = payload.folder || {};
      const images = Array.isArray(raw.images)
        ? raw.images.map((image) => normalizeImage(image, activity)).filter((image) => image.id)
        : [];
      const currentFolder = {
        id: String(raw.id || folderId),
        mangaId: String(raw.manga_id || (state.currentManga && state.currentManga.id) || ""),
        name: String(raw.name || "Untitled folder"),
        currentImageId: String(raw.current_image_id || ""),
        revision: nonNegativeInteger(raw.revision),
        images,
      };
      if (activity === EDIT) {
        currentFolder.selectedCount = nonNegativeInteger(raw.selected_count);
        currentFolder.mangaSelectedCount = nonNegativeInteger(raw.manga_selected_count);
      }
      state.currentFolder = currentFolder;
      updateFolderSummary(state.currentFolder);
      elements.folderPicker.value = state.currentFolder.id;
      populateImagePickers();
      showReader();
      setHistory(
        {
          view: "reader",
          manga_id: state.currentManga ? state.currentManga.id : "",
          activity,
        },
        historyMode,
      );

      if (!images.length) {
        elements.imageLoading.hidden = true;
        elements.imageError.hidden = false;
        elements.imageError.querySelector("p").textContent = "This folder has no readable images.";
        updateReaderLabels();
        return;
      }
      const requestedIndex = images.findIndex((image) => image.id === preferredImageId);
      const savedIndex = images.findIndex((image) => image.id === state.currentFolder.currentImageId);
      const index = entryEdge === "first"
        ? 0
        : entryEdge === "last"
          ? images.length - 1
          : requestedIndex >= 0
            ? requestedIndex
            : savedIndex >= 0
              ? savedIndex
              : 0;
      showImage(index, { persist: false });
      if (persist) {
        queuePosition(activity, state.currentFolder.id, images[index].id, requestEpoch);
      }
      showWarnings(payload.warnings);
    } catch (error) {
      if (requestToken === state.viewRequestToken && requestEpoch === state.activityEpoch) {
        if (state.currentFolder) {
          elements.folderPicker.value = state.currentFolder.id;
        }
        if (isSessionGateError(error)) {
          handleSessionError(
            error,
            () => openFolder(
              folderId,
              preferredImageId,
              { persist, requestEpoch, entryEdge },
            ),
          );
        } else {
          showActionError(
            "Couldn’t open folder",
            friendlyMessage(error),
            () => openFolder(
              folderId,
              preferredImageId,
              { persist, requestEpoch, entryEdge },
            ),
          );
        }
      }
    }
  }

  function normalizeImage(image, activity) {
    const normalized = {
      id: String(image && image.id || ""),
      name: String(image && image.name || "Untitled image"),
    };
    if (activity === EDIT) {
      normalized.selected = Boolean(image && image.selected);
    }
    return normalized;
  }

  function populateFolderPicker(folders) {
    elements.folderPicker.replaceChildren();
    for (const folder of folders) {
      const option = document.createElement("option");
      option.value = folder.id;
      option.textContent = folder.name;
      elements.folderPicker.append(option);
    }
  }

  function populateImagePickers() {
    const folder = state.currentFolder;
    elements.imagePicker.replaceChildren();
    elements.selectedPicker.replaceChildren();
    if (!folder) {
      return;
    }
    const selectedImages = state.activity === EDIT
      ? folder.images.filter((image) => image.selected)
      : [];
    if (state.activity === EDIT) {
      const selectedLabel = document.createElement("option");
      selectedLabel.value = "";
      selectedLabel.textContent = `Selected · ${selectedImages.length}`;
      elements.selectedPicker.append(selectedLabel);
    }
    for (const image of folder.images) {
      const option = document.createElement("option");
      option.value = image.id;
      option.textContent = image.name;
      elements.imagePicker.append(option);
      if (state.activity === EDIT && image.selected) {
        elements.selectedPicker.append(option.cloneNode(true));
      }
    }
    elements.selectedPicker.disabled = state.activity !== EDIT || selectedImages.length === 0;
    elements.selectedPicker.value = "";
  }

  function showReader() {
    hideActionError();
    clearReaderFeedback();
    elements.stateScreen.hidden = true;
    elements.libraryScreen.hidden = true;
    elements.activityScreen.hidden = true;
    elements.readerScreen.hidden = false;
    elements.readerScreen.dataset.activity = state.activity || "";
    const editing = state.activity === EDIT;
    elements.selectionFrame.hidden = !editing;
    elements.selectionZone.hidden = !editing;
    elements.selectionZone.disabled = !editing;
    elements.selectedPickerShell.hidden = !editing;
    state.chromeVisible = true;
    elements.readerScreen.classList.remove("chrome-hidden");
    elements.chromeToggle.setAttribute("aria-pressed", "false");
    elements.chromeToggle.setAttribute("aria-label", "Hide reader controls");
    if (!editing) {
      clearSelectionPresentation();
    }
    const mangaName = state.currentManga ? state.currentManga.name : "Reader";
    document.title = `${mangaName} · ${editing ? "Edit" : "Read"} · Pocket Manga`;
  }

  function showImage(index, { persist = true } = {}) {
    const folder = state.currentFolder;
    if (!folder || !folder.images.length) {
      return;
    }
    const bounded = Math.max(0, Math.min(index, folder.images.length - 1));
    state.currentImageIndex = bounded;
    const image = folder.images[bounded];
    elements.imagePicker.value = image.id;
    updateReaderLabels();
    if (state.activity === EDIT) {
      renderSelectionState(image);
    } else {
      clearSelectionPresentation();
    }
    loadCurrentImage();
    if (persist && state.activity) {
      queuePosition(
        state.activity,
        folder.id,
        image.id,
        state.activityEpoch,
      );
    }
  }

  function goToImageId(imageId) {
    const folder = state.currentFolder;
    if (!folder) {
      return;
    }
    const index = folder.images.findIndex((image) => image.id === imageId);
    if (index >= 0 && index !== state.currentImageIndex) {
      showImage(index);
    }
  }

  function navigateImage(direction) {
    if (state.entryNavigationPending) {
      return;
    }
    const folder = state.currentFolder;
    if (!folder || !folder.images.length) {
      return;
    }
    const target = state.currentImageIndex + direction;
    if (target < 0) {
      void navigateAdjacentEntry(-1);
      return;
    }
    if (target >= folder.images.length) {
      void navigateAdjacentEntry(1);
      return;
    }
    showImage(target);
  }

  async function navigateAdjacentEntry(direction) {
    const manga = state.currentManga;
    const folder = state.currentFolder;
    const step = direction < 0 ? -1 : 1;
    const edge = step < 0 ? "left" : "right";
    if (!manga || !folder || state.entryNavigationPending) {
      return;
    }
    const currentFolderIndex = manga.folders.findIndex(
      (candidate) => candidate.id === folder.id,
    );
    if (currentFolderIndex < 0) {
      showBoundaryCue(step < 0 ? "No Previous Entry" : "No Next Entry", edge);
      return;
    }
    const targetFolderIndex = currentFolderIndex + step;
    if (targetFolderIndex < 0) {
      showBoundaryCue("No Previous Entry", edge);
      return;
    }
    if (targetFolderIndex >= manga.folders.length) {
      showBoundaryCue("No Next Entry", edge);
      return;
    }

    const targetFolder = manga.folders[targetFolderIndex];
    const requestEpoch = state.activityEpoch;
    const navigationToken = ++state.entryNavigationToken;
    state.entryNavigationPending = true;
    showBoundaryCue(step < 0 ? "Previous Entry" : "Next Entry", edge);
    try {
      await openFolder(targetFolder.id, "", {
        persist: true,
        requestEpoch,
        entryEdge: step < 0 ? "last" : "first",
      });
    } finally {
      if (navigationToken === state.entryNavigationToken) {
        state.entryNavigationPending = false;
      }
    }
  }

  function updateReaderLabels() {
    const folder = state.currentFolder;
    if (!folder || !folder.images.length) {
      elements.bottomFolder.textContent = folder ? folder.name : "—";
      elements.bottomPosition.textContent = "No images";
      elements.selectionZone.disabled = true;
      elements.previousZone.disabled = true;
      elements.nextZone.disabled = true;
      return;
    }
    const image = folder.images[state.currentImageIndex];
    elements.bottomFolder.textContent = folder.name;
    elements.bottomPosition.textContent = `${state.currentImageIndex + 1} of ${folder.images.length}`;
    elements.imageDisplay.alt = `${state.currentManga ? state.currentManga.name : "Manga"}, ${folder.name}, ${image.name}`;
    elements.selectionZone.disabled = state.activity !== EDIT;
    elements.previousZone.disabled = false;
    elements.nextZone.disabled = false;
  }

  function toggleChrome() {
    state.chromeVisible = !state.chromeVisible;
    elements.readerScreen.classList.toggle("chrome-hidden", !state.chromeVisible);
    elements.chromeToggle.setAttribute("aria-pressed", String(!state.chromeVisible));
    elements.chromeToggle.setAttribute(
      "aria-label",
      state.chromeVisible ? "Hide reader controls" : "Show reader controls",
    );
  }

  function showBoundaryCue(message, edge) {
    window.clearTimeout(state.boundaryTimer);
    elements.boundaryCue.textContent = message;
    elements.boundaryCue.dataset.edge = edge;
    elements.boundaryCue.classList.add("is-visible");
    state.boundaryTimer = window.setTimeout(() => {
      elements.boundaryCue.classList.remove("is-visible");
    }, 850);
  }

  function currentImage() {
    const folder = state.currentFolder;
    return folder && folder.images[state.currentImageIndex] || null;
  }

  function toggleCurrentSelection() {
    if (state.activity !== EDIT) {
      return;
    }
    const image = currentImage();
    const folder = state.currentFolder;
    if (!image || !folder || state.selectionPending.has(image.id)) {
      return;
    }
    void setSelection(folder, image, !image.selected, state.activityEpoch);
  }

  async function setSelection(folder, image, desired, epoch) {
    if (state.activity !== EDIT || state.selectionPending.has(image.id)) {
      return;
    }
    state.selectionPending.set(image.id, { desired, folderId: folder.id, epoch });
    if (isCurrentImage(folder.id, image.id, epoch)) {
      renderSelectionState(currentImage());
    }
    try {
      const payload = await queueSelectionRequest(folder, image, desired, epoch);
      const selection = payload.selection || {};
      const confirmed = typeof selection.selected === "boolean" ? selection.selected : desired;
      const responseRevision = nonNegativeInteger(selection.revision ?? folder.revision);
      const capturedAggregateAccepted = applySelectionConfirmation(
        folder,
        image,
        confirmed,
        selection,
        responseRevision,
      );
      state.selectionPending.delete(image.id);

      const editingSameManga = (
        state.activity === EDIT
        && state.currentManga
        && state.currentManga.id === folder.mangaId
      );
      const liveFolder = editingSameManga
        && state.currentFolder
        && state.currentFolder.id === folder.id
        ? state.currentFolder
        : null;
      const liveImage = liveFolder
        ? liveFolder.images.find((candidate) => candidate.id === image.id) || null
        : null;
      let currentAggregateAccepted = capturedAggregateAccepted;
      if (liveFolder && liveImage && (liveFolder !== folder || liveImage !== image)) {
        currentAggregateAccepted = applySelectionConfirmation(
          liveFolder,
          liveImage,
          confirmed,
          selection,
          responseRevision,
        );
      }
      if (editingSameManga) {
        updateFolderSummary(liveFolder || folder);
        if (
          currentAggregateAccepted
          && Number.isInteger(selection.manga_selected_count)
        ) {
          state.currentManga.selectedCount = Math.max(0, selection.manga_selected_count);
        }
      }
      if (liveFolder && epoch === state.activityEpoch && state.activity === EDIT) {
        populateImagePickers();
        elements.imagePicker.value = currentImage() ? currentImage().id : "";
        if (currentImage() && currentImage().id === image.id) {
          renderSelectionState(currentImage());
          pulseSelection("selection-pulse");
        }
      }
    } catch (error) {
      state.selectionPending.delete(image.id);
      const failureStillRelevant = (
        epoch === state.activityEpoch
        && state.activity === EDIT
      );
      const failedCurrentImage = isCurrentImage(folder.id, image.id, epoch);
      if (failedCurrentImage && state.activity === EDIT) {
        renderSelectionState(currentImage());
        pulseSelection("selection-failed");
        showReaderFeedback("Selection not saved", "error", 2_100);
      }
      if (failureStillRelevant) {
        showActionError(
          "Selection not saved",
          friendlyMessage(
            error,
            failedCurrentImage
              ? "The image remains in its last confirmed state."
              : "The prior folder retains its last confirmed selection state.",
          ),
          isSessionGateError(error)
            ? bootstrap
            : () => setSelection(folder, image, desired, epoch),
        );
      }
    }
  }

  function queueSelectionRequest(folder, image, desired, epoch) {
    const request = state.selectionRequestTail.then(() => {
      if (epoch !== state.activityEpoch || state.activity !== EDIT) {
        throw new ApiError(
          "activity_changed",
          "The activity changed before this selection could be saved.",
        );
      }
      return requestJson(ROUTES.editSelection(folder.id), {
        method: "PUT",
        body: { image_id: image.id, selected: desired },
      });
    });
    state.selectionRequestTail = request.catch(() => {});
    return request;
  }

  function applySelectionConfirmation(folder, image, confirmed, selection, responseRevision) {
    const aggregateAccepted = responseRevision >= folder.revision;
    image.selected = confirmed;
    if (aggregateAccepted && Number.isInteger(selection.folder_selected_count)) {
      folder.selectedCount = Math.max(0, selection.folder_selected_count);
    } else {
      folder.selectedCount = folder.images.filter((candidate) => candidate.selected).length;
    }
    if (aggregateAccepted && Number.isInteger(selection.manga_selected_count)) {
      folder.mangaSelectedCount = Math.max(0, selection.manga_selected_count);
    }
    folder.revision = Math.max(folder.revision, responseRevision);
    return aggregateAccepted;
  }

  function renderSelectionState(image) {
    if (state.activity !== EDIT) {
      clearSelectionPresentation();
      return;
    }
    const pending = image ? state.selectionPending.get(image.id) : null;
    const confirmed = Boolean(image && image.selected);
    elements.readerScreen.classList.toggle("is-selected", confirmed);
    elements.readerScreen.classList.toggle("selection-pending-add", Boolean(pending && pending.desired));
    elements.readerScreen.classList.toggle("selection-pending-remove", Boolean(pending && !pending.desired));
    elements.selectionTab.textContent = pending ? "…" : "✓";
    elements.selectionZone.setAttribute("aria-pressed", String(confirmed));
    elements.selectionZone.setAttribute(
      "aria-label",
      pending
        ? "Selection save pending"
        : confirmed
          ? "Deselect this image"
          : "Select this image",
    );
    elements.selectionZone.setAttribute("aria-busy", String(Boolean(pending)));
    syncSelectionFrame();
  }

  function clearSelectionPresentation() {
    elements.readerScreen.classList.remove(
      "is-selected",
      "selection-pending-add",
      "selection-pending-remove",
      "selection-pulse",
      "selection-failed",
    );
    elements.selectionZone.setAttribute("aria-pressed", "false");
    elements.selectionZone.setAttribute("aria-busy", "false");
  }

  function pulseSelection(className) {
    window.clearTimeout(state.animationTimer);
    elements.readerScreen.classList.remove("selection-pulse", "selection-failed");
    void elements.readerScreen.offsetWidth;
    elements.readerScreen.classList.add(className);
    state.animationTimer = window.setTimeout(() => {
      elements.readerScreen.classList.remove(className);
    }, 520);
  }

  function showReaderFeedback(message, kind = "neutral", duration = 1_500) {
    window.clearTimeout(state.feedbackTimer);
    elements.readerFeedback.textContent = message;
    elements.readerFeedback.classList.remove("is-success", "is-neutral", "is-error");
    elements.readerFeedback.classList.add("is-visible", `is-${kind}`);
    state.feedbackTimer = window.setTimeout(() => {
      elements.readerFeedback.classList.remove("is-visible");
    }, duration);
  }

  function clearReaderFeedback() {
    window.clearTimeout(state.feedbackTimer);
    state.feedbackTimer = 0;
    elements.readerFeedback.textContent = "";
    elements.readerFeedback.classList.remove(
      "is-visible",
      "is-success",
      "is-neutral",
      "is-error",
    );
  }

  function updateFolderSummary(folder) {
    if (!state.currentManga || !folder) {
      return;
    }
    const summary = state.currentManga.folders.find((candidate) => candidate.id === folder.id);
    if (summary) {
      if (state.activity === EDIT) {
        summary.selectedCount = folder.selectedCount;
      }
    }
    if (state.activity === EDIT && Number.isInteger(folder.mangaSelectedCount)) {
      state.currentManga.selectedCount = folder.mangaSelectedCount;
    }
  }

  function queuePosition(activity, folderId, imageId, epoch) {
    const mangaId = state.currentManga ? state.currentManga.id : folderId;
    const key = `${activity}:${mangaId}`;
    state.positionQueue.set(key, { activity, folderId, imageId, epoch });
    ensurePositionFlush();
  }

  function ensurePositionFlush() {
    if (!state.positionFlushActive && state.positionQueue.size) {
      state.positionFlushPromise = flushPositionQueue().catch(() => {});
    }
    return state.positionFlushPromise;
  }

  async function flushPositionQueue() {
    if (state.positionFlushActive) {
      return state.positionFlushPromise;
    }
    state.positionFlushActive = true;
    try {
      while (state.positionQueue.size) {
        const [key, pending] = state.positionQueue.entries().next().value;
        state.positionQueue.delete(key);
        const route = pending.activity === READ
          ? ROUTES.readPosition(pending.folderId)
          : ROUTES.editPosition(pending.folderId);
        try {
          const payload = await requestJson(route, {
            method: "PUT",
            body: { image_id: pending.imageId },
          });
          const position = payload.position || {};
          if (
            state.activity === pending.activity
            && pending.epoch === state.activityEpoch
            && state.currentFolder
            && state.currentFolder.id === pending.folderId
          ) {
            state.currentFolder.currentImageId = String(
              position.current_image_id || pending.imageId,
            );
            if (state.currentManga) {
              state.currentManga.currentFolderId = pending.folderId;
              state.currentManga.currentImageId = state.currentFolder.currentImageId;
            }
            state.currentFolder.revision = nonNegativeInteger(
              position.revision ?? state.currentFolder.revision,
            );
            updateFolderSummary(state.currentFolder);
          }
        } catch (error) {
          const failureStillRelevant = (
            !state.positionQueue.has(key)
            && state.activity === pending.activity
            && pending.epoch === state.activityEpoch
          );
          const failedCurrentFolder = Boolean(
            failureStillRelevant
            && state.currentFolder
            && state.currentFolder.id === pending.folderId
          );
          let failedIndex = -1;
          if (failedCurrentFolder) {
            failedIndex = state.currentFolder.images.findIndex(
              (image) => image.id === pending.imageId,
            );
            const confirmedIndex = state.currentFolder.images.findIndex(
              (image) => image.id === state.currentFolder.currentImageId,
            );
            if (confirmedIndex >= 0) {
              showImage(confirmedIndex, { persist: false });
              showReaderFeedback("Position not saved · restored", "error", 2_100);
            }
          }
          if (failureStillRelevant) {
            showActionError(
              "Position not saved",
              friendlyMessage(
                error,
                failedCurrentFolder
                  ? "The reader returned to the last confirmed image."
                  : "The prior folder retains its last confirmed reading position.",
              ),
              () => {
                if (failedIndex >= 0 && state.currentFolder && state.currentFolder.id === pending.folderId) {
                  showImage(failedIndex);
                } else {
                  queuePosition(
                    pending.activity,
                    pending.folderId,
                    pending.imageId,
                    pending.epoch,
                  );
                }
              },
            );
          }
        }
      }
    } finally {
      state.positionFlushActive = false;
    }
  }

  function drainPendingMutations() {
    const drain = state.mutationBarrierTail.then(async () => {
      while (true) {
        const positionTail = ensurePositionFlush();
        const selectionTail = state.selectionRequestTail;
        await Promise.allSettled([positionTail, selectionTail]);
        // Allow setSelection's confirmation/failure continuation to clear its
        // pending marker after the serialized request tail settles.
        await Promise.resolve();
        if (
          positionTail === state.positionFlushPromise
          && selectionTail === state.selectionRequestTail
          && !state.positionFlushActive
          && state.positionQueue.size === 0
          && state.selectionPending.size === 0
        ) {
          return;
        }
      }
    });
    state.mutationBarrierTail = drain.catch(() => {});
    return drain;
  }

  function isCurrentImage(folderId, imageId, epoch) {
    return (
      epoch === state.activityEpoch
      && state.currentFolder
      && state.currentFolder.id === folderId
      && currentImage()
      && currentImage().id === imageId
    );
  }

  function loadCurrentImage() {
    const image = currentImage();
    if (!image) {
      return;
    }
    const token = ++state.imageRequestToken;
    if (state.imageController) {
      state.imageController.abort();
    }
    state.imageController = new AbortController();
    cancelPrefetch(image.id);
    elements.imageError.hidden = true;
    elements.imageLoading.hidden = false;
    elements.imageDisplay.classList.add("is-loading");

    const prefetched = state.prefetchedImages.get(image.id);
    if (prefetched) {
      state.prefetchedImages.delete(image.id);
      installImageUrl(prefetched);
      return;
    }
    requestImage(image.id, state.imageController.signal)
      .then((blob) => {
        if (token === state.imageRequestToken) {
          installImageUrl(URL.createObjectURL(blob));
        }
      })
      .catch((error) => {
        if (error.name !== "AbortError" && token === state.imageRequestToken) {
          elements.imageLoading.hidden = true;
          elements.imageDisplay.classList.add("is-loading");
          elements.imageError.hidden = false;
          elements.imageError.querySelector("p").textContent = friendlyMessage(
            error,
            "This image could not be loaded.",
          );
        }
      });
  }

  function installImageUrl(url) {
    if (state.currentImageUrl) {
      URL.revokeObjectURL(state.currentImageUrl);
    }
    state.currentImageUrl = url;
    elements.imageDisplay.src = url;
  }

  function imageLoaded() {
    elements.imageLoading.hidden = true;
    elements.imageError.hidden = true;
    elements.imageDisplay.classList.remove("is-loading");
    syncSelectionFrame();
    prefetchAdjacentImages();
  }

  function imageFailed() {
    elements.imageLoading.hidden = true;
    elements.imageDisplay.classList.add("is-loading");
    elements.imageError.hidden = false;
  }

  function syncSelectionFrame() {
    if (
      state.activity !== EDIT
      || elements.readerScreen.hidden
      || !elements.imageDisplay.complete
      || !elements.imageDisplay.naturalWidth
    ) {
      return;
    }
    const stageRect = elements.readerStage.getBoundingClientRect();
    const imageRect = elements.imageDisplay.getBoundingClientRect();
    elements.selectionFrame.style.setProperty("--frame-left", `${imageRect.left - stageRect.left}px`);
    elements.selectionFrame.style.setProperty("--frame-top", `${imageRect.top - stageRect.top}px`);
    elements.selectionFrame.style.setProperty("--frame-width", `${imageRect.width}px`);
    elements.selectionFrame.style.setProperty("--frame-height", `${imageRect.height}px`);
  }

  function prefetchAdjacentImages() {
    const folder = state.currentFolder;
    if (!folder) {
      return;
    }
    cancelPrefetch();
    const neighbors = [state.currentImageIndex - 1, state.currentImageIndex + 1]
      .filter((index) => index >= 0 && index < folder.images.length)
      .map((index) => folder.images[index]);
    if (!neighbors.length) {
      return;
    }
    state.prefetchController = new AbortController();
    const signal = state.prefetchController.signal;
    for (const image of neighbors) {
      requestImage(image.id, signal)
        .then((blob) => {
          if (!signal.aborted) {
            const prior = state.prefetchedImages.get(image.id);
            if (prior) {
              URL.revokeObjectURL(prior);
            }
            state.prefetchedImages.set(image.id, URL.createObjectURL(blob));
          }
        })
        .catch(() => {
          // Prefetch is opportunistic; current-image loading owns visible errors.
        });
    }
  }

  function cancelPrefetch(keepImageId = "") {
    if (state.prefetchController) {
      state.prefetchController.abort();
      state.prefetchController = null;
    }
    for (const [imageId, url] of state.prefetchedImages) {
      if (imageId !== keepImageId) {
        URL.revokeObjectURL(url);
        state.prefetchedImages.delete(imageId);
      }
    }
  }

  function clearReaderMedia() {
    state.imageRequestToken += 1;
    if (state.imageController) {
      state.imageController.abort();
      state.imageController = null;
    }
    if (state.currentImageUrl) {
      URL.revokeObjectURL(state.currentImageUrl);
      state.currentImageUrl = null;
      elements.imageDisplay.removeAttribute("src");
    }
    cancelPrefetch();
  }

  async function navigateBackAfterMutations() {
    if (state.historyNavigationPending) {
      return;
    }
    state.historyNavigationPending = true;
    await drainPendingMutations();
    window.history.back();
    window.setTimeout(() => {
      state.historyNavigationPending = false;
    }, 500);
  }

  async function historyChanged(event) {
    const changeToken = ++state.historyChangeToken;
    await drainPendingMutations();
    if (changeToken !== state.historyChangeToken) {
      return;
    }
    state.historyNavigationPending = false;
    const historyState = event.state && typeof event.state === "object" ? event.state : {};
    if (historyState.view === "activity") {
      const manga = state.library.find((candidate) => candidate.id === String(historyState.manga_id || ""));
      showActivityChoice(manga || state.activityManga, { historyMode: "none" });
      return;
    }
    if (historyState.view === "reader") {
      const manga = state.library.find((candidate) => candidate.id === String(historyState.manga_id || ""));
      showActivityChoice(manga || state.activityManga, { historyMode: "replace" });
      return;
    }
    showLibrary({ historyMode: "none" });
  }

  function setHistory(historyState, mode) {
    if (mode === "push") {
      window.history.pushState(historyState, "", window.location.pathname);
    } else if (mode === "replace") {
      window.history.replaceState(historyState, "", window.location.pathname);
    }
  }

  function showWarnings(warnings) {
    if (Array.isArray(warnings) && warnings.length) {
      showToast(String(warnings[0]), 3_000);
    }
  }

  function showActionError(title, message, retry = null) {
    state.actionRetry = retry;
    elements.actionErrorTitle.textContent = title;
    elements.actionErrorMessage.textContent = message;
    elements.actionErrorRetry.hidden = typeof retry !== "function";
    elements.actionError.hidden = false;
  }

  function hideActionError() {
    state.actionRetry = null;
    elements.actionError.hidden = true;
  }

  function showToast(message, duration = 1_800) {
    window.clearTimeout(state.toastTimer);
    elements.toast.textContent = message;
    elements.toast.hidden = false;
    state.toastTimer = window.setTimeout(() => {
      elements.toast.hidden = true;
    }, duration);
  }

  function announce(message) {
    elements.connectionAnnouncer.textContent = "";
    window.setTimeout(() => {
      elements.connectionAnnouncer.textContent = message;
    }, 10);
  }

  function friendlyMessage(error, fallback = "The request could not be completed.") {
    if (error && error.name === "AbortError") {
      return "The PC did not respond in time.";
    }
    if (error instanceof ApiError && error.message) {
      return error.message;
    }
    return fallback;
  }

  function isSessionGateError(error) {
    if (!(error instanceof ApiError)) {
      return false;
    }
    return [
      "unauthorized",
      "unpaired",
      "inactive_mode",
      "lease_conflict",
      "lease_expired",
      "stale_snapshot",
      "invalid_snapshot",
      "shutdown_transition",
    ].includes(error.code);
  }

  function nonNegativeInteger(value) {
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed >= 0 ? parsed : 0;
  }

  function plural(count, singular, pluralValue) {
    return count === 1 ? singular : pluralValue;
  }

  bindEvents();
  void bootstrap();
})();
