from django.conf import settings


DEFAULT_CHINESE_MIDS = ("412", "413", "414")


def chinese_mids():
    configured = getattr(settings, "ILLEGAL_BERTHING_DETECTION", {})
    values = (
        configured.get("chinese_mids", DEFAULT_CHINESE_MIDS)
        if isinstance(configured, dict)
        else DEFAULT_CHINESE_MIDS
    )
    mids = {
        str(value).strip()
        for value in values
        if len(str(value).strip()) == 3
        and str(value).strip().isdigit()
    }
    return mids or set(DEFAULT_CHINESE_MIDS)


def classify_vessel_mmsi(value):
    """
    Classify a ship-station MMSI.

    Ordinary ship-station MMSIs are nine digits and begin with a MID whose
    first digit is 2-7. Group, coast, aircraft, AtoN and auxiliary-device
    identities are intentionally excluded.
    """
    mmsi = str(value or "").strip()
    if len(mmsi) != 9 or not mmsi.isdigit() or mmsi[0] not in "234567":
        return None
    return "chinese" if mmsi[:3] in chinese_mids() else "foreign"
