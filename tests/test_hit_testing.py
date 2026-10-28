"""Pure-logic tests for the selective click-through decisions (run on any OS, no display)."""
from hit_testing import HTCLIENT, HTTRANSPARENT, hit_code, point_in_rect, should_be_solid

HEADER = (100, 50, 350, 80)          # screen rect of the header
RECTS = [HEADER]


def test_point_in_rect_edges_are_half_open():
    assert point_in_rect(100, 50, HEADER)
    assert point_in_rect(349, 79, HEADER)
    assert not point_in_rect(350, 60, HEADER) and not point_in_rect(200, 80, HEADER)


def test_header_is_solid_rows_are_click_through():
    assert should_be_solid(200, 60, RECTS) is True         # pin / gear / close area
    assert should_be_solid(200, 120, RECTS) is False       # over a stock row
    assert should_be_solid(10, 10, RECTS) is False         # outside the window


def test_drag_keeps_window_solid_until_release():
    assert should_be_solid(500, 500, RECTS, button_down=True, currently_solid=True) is True
    assert should_be_solid(500, 500, RECTS, button_down=False, currently_solid=True) is False
    assert should_be_solid(500, 500, RECTS, button_down=True, currently_solid=False) is False


def test_nchittest_codes():
    assert hit_code(200, 60, RECTS) == HTCLIENT
    assert hit_code(200, 120, RECTS) == HTTRANSPARENT
