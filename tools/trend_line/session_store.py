from tools._polyline import PolylineStore

_DEFAULT_AGE_OFF_DAYS = 365.0


class TrendLineStore(PolylineStore):

    extension_name = "trend_line"
    store_key = "trend_line.drawings"
    key_prefix = "trend_line"
    default_age_off_days = _DEFAULT_AGE_OFF_DAYS
