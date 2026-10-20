from typing import Any

from canvas_reminder import killswitch

SCHEDULE = {
    "Name": "sched",
    "GroupName": "default",
    "ScheduleExpression": "rate(30 minutes)",
    "FlexibleTimeWindow": {"Mode": "OFF"},
    "Target": {"Arn": "arn:fn", "RoleArn": "arn:role"},
    "State": "ENABLED",
    "Arn": "arn:schedule",  # returned by GetSchedule but not accepted by UpdateSchedule
    "CreationDate": "x",
}


class FakeScheduler:
    def __init__(self) -> None:
        self.schedule = dict(SCHEDULE)
        self.updates: list[dict[str, Any]] = []

    def get_schedule(self, Name: str) -> dict[str, Any]:
        return dict(self.schedule)

    def update_schedule(self, **kwargs: Any) -> None:
        assert "Arn" not in kwargs and "CreationDate" not in kwargs
        assert kwargs["Target"] == SCHEDULE["Target"]  # update replaces the whole schedule
        self.updates.append(kwargs)
        self.schedule["State"] = kwargs["State"]


class FakeLambda:
    def __init__(self) -> None:
        self.concurrency: dict[str, int | None] = {}

    def put_function_concurrency(
        self, FunctionName: str, ReservedConcurrentExecutions: int
    ) -> None:
        self.concurrency[FunctionName] = ReservedConcurrentExecutions

    def delete_function_concurrency(self, FunctionName: str) -> None:
        self.concurrency[FunctionName] = None


ARGS = {"schedule_name": "sched", "functions": ["reminder", "apple"]}


def test_pause_stops_everything_billable_and_is_idempotent() -> None:
    scheduler, lam = FakeScheduler(), FakeLambda()
    done = killswitch.pause(scheduler, lam, **ARGS)
    assert scheduler.schedule["State"] == "DISABLED"
    assert lam.concurrency == {"reminder": 0, "apple": 0}
    assert done == [
        "schedule sched disabled",
        "function reminder blocked",
        "function apple blocked",
    ]

    killswitch.pause(scheduler, lam, **ARGS)  # second run: no errors, no extra update
    assert len(scheduler.updates) == 1


def test_resume_undoes_pause() -> None:
    scheduler, lam = FakeScheduler(), FakeLambda()
    killswitch.pause(scheduler, lam, **ARGS)
    killswitch.resume(scheduler, lam, **ARGS)
    assert scheduler.schedule["State"] == "ENABLED"
    assert lam.concurrency == {"reminder": None, "apple": None}


def test_handler_notifies_then_pauses(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    scheduler, lam = FakeScheduler(), FakeLambda()
    fakes = {"scheduler": scheduler, "lambda": lam}
    monkeypatch.setattr(killswitch.boto3, "client", lambda name: fakes[name])
    calls: list[str] = []
    monkeypatch.setattr(killswitch, "_notify_phone", lambda: calls.append("notified"))
    monkeypatch.setenv("SCHEDULE_NAME", "sched")
    monkeypatch.setenv("FUNCTION_NAMES", "reminder,apple")
    done = killswitch.lambda_handler({"Records": []}, None)
    assert calls == ["notified"] and scheduler.schedule["State"] == "DISABLED" and done


def test_notify_phone_never_raises(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def broken(**_: object) -> None:
        raise RuntimeError("no parameter store")

    monkeypatch.setattr(killswitch, "SecretStore", broken)
    killswitch._notify_phone()  # logs a warning instead of failing the shutdown
