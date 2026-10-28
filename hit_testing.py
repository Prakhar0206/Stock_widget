"""hit_testing.py - platform-neutral decisions for the Mini Tracker's selective click-through.

Kept free of Win32 imports so it can be unit-tested anywhere. All rectangles are
(left, top, right, bottom) in *screen* coordinates, right/bottom exclusive.
"""
HTCLIENT, HTTRANSPARENT = 1, -1


def point_in_rect(x, y, rect):
    left, top, right, bottom = rect
    return left <= x < right and top <= y < bottom


def point_in_any(x, y, rects):
    return any(point_in_rect(x, y, r) for r in rects)


def should_be_solid(x, y, solid_rects, button_down=False, currently_solid=False):
    """True when the window must receive the mouse (cursor over a solid rect such as the header).

    While the left button is held and the window is already solid we stay solid, so a header drag
    is not cut short when the cursor briefly outruns the window.
    """
    if point_in_any(x, y, solid_rects):
        return True
    return bool(button_down and currently_solid)


def hit_code(x, y, solid_rects):
    """WM_NCHITTEST answer: HTCLIENT over the solid rects, HTTRANSPARENT elsewhere."""
    return HTCLIENT if point_in_any(x, y, solid_rects) else HTTRANSPARENT
