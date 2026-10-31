// Unsaved work kept on the server as you work: the Channels page's Auto-match suggestions and the On demand page's
// adds and removes. Only a holding area: nothing is applied until the page's Save. One request at a time and the
// newest copy wins; `rev` stops a stale page (another tab or device) from writing over newer work.
import { toastError } from "./ui.js";

// A fresh draft record. `sent` is the key of the copy the server has; `rev` its version.
export const newDraft = (extra = {}) => ({ loaded: false, rev: 0, sent: "", busy: false, again: false, warned: false, ...extra });

// Send the page's current copy (identified by `key`) unless the server already has it.
//   send(rev)        -> Promise of the server's answer ({rev, ...})
//   alive()          -> the page is still the one that started this (it was not left or rebuilt)
//   current()        -> `d` is still the page's draft (a conflict or reload did not replace it)
//   saved(r)         -> after a successful write
//   conflict(d)      -> the server has newer work (409): the page takes the server's copy
//   retry()          -> the page's own save, called again later
export function keepOnServer(d, key, { send, alive, current, saved, conflict, retry, what }) {
  if (!d.loaded || key === d.sent) return;
  if (d.busy) { d.again = true; return; }
  d.busy = true;
  send(d.rev)
    .then((r) => {
      if (!alive() || !current()) return;
      d.rev = r.rev; d.sent = key; d.warned = false;
      saved?.(r);
    })
    .catch(async (e) => {
      if (!alive()) return;
      if (e?.status === 409) { await conflict(d); return; }
      if (!d.warned) { d.warned = true; toastError(`Could not keep the ${what} on the server; retrying`, e); }
      setTimeout(() => { if (alive()) retry(); }, 5000);
    })
    .finally(() => {
      d.busy = false;
      if (current() && d.again && alive()) { d.again = false; retry(); }
    });
}
