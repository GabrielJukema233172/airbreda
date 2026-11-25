"""Day 2 data-quality handlers (required by the lab) and their supporting logic."""

import logging

import ingest_air
import ingest_traffic
from common.observability import ServiceState
from test_ingest_traffic import CONFIG, MEASURED, SITE


def _air_records(values, start_hour=8):
    return [
        {"formula": "NO2", "timestamp_measured": f"2026-11-23T{start_hour + i:02d}:00:00+00:00", "value": v}
        for i, v in enumerate(values)
    ]


def _air_ingester(records, writer, publisher=None):
    state = ServiceState("Luchtmeetnet", ingest_air.log, poll_interval=3600)
    return ingest_air.AirIngester(state, writer=writer, publisher=publisher, fetch=lambda: records), state


# --------------------------------------------------------------------------- #
# Required test 1: stale/null Luchtmeetnet reading is written with is_flagged = TRUE
# --------------------------------------------------------------------------- #
def test_null_luchtmeetnet_reading_is_written_flagged_not_dropped(writer):
    ingester, state = _air_ingester(_air_records([20.1, None, 22.3]), writer)
    assert ingester.run_cycle()

    assert len(writer.rows) == 3                       # nothing dropped
    null_rows = [r for r in writer.rows if r[3] is None]
    assert len(null_rows) == 1
    assert null_rows[0][4] is True                     # is_flagged
    assert state.bad_data_count == 1                   # luchtmeetnet_bad_data_count


def test_stale_luchtmeetnet_reading_is_written_flagged_not_dropped(writer):
    # 25.0 repeated for 4 consecutive hours: the 3rd and 4th are stale
    ingester, state = _air_ingester(_air_records([18.0, 25.0, 25.0, 25.0, 25.0, 19.5]), writer)
    assert ingester.run_cycle()

    assert len(writer.rows) == 6
    flags = [r[4] for r in sorted(writer.rows, key=lambda r: r[1])]
    assert flags == [False, False, False, True, True, False]
    assert state.bad_data_count == 2


# --------------------------------------------------------------------------- #
# Required test 2: NDW speed = -1 is not written and increments ndw_bad_data_count
# --------------------------------------------------------------------------- #
def test_ndw_speed_minus_one_is_not_written_and_counted(writer, tmp_path):
    state = ServiceState("NDW", ingest_traffic.log, poll_interval=3600)
    feeds = {ingest_traffic.CONFIG_URL: CONFIG, ingest_traffic.MEASURED_URL: MEASURED}
    ingester = ingest_traffic.TrafficIngester(state, tmp_path, writer=writer,
                                              download=feeds.__getitem__, sites={"hrl": SITE})
    assert ingester.run_cycle()

    # Fixture: lane speeds are 106.0 and -1 -> speed row uses only the valid lane
    assert all(r[3] is not None and r[3] >= 0 for r in writer.rows)  # no sentinel reaches the table
    speed_rows = [r for r in writer.rows if r[2] == "speed"]
    assert [r[3] for r in speed_rows] == [106.0]
    assert state.bad_data_count == 1                   # ndw_bad_data_count


def test_ndw_site_with_all_speeds_invalid_writes_no_speed_row(writer, tmp_path):
    measured = MEASURED.replace(b"<com:speed>106.0</com:speed>", b"<com:speed>-1</com:speed>")
    state = ServiceState("NDW", ingest_traffic.log, poll_interval=3600)
    feeds = {ingest_traffic.CONFIG_URL: CONFIG, ingest_traffic.MEASURED_URL: measured}
    ingester = ingest_traffic.TrafficIngester(state, tmp_path, writer=writer,
                                              download=feeds.__getitem__, sites={"hrl": SITE})
    assert ingester.run_cycle()
    assert [r[2] for r in writer.rows] == ["flow"]     # speed row skipped entirely
    assert state.bad_data_count == 2


def test_ndw_raw_csv_still_keeps_the_bad_value(tmp_path, writer):
    state = ServiceState("NDW", ingest_traffic.log, poll_interval=3600)
    feeds = {ingest_traffic.CONFIG_URL: CONFIG, ingest_traffic.MEASURED_URL: MEASURED}
    ingest_traffic.TrafficIngester(state, tmp_path, writer=writer, download=feeds.__getitem__,
                                   sites={"hrl": SITE}).run_cycle()
    csv_text = (tmp_path / "ndw/2026-11-23/13-hrl.csv").read_text()
    assert ",-1,,False" in csv_text                     # audit trail keeps the raw -1


# --------------------------------------------------------------------------- #
# Supporting logic
# --------------------------------------------------------------------------- #
def test_refetching_the_same_window_does_not_recount_bad_data(writer):
    records = _air_records([20.1, None, 22.3])
    ingester, state = _air_ingester(records, writer)
    ingester.run_cycle()
    ingester.run_cycle()   # next hour: same 50-hour window comes back
    assert state.bad_data_count == 1


def test_gap_in_hours_breaks_a_stale_run():
    df = ingest_air.to_dataframe([
        {"formula": "NO2", "timestamp_measured": "2026-11-23T08:00:00+00:00", "value": 25.0},
        {"formula": "NO2", "timestamp_measured": "2026-11-23T09:00:00+00:00", "value": 25.0},
        {"formula": "NO2", "timestamp_measured": "2026-11-23T11:00:00+00:00", "value": 25.0},  # 10:00 missing
    ])
    assert not ingest_air.flag_stale_or_null(df)["is_flagged"].any()


def test_threshold_logs_one_error_when_more_than_10_in_an_hour(caplog):
    from conftest import FakeClock

    clock = FakeClock()
    state = ServiceState("NDW", logging.getLogger("t"), poll_interval=3600, clock=clock)
    with caplog.at_level(logging.ERROR):
        state.record_bad_data(10)
        assert not caplog.records                       # 10 is not "more than 10"
        state.record_bad_data(1)
        state.record_bad_data(5)                        # still the same incident
    errors = [r.msg for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == [{"event": "BAD_DATA_THRESHOLD_EXCEEDED", "source": "NDW", "count": 11, "window_minutes": 60}]


def test_threshold_window_slides_and_rearms(caplog):
    from conftest import FakeClock

    clock = FakeClock()
    state = ServiceState("NDW", logging.getLogger("t"), poll_interval=3600, clock=clock)
    with caplog.at_level(logging.ERROR):
        state.record_bad_data(11)
        clock.t += 3601                                 # an hour later the window is empty
        assert state.bad_data_last_hour() == 0
        state.record_bad_data(11)
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 2
    assert state.bad_data_count == 22                   # lifetime total keeps counting


def test_health_endpoint_body_and_status():
    from conftest import FakeClock

    clock = FakeClock()
    state = ServiceState("NDW", logging.getLogger("t"), poll_interval=3600, clock=clock)
    assert state.health()[1]["status"] == "starting"
    state.record_success()
    state.record_bad_data(3)
    code, body = state.health()
    assert code == 200
    assert body["source"] == "NDW" and body["bad_data_count"] == 3 and body["status"] == "ok"
    assert body["last_successful_fetch"].endswith("+00:00")
    clock.t += 3 * 3600                                 # no success for 3 hours
    assert state.health() == (503, {**body, "status": "stale", "bad_data_count_last_hour": 0})


def test_health_server_serves_json():
    import json
    import urllib.request

    from common.observability import start_health_server

    state = ServiceState("Luchtmeetnet", logging.getLogger("t"), poll_interval=3600)
    server = start_health_server(state, port=0)
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            body = json.load(resp)
        assert resp.status == 200 and body["source"] == "Luchtmeetnet"
    finally:
        server.shutdown()


def test_air_publishes_only_new_or_changed_readings(writer, publisher):
    records = _air_records([20.1, None, 22.3])
    ingester, _ = _air_ingester(records, writer, publisher)
    ingester.run_cycle()
    assert len(publisher.messages) == 3
    assert publisher.messages[0] == {"source": "Luchtmeetnet", "station_id": "NL10240",
                                     "timestamp": "2026-11-23T08:00:00Z", "component": "NO2",
                                     "value": 20.1, "is_flagged": False}
    ingester.run_cycle()                                # same data again
    assert len(publisher.messages) == 3
    records[1]["value"] = 21.0                          # late value fills the null
    ingester.run_cycle()
    assert len(publisher.messages) == 4 and publisher.messages[-1]["value"] == 21.0


def test_redis_down_does_not_stop_db_write_and_retries_next_cycle(writer):
    from conftest import FakePublisher

    broken = FakePublisher(fail=True)
    ingester, _ = _air_ingester(_air_records([20.1, 22.3]), writer, broken)
    assert ingester.run_cycle()                         # cycle still succeeds
    assert len(writer.rows) == 2                        # database write happened
    broken.fail = False
    ingester.run_cycle()
    assert len(broken.messages) == 2                    # published once Redis is back


def test_json_log_line_format(capsys):
    import json

    from common.observability import log_event, setup_logging

    setup_logging("air-ingest")
    log_event(logging.getLogger("x"), logging.WARNING, "DATA_QUALITY_ERROR", source="NDW", value=-1)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["level"] == "WARNING" and line["service"] == "air-ingest"
    assert line["event"] == "DATA_QUALITY_ERROR" and line["value"] == -1
