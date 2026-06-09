from tools._line import LineStore
from tools.vertical_line.models import VerticalLineRecord, VerticalLineShape


class VerticalLineStore(VerticalLineShape, LineStore[VerticalLineRecord]):

    extension_name = "vertical_line"
    store_key = "vertical_line.lines"
    default_age_off_days = 60.0
