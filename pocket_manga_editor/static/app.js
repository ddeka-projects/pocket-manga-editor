"use strict";

(() => {
  const REQUEST_TIMEOUT_MS = 15_000;
  // Takeover waits for any save or rescan the previous owner is committing.
  const TAKEOVER_TIMEOUT_MS = 10 * 60_000;
  // Position and selection saves are small, idempotent LAN writes. They retry
  // quietly before the user is asked to intervene.
  const SAVE_TIMEOUT_MS = 5_000;
  const POSITION_RETRY_DELAYS_MS = [1_000, 2_000, 4_000];
  const SELECTION_RETRY_DELAYS_MS = [500, 1_500];
  const READ = "read";
  const EDIT = "edit";

  const ROUTES = Object.freeze({
    claim: "/api/controller/claim",
    takeover: "/api/controller/takeover",
    release: "/api/controller/release",
    library: "/api/library",
    rescan: "/api/library/rescan",
    mangaScan: (id) => `/api/manga/${encodeURIComponent(id)}/scan`,
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
    libraryScreen: element("library-screen"),
    libraryRefresh: element("library-refresh"),
    releaseControl: element("release-control"),
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
    pageId: createOpaqueId(),
    controllerToken: null,
    controllerEpoch: 0,
    ownsControl: false,
    connectionInterrupted: false,
    snapshotId: null,
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
    positionRetryTimer: 0,
    positionRetryAttempt: 0,
    mutationBarrierTail: Promise.resolve(),
    activityOpening: false,
    mangaOpening: false,
    rescanBusy: false,
    entryNavigationPending: false,
    entryNavigationToken: 0,
    historyNavigationPending: false,
    historyChangeToken: 0,
  };

  function createOpaqueId() {
    if (typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }

  function bindEvents() {
    elements.stateAction.addEventListener("click", () => {
      if (typeof state.stateAction === "function") {
        void state.stateAction();
      }
    });
    elements.libraryRefresh.addEventListener("click", () => void rescanLibrary());
    elements.releaseControl.addEventListener("click", () => void releaseController());
    elements.emptyRetry.addEventListener("click", () => void rescanLibrary());
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
  }

  async function requestJson(path, options = {}) {
    const needsController = options.controller !== false;
    const ownershipEpoch = state.controllerEpoch;
    const controllerToken = state.controllerToken;
    if (needsController && (!state.ownsControl || !controllerToken)) {
      throw new ApiError(
        "controller_required",
        "Take control before using the library.",
        409,
      );
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(
      () => controller.abort(),
      Number.isFinite(options.timeoutMs) ? options.timeoutMs : REQUEST_TIMEOUT_MS,
    );
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (needsController) {
      headers.set("X-Page-Id", state.pageId);
      headers.set("X-Controller-Token", controllerToken);
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
      if (needsController && ownershipEpoch !== state.controllerEpoch) {
        throw new ApiError(
          "controller_changed",
          "Controller ownership changed while the request was running.",
          409,
        );
      }
      state.connectionInterrupted = false;
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
    const ownershipEpoch = state.controllerEpoch;
    const controllerToken = state.controllerToken;
    if (!state.ownsControl || !controllerToken) {
      throw new ApiError("controller_required", "Take control to load images.", 409);
    }
    let response;
    try {
      response = await fetch(ROUTES.image(imageId), {
        method: "GET",
        headers: {
          Accept: "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.5",
          "X-Page-Id": state.pageId,
          "X-Controller-Token": controllerToken,
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
    if (ownershipEpoch !== state.controllerEpoch) {
      throw new ApiError(
        "controller_changed",
        "Controller ownership changed while the image was loading.",
        409,
      );
    }
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.startsWith("image/")) {
      throw new ApiError("invalid_image", "The PC returned an invalid image.");
    }
    return response.blob();
  }

  async function bootstrap() {
    showState({
      kicker: "Pocket Manga",
      title: "Connecting to your PC",
      message: "Opening your library…",
      loading: true,
    });
    await acquireController();
  }

  async function acquireController({ takeover = false, quiet = false } = {}) {
    if (!quiet) {
      showState({
        kicker: "Pocket Manga",
        title: takeover ? "Taking control" : "Opening your library",
        message: takeover
          ? "Waiting for any active save to finish…"
          : "Connecting to the local server…",
        loading: true,
      });
    }
    const priorView = captureActiveView();
    const uncertainCount = state.selectionPending.size + state.positionQueue.size;
    try {
      const payload = await requestJson(takeover ? ROUTES.takeover : ROUTES.claim, {
        method: "POST",
        body: { page_id: state.pageId },
        controller: false,
        timeoutMs: takeover ? TAKEOVER_TIMEOUT_MS : REQUEST_TIMEOUT_MS,
      });
      const controller = payload.controller || {};
      state.controllerToken = String(controller.token || "");
      state.ownsControl = Boolean(controller.owned && state.controllerToken);
      state.controllerEpoch += 1;
      state.connectionInterrupted = false;
      state.snapshotId = payload.snapshot_id || null;
      if (!state.ownsControl) {
        throw new ApiError("controller_required", "The PC did not grant control.", 409);
      }
      if (takeover) {
        discardUnconfirmedMutations();
      }
      updateControllerControls();
      await loadLibrary({ drain: false });
      if (takeover && priorView) {
        await restoreActiveView(priorView);
      }
      if (takeover && uncertainCount) {
        showActionError(
          "Review unconfirmed changes",
          `${uncertainCount} pending ${plural(uncertainCount, "save was", "saves were")} not replayed after control changed. The refreshed view shows what the PC actually saved.`,
        );
      }
    } catch (error) {
      if (error instanceof ApiError && error.code === "controller_occupied") {
        showOccupied();
      } else {
        showUnavailable(
          friendlyMessage(error, "The Pocket Manga server could not be reached."),
          () => acquireController({ takeover }),
        );
      }
    }
  }

  async function recoverConnection() {
    if (!state.ownsControl) {
      await acquireController({ takeover: true });
      return;
    }
    try {
      const payload = await requestJson(ROUTES.library);
      if (payload.snapshot_id && state.snapshotId && payload.snapshot_id !== state.snapshotId) {
        discardUnconfirmedMutations();
        applyLibraryPayload(payload);
        showActionError(
          "Library changed",
          "The PC library changed while disconnected. Unconfirmed saves were not replayed; review the refreshed library.",
        );
        return;
      }
      state.connectionInterrupted = false;
      retryPendingMutations();
      if (await drainPendingMutations()) {
        hideActionError();
        announce("Reconnected and pending saves confirmed.");
      }
    } catch (error) {
      handleSessionError(error, recoverConnection);
    }
  }

  function captureActiveView() {
    const image = currentImage();
    if (!state.activityManga) {
      return null;
    }
    return {
      mangaId: state.activityManga.id,
      activity: state.activity,
      folderId: state.currentFolder ? state.currentFolder.id : "",
      imageId: image ? image.id : "",
    };
  }

  async function restoreActiveView(view) {
    const manga = state.library.find((candidate) => candidate.id === view.mangaId);
    if (!manga) {
      return;
    }
    showActivityChoice(manga, { historyMode: "none" });
    if (!view.activity) {
      return;
    }
    await chooseActivity(view.activity, { historyMode: "none" });
    if (
      view.folderId
      && state.currentManga
      && state.currentManga.folders.some((folder) => folder.id === view.folderId)
      && (!state.currentFolder || state.currentFolder.id !== view.folderId)
    ) {
      await openFolder(view.folderId, view.imageId, {
        persist: false,
        historyMode: "none",
      });
    } else if (view.imageId && state.currentFolder) {
      const index = state.currentFolder.images.findIndex(
        (image) => image.id === view.imageId,
      );
      if (index >= 0) {
        showImage(index, { persist: false });
      }
    }
  }

  function discardUnconfirmedMutations() {
    state.selectionPending.clear();
    state.positionQueue.clear();
    state.connectionInterrupted = false;
    if (state.activity === EDIT) {
      renderSelectionState(currentImage());
    }
  }

  async function releaseController() {
    if (!state.ownsControl || !(await drainPendingMutations())) {
      return;
    }
    try {
      await requestJson(ROUTES.release, {
        method: "POST",
        body: { page_id: state.pageId, token: state.controllerToken },
      });
      state.ownsControl = false;
      state.controllerToken = null;
      state.controllerEpoch += 1;
      updateControllerControls();
      showState({
        kicker: "Control released",
        title: "Ready when you are",
        message: "Take control here or open the app on another device.",
        loading: false,
        actionLabel: "Take Control",
        action: () => acquireController(),
      });
    } catch (error) {
      await handleSessionError(error, releaseController);
    }
  }

  function loseControl() {
    state.ownsControl = false;
    state.controllerToken = null;
    state.controllerEpoch += 1;
    state.connectionInterrupted = false;
    updateControllerControls();
    showOccupied();
  }

  async function handleSessionError(error, retry) {
    const code = error instanceof ApiError ? error.code : "network_error";
    if (["controller_occupied", "controller_required", "controller_transferred"].includes(code)) {
      loseControl(friendlyMessage(error));
      return;
    }
    if (code === "controller_changed") {
      return;
    }
    if (code === "stale_snapshot" || code === "invalid_snapshot") {
      const uncertainCount = state.selectionPending.size + state.positionQueue.size;
      discardUnconfirmedMutations();
      if (state.ownsControl) {
        await loadLibrary();
      }
      if (uncertainCount) {
        showActionError(
          "Library changed",
          "Unconfirmed saves were not replayed because their source entries are no longer in the active library snapshot.",
        );
      }
      return;
    }
    if (hasVisibleAppView()) {
      state.connectionInterrupted = true;
      showActionError(
        "Connection interrupted",
        "This screen and any unconfirmed saves have been kept. Reconnect to the PC, then retry.",
        recoverConnection,
        "Retry",
      );
      announce("Connection interrupted. Current screen retained.");
      return;
    }
    showUnavailable(friendlyMessage(error, "The Pocket Manga server could not be reached."), retry);
  }

  function hasVisibleAppView() {
    return (
      elements.stateScreen.hidden
      && (!elements.libraryScreen.hidden
        || !elements.activityScreen.hidden
        || !elements.readerScreen.hidden)
    );
  }

  function showOccupied() {
    state.ownsControl = false;
    state.controllerToken = null;
    showState({
      kicker: "Controller in use",
      title: "Pocket Manga is open elsewhere",
      message: "Another tab or device currently controls this library.",
      loading: false,
      actionLabel: "Take Control",
      action: () => acquireController({ takeover: true }),
      detail: "Taking control is immediate once any in-progress save finishes.",
    });
  }

  function showUnavailable(message, retry = bootstrap) {
    showState({
      kicker: "Connection unavailable",
      title: "Can’t reach your PC",
      message,
      loading: false,
      actionLabel: "Try again",
      action: retry,
      detail: "Confirm the PC is awake, the server is running, and both devices use the same local network.",
    });
    announce("Pocket Manga is unavailable.");
  }

  function showState({ kicker, title, message, loading, actionLabel = "", action = null, detail = "" }) {
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
    elements.stateAction.hidden = !actionLabel;
    elements.stateAction.textContent = actionLabel || "Try again";
    elements.stateDetail.hidden = !detail;
    elements.stateDetail.textContent = detail;
    state.stateAction = action;
  }

  async function loadLibrary({ drain = true } = {}) {
    if (drain && !(await drainPendingMutations())) {
      return;
    }
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
      applyLibraryPayload(payload);
    } catch (error) {
      if (
        requestToken === state.viewRequestToken
        && epoch === state.activityEpoch
      ) {
        handleSessionError(error, loadLibrary);
      }
    }
  }

  async function rescanLibrary() {
    if (!state.ownsControl || state.rescanBusy) {
      return;
    }
    state.rescanBusy = true;
    elements.libraryRefresh.disabled = true;
    elements.libraryRefresh.classList.add("is-busy");
    elements.libraryRefresh.setAttribute("aria-busy", "true");
    elements.emptyRetry.disabled = true;
    hideActionError();
    showToast("Rescanning library…", 10_000);
    if (!(await drainPendingMutations())) {
      state.rescanBusy = false;
      elements.libraryRefresh.classList.remove("is-busy");
      elements.libraryRefresh.setAttribute("aria-busy", "false");
      updateControllerControls();
      return;
    }
    const requestToken = ++state.viewRequestToken;
    const epoch = ++state.activityEpoch;
    try {
      const payload = await requestJson(ROUTES.rescan, {
        method: "POST",
        body: {},
      });
      if (requestToken !== state.viewRequestToken || epoch !== state.activityEpoch) {
        return;
      }
      applyLibraryPayload(payload);
      showToast("Library rescanned.", 2_000);
      announce("Library rescan complete.");
    } catch (error) {
      if (requestToken === state.viewRequestToken && epoch === state.activityEpoch) {
        if (isSessionGateError(error)) {
          handleSessionError(error, rescanLibrary);
        } else {
          showActionError(
            "Couldn’t rescan library",
            friendlyMessage(error),
            rescanLibrary,
          );
        }
      }
    } finally {
      state.rescanBusy = false;
      elements.libraryRefresh.disabled = !state.ownsControl;
      elements.libraryRefresh.classList.remove("is-busy");
      elements.libraryRefresh.setAttribute("aria-busy", "false");
      elements.emptyRetry.disabled = !state.ownsControl;
    }
  }

  function applyLibraryPayload(payload) {
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
  }

  function normalizeMangaSummary(manga) {
    return {
      id: String(manga && manga.id || ""),
      name: String(manga && manga.name || "Untitled manga"),
    };
  }

  function renderLibrary() {
    elements.libraryList.replaceChildren();
    for (const manga of state.library) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "manga-card";
      button.addEventListener("click", () => void openManga(manga));

      const text = document.createElement("span");
      const title = document.createElement("span");
      title.className = "manga-card-title";
      title.textContent = manga.name;
      text.append(title);

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

    elements.librarySummary.textContent = `${state.library.length} manga`;
    elements.libraryEmpty.hidden = state.library.length > 0;
    elements.libraryList.hidden = state.library.length === 0;
    elements.libraryNotice.hidden = state.libraryIssueCount === 0;
    if (state.libraryIssueCount) {
      elements.libraryNotice.textContent = `${state.libraryIssueCount} source ${state.libraryIssueCount === 1 ? "item was" : "items were"} skipped by the PC scan.`;
    }
  }

  // The library lists names only. Tapping a manga scans its entries and
  // images on the PC, so chapters added since the last visit appear here.
  async function openManga(manga) {
    if (!state.ownsControl || state.mangaOpening || state.rescanBusy) {
      return;
    }
    state.mangaOpening = true;
    try {
      if (!(await drainPendingMutations())) {
        return;
      }
      const requestToken = ++state.viewRequestToken;
      const epoch = ++state.activityEpoch;
      showToast("Opening manga…", 1_200);
      try {
        await requestJson(ROUTES.mangaScan(manga.id), { method: "POST", body: {} });
        if (requestToken === state.viewRequestToken && epoch === state.activityEpoch) {
          showActivityChoice(manga, { historyMode: "push" });
        }
      } catch (error) {
        if (requestToken !== state.viewRequestToken || epoch !== state.activityEpoch) {
          return;
        }
        if (isSessionGateError(error)) {
          handleSessionError(error, () => openManga(manga));
        } else {
          showActionError(`Can’t open ${manga.name}`, friendlyMessage(error), () => openManga(manga));
        }
      }
    } finally {
      state.mangaOpening = false;
    }
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

  function setActivityActionsDisabled(disabled) {
    const blocked = disabled || !state.ownsControl;
    elements.chooseRead.disabled = blocked;
    elements.chooseEdit.disabled = blocked;
  }

  async function chooseActivity(activity, { historyMode = "push" } = {}) {
    const manga = state.activityManga;
    if (
      !manga
      || !state.ownsControl
      || (activity !== READ && activity !== EDIT)
      || state.activityOpening
    ) {
      return;
    }
    state.activityOpening = true;
    elements.chooseRead.disabled = true;
    elements.chooseEdit.disabled = true;
    if (!(await drainPendingMutations())) {
      state.activityOpening = false;
      setActivityActionsDisabled(false);
      return;
    }
    if (state.activityManga !== manga) {
      state.activityOpening = false;
      setActivityActionsDisabled(false);
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
      const raw = payload.manga || {};
      const folders = Array.isArray(raw.folders)
        ? raw.folders.map(normalizeFolderSummary).filter((folder) => folder.id)
        : [];
      if (!folders.length) {
        throw new ApiError("no_images", "This manga has no images to open.");
      }
      state.activity = activity;
      state.chromeVisible = true;
      state.currentManga = {
        id: String(raw.id || manga.id),
        name: String(raw.name || manga.name),
        currentFolderId: String(raw.current_folder_id || ""),
        currentImageId: String(raw.current_image_id || ""),
        folders,
      };
      populateFolderPicker(folders);
      const preferred = folders.find((folder) => folder.id === state.currentManga.currentFolderId)
        || folders[0];
      await openFolder(preferred.id, state.currentManga.currentImageId, {
        persist: false,
        historyMode,
        requestEpoch: epoch,
      });
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
      setActivityActionsDisabled(false);
    }
  }

  function normalizeFolderSummary(folder) {
    return {
      id: String(folder && folder.id || ""),
      name: String(folder && folder.name || "Untitled folder"),
    };
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
      state.currentFolder = {
        id: String(raw.id || folderId),
        mangaId: String(raw.manga_id || (state.currentManga && state.currentManga.id) || ""),
        name: String(raw.name || "Untitled folder"),
        currentImageId: String(raw.current_image_id || ""),
        images,
      };
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
    elements.selectedPicker.disabled = (
      !state.ownsControl
      || state.activity !== EDIT
      || selectedImages.length === 0
    );
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
    elements.selectionZone.disabled = !editing || !state.ownsControl;
    elements.selectedPickerShell.hidden = !editing;
    elements.readerScreen.classList.toggle("chrome-hidden", !state.chromeVisible);
    elements.chromeToggle.setAttribute("aria-pressed", String(!state.chromeVisible));
    elements.chromeToggle.setAttribute(
      "aria-label",
      state.chromeVisible ? "Hide reader controls" : "Show reader controls",
    );
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
    if (!state.ownsControl || state.entryNavigationPending) {
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
    elements.selectionZone.disabled = state.activity !== EDIT || !state.ownsControl;
    elements.previousZone.disabled = !state.ownsControl;
    elements.nextZone.disabled = !state.ownsControl;
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
    }, 425);
  }

  function currentImage() {
    const folder = state.currentFolder;
    return folder && folder.images[state.currentImageIndex] || null;
  }

  function toggleCurrentSelection() {
    if (!state.ownsControl || state.activity !== EDIT) {
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
    const pending = {
      desired,
      folderId: folder.id,
      epoch,
      folder,
      image,
      inFlight: false,
      retries: 0,
      ownershipEpoch: state.controllerEpoch,
    };
    state.selectionPending.set(image.id, pending);
    if (isCurrentImage(folder.id, image.id, epoch)) {
      renderSelectionState(currentImage());
    }
    await commitSelection(pending);
  }

  async function commitSelection(pending) {
    if (pending.inFlight || state.selectionPending.get(pending.image.id) !== pending) {
      return;
    }
    const { folder, image, desired, epoch } = pending;
    pending.inFlight = true;
    try {
      const payload = await queueSelectionRequest(pending);
      const selection = payload.selection || {};
      const confirmed = typeof selection.selected === "boolean" ? selection.selected : desired;
      image.selected = confirmed;
      if (state.selectionPending.get(image.id) === pending) {
        state.selectionPending.delete(image.id);
      }

      // The folder may have been reloaded while the save was in flight.
      const liveFolder = state.activity === EDIT
        && state.currentFolder
        && state.currentFolder.id === folder.id
        ? state.currentFolder
        : null;
      const liveImage = liveFolder
        ? liveFolder.images.find((candidate) => candidate.id === image.id) || null
        : null;
      if (liveImage) {
        liveImage.selected = confirmed;
      }
      if (liveFolder && epoch === state.activityEpoch) {
        populateImagePickers();
        elements.imagePicker.value = currentImage() ? currentImage().id : "";
        if (currentImage() && currentImage().id === image.id) {
          renderSelectionState(currentImage());
          pulseSelection("selection-pulse");
        }
      }
    } catch (error) {
      pending.inFlight = false;
      if (state.selectionPending.get(image.id) !== pending) {
        return;
      }
      if (
        isTransientNetworkError(error)
        && pending.ownershipEpoch === state.controllerEpoch
        && pending.retries < SELECTION_RETRY_DELAYS_MS.length
      ) {
        // Keep the pending marker and try again before reporting an interruption.
        scheduleSelectionRetry(pending);
        return;
      }
      if (isUncertainSaveError(error)) {
        state.connectionInterrupted = true;
        if (!(error instanceof ApiError) || error.code !== "controller_changed") {
          void handleSessionError(error, recoverConnection);
        }
        if (isCurrentImage(folder.id, image.id, epoch)) {
          renderSelectionState(currentImage());
        }
        return;
      }
      state.selectionPending.delete(image.id);
      const failedCurrentImage = isCurrentImage(folder.id, image.id, epoch);
      if (failedCurrentImage && state.activity === EDIT) {
        renderSelectionState(currentImage());
        pulseSelection("selection-failed");
      }
      if (epoch === state.activityEpoch && state.activity === EDIT) {
        showReaderFeedback("Selection not saved", "error", 425);
      }
    }
  }

  function queueSelectionRequest(pending) {
    const request = state.selectionRequestTail.then(() => {
      if (
        pending.ownershipEpoch !== state.controllerEpoch
        || state.selectionPending.get(pending.image.id) !== pending
      ) {
        throw new ApiError(
          "controller_changed",
          "Controller ownership changed before the selection could be saved.",
          409,
        );
      }
      return requestJson(ROUTES.editSelection(pending.folderId), {
        method: "PATCH",
        body: { image_id: pending.image.id, selected: pending.desired },
        timeoutMs: SAVE_TIMEOUT_MS,
      });
    });
    state.selectionRequestTail = request.catch(() => {});
    return request;
  }

  function scheduleSelectionRetry(pending) {
    const delay = SELECTION_RETRY_DELAYS_MS[pending.retries];
    pending.retries += 1;
    window.setTimeout(() => {
      if (
        !state.connectionInterrupted
        && state.ownsControl
        && pending.ownershipEpoch === state.controllerEpoch
      ) {
        void commitSelection(pending);
      }
    }, delay);
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

  function queuePosition(activity, folderId, imageId, epoch) {
    const mangaId = state.currentManga ? state.currentManga.id : folderId;
    const key = `${activity}:${mangaId}`;
    state.positionQueue.set(key, {
      activity,
      folderId,
      imageId,
      epoch,
      ownershipEpoch: state.controllerEpoch,
    });
    state.positionRetryAttempt = 0;
    ensurePositionFlush();
  }

  function schedulePositionRetry() {
    window.clearTimeout(state.positionRetryTimer);
    state.positionRetryTimer = 0;
    if (state.positionRetryAttempt >= POSITION_RETRY_DELAYS_MS.length) {
      // The next page turn, view change, or explicit retry tries again.
      return;
    }
    const delay = POSITION_RETRY_DELAYS_MS[state.positionRetryAttempt];
    state.positionRetryAttempt += 1;
    state.positionRetryTimer = window.setTimeout(() => {
      state.positionRetryTimer = 0;
      ensurePositionFlush();
    }, delay);
  }

  function abandonQueuedPositions() {
    window.clearTimeout(state.positionRetryTimer);
    state.positionRetryTimer = 0;
    state.positionRetryAttempt = 0;
    state.positionQueue.clear();
  }

  function ensurePositionFlush() {
    if (
      !state.connectionInterrupted
      && state.ownsControl
      && !state.positionFlushActive
      && state.positionQueue.size
    ) {
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
            method: "PATCH",
            body: { image_id: pending.imageId },
            timeoutMs: SAVE_TIMEOUT_MS,
          });
          state.positionRetryAttempt = 0;
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
          }
        } catch (error) {
          const ownershipIsCurrent = pending.ownershipEpoch === state.controllerEpoch;
          if (ownershipIsCurrent && isTransientNetworkError(error)) {
            // Positions are best-effort and latest-wins: retry quietly
            // without interrupting reading.
            if (!state.positionQueue.has(key)) {
              state.positionQueue.set(key, pending);
            }
            schedulePositionRetry();
            break;
          }
          if (ownershipIsCurrent && isUncertainSaveError(error)) {
            if (!state.positionQueue.has(key)) {
              state.positionQueue.set(key, pending);
            }
            state.connectionInterrupted = true;
            if (!(error instanceof ApiError) || error.code !== "controller_changed") {
              void handleSessionError(error, recoverConnection);
            }
            break;
          }
          const failureStillRelevant = (
            !state.positionQueue.has(key)
            && state.activity === pending.activity
            && pending.epoch === state.activityEpoch
          );
          if (failureStillRelevant) {
            showReaderFeedback("Position not saved", "error", 425);
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
        if (state.connectionInterrupted || !state.ownsControl) {
          if (state.connectionInterrupted && state.ownsControl) {
            showActionError(
              "Confirm pending saves",
              "Reconnect to the PC before leaving this view or starting a filesystem operation.",
              recoverConnection,
              "Retry",
              false,
            );
          }
          return false;
        }
        for (const pending of state.selectionPending.values()) {
          if (!pending.inFlight) {
            void commitSelection(pending);
          }
        }
        const positionTail = ensurePositionFlush();
        const selectionTail = state.selectionRequestTail;
        await Promise.allSettled([positionTail, selectionTail]);
        // Allow setSelection's confirmation/failure continuation to clear its
        // pending marker after the serialized request tail settles.
        await Promise.resolve();
        if (
          positionTail === state.positionFlushPromise
          && !state.positionFlushActive
          && state.positionQueue.size
          && !state.connectionInterrupted
        ) {
          // That flush stopped on a network failure. A missed position is
          // harmless, so leaving the view does not wait for it.
          abandonQueuedPositions();
        }
        if (
          positionTail === state.positionFlushPromise
          && selectionTail === state.selectionRequestTail
          && !state.positionFlushActive
          && state.positionQueue.size === 0
          && state.selectionPending.size === 0
        ) {
          return true;
        }
      }
    });
    state.mutationBarrierTail = drain.catch(() => {});
    return drain;
  }

  function retryPendingMutations() {
    if (!state.ownsControl) {
      return;
    }
    state.connectionInterrupted = false;
    for (const pending of state.selectionPending.values()) {
      pending.inFlight = false;
      pending.retries = 0;
      void commitSelection(pending);
    }
    state.positionRetryAttempt = 0;
    ensurePositionFlush();
  }

  function isTransientNetworkError(error) {
    if (error && error.name === "AbortError") {
      return true;
    }
    return error instanceof ApiError && error.code === "network_error";
  }

  function isUncertainSaveError(error) {
    if (error && error.name === "AbortError") {
      return true;
    }
    return error instanceof ApiError && [
      "network_error",
      "controller_required",
      "controller_transferred",
      "controller_changed",
    ].includes(error.code);
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
          if (isSessionGateError(error)) {
            void handleSessionError(error, loadCurrentImage);
          }
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
    if (!(await drainPendingMutations())) {
      state.historyNavigationPending = false;
      return;
    }
    window.history.back();
    window.setTimeout(() => {
      state.historyNavigationPending = false;
    }, 500);
  }

  async function historyChanged(event) {
    const changeToken = ++state.historyChangeToken;
    if (!(await drainPendingMutations())) {
      return;
    }
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

  function showActionError(
    title,
    message,
    retry = null,
    retryLabel = "Retry",
    dismissible = true,
  ) {
    state.actionRetry = retry;
    elements.actionErrorTitle.textContent = title;
    elements.actionErrorMessage.textContent = message;
    elements.actionErrorRetry.hidden = typeof retry !== "function";
    elements.actionErrorRetry.textContent = retryLabel;
    elements.actionErrorDismiss.hidden = !dismissible;
    elements.actionError.hidden = false;
  }

  function hideActionError() {
    state.actionRetry = null;
    elements.actionErrorDismiss.hidden = false;
    elements.actionError.hidden = true;
  }

  function updateControllerControls() {
    const disabled = !state.ownsControl;
    elements.libraryRefresh.disabled = disabled || state.rescanBusy;
    elements.releaseControl.disabled = disabled || state.rescanBusy;
    elements.emptyRetry.disabled = disabled || state.rescanBusy;
    elements.chooseRead.disabled = disabled || state.activityOpening;
    elements.chooseEdit.disabled = disabled || state.activityOpening;
    elements.folderPicker.disabled = disabled;
    elements.imagePicker.disabled = disabled;
    elements.selectedPicker.disabled = disabled || state.activity !== EDIT;
    elements.selectionZone.disabled = disabled || state.activity !== EDIT;
    elements.previousZone.disabled = disabled || !state.currentFolder;
    elements.nextZone.disabled = disabled || !state.currentFolder;
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
      "controller_occupied",
      "controller_required",
      "controller_transferred",
      "controller_changed",
      "stale_snapshot",
      "invalid_snapshot",
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
