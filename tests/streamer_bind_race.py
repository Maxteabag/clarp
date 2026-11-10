"""Force transcript writes on either side of real inotify registration."""
import json
import threading


def force_bind_write(monkeypatch, streamer, agent_id, path, entry, when):
    startup = threading.Event()
    written = threading.Event()
    event_tick = threading.Event()
    original_tick_all = streamer.pool.tick_all
    original_watch = streamer.dispatcher.watch

    def startup_tick():
        result = original_tick_all()
        startup.set()
        return result

    def watch(selected, selected_path):
        assert startup.wait(5), "dispatcher startup tick finishes before the boundary write"
        watcher = streamer.pool.watcher_for(selected)
        original_tick = watcher.tick

        def observe_tick():
            result = original_tick()
            if threading.current_thread() is streamer.dispatcher._thread:
                event_tick.set()
            return result

        monkeypatch.setattr(watcher, "tick", observe_tick)
        assert selected == agent_id and selected_path == path
        if when == "before_watch":
            append()
        original_watch(selected, selected_path)
        if when == "after_watch":
            append()
        written.set()

    def append():
        with path.open("a") as stream:
            stream.write(json.dumps(entry) + "\n")

    monkeypatch.setattr(streamer.pool, "tick_all", startup_tick)
    monkeypatch.setattr(streamer.dispatcher, "watch", watch)
    return written, event_tick
