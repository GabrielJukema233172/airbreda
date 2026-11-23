"""Traffic ingestion tests, using XML trimmed from the real NDW feeds
(same namespaces and nesting as opendata.ndw.nu, November 2026)."""

from ingest_traffic import clean_value, object_key, parse_site, summarise

NS = ('xmlns:mc="http://datex2.eu/schema/3/messageContainer" '
      'xmlns:roa="http://datex2.eu/schema/3/roadTrafficData" '
      'xmlns:com="http://datex2.eu/schema/3/common" '
      'xmlns:loc="http://datex2.eu/schema/3/locationReferencing" '
      'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')

SITE = "RWS01_MONIBAS_0271hrl0063ra"


def _characteristic(index, value_type, lane):
    return (f'<roa:measurementSpecificCharacteristics index="{index}"><roa:measurementSpecificCharacteristics>'
            f'<roa:period>60.0</roa:period>'
            f'<roa:specificMeasurementValueType>{value_type}</roa:specificMeasurementValueType>'
            f'<roa:specificVehicleCharacteristics><com:vehicleType>anyVehicle</com:vehicleType>'
            f'</roa:specificVehicleCharacteristics><roa:specificLane><loc:laneNumber>{lane}</loc:laneNumber>'
            f'</roa:specificLane></roa:measurementSpecificCharacteristics></roa:measurementSpecificCharacteristics>')


CONFIG = (f'<?xml version="1.0" encoding="UTF-8"?><mc:messageContainer {NS}><mc:payload>'
          f'<roa:measurementSiteTable id="T">'
          f'<roa:measurementSite id="OTHER_SITE">{_characteristic(1, "trafficFlow", 1)}</roa:measurementSite>'
          f'<roa:measurementSite id="{SITE}">'
          f'{_characteristic(1, "trafficFlow", 1)}{_characteristic(2, "trafficSpeed", 1)}'
          f'{_characteristic(3, "trafficFlow", 2)}{_characteristic(4, "trafficSpeed", 2)}'
          f'</roa:measurementSite></roa:measurementSiteTable></mc:payload></mc:messageContainer>').encode()


def _flow(index, v):
    return (f'<roa:physicalQuantity index="{index}"><roa:physicalQuantity xsi:type="roa:SinglePhysicalQuantity">'
            f'<roa:basicData xsi:type="roa:TrafficFlow"><roa:vehicleFlow accuracy="0.0">'
            f'<com:vehicleFlowRate>{v}</com:vehicleFlowRate></roa:vehicleFlow></roa:basicData>'
            f'</roa:physicalQuantity></roa:physicalQuantity>')


def _speed(index, v):
    return (f'<roa:physicalQuantity index="{index}"><roa:physicalQuantity xsi:type="roa:SinglePhysicalQuantity">'
            f'<roa:basicData xsi:type="roa:TrafficSpeed"><roa:averageVehicleSpeed accuracy="0.0">'
            f'<com:speed>{v}</com:speed></roa:averageVehicleSpeed></roa:basicData>'
            f'</roa:physicalQuantity></roa:physicalQuantity>')


def _site_measurements(site_id, body):
    return (f'<roa:siteMeasurements><roa:measurementSiteReference targetClass="roa:MeasurementSite" '
            f'id="{site_id}" version="1750"/>{body}<roa:measurementTimeDefault>'
            f'<roa:timeValue>2026-11-23T13:11:00Z</roa:timeValue></roa:measurementTimeDefault></roa:siteMeasurements>')


MEASURED = (f'<?xml version="1.0" encoding="UTF-8"?><mc:messageContainer {NS}><mc:payload>'
            + _site_measurements("OTHER_SITE", _flow(1, 999))
            + _site_measurements(SITE, _flow(1, 420) + _speed(2, 106.0) + _flow(3, 780) + _speed(4, -1))
            + '</mc:payload></mc:messageContainer>').encode()


def test_parse_site_extracts_only_the_requested_site():
    rows = parse_site(CONFIG, MEASURED, "hrl", SITE)
    assert len(rows) == 4
    assert {r["site_id"] for r in rows} == {SITE}
    assert all(r["measurement_time"] == "2026-11-23T13:11:00Z" for r in rows)


def test_speed_minus_one_is_kept_as_invalid_row():
    rows = parse_site(CONFIG, MEASURED, "hrl", SITE)
    bad = [r for r in rows if r["index"] == 4][0]
    assert bad["raw_value"] == "-1"
    assert bad["value"] is None
    assert bad["is_valid"] is False


def test_summary_ignores_invalid_values():
    s = summarise(parse_site(CONFIG, MEASURED, "hrl", SITE))
    assert s["total_flow"] == 1200       # 420 + 780
    assert s["avg_speed"] == 106.0       # the -1 lane is not averaged in
    assert s["invalid_values"] == 1


def test_object_key_uses_measurement_time_not_today():
    assert object_key("hrl", "2026-11-23T13:11:00Z") == "ndw/2026-11-23/13-hrl.csv"


def test_clean_value():
    assert clean_value("420") == 420.0
    assert clean_value("-1") is None
    assert clean_value("") is None
    assert clean_value(None) is None
