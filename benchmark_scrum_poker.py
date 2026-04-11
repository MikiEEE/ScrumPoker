"""Lightweight local benchmark for SmallOS scrum poker room shapes."""

import time

import scrum_poker_core as core
from scrum_poker_app import ScrumPokerApp


class BenchmarkWriterTask:
    """Tiny stand-in for the SmallOS writer task used during benchmarks."""

    def __init__(self):
        self.done = False
        self.signals = 0

    def acceptSignal(self, sig):
        self.signals += 1


class BenchmarkKernel:
    """Minimal kernel surface needed by the room state helpers."""

    def __init__(self):
        self.now_ms = 1000

    def scheduler_now_ms(self):
        return self.now_ms

    def socket_close(self, sock):
        return None


class BenchmarkTaskRegistry:
    def list(self):
        return []


class BenchmarkRuntime:
    """Minimal runtime surface used by the benchmark harness."""

    def __init__(self):
        self.kernel = BenchmarkKernel()
        self.tasks = BenchmarkTaskRegistry()

    def cancel_task(self, task, recursive=False):
        return 0


def build_room(app_id, base_path, runtime):
    """Create one isolated benchmark room instance."""
    return ScrumPokerApp(
        app_id=app_id,
        base_path=base_path,
        runtime=runtime,
        title="Sprint Poker",
        room_kind="ephemeral",
        join_limit=core.EPHEMERAL_JOIN_LIMIT,
        admin_auth_mode="room",
        room_admin_passphrase="benchmark",
    )


def add_connected_user(room, name):
    """Attach one synthetic connected participant to a room."""
    connection = core._make_connection_record(room.state, ("127.0.0.1", 5000))
    connection["connected"] = True
    connection["name"] = name
    connection["session_task"] = BenchmarkWriterTask()
    connection["socket"] = object()
    connection["writer_task"] = BenchmarkWriterTask()
    return connection


def clear_pending(room):
    """Reset queued outbound work so each timed iteration starts fresh."""
    for connection in room.state["connections"].values():
        connection["outbox"] = []
        if connection.get("writer_task") is not None:
            connection["writer_task"].signals = 0


def build_scenario(room_count, users_per_room):
    """Construct one synthetic benchmark scenario."""
    runtime = BenchmarkRuntime()
    rooms = []
    for index in range(room_count):
        room = build_room(
            app_id="room{}".format(index),
            base_path="/room{}".format(index),
            runtime=runtime,
        )
        room.state["session_open"] = True
        for user_index in range(users_per_room):
            add_connected_user(room, "User {} {}".format(index, user_index))
        rooms.append(room)
    return rooms


def time_broadcast(room, iterations=200):
    """Measure average broadcast time for one room while draining between runs."""
    participants = list(room.state["connections"].values())
    if not participants:
        return 0.0

    timings = []
    for index in range(iterations):
        user = participants[index % len(participants)]
        user["vote"] = core.ALLOWED_VOTES[index % len(core.ALLOWED_VOTES)]
        clear_pending(room)
        started = time.perf_counter()
        room.broadcast_state()
        timings.append((time.perf_counter() - started) * 1000.0)

    return sum(timings) / len(timings)


def burst_pressure(room, bursts=40):
    """Exercise repeated undrained broadcasts and report backlog size."""
    participants = list(room.state["connections"].values())
    if not participants:
        return {"pending_messages": 0, "writer_signals": 0}

    clear_pending(room)
    for index in range(bursts):
        user = participants[index % len(participants)]
        user["vote"] = core.ALLOWED_VOTES[index % len(core.ALLOWED_VOTES)]
        room.broadcast_state()

    return {
        "pending_messages": sum(len(connection["outbox"]) for connection in room.state["connections"].values()),
        "writer_signals": sum(connection["writer_task"].signals for connection in room.state["connections"].values()),
    }


def run_scenario(label, room_count, users_per_room):
    """Build, time, and print one scenario summary."""
    rooms = build_scenario(room_count, users_per_room)
    target_room = rooms[0]
    mean_broadcast_ms = time_broadcast(target_room)
    pressure = burst_pressure(target_room)
    print(
        "{label}: rooms={rooms} users_per_room={users} target_room_users={target_users} "
        "avg_broadcast_ms={avg:.4f} pending_messages={pending} writer_signals={signals}".format(
            label=label,
            rooms=room_count,
            users=users_per_room,
            target_users=len(target_room.state["connections"]),
            avg=mean_broadcast_ms,
            pending=pressure["pending_messages"],
            signals=pressure["writer_signals"],
        )
    )


def main():
    """Run the baseline room-shape scenarios."""
    print("Scrum poker local benchmark")
    run_scenario("1 room x 8 users", room_count=1, users_per_room=8)
    run_scenario("19 rooms x 8 users", room_count=19, users_per_room=8)
    run_scenario("1 room x 20 users", room_count=1, users_per_room=20)


if __name__ == "__main__":
    main()
