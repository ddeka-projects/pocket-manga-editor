"use strict";

(() => {
  const HEARTBEAT_INTERVAL_MS = 12_000;
  const STATUS_POLL_INTERVAL_MS = 4_000;
  const REQUEST_TIMEOUT_MS = 15_000;

  const ROUTES = Object.freeze({
    status: "/api/status",
    pair: "/api/pair",
    claim: "/api/controller/claim",
    heartbeat: "/api/controller/heartbeat",
    release: "/api/controller/release",
    library: "/api/library",
    manga: (id) => `/api/manga/${encodeURIComponent(id)}`,
    volume: (id) => `/api/volume/${encodeURIComponent(id)}`,
    image: (id) => `/api/page/${encodeURIComponent(id)}/image`,
    position: (id) => `/api/volume/${encodeURIComponent(id)}/position`,
    selection: (id) => `/api/volume/${encodeURIComponent(id)}/selection`,
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
    readerScreen: element("reader-screen"),
    readerStage: element("reader-stage"),
    pageImage: element("page-image"),
    selectionFrame: element("selection-frame"),
    selectionTab: element("selection-tab"),
    previousZone: element("previous-zone"),
    selectionZone: element("selection-zone"),
    nextZone: element("next-zone"),
    chromeToggle: element("chrome-toggle"),
    topChrome: element("top-chrome"),
    backToLibrary: element("back-to-library"),
    volumePicker: element("volume-picker"),
    selectedPicker: element("selected-picker"),
    pagePicker: element("page-picker"),
    bottomVolume: element("bottom-volume"),
    bottomPage: element("bottom-page"),
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
    libraryContext: null,
    lastVolumeByManga: new Map(),
    currentManga: null,
    currentVolume: null,
    currentPageIndex: 0,
    viewRequestToken: 0,
    chromeVisible: true,
    selectionPending: new Map(),
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
  };

  function getClientId() {
    const storageKey = "pocket-manga-companion-client";
    // This stable identity supports background/reload reclaim. A separate
    // in-memory pageInstanceId is included in every lease request so a copied
    // sessionStorage value cannot let a duplicated tab share active authority.
    try {
      const existing = window.sessionStorage.getItem(storageKey);
      if (existing && /^[A-Za-z0-9._~-]{1,128}$/.test(existing)) {
        return existing;
      }
    } catch (_error) {
      // Storage can be unavailable in privacy modes. The in-memory identity
      // remains valid for this document lifetime.
    }

    const id = createOpaqueId();
    try {
      window.sessionStorage.setItem(storageKey, id);
    } catch (_error) {
      // The in-memory identity remains usable.
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
    elements.backToLibrary.addEventListener("click", showLibrary);
    elements.volumePicker.addEventListener("change", (event) => {
      const volumeId = event.currentTarget.value;
      if (volumeId) {
        void openVolume(volumeId);
      }
    });
    elements.selectedPicker.addEventListener("change", (event) => {
      const pageId = event.currentTarget.value;
      event.currentTarget.value = "";
      if (pageId) {
        goToPageId(pageId);
      }
    });
    elements.pagePicker.addEventListener("change", (event) => {
      goToPageId(event.currentTarget.value);
    });
    elements.previousZone.addEventListener("click", () => navigatePage(-1));
    elements.nextZone.addEventListener("click", () => navigatePage(1));
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

    // Native controls sit above the reader gesture layer. Explicitly consuming
    // their events protects against WebKit event retargeting around select menus.
    for (const control of [elements.topChrome, elements.backToLibrary]) {
      for (const eventName of ["click", "pointerup", "touchend"]) {
        control.addEventListener(eventName, (event) => event.stopPropagation());
      }
    }

    for (const eventName of ["contextmenu", "dragstart", "dblclick", "selectstart"]) {
      elements.readerStage.addEventListener(eventName, (event) => event.preventDefault());
    }

    elements.pageImage.addEventListener("load", pageImageLoaded);
    elements.pageImage.addEventListener("error", pageImageFailed);
    window.addEventListener("resize", syncSelectionFrame, { passive: true });
    window.addEventListener("orientationchange", () => window.setTimeout(syncSelectionFrame, 80));
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", syncSelectionFrame, { passive: true });
    }

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
      let payload = null;
      if (contentType.includes("application/json")) {
        payload = await response.json();
      }
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
      throw new ApiError(
        "network_error",
        "Could not reach Pocket Manga Editor on this network.",
        0,
      );
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function requestImage(pageId, signal) {
    let response;
    try {
      response = await fetch(ROUTES.image(pageId), {
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
      throw new ApiError("network_error", "The page image could not be reached.");
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
        apiDetail.message || "The page image is unavailable.",
        response.status,
        detail,
      );
    }

    const contentType = response.headers.get("content-type") || "";
    if (!contentType.startsWith("image/")) {
      throw new ApiError("invalid_image", "The PC returned an invalid page image.");
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
      // The nested envelope is canonical; accepting the earlier flat spike keeps
      // a cached frontend usable while a desktop process is being upgraded.
      const status = payload.status || payload;
      if (status.server && status.server !== "available") {
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
        && isOneOf(error.code, "pairing_closed", "pairing_rate_limited")
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
      state.leaseClaimed = Boolean(payload.controller && payload.controller.claimed !== false);
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
    // Keepalive allows an orderly navigation/close to release immediately. If
    // iOS terminates the page without this event, the server lease still expires.
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
      state.leaseClaimed = Boolean(payload.controller && payload.controller.claimed !== false);
      state.snapshotId = payload.snapshot_id || null;
      startHeartbeat();
      if (!state.snapshotId || state.snapshotId !== previousSnapshotId) {
        await loadLibrary();
        return;
      }
      if (state.currentVolume && state.currentManga) {
        showReader();
        showPage(state.currentPageIndex, { persist: false });
      } else {
        showLibrary();
      }
      announce("Companion reconnected.");
    } catch (error) {
      handleSessionError(error, bootstrap);
    }
  }

  function handleSessionError(error, retry) {
    const code = error instanceof ApiError ? error.code : "network_error";
    if (isOneOf(code, "unpaired", "not_paired", "unauthorized", "authorization_required")) {
      showPairing(false, "This browser is not authorized. Open pairing on the PC to pair again.");
      return;
    }
    if (isOneOf(code, "inactive", "companion_inactive", "mode_inactive", "inactive_mode", "not_active")) {
      showInactive();
      return;
    }
    if (
      isOneOf(code, "lease_occupied", "lease_conflict", "controller_conflict", "controller_occupied")
    ) {
      showOccupied();
      return;
    }
    if (isOneOf(code, "stale_snapshot", "invalid_snapshot")) {
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
    }
    if (pairingOpen) {
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
    try {
      const payload = await requestJson(ROUTES.library);
      if (payload.snapshot_id) {
        state.snapshotId = payload.snapshot_id;
      }
      state.library = Array.isArray(payload.mangas)
        ? payload.mangas.map(normalizeMangaSummary).filter((manga) => manga.id)
        : [];
      state.libraryContext = payload.context && typeof payload.context === "object"
        ? payload.context
        : null;
      state.libraryIssueCount = nonNegativeInteger(
        payload.issue_count ?? payload.scan_issue_count,
      );
      showLibrary();
    } catch (error) {
      handleSessionError(error, loadLibrary);
    }
  }

  function normalizeMangaSummary(manga) {
    return {
      id: String(manga && manga.id || ""),
      name: String(manga && manga.name || "Untitled manga"),
      volumeCount: nonNegativeInteger(manga && manga.volume_count),
      selectedCount: nonNegativeInteger(manga && manga.selected_count),
    };
  }

  function renderLibrary(payload = {}) {
    elements.libraryList.replaceChildren();
    let volumeCount = 0;
    let selectedCount = 0;
    for (const manga of state.library) {
      volumeCount += manga.volumeCount;
      selectedCount += manga.selectedCount;

      const button = document.createElement("button");
      button.type = "button";
      button.className = "manga-card";
      button.disabled = manga.volumeCount === 0;
      button.setAttribute("aria-label", mangaCardLabel(manga));
      button.addEventListener("click", () => void openManga(manga.id));

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
    const volumeText = `${volumeCount} ${volumeCount === 1 ? "volume" : "volumes"}`;
    const selectionText = `${selectedCount} selected ${selectedCount === 1 ? "page" : "pages"}`;
    elements.librarySummary.textContent = `${mangaText} · ${volumeText} · ${selectionText}`;
    elements.libraryEmpty.hidden = state.library.length > 0;
    elements.libraryList.hidden = state.library.length === 0;

    const issueCount = nonNegativeInteger(
      payload.issue_count ?? payload.scan_issue_count ?? state.libraryIssueCount,
    );
    state.libraryIssueCount = issueCount;
    elements.libraryNotice.hidden = issueCount === 0;
    if (issueCount) {
      elements.libraryNotice.textContent = `${issueCount} source ${issueCount === 1 ? "item was" : "items were"} skipped by the PC scan.`;
    }
  }

  function mangaCardMeta(manga) {
    if (!manga.volumeCount) {
      return "No readable volumes";
    }
    return `${plural(manga.volumeCount, "1 volume", `${manga.volumeCount} volumes`)} · ${plural(manga.selectedCount, "1 selected", `${manga.selectedCount} selected`)}`;
  }

  function mangaCardLabel(manga) {
    return `${manga.name}, ${mangaCardMeta(manga)}`;
  }

  function showLibrary() {
    hideActionError();
    renderLibrary({ issue_count: state.libraryIssueCount });
    elements.stateScreen.hidden = true;
    elements.readerScreen.hidden = true;
    elements.libraryScreen.hidden = false;
    document.title = "Library · Pocket Manga";
    clearReaderMedia();
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
      elements.pageImage.removeAttribute("src");
    }
    cancelPrefetch();
  }

  async function openManga(mangaId) {
    showToast("Opening manga…", 1_500);
    const requestToken = ++state.viewRequestToken;
    try {
      const payload = await requestJson(ROUTES.manga(mangaId));
      if (requestToken !== state.viewRequestToken) {
        return;
      }
      if (payload.snapshot_id) {
        state.snapshotId = payload.snapshot_id;
      }
      const raw = payload.manga || {};
      const volumes = Array.isArray(raw.volumes)
        ? raw.volumes.map(normalizeVolumeSummary).filter((volume) => volume.id)
        : [];
      state.currentManga = {
        id: String(raw.id || mangaId),
        name: String(raw.name || "Untitled manga"),
        volumes,
      };
      if (!volumes.length) {
        showActionError(
          "No readable volume",
          "This manga no longer has a readable volume in the active snapshot.",
          () => openManga(mangaId),
        );
        return;
      }

      populateVolumePicker(volumes);
      const context = state.libraryContext || {};
      const contextVolume = String(context.manga_id || "") === state.currentManga.id
        ? String(context.volume_id || "")
        : "";
      const rememberedVolume = state.lastVolumeByManga.get(state.currentManga.id) || "";
      const preferred = volumes.find((volume) => volume.id === contextVolume)
        || volumes.find((volume) => volume.id === rememberedVolume)
        || volumes.find((volume) => volume.id === String(raw.current_volume_id || raw.last_volume_id || ""))
        || volumes[0];
      const preferredPage = preferred.id === contextVolume ? String(context.page_id || "") : "";
      await openVolume(preferred.id, preferredPage);
    } catch (error) {
      if (requestToken === state.viewRequestToken) {
        if (isSessionGateError(error)) {
          handleSessionError(error, () => openManga(mangaId));
        } else {
          showActionError("Couldn’t open manga", friendlyMessage(error), () => openManga(mangaId));
        }
      }
    }
  }

  function normalizeVolumeSummary(volume) {
    return {
      id: String(volume && volume.id || ""),
      label: String(volume && (volume.display_name || volume.label) || "Volume"),
      pageCount: nonNegativeInteger(volume && volume.page_count),
      selectedCount: nonNegativeInteger(volume && volume.selected_count),
      currentPageId: String(volume && volume.current_page_id || ""),
    };
  }

  async function openVolume(volumeId, preferredPageId = "") {
    const requestToken = ++state.viewRequestToken;
    showToast("Loading volume…", 1_200);
    try {
      const payload = await requestJson(ROUTES.volume(volumeId));
      if (requestToken !== state.viewRequestToken) {
        return;
      }
      if (payload.snapshot_id) {
        state.snapshotId = payload.snapshot_id;
      }
      const raw = payload.volume || {};
      const pages = Array.isArray(raw.pages)
        ? raw.pages.map(normalizePage).filter((page) => page.id)
        : [];
      state.currentVolume = {
        id: String(raw.id || volumeId),
        mangaId: String(raw.manga_id || (state.currentManga && state.currentManga.id) || ""),
        label: String(raw.display_name || raw.label || "Volume"),
        currentPageId: String(raw.current_page_id || ""),
        currentIndex: nonNegativeInteger(raw.current_index),
        selectedCount: nonNegativeInteger(raw.selected_count),
        revision: nonNegativeInteger(raw.revision),
        pages,
      };
      if (state.currentVolume.mangaId) {
        state.lastVolumeByManga.set(
          state.currentVolume.mangaId,
          state.currentVolume.id,
        );
      }
      updateVolumeSummary(state.currentVolume);
      elements.volumePicker.value = state.currentVolume.id;
      populatePagePickers();
      showReader();

      if (!pages.length) {
        elements.imageLoading.hidden = true;
        elements.imageError.hidden = false;
        elements.imageError.querySelector("p").textContent = "This volume has no readable pages.";
        updateReaderLabels();
        return;
      }

      const requestedIndex = pages.findIndex((page) => page.id === preferredPageId);
      const savedIndex = pages.findIndex((page) => page.id === state.currentVolume.currentPageId);
      const index = requestedIndex >= 0
        ? requestedIndex
        : savedIndex >= 0
          ? savedIndex
          : Math.min(state.currentVolume.currentIndex, pages.length - 1);
      showPage(index, { persist: false });
    } catch (error) {
      if (requestToken === state.viewRequestToken) {
        if (state.currentVolume) {
          elements.volumePicker.value = state.currentVolume.id;
        }
        if (isSessionGateError(error)) {
          handleSessionError(error, () => openVolume(volumeId, preferredPageId));
        } else {
          showActionError("Couldn’t open volume", friendlyMessage(error), () => openVolume(volumeId, preferredPageId));
        }
      }
    }
  }

  function normalizePage(page, index) {
    return {
      id: String(page && page.id || ""),
      pageLabel: String(page && page.page_label || index + 1),
      chapterLabel: page && page.chapter_label != null ? String(page.chapter_label) : "",
      chapterTitle: page && page.chapter_title != null ? String(page.chapter_title) : "",
      selected: Boolean(page && page.selected),
    };
  }

  function populateVolumePicker(volumes) {
    elements.volumePicker.replaceChildren();
    for (const volume of volumes) {
      const option = document.createElement("option");
      option.value = volume.id;
      option.textContent = volume.label;
      elements.volumePicker.append(option);
    }
  }

  function populatePagePickers() {
    const volume = state.currentVolume;
    elements.pagePicker.replaceChildren();
    elements.selectedPicker.replaceChildren();
    if (!volume) {
      return;
    }

    const selectedPages = volume.pages.filter((page) => page.selected);
    const selectedLabel = document.createElement("option");
    selectedLabel.value = "";
    selectedLabel.textContent = `Selected · ${selectedPages.length}`;
    elements.selectedPicker.append(selectedLabel);
    for (const [index, page] of volume.pages.entries()) {
      const pageOption = document.createElement("option");
      pageOption.value = page.id;
      pageOption.textContent = pickerPageLabel(page, index);
      elements.pagePicker.append(pageOption);
      if (page.selected) {
        const selectedOption = pageOption.cloneNode(true);
        elements.selectedPicker.append(selectedOption);
      }
    }
    elements.selectedPicker.disabled = selectedPages.length === 0;
    elements.selectedPicker.value = "";
  }

  function pickerPageLabel(page, index) {
    const chapter = page.chapterLabel ? `Ch. ${page.chapterLabel} · ` : "";
    return `${chapter}Page ${page.pageLabel || index + 1}`;
  }

  function showReader() {
    hideActionError();
    elements.stateScreen.hidden = true;
    elements.libraryScreen.hidden = true;
    elements.readerScreen.hidden = false;
    state.chromeVisible = true;
    elements.readerScreen.classList.remove("chrome-hidden");
    elements.chromeToggle.setAttribute("aria-pressed", "false");
    elements.chromeToggle.setAttribute("aria-label", "Hide reader controls");
    const mangaName = state.currentManga ? state.currentManga.name : "Reader";
    document.title = `${mangaName} · Pocket Manga`;
  }

  function showPage(index, { persist = true } = {}) {
    const volume = state.currentVolume;
    if (!volume || !volume.pages.length) {
      return;
    }
    const bounded = Math.max(0, Math.min(index, volume.pages.length - 1));
    state.currentPageIndex = bounded;
    const page = volume.pages[bounded];
    state.libraryContext = {
      manga_id: state.currentManga ? state.currentManga.id : volume.mangaId,
      volume_id: volume.id,
      page_id: page.id,
    };
    elements.pagePicker.value = page.id;
    updateReaderLabels();
    renderSelectionState(page);
    loadCurrentImage();
    if (persist) {
      queuePosition(volume.id, page.id);
    }
  }

  function goToPageId(pageId) {
    const volume = state.currentVolume;
    if (!volume) {
      return;
    }
    const index = volume.pages.findIndex((page) => page.id === pageId);
    if (index >= 0 && index !== state.currentPageIndex) {
      showPage(index);
    }
  }

  function navigatePage(direction) {
    const volume = state.currentVolume;
    if (!volume || !volume.pages.length) {
      return;
    }
    const target = state.currentPageIndex + direction;
    if (target < 0) {
      showBoundaryCue("First page", "left");
      return;
    }
    if (target >= volume.pages.length) {
      showBoundaryCue("Last page", "right");
      return;
    }
    showPage(target);
  }

  function updateReaderLabels() {
    const volume = state.currentVolume;
    if (!volume || !volume.pages.length) {
      elements.bottomVolume.textContent = volume ? volume.label : "—";
      elements.bottomPage.textContent = "No pages";
      elements.selectionZone.disabled = true;
      elements.previousZone.disabled = true;
      elements.nextZone.disabled = true;
      return;
    }
    const page = volume.pages[state.currentPageIndex];
    elements.bottomVolume.textContent = volume.label;
    elements.bottomPage.textContent = `${state.currentPageIndex + 1} of ${volume.pages.length}`;
    elements.pageImage.alt = `${state.currentManga ? state.currentManga.name : "Manga"}, ${volume.label}, page ${page.pageLabel}`;
    elements.selectionZone.disabled = false;
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

  function currentPage() {
    const volume = state.currentVolume;
    return volume && volume.pages[state.currentPageIndex] || null;
  }

  function toggleCurrentSelection() {
    const page = currentPage();
    const volume = state.currentVolume;
    if (!page || !volume || state.selectionPending.has(page.id)) {
      return;
    }
    void setSelection(volume, page, !page.selected);
  }

  async function setSelection(volume, page, desired) {
    if (state.selectionPending.has(page.id)) {
      return;
    }
    state.selectionPending.set(page.id, { desired, volumeId: volume.id });
    if (
      state.currentVolume
      && state.currentVolume.id === volume.id
      && currentPage()
      && currentPage().id === page.id
    ) {
      renderSelectionState(currentPage());
    }

    try {
      const payload = await requestJson(ROUTES.selection(volume.id), {
        method: "PUT",
        body: { page_id: page.id, selected: desired },
      });
      const review = payload.review || {};
      const confirmed = typeof review.selected === "boolean" ? review.selected : desired;
      const previous = page.selected;
      const responseRevision = nonNegativeInteger(review.revision ?? volume.revision);
      const liveVolume = state.currentVolume && state.currentVolume.id === volume.id
        ? state.currentVolume
        : null;
      const livePage = liveVolume
        ? liveVolume.pages.find((candidate) => candidate.id === page.id) || null
        : null;
      applySelectionConfirmation(volume, page, confirmed, review, responseRevision);
      if (liveVolume && livePage && liveVolume !== volume) {
        applySelectionConfirmation(
          liveVolume,
          livePage,
          confirmed,
          review,
          responseRevision,
        );
      }
      state.selectionPending.delete(page.id);
      const displayedVolume = liveVolume || volume;
      updateVolumeSummary(displayedVolume);
      if (state.currentManga && state.currentManga.id === volume.mangaId) {
        syncLibrarySelectionCount(volume.mangaId);
      } else {
        updateLibrarySelectionCount(
          volume.mangaId,
          confirmed === previous ? 0 : confirmed ? 1 : -1,
        );
      }

      if (liveVolume) {
        populatePagePickers();
        elements.pagePicker.value = currentPage() ? currentPage().id : "";
        if (currentPage() && currentPage().id === page.id) {
          renderSelectionState(currentPage());
          pulseSelection("selection-pulse");
        }
      }
    } catch (error) {
      state.selectionPending.delete(page.id);
      if (
        state.currentVolume
        && state.currentVolume.id === volume.id
        && currentPage()
        && currentPage().id === page.id
      ) {
        renderSelectionState(currentPage());
        pulseSelection("selection-failed");
        showReaderFeedback("Selection not saved", "error", 2_100);
      }
      const retry = isSessionGateError(error)
        ? bootstrap
        : () => setSelection(volume, page, desired);
      showActionError(
        "Selection not saved",
        friendlyMessage(error, "The page remains in its last confirmed state."),
        retry,
      );
    }
  }

  function applySelectionConfirmation(volume, page, confirmed, review, responseRevision) {
    page.selected = confirmed;
    if (responseRevision >= volume.revision && Number.isInteger(review.selected_count)) {
      volume.selectedCount = Math.max(0, review.selected_count);
    } else {
      volume.selectedCount = volume.pages.filter((candidate) => candidate.selected).length;
    }
    volume.revision = Math.max(volume.revision, responseRevision);
  }

  function renderSelectionState(page) {
    const pending = page ? state.selectionPending.get(page.id) : null;
    const confirmed = Boolean(page && page.selected);
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
          ? "Deselect this page"
          : "Select this page",
    );
    elements.selectionZone.setAttribute("aria-busy", String(Boolean(pending)));
    syncSelectionFrame();
  }

  function pulseSelection(className) {
    window.clearTimeout(state.animationTimer);
    elements.readerScreen.classList.remove("selection-pulse", "selection-failed");
    // Force a style boundary so consecutive confirmations replay the pulse.
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

  function updateVolumeSummary(volume) {
    if (!state.currentManga || !volume) {
      return;
    }
    const summary = state.currentManga.volumes.find((candidate) => candidate.id === volume.id);
    if (summary) {
      summary.selectedCount = volume.selectedCount;
      summary.currentPageId = volume.currentPageId;
    }
  }

  function updateLibrarySelectionCount(mangaId, delta) {
    if (!delta) {
      return;
    }
    const manga = state.library.find((candidate) => candidate.id === mangaId);
    if (manga) {
      manga.selectedCount = Math.max(0, manga.selectedCount + delta);
    }
  }

  function syncLibrarySelectionCount(mangaId) {
    if (!state.currentManga || state.currentManga.id !== mangaId) {
      return;
    }
    const manga = state.library.find((candidate) => candidate.id === mangaId);
    if (manga) {
      manga.selectedCount = state.currentManga.volumes.reduce(
        (total, volume) => total + volume.selectedCount,
        0,
      );
    }
  }

  function queuePosition(volumeId, pageId) {
    state.positionQueue.set(volumeId, pageId);
    void flushPositionQueue();
  }

  async function flushPositionQueue() {
    if (state.positionFlushActive) {
      return;
    }
    state.positionFlushActive = true;
    try {
      while (state.positionQueue.size) {
        const [volumeId, pageId] = state.positionQueue.entries().next().value;
        state.positionQueue.delete(volumeId);
        try {
          const payload = await requestJson(ROUTES.position(volumeId), {
            method: "PUT",
            body: { page_id: pageId },
          });
          const review = payload.review || {};
          if (state.currentVolume && state.currentVolume.id === volumeId) {
            state.currentVolume.currentPageId = String(review.current_page_id || pageId);
            const confirmedIndex = state.currentVolume.pages.findIndex(
              (page) => page.id === state.currentVolume.currentPageId,
            );
            if (confirmedIndex >= 0) {
              state.currentVolume.currentIndex = confirmedIndex;
            }
            state.currentVolume.revision = nonNegativeInteger(review.revision ?? state.currentVolume.revision);
            updateVolumeSummary(state.currentVolume);
          }
        } catch (error) {
          // If a newer page is already queued for the same volume, that request
          // is the useful retry and should not be replaced with stale position.
          if (!state.positionQueue.has(volumeId)) {
            let failedIndex = -1;
            if (state.currentVolume && state.currentVolume.id === volumeId) {
              failedIndex = state.currentVolume.pages.findIndex((page) => page.id === pageId);
              const confirmedIndex = state.currentVolume.pages.findIndex(
                (page) => page.id === state.currentVolume.currentPageId,
              );
              if (confirmedIndex >= 0) {
                showPage(confirmedIndex, { persist: false });
                showReaderFeedback("Position not saved · restored", "error", 2_100);
              }
            }
            showActionError(
              "Position not saved",
              friendlyMessage(error, "The reader returned to the last confirmed page."),
              () => {
                if (
                  failedIndex >= 0
                  && state.currentVolume
                  && state.currentVolume.id === volumeId
                ) {
                  showPage(failedIndex);
                  return;
                }
                state.positionQueue.set(volumeId, pageId);
                void flushPositionQueue();
              },
            );
          }
        }
      }
    } finally {
      state.positionFlushActive = false;
    }
  }

  function loadCurrentImage() {
    const page = currentPage();
    if (!page) {
      return;
    }
    const token = ++state.imageRequestToken;
    if (state.imageController) {
      state.imageController.abort();
    }
    state.imageController = new AbortController();
    cancelPrefetch(page.id);
    elements.imageError.hidden = true;
    elements.imageLoading.hidden = false;
    elements.pageImage.classList.add("is-loading");

    const prefetched = state.prefetchedImages.get(page.id);
    if (prefetched) {
      state.prefetchedImages.delete(page.id);
      installImageUrl(prefetched);
      return;
    }

    requestImage(page.id, state.imageController.signal)
      .then((blob) => {
        if (token !== state.imageRequestToken) {
          return;
        }
        installImageUrl(URL.createObjectURL(blob));
      })
      .catch((error) => {
        if (error.name !== "AbortError" && token === state.imageRequestToken) {
          elements.imageLoading.hidden = true;
          elements.pageImage.classList.add("is-loading");
          elements.imageError.hidden = false;
          const message = elements.imageError.querySelector("p");
          message.textContent = friendlyMessage(error, "This page image could not be loaded.");
        }
      });
  }

  function installImageUrl(url) {
    if (state.currentImageUrl) {
      URL.revokeObjectURL(state.currentImageUrl);
    }
    state.currentImageUrl = url;
    elements.pageImage.src = url;
  }

  function pageImageLoaded() {
    elements.imageLoading.hidden = true;
    elements.imageError.hidden = true;
    elements.pageImage.classList.remove("is-loading");
    syncSelectionFrame();
    prefetchAdjacentPages();
  }

  function pageImageFailed() {
    elements.imageLoading.hidden = true;
    elements.pageImage.classList.add("is-loading");
    elements.imageError.hidden = false;
  }

  function syncSelectionFrame() {
    if (elements.readerScreen.hidden || !elements.pageImage.complete || !elements.pageImage.naturalWidth) {
      return;
    }
    const stageRect = elements.readerStage.getBoundingClientRect();
    const imageRect = elements.pageImage.getBoundingClientRect();
    elements.selectionFrame.style.setProperty("--frame-left", `${imageRect.left - stageRect.left}px`);
    elements.selectionFrame.style.setProperty("--frame-top", `${imageRect.top - stageRect.top}px`);
    elements.selectionFrame.style.setProperty("--frame-width", `${imageRect.width}px`);
    elements.selectionFrame.style.setProperty("--frame-height", `${imageRect.height}px`);
  }

  function prefetchAdjacentPages() {
    const volume = state.currentVolume;
    if (!volume) {
      return;
    }
    cancelPrefetch();
    const neighbors = [state.currentPageIndex - 1, state.currentPageIndex + 1]
      .filter((index) => index >= 0 && index < volume.pages.length)
      .map((index) => volume.pages[index]);
    if (!neighbors.length) {
      return;
    }
    state.prefetchController = new AbortController();
    const signal = state.prefetchController.signal;
    for (const page of neighbors) {
      requestImage(page.id, signal)
        .then((blob) => {
          if (!signal.aborted) {
            const prior = state.prefetchedImages.get(page.id);
            if (prior) {
              URL.revokeObjectURL(prior);
            }
            state.prefetchedImages.set(page.id, URL.createObjectURL(blob));
          }
        })
        .catch(() => {
          // Prefetch is opportunistic; current-page loading owns visible errors.
        });
    }
  }

  function cancelPrefetch(keepPageId = "") {
    if (state.prefetchController) {
      state.prefetchController.abort();
      state.prefetchController = null;
    }
    for (const [pageId, url] of state.prefetchedImages) {
      if (pageId !== keepPageId) {
        URL.revokeObjectURL(url);
        state.prefetchedImages.delete(pageId);
      }
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
    return isOneOf(
      error.code,
      "unpaired",
      "not_paired",
      "unauthorized",
      "authorization_required",
      "inactive",
      "companion_inactive",
      "mode_inactive",
      "inactive_mode",
      "not_active",
      "lease_occupied",
      "lease_conflict",
      "controller_conflict",
      "controller_occupied",
      "lease_lost",
      "lease_expired",
      "not_controller",
      "stale_snapshot",
      "invalid_snapshot",
      "shutdown",
      "shutting_down",
      "shutdown_transition",
    );
  }

  function isOneOf(value, ...choices) {
    return choices.includes(String(value || "").toLowerCase());
  }

  function nonNegativeInteger(value) {
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed >= 0 ? parsed : 0;
  }

  function plural(count, singular, pluralValue) {
    return count === 1 ? singular : typeof pluralValue === "string" ? pluralValue : `${count} ${pluralValue}`;
  }

  bindEvents();
  void bootstrap();
})();
