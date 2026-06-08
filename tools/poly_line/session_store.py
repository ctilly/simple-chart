from tools._polyline import PolylineStore

_DEFAULT_AGE_OFF_DAYS = 365.0


class PolyLineStore(PolylineStore):

    extension_name = "poly_line"
    store_key = "poly_line.drawings"
    key_prefix = "poly_line"
    default_age_off_days = _DEFAULT_AGE_OFF_DAYS
