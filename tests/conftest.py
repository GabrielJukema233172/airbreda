import pytest


class FakeWriter:
    """Stands in for the database: records the rows it was asked to write."""

    def __init__(self):
        self.rows = []

    def __call__(self, rows):
        self.rows.extend(rows)
        return len(rows), 0


class FakePublisher:
    """Stands in for Redis."""

    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    def publish(self, messages):
        if self.fail:
            raise ConnectionError("redis down")
        self.messages.extend(messages)
        return len(messages)


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def writer():
    return FakeWriter()


@pytest.fixture
def publisher():
    return FakePublisher()
