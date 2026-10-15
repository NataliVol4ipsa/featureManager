"""Building and copying of user-facing messages (link lists, release notes).

Two Settings control the output:

* **Teams-friendly links** - when off, a link renders as ``"text - url"``
  (plain text); when on, the message is HTML with the link embedded in the
  text and is placed on the clipboard as HTML (plus a plain-text fallback), so
  pasting into Teams/Outlook shows clickable names.
* **Numbered lists** - when on, a message's list part is numbered once it has
  at least *threshold* items. Header/footer extras are never numbered.
"""

import html
import re
import sys
import time

import theme


class HtmlMessage(str):
    """A message whose content is an HTML fragment (Teams-friendly links)."""


def format_link(text, url, formatted=None):
    """Return *text* + *url* as one string, per the "Teams-friendly links" setting.

    *formatted* overrides the setting (used to read it once per message).
    """
    if formatted is None:
        formatted = theme.load_copy_links_formatted()
    if formatted:
        return (f'<a href="{html.escape(url or "", quote=True)}">'
                f'{html.escape(text or "")}</a>')
    return f"{text} - {url}"


def build_message(items, header=(), footer=()):
    """Return a multi-line message honouring the link-format and numbering settings.

    Each entry of *items*, *header* and *footer* is either a plain string or a
    ``(text, url)`` tuple rendered via :func:`format_link`. Only *items* are
    numbered (when enabled and ``len(items) >= threshold``). Returns an
    :class:`HtmlMessage` in Teams-friendly mode, a plain ``str`` otherwise, and
    ``""`` when there is nothing to copy.
    """
    formatted = theme.load_copy_links_formatted()

    def _render(entry):
        if isinstance(entry, tuple):
            return format_link(entry[0], entry[1], formatted=formatted)
        return html.escape(entry) if formatted else entry

    body = [_render(entry) for entry in items]
    enabled, threshold = theme.load_list_numbering()
    if enabled and body and len(body) >= threshold:
        body = [f"{index}. {line}" for index, line in enumerate(body, start=1)]
    lines = ([_render(entry) for entry in header] + body
             + [_render(entry) for entry in footer])
    if not lines:
        return ""
    if formatted:
        return HtmlMessage("<br>".join(lines))
    return "\n".join(lines)


_ANCHOR_RE = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.DOTALL)


def plain_text(message):
    """Return *message* as plain text (HTML links become ``"text - url"``)."""
    if not isinstance(message, HtmlMessage):
        return str(message)
    text = _ANCHOR_RE.sub(
        lambda m: f"{html.unescape(m.group(2))} - {html.unescape(m.group(1))}",
        str(message),
    )
    text = text.replace("<br>", "\n")
    return html.unescape(text)


def copy_message(widget, message):
    """Put *message* on the clipboard; returns False when it is empty.

    An :class:`HtmlMessage` is copied as HTML (with a plain-text fallback) on
    Windows; anything else, or a failed HTML copy, goes through Tk as text.
    """
    if not message:
        return False
    if isinstance(message, HtmlMessage) and _set_windows_html_clipboard(
        str(message), plain_text(message)
    ):
        return True
    widget.clipboard_clear()
    widget.clipboard_append(plain_text(message))
    return True


def _cf_html(fragment):
    """Wrap an HTML *fragment* in the Windows "HTML Format" envelope (bytes)."""
    header_tpl = ("Version:0.9\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\n"
                  "StartFragment:{:010d}\r\nEndFragment:{:010d}\r\n")
    start = b"<html><body><!--StartFragment-->"
    end = b"<!--EndFragment--></body></html>"
    body = fragment.encode("utf-8")
    start_html = len(header_tpl.format(0, 0, 0, 0))
    start_fragment = start_html + len(start)
    end_fragment = start_fragment + len(body)
    end_html = end_fragment + len(end)
    header = header_tpl.format(start_html, end_html, start_fragment, end_fragment)
    return header.encode("ascii") + start + body + end


def _set_windows_html_clipboard(fragment, plain):
    """Place HTML + Unicode text on the Windows clipboard; False on failure."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    gmem_moveable = 0x0002
    cf_unicodetext = 13
    cf_html = user32.RegisterClipboardFormatW("HTML Format")
    if not cf_html:
        return False

    def _put(fmt, data):
        handle = kernel32.GlobalAlloc(gmem_moveable, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(fmt, handle):
            kernel32.GlobalFree(handle)  # only freed when the system did not take it
            return False
        return True

    # Another app may hold the clipboard briefly; retry a few times.
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        return False
    try:
        user32.EmptyClipboard()
        ok = _put(cf_html, _cf_html(fragment) + b"\0")
        ok = _put(cf_unicodetext, plain.encode("utf-16-le") + b"\0\0") and ok
        return ok
    finally:
        user32.CloseClipboard()
