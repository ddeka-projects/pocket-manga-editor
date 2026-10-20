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
}
checks().then(
  () => { if (typeof print === "function") print("Frontend behavior checks passed"); else console.log("Frontend behavior checks passed"); },
  (error) => { if (typeof print === "function") { print(error.stack); throw error; } else { console.error(error); process.exitCode = 1; } },
);
