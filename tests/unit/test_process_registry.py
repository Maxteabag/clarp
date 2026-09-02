from lib.process_registry import ProcessRegistry, TurnHandle


class _Proc:
    pid = 42

    def __init__(self, alive=True):
        self.alive = alive
        self.terminated = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def test_registry_tracks_interrupts_and_unregisters():
    errors = []
    registry = ProcessRegistry(log_exception=lambda *a, **kw: errors.append((a, kw)))
    live = TurnHandle(_Proc(), None)
    done = TurnHandle(_Proc(alive=False), None)
    registry.register("agent-1", live)
    registry.register("agent-1", done)

    assert registry.interrupt("agent-1", event="interruptFail") == 1
    assert live.proc.terminated is True
    assert done.proc.terminated is False
    assert errors == []

    registry.unregister("agent-1", live)
    registry.unregister("agent-1", done)
    assert registry.active_handles("agent-1") == []
