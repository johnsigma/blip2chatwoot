"""Markdown (as the Chatwoot Captain writes it) to WhatsApp formatting.

Chatwoot's native WhatsApp channel converts this itself; messages forwarded through BLiP go out as
plain text, so `**bold**` would reach the customer with the asterisks.
"""

import re

_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_BOLD_UNDERSCORE = re.compile(r"__(?=\S)(.+?)(?<=\S)__")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_STAR_BULLET = re.compile(r"^([ \t]*)\*[ \t]+", re.MULTILINE)


def markdown_to_whatsapp(text: str) -> str:
    def link(match: re.Match[str]) -> str:
        label, url = match.group(1).strip(), match.group(2)
        return url if label == url else f"{label}: {url}"

    # Bullets first: "* item" would otherwise look like the start of a bold run.
    text = _STAR_BULLET.sub(r"\1- ", text)
    text = _LINK.sub(link, text)
    text = _HEADING.sub(r"*\1*", text)
    text = _BOLD.sub(r"*\1*", text)
    text = _BOLD_UNDERSCORE.sub(r"*\1*", text)
    return _STRIKE.sub(r"~\1~", text)
