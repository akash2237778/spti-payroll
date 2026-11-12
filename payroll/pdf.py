"""Tiny PDF writer (no extra packages): text, lines and filled boxes with the built-in Helvetica fonts.
Enough for clean tabular reports; pages are A4 portrait or landscape."""
import zlib

A4 = (595.28, 841.89)
A4_LANDSCAPE = (841.89, 595.28)

# Helvetica glyph widths (1/1000 em) for characters 32..126
_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
      556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
      1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
      667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
      333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
      556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]


def _clean(s):
    s = str(s if s is not None else "")
    s = s.replace("–", "-").replace("—", "-").replace("→", "->").replace("·", "-").replace("−", "-")
    return s.encode("latin-1", "replace").decode("latin-1")


def text_width(s, size, bold=False):
    w = sum(_W[ord(ch) - 32] if 32 <= ord(ch) <= 126 else 556 for ch in _clean(s))
    return w * size / 1000.0 * (1.06 if bold else 1.0)


def fit(s, width, size, bold=False):
    """Cut a string with '..' so it fits the width."""
    s = _clean(s)
    if text_width(s, size, bold) <= width:
        return s
    while s and text_width(s + "..", size, bold) > width:
        s = s[:-1]
    return s + ".."


def _esc(s):
    return _clean(s).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _rgb(c):
    if isinstance(c, str):
        c = c.lstrip("#")
        c = tuple(int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return "%.3f %.3f %.3f" % tuple(c)


class PDF:
    def __init__(self, title="", author=""):
        self.pages, self.title, self.author = [], title, author
        self.ops, self.size = None, A4

    def add_page(self, size=A4):
        self.size = size
        self.ops = []
        self.pages.append((size, self.ops))

    @property
    def w(self):
        return self.size[0]

    @property
    def h(self):
        return self.size[1]

    # coordinates: x from the left, y from the TOP of the page (converted here)
    def text(self, x, y, s, size=9, bold=False, color="#1f2a37", align="left", width=None):
        s = _clean(s)
        if width is not None:
            s = fit(s, width, size, bold)
            if align == "right":
                x = x + width - text_width(s, size, bold)
            elif align == "center":
                x = x + (width - text_width(s, size, bold)) / 2
        self.ops.append("BT /%s %.2f Tf %s rg %.2f %.2f Td (%s) Tj ET"
                        % ("F2" if bold else "F1", size, _rgb(color), x, self.h - y, _esc(s)))

    def line(self, x1, y1, x2, y2, color="#c9d1db", width=0.5):
        self.ops.append("%s RG %.2f w %.2f %.2f m %.2f %.2f l S" % (_rgb(color), width, x1, self.h - y1, x2, self.h - y2))

    def rect(self, x, y, w, h, fill=None, stroke=None, width=0.5):
        op = []
        if fill:
            op.append("%s rg" % _rgb(fill))
        if stroke:
            op.append("%s RG %.2f w" % (_rgb(stroke), width))
        op.append("%.2f %.2f %.2f %.2f re %s" % (x, self.h - y - h, w, h, "B" if fill and stroke else ("f" if fill else "S")))
        self.ops.append(" ".join(op))

    def output(self):
        objs = []

        def add(body):
            objs.append(body)
            return len(objs)

        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        pages_id = add(None)
        kids = []
        for size, ops in self.pages:
            data = zlib.compress("\n".join(ops).encode("latin-1"))
            cid = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream")
            kids.append(add(("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Contents %d 0 R "
                             "/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>"
                             % (pages_id, size[0], size[1], cid, f1, f2)).encode()))
        objs[pages_id - 1] = ("<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join("%d 0 R" % k for k in kids), len(kids))).encode()
        info = add(("<< /Title (%s) /Author (%s) /Producer (SPTI Payroll) >>" % (_esc(self.title), _esc(self.author))).encode("latin-1"))
        root = add(("<< /Type /Catalog /Pages %d 0 R >>" % pages_id).encode())
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        for o in offsets:
            out += b"%010d 00000 n \n" % o
        out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, root, info, xref)
        return bytes(out)
