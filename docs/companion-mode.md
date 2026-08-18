# Companion Mode

Companion Mode is a local-network extension of Pocket Manga Editor. The desktop
process remains the only filesystem authority while one paired browser controls
review position and page selections through the same `SessionStore` JSON used by
the desktop UI.

## Architecture decision

The initial server uses Python's standard-library `ThreadingHTTPServer` in a
dedicated daemon thread. It was selected instead of FastAPI/Uvicorn because the
repository otherwise has no web runtime dependencies, the required route set is
small, and the standard server provides explicit bind, shutdown, and testable
request-dispatch behavior. The API dispatcher and ownership coordinator remain
framework-independent so the transport can be replaced later without changing
the persistence boundary.

The server starts after the per-user `QLockFile` singleton is acquired and stays
available for the desktop process lifetime. Outside Companion Mode it serves
only the installable shell, pairing state, and a minimal inactive status. Library
names, metadata, and images require a paired credential and the active controller
lease.

## Ownership model

The coordinator has five states: desktop active, entering Companion, Companion
active, exiting Companion, and Companion error. Desktop mutation methods check
the coordinator gate even if their buttons have already been disabled. Mobile
writes require all of the following:

- Companion Mode is active.
- The persistent device credential is valid.
- The browser instance owns the single live controller lease.
- Every manga, volume, and page ID resolves through the active immutable
  snapshot.
- The live source file remains a regular JPG or PNG inside the expected manga
  directory.

Desktop autosaves are flushed before entry. Mobile selection and position
commands are serialized and persisted before success is returned. Exit rejects
new phone writes, drains the current command, discards the snapshot, rescans,
and reloads `SessionStore` before desktop controls are enabled again.

## Network setup

1. Reserve the desktop PC's address in the router or DHCP server. An address such
   as `192.168.1.20` is typical; use the value assigned by the user's own router.
2. In Pocket Manga Editor's sidebar, find **Mobile Companion**, open
   **Connection…**, enter that address, and choose a fixed port from `1024`
   through `65535`.
3. If the operating system asks whether Python may accept incoming connections,
   permit it on private networks. Do not expose the port through router port
   forwarding.
4. Verify the displayed URL opens from Safari while the phone is on the same
   Wi-Fi.
5. Pair using the six-digit code displayed by the PC. The code expires after
   five minutes and closes after repeated failed attempts.
6. Add the stable page to the iPhone Home Screen. The manifest uses standalone
   portrait mode, safe-area insets, and app icons included with the repository.

The persistent browser credential is an HttpOnly, SameSite=Strict cookie. The PC
stores only its SHA-256 verifier under the current user's application-data
folder. A session-scoped client identity supports background reconnection, while
a separate per-document claim prevents a duplicated tab from sharing the live
controller lease. Protected requests carry both identities in
`X-Companion-Instance` and `X-Companion-Page`; controller lifecycle requests
also send them as `client_id` and `page_id`. Clearing Safari website data
requires pairing again.

## Reader controls

The reader shows one contained page on a black viewport. In the middle reading
band, the left 30% goes to the previous page, the center 40% sets selection, and
the right 30% goes to the next page. Navigation never wraps. The lower band
shows or hides the chrome; selection framing remains visible. The top controls
return to the library or choose a volume, selected page, or any page. Only
adjacent images are prefetched.

## Troubleshooting

### Connection refused

Keep the desktop app running and awake. Confirm the phone is on the same LAN,
the displayed IP still belongs to the PC, and the firewall permits the selected
port. A loopback address such as `127.0.0.1` works only on the PC; configure the
reserved LAN address for the phone.

### Port already in use

Desktop review remains usable. Open **Connection…**, choose another fixed port,
save, and then update the Home Screen shortcut if its URL changed.

### Pairing code rejected

Generate a new code with **Pair Phone**. Codes expire and repeated incorrect
attempts close the pairing window. If the phone was previously paired but its
website data was cleared, use **Forget Phone** first and pair again.

### Companion is active elsewhere

Only one browser instance may hold the controller lease. Return to the original
Home Screen instance, wait through its reconnection grace period, or use
**Disconnect Mobile Client** on the PC before retrying.

### Desktop controls are disabled

This is expected while Companion Mode owns review state. Use **End Companion
Mode**. If an exit or save error is shown, correct the reported filesystem or
lock issue and use the recovery/retry control; the coordinator deliberately
keeps both writers blocked until the state is reconciled.

## Security boundary

The initial release intentionally uses HTTP without transport encryption and is
for a trusted local network only. Host and same-origin checks, short-lived
pairing codes, a remembered device verifier, one-client leasing, opaque snapshot
IDs, authenticated image routes, strict JSON writes, and path revalidation
reduce accidental or cross-origin access. They do not make the service suitable
for the public internet, hostile Wi-Fi, VPN publication, or router port
forwarding.
