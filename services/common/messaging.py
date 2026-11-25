"""Publishing readings to the shared Redis list `readings`."""

from __future__ import annotations

import json

QUEUE_NAME = "readings"
# Nothing consumes the queue yet (Day 2 lab), so cap it: Redis keeps everything
# in memory, and an unbounded list would eventually fill the VM's RAM.
MAX_QUEUE_LENGTH = 10_000


class RedisPublisher:
    def __init__(self, host: str, port: int = 6379):
        import redis  # lazy: unit tests use a fake publisher

        self.client = redis.Redis(host=host, port=port, socket_connect_timeout=3, socket_timeout=3)

    def publish(self, messages: list[dict]) -> int:
        """RPUSH each message as JSON, then trim to the newest MAX_QUEUE_LENGTH.
        Raises redis.RedisError if the broker is unreachable."""
        if not messages:
            return 0
        pipe = self.client.pipeline()
        for m in messages:
            pipe.rpush(QUEUE_NAME, json.dumps(m))
        pipe.ltrim(QUEUE_NAME, -MAX_QUEUE_LENGTH, -1)
        pipe.execute()
        return len(messages)
