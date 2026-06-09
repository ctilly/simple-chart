from tools._line import LineStore
from tools.horizontal_line.models import HorizontalLineRecord, HorizontalLineShape


class HorizontalLineStore(HorizontalLineShape, LineStore[HorizontalLineRecord]):

    extension_name = "horizontal_line"
    store_key = "horizontal_line.lines"
    default_age_off_days = 365.0
