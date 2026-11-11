# Nous Sync (local folder agent)

Watches chosen folders on this computer and sends **metadata only** (names, sizes, times, structure) to the
Nous Drive index. Phase 2 sends extracted text only for files the server asks about, and only when content
indexing is switched on for that folder.

```bash
curl -fsSL https://drive.nous.example/install.sh | bash -s -- --code ABCD-EFGH --server https://drive.nous.example --name "Nous MacBook Pro"
nous-sync add ~/Footage
nous-sync add "/Volumes/Edit RAID/Projects" --ignore "Render Cache/*"
nous-sync status
```

* Starts at login (launchd / systemd --user / Task Scheduler) and runs at low priority.
* Live changes via FSEvents / ReadDirectoryChangesW / inotify; files are reported once they stop changing.
* Renames and moves keep the file's identity (inode / file id), also for changes made while offline.
* Catch-up on start: a fresh scan is compared with the last known state and only the difference is sent.
* Events are written to a local durable outbox first, so nothing is lost while offline.
* External drives: when a drive is unplugged its files are marked unavailable (not deleted).
* Symlinks are recorded, not followed. Default ignore rules skip system/temp files (.DS_Store, Thumbs.db, ~$ lock
  files, .tmp, caches, .git); add your own per folder with `--ignore` or in the web app.
* Re-pairing the same machine (e.g. after a reinstall) keeps the device's name and its folders.
