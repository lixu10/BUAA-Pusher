"""Convert upstream rich text to inert readable text, preserving paragraphs."""
import re
from html.parser import HTMLParser


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1
        if not self.ignored and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "pre"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)
        elif not self.ignored and tag in {"p", "div", "li", "tr", "pre"}:
            self.parts.append("\n")
        elif not self.ignored and tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def plain_text(value):
    if not isinstance(value, str) or not value.strip():
        return None
    parser = _Text()
    parser.feed(value)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts)).replace("\xa0", " ")
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)*", "\n\n", text).strip() or None
