import threading
import time

from nous_sync.watcher import FolderWatcher


def test_debounced_events_report_once_when_stable(tmp_path):
    seen, moves = [], []
    lock = threading.Lock()
    w = FolderWatcher(str(tmp_path), 0.4, lambda p: (lock.acquire(), seen.append(p), lock.release()),
                      lambda s, d: moves.append((s, d)))
    w.start()
    try:
        f = tmp_path / "render.mov"
        with open(f, "wb") as fh:  # a "slow copy": keeps growing for a while
            for _ in range(6):
                fh.write(b"x" * 1000)
                fh.flush()
                time.sleep(0.15)
        (tmp_path / "a.txt").write_text("1")
        time.sleep(0.2)
        (tmp_path / "a.txt").rename(tmp_path / "b.txt")
        deadline = time.time() + 6
        while time.time() < deadline and (str(f) not in seen or not moves):
            time.sleep(0.1)
    finally:
        w.stop()
    assert seen.count(str(f)) == 1
    assert (str(tmp_path / "a.txt"), str(tmp_path / "b.txt")) in moves
