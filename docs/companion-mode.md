# Companion Mode

Companion Mode is a trusted-local-network extension of Pocket Manga Editor. The
desktop process remains the only filesystem authority while one paired Home
Screen app owns the mobile controller lease.

## Read and Edit activities

Tapping a manga always opens an activity choice before metadata is loaded:

- **Read** uses only the manga's `reading.json`. It remembers a last folder and
  one exact current filename per visited folder. It contains no selected counts,
  selection controls, indicators, or selection requests.
- **Edit** uses the same `editing.json` as the desktop. It remembers editing
  position, exact selected filenames, and output ownership records.

Returning from the reader returns to the activity choice; returning again opens
the manga library. Switching activity loads the other metadata document and
does not carry position or selection presentation across the boundary.

## Reader controls

Both activities show one contained image on a black viewport and prefetch only
its adjacent images. In the middle band, the left 30% moves back, the center 40%
shows or hides controls, and the right 30% moves forward. At a folder boundary,
forward opens the next folder's first image, while back opens the previous
folder's last image. Navigation stops only at the first or last folder in the
manga. Choosing a folder from the picker instead resumes that folder's saved
image. In Edit, the lower band selects or deselects the image. In Read, that
lower band is disabled and does not intercept taps.

Folder and image pickers display exact source names. Edit also has a
current-folder selected-image picker and the confirmed green frame/checkmark.
There is no decorative navigation delay or routine Selected/Deselected message.

## Ownership and persistence

The coordinator retains its fail-closed desktop/Companion state machine. Before
entry, desktop editing is saved and the library is rescanned into an immutable
opaque-ID snapshot. While Companion is active, desktop navigation, selections,
export, completion, rescan, and folder changes remain disabled for both Read
and Edit.

Every protected request requires:

- active Companion ownership;
- the paired-device credential;
- the exact `(client_id, page_instance_id)` controller lease;
- a current opaque manga, folder, and image ID;
- a server-bound `read` or `edit` activity; and
- a live regular JPG/PNG at the expected direct-child source path.

The server rejects Edit selection calls while bound to Read before opening
`editing.json`. Successful position and selection responses are returned only
after the corresponding atomic JSON write succeeds. An Edit save failure keeps
both writers blocked for recovery. A Read save failure keeps the last confirmed
bookmark and never touches editing state.

Ending Companion rejects new phone writes, drains the active request, rescans,
and reloads desktop editing state before returning desktop authority. A final
Read location does not move the desktop editor; a final Edit location does.

## Network setup

1. Reserve the PC's LAN address in the router or DHCP server.
2. Under **Mobile Companion → Connection…**, enter that address and a fixed port
   from `1024` through `65535`.
3. Permit incoming private-network connections when the OS firewall asks. Never
   expose the port through router forwarding.
4. Open the displayed URL in Safari, pair with the six-digit code, then choose
   **Share → Add to Home Screen**.

The server runs in a dedicated standard-library HTTP thread after the desktop
singleton is acquired. The persistent device credential is an HttpOnly,
SameSite=Strict cookie; the PC stores only its SHA-256 verifier. Protected
requests include `X-Companion-Instance` and `X-Companion-Page`. A separate
per-document claim prevents a duplicated tab from sharing authority.

After a phone has been paired, launching the desktop application automatically
enters Companion Mode when its saved working folder scans successfully and the
Companion server is running. The phone does not need to be online at that moment;
it can reconnect later. Missing credentials, an unavailable server, or an empty
or invalid saved library leave the application in desktop mode instead.

## Troubleshooting

### Connection refused

Keep the PC awake and the desktop app running. Confirm both devices are on the
same LAN, the displayed address still belongs to the PC, and the firewall allows
the chosen port. `127.0.0.1` is reachable only from the PC.

### Port already in use

Desktop editing remains available. Choose another fixed port under
**Connection…** and update the Home Screen shortcut if the address changes.

### Pairing code rejected

Generate a new code. Codes expire after five minutes and close after repeated
failures. If Safari data was cleared, use **Forget Phone** and pair again.

### Companion is active elsewhere

Only one page instance may own the lease. Return to the original Home Screen
instance, wait for its reclaim window to expire, or choose **Disconnect Mobile
Client** on the PC.

### Desktop controls are disabled

This is expected during Companion ownership. Choose **End Companion Mode**. If
an exit or Edit save error occurred, correct the reported filesystem/lock issue
and use recovery; both writers stay blocked until the library is reconciled.

## Security boundary

Companion deliberately uses unencrypted HTTP and is for a trusted LAN only.
Host and same-origin validation, short-lived pairing, one-controller leasing,
opaque snapshot IDs, activity authorization, strict JSON, authenticated image
delivery, private media caching, and live path revalidation reduce accidental
or cross-origin access. They do not make the service suitable for public Wi-Fi,
the internet, VPN publication, or port forwarding.
