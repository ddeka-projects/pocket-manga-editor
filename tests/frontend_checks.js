async function checks() {
  const { state, acquireController, releaseController, loseControl, requestJson } = client;
  const sent = [];
  transport = async (path) => {
    sent.push(path);
    if (path === "/api/controller/claim") return reply({ ok: true, controller: { owned: true, token: "first-token" } });
    return reply({ ok: true, mangas: [], snapshot_id: "first-snapshot" });
  };
  await acquireController();
  assert(state.ownsControl, "First page claims control automatically");
  assert(element("state-screen").hidden && !element("library-screen").hidden, "Claim opens the library");
  assert(sent.join(",") === "/api/controller/claim,/api/library", "No polling or extra requests on entry");

  state.positionQueue.set("read:manga", { activity: "read", folderId: "folder", imageId: "image", epoch: state.activityEpoch, ownershipEpoch: state.controllerEpoch });
  sent.length = 0;
  transport = async (path, options) => {
    sent.push(path);
    assert(options.headers.get("X-Page-Id") === state.pageId, "Protected requests use the common page header");
    assert(options.headers.get("X-Controller-Token") === "first-token", "Existing token protects both requests");
    return reply({ ok: true, released: true });
  };
  await releaseController();
  assert(sent.join(",") === "/api/read/folder/folder/position,/api/controller/release", "Release waits for the pending position to be saved");
  assert(!state.ownsControl && state.controllerToken === null, "Confirmed release clears local authority");
  assert(!element("state-screen").hidden && element("library-screen").hidden, "Release hides the entire workspace");
  assert(element("state-action").textContent === "Take Control", "Explicit reentry remains available");

  state.ownsControl = true;
  state.controllerToken = "live-token";
  element("state-screen").hidden = true;
  element("library-screen").hidden = false;
  transport = async () => { throw new Error("offline"); };
  await releaseController();
  assert(state.ownsControl && state.controllerToken === "live-token", "An unconfirmed release retains its identity for recovery");
  assert(element("action-error-title").textContent === "Connection interrupted", "Network failure states that confirmation is missing");

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

  await transientSaveChecks();
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
