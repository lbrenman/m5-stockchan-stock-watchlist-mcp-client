# =============================================================================
# Stock Watchlist - two-screen MCP client for the M5Stack StackChan
# (UiFlow2 / MicroPython, m5ui + LVGL 9)
#
# Tested target: M5Stack StackChan (CoreS3-based) running UIFlow2.0 for
# StackChan v2.5.3. Uses no StackChan-body hardware, so it should also run on
# a plain CoreS3.
#
#   Screen 1: Watchlist - quotes for every symbol, refreshed every 60 s
#                         (tap the On/Off toggle to pause auto refresh)
#   Screen 2: Pick      - tap preset symbol buttons (QQQ + blue chips) to
#                         add / remove them; highlighted = in the watchlist
#
#   Swipe LEFT / RIGHT to switch screens (wraps around).
#
# Talks to an MCP server over "Streamable HTTP": JSON-RPC 2.0 messages sent
# as HTTP POSTs. Tool names are discovered with tools/list at startup.
# =============================================================================

import os, sys, io
import M5
from M5 import *
import m5ui
import lvgl as lv
import time
import json

# =============================================================================
# CONFIG
# =============================================================================
# --- MCP server ---
# EDIT THESE before running: the URL and API key of your FusionStockWatchListMCP
# server (see https://github.com/lbrenman/Amplify-Fusion-Stock-Watch-List-MCP).
# Don't commit your real key to a public repo.
MCP_URL = "https://YOUR-FUSION-HOST:4443/FusionStockWatchListMCP"
MCP_API_KEY_HEADER = "x-api-key"
MCP_API_KEY = "YOUR-API-KEY"
MCP_PROTOCOL = "2025-03-26"

# HTTP transport: "socket" = this file's own HTTP/1.1 client (default),
# "requests" = the firmware's requests2/urequests library.
HTTP_CLIENT = "socket"
HTTP_TIMEOUT_S = 15
DEBUG = True                # print every MCP request/response to the console

# Tool used for each role. These are the FusionStockWatchListMCP tool names.
# Set a role to None to have it discovered from tools/list instead (the
# console prints the server's tools and the mapping used).
TOOL_NAMES = {
    "quotes": "GetStockWatchList",                # quotes for every symbol
    "list": "GetStockWatchListSymbols",           # just the symbols
    "add": "AddSymbolToStockWatchList",
    "remove": "RemoveSymbolFromStockWatchList",
}

# --- Watchlist screen ---
AUTO_REFRESH = True         # auto refresh at startup (the On/Off toggle changes it)
REFRESH_MS = 60 * 1000      # quote refresh while the Watchlist screen is visible
RETRY_MS = 15 * 1000        # retry delay after a failed request
PAGE_MS = 5000              # page rotation when there are more rows than fit
ROWS_PER_PAGE = 7

# --- Pick screen: one tab per group, up to 25 symbols (5 x 5) per tab ---
# QQQ: a curated set of large Invesco QQQ (Nasdaq-100) holdings, mid-2026.
# Blue chip: large NYSE-listed companies that aren't in QQQ.
# Edit freely; symbols must be ones your quote service knows.
SYMBOL_GROUPS = (
    ("QQQ 1", ("NVDA", "AAPL", "MSFT", "GOOGL", "AMZN",
               "META", "AVGO", "TSLA", "MU", "AMD",
               "WMT", "INTC", "NFLX", "COST", "PLTR",
               "CSCO", "ASML", "LRCX", "AMAT", "TMUS",
               "PEP", "QCOM", "TXN", "ADI", "KLAC")),
    ("QQQ 2", ("GOOG", "ISRG", "LIN", "INTU", "AMGN",
               "ARM", "GILD", "SHOP", "APP", "BKNG",
               "PANW", "CRWD", "HON", "ADBE", "ADP",
               "SBUX", "MELI", "CMCSA", "VRTX", "REGN",
               "MRVL", "CDNS", "SNPS", "ABNB", "PYPL")),
    ("Blue Chip", ("JPM", "V", "MA", "BAC", "GS",
                   "JNJ", "UNH", "LLY", "MRK", "ABBV",
                   "PG", "KO", "HD", "MCD", "NKE",
                   "DIS", "IBM", "ORCL", "CRM", "VZ",
                   "XOM", "CVX", "CAT", "BA", "AXP")),
)
OTHER_TAB = "Other"         # symbols in your watchlist that aren't in a group

# --- Navigation ---
SWIPE_ANIM_MS = 250

SCREEN_W = 320
SCREEN_H = 240

# --- Colors ---
BG = 0x0A0F1A
ACCENT = 0x0DC9F4
TEXT_MAIN = 0xFFFFFF
TEXT_SOFT = 0xB3C7E6
TEXT_DIM = 0x5C6B80
UP_COLOR = 0x00E676
DOWN_COLOR = 0xFF5252
OK_COLOR = 0x69F0AE
WARN_COLOR = 0xFFD740
ERR_COLOR = 0xFF8A80
SYM_OFF = 0x263040          # symbol button: not in the watchlist
SYM_ON = 0x2E7D32           # symbol button: in the watchlist
SYM_PENDING = 0xB26A00      # symbol button: add/remove in progress
TAB_OFF = 0x1A2438
TAB_ON = 0x1E88E5
TOGGLE_ON = 0x2E7D32        # refresh toggle: auto refresh on
TOGGLE_OFF = 0x6D4C41       # refresh toggle: paused


# =============================================================================
# SMALL LVGL HELPERS (work around minor v8/v9 binding differences)
# =============================================================================
CIRCLE = getattr(lv, "RADIUS_CIRCLE", 0x7FFF)


def flag_off(obj, flag):
    fn = getattr(obj, "remove_flag", None) or getattr(obj, "clear_flag", None)
    if fn:
        try:
            fn(flag)
        except Exception:
            pass


def flag_on(obj, flag):
    try:
        obj.add_flag(flag)
    except Exception:
        pass


def event_code(e):
    try:
        return e.code
    except AttributeError:
        return e.get_code()


def active_indev():
    fn = getattr(lv, "indev_active", None) or getattr(lv, "indev_get_act", None)
    return fn() if fn else None


def refresh_now():
    """Force LVGL to draw right away (used before a blocking network call)."""
    try:
        lv.refr_now(None)
    except Exception:
        pass


def shape(parent, x, y, w, h, color, radius=0, opa=255):
    """A plain filled rectangle. Not clickable, so swipes pass through."""
    o = lv.obj(parent)
    o.set_pos(int(x), int(y))
    o.set_size(int(w), int(h))
    o.set_style_radius(radius, 0)
    o.set_style_bg_color(lv.color_hex(color), 0)
    o.set_style_bg_opa(opa, 0)
    o.set_style_border_width(0, 0)
    o.set_style_pad_all(0, 0)
    o.set_style_shadow_width(0, 0)
    flag_off(o, lv.obj.FLAG.SCROLLABLE)
    flag_off(o, lv.obj.FLAG.CLICKABLE)
    return o


def container(parent, x, y, w, h):
    """Invisible, non-clickable box used to group widgets."""
    c = lv.obj(parent)
    c.set_pos(x, y)
    c.set_size(w, h)
    c.set_style_bg_opa(0, 0)
    c.set_style_border_width(0, 0)
    c.set_style_pad_all(0, 0)
    c.set_style_radius(0, 0)
    flag_off(c, lv.obj.FLAG.SCROLLABLE)
    flag_off(c, lv.obj.FLAG.CLICKABLE)
    return c


def label(parent, text, x, y, color, font):
    return m5ui.M5Label(
        text, x=x, y=y, text_c=color, bg_c=0x000000, bg_opa=0, font=font, parent=parent
    )


def fixed_label(parent, text, x, y, w, color, font, align=None):
    """Label with a fixed width, so right-aligned number columns line up."""
    lbl = label(parent, text, x, y, color, font)
    try:
        lbl.set_width(w)
        if align is not None:
            lbl.set_style_text_align(align, 0)
    except Exception as e:
        print("fixed_label:", e)
    return lbl


def set_color(lbl, color):
    lbl.set_style_text_color(lv.color_hex(color), 0)


def set_btn_text(btn, text):
    """Change a button's caption. M5Button has set_btn_text(); fall back to
    the button's first child (its label) on builds without it."""
    fn = getattr(btn, "set_btn_text", None)
    if fn:
        try:
            fn(text)
            return
        except Exception:
            pass
    try:
        btn.get_child(0).set_text(text)
    except Exception as e:
        print("set_btn_text:", e)


def fmt_age(ms):
    """Milliseconds -> '12s' / '5m' / '2h'."""
    s = ms // 1000
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    return "%dh" % (s // 3600)


def show(obj, visible):
    if visible:
        flag_off(obj, lv.obj.FLAG.HIDDEN)
    else:
        flag_on(obj, lv.obj.FLAG.HIDDEN)


def button(parent, text, x, y, w, h, color, font, on_press, event=None):
    """Button that calls on_press() on `event` (default PRESSED, which
    registers even when the loop is busy; use SHORT_CLICKED where a swipe
    starting on the button must not trigger it). Swipes that start on the
    button still reach the page."""
    if event is None:
        event = lv.EVENT.PRESSED
    btn = m5ui.M5Button(
        text=text, x=x, y=y, bg_c=color, text_c=0xFFFFFF, font=font, parent=parent
    )
    btn.set_size(w, h)
    try:
        btn.set_style_pad_all(0, 0)
    except Exception:
        pass
    flag_on(btn, lv.obj.FLAG.GESTURE_BUBBLE)

    def handler(e):
        if event_code(e) == event:
            on_press()

    btn.add_event_cb(handler, lv.EVENT.ALL, None)
    return btn


def wifi_connected():
    try:
        import network

        return network.WLAN(network.STA_IF).isconnected()
    except Exception:
        return True   # can't tell; just try the request


def short_err(e, n=46):
    s = str(e) or type(e).__name__
    return s if len(s) <= n else s[: n - 3] + "..."


def log_exc(msg, e):
    """Print a message plus the full traceback to the console."""
    print(msg)
    try:
        sys.print_exception(e)
    except Exception:
        print(repr(e))


# =============================================================================
# HTTP + MCP CLIENT
# =============================================================================
class McpError(Exception):
    """Transport/protocol problem - worth reconnecting and retrying."""


class McpToolError(McpError):
    """The tool itself reported an error - retrying won't help."""


def split_url(url):
    """'https://host:4443/path' -> (tls, host, port, path)"""
    scheme, rest = url.split("://", 1)
    i = rest.find("/")
    hostport, path = (rest, "/") if i < 0 else (rest[:i], rest[i:])
    j = hostport.find(":")
    if j >= 0:
        host, port = hostport[:j], int(hostport[j + 1:])
    else:
        host, port = hostport, 443 if scheme == "https" else 80
    return scheme == "https", host, port, path


def tls_wrap(sock, host):
    """Wrap a socket in TLS (no certificate check: the device has no CA store)."""
    import ssl
    ctx_cls = getattr(ssl, "SSLContext", None)
    if ctx_cls is not None:
        ctx = ctx_cls(ssl.PROTOCOL_TLS_CLIENT)
        try:
            ctx.verify_mode = ssl.CERT_NONE
        except Exception:
            pass
        return ctx.wrap_socket(sock, server_hostname=host)
    return ssl.wrap_socket(sock, server_hostname=host)


def read_chunked(s):
    out = b""
    while True:
        line = s.readline()
        if not line:
            break
        size = int(line.decode().split(";")[0].strip() or "0", 16)
        if size == 0:
            break
        out += s.read(size)
        s.readline()                         # CRLF after each chunk
    return out


def read_http_response(s):
    """Read status line, headers and body from a stream with
    readline()/read(). Returns (status, headers with lowercase names, text)."""
    status_line = s.readline()
    if not status_line or not status_line.startswith(b"HTTP/"):
        raise McpError("bad HTTP reply: %r" % (status_line or b"")[:60])
    status = int(status_line.split(b" ")[1])
    hdrs = {}
    while True:
        line = s.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        line = line.decode().strip()
        i = line.find(":")
        if i > 0:
            hdrs[line[:i].strip().lower()] = line[i + 1:].strip()
    if "content-length" in hdrs:
        n = int(hdrs["content-length"])
        body = s.read(n) if n > 0 else b""
    elif "chunked" in hdrs.get("transfer-encoding", "").lower():
        body = read_chunked(s)
    else:
        body = s.read()                      # until the server closes
    return status, hdrs, (body or b"").decode("utf-8")


def http_post_socket(url, body, headers):
    """Minimal HTTP/1.1 POST over TLS, one connection per request.
    Sends HTTP/1.1 (some proxies, e.g. Envoy, reject HTTP/1.0) and reads the
    body by Content-Length, chunked encoding, or until close."""
    import socket
    tls, host, port, path = split_url(url)
    data = body.encode()
    ai = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0]
    sock = socket.socket(ai[0], socket.SOCK_STREAM)
    s = sock
    try:
        sock.settimeout(HTTP_TIMEOUT_S)
        sock.connect(ai[-1])
        if tls:
            s = tls_wrap(sock, host)
        lines = ["POST %s HTTP/1.1" % path,
                 "Host: %s" % (host if port in (80, 443) else "%s:%d" % (host, port))]
        for k, v in headers.items():
            lines.append("%s: %s" % (k, v))
        lines.append("Content-Length: %d" % len(data))
        lines.append("Connection: close")
        s.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        s.write(data)
        return read_http_response(s)
    finally:
        try:
            s.close()
        except Exception:
            pass
        if s is not sock:
            try:
                sock.close()
            except Exception:
                pass


def _requests_module():
    try:
        import requests2 as rq
    except ImportError:
        try:
            import urequests as rq
        except ImportError:
            import requests as rq
    return rq


def http_post_requests(url, body, headers):
    """Fallback: the firmware's HTTP library (may send HTTP/1.0)."""
    r = _requests_module().post(url, data=body.encode(), headers=headers)
    try:
        status = getattr(r, "status_code", 200)
        hdrs = getattr(r, "headers", None) or {}
        try:
            text = r.text
        except Exception:
            text = ""
    finally:
        r.close()
    return status, hdrs, text or ""


def http_post(url, body, headers):
    """POST a string body. Returns (status, headers dict, text)."""
    if HTTP_CLIENT == "socket":
        return http_post_socket(url, body, headers)
    return http_post_requests(url, body, headers)


def header_get(hdrs, name):
    """Case-insensitive header lookup (header names vary in case)."""
    name = name.lower()
    try:
        for k, v in hdrs.items():
            if isinstance(k, bytes):
                k = k.decode()
            if k.lower() == name:
                return v.decode() if isinstance(v, bytes) else v
    except Exception:
        pass
    return None


def parse_rpc_reply(text, rid):
    """A Streamable HTTP reply is either plain JSON or a Server-Sent Events
    stream whose 'data:' lines carry JSON-RPC messages. Return our reply."""
    t = text.strip()
    if not t:
        raise McpError("empty reply")
    if t[0] in "{[":
        msgs = json.loads(t)
        if not isinstance(msgs, list):
            msgs = [msgs]
    else:
        msgs, data = [], []
        for line in (t + "\n\n").split("\n"):
            line = line.rstrip("\r")
            if line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line and data:
                try:
                    msgs.append(json.loads("\n".join(data)))
                except Exception:
                    pass
                data = []
    for m in msgs:
        if isinstance(m, dict) and m.get("id") == rid:
            return m
    raise McpError("no reply to request %d: %s" % (rid, t[:80]))


def decode_json_text(text):
    """Tool results arrive as text. Decode JSON if it is JSON (some servers
    encode twice, so try twice); otherwise return the text."""
    v = text
    for _ in range(2):
        if not isinstance(v, str):
            break
        s = v.strip()
        if not s or s[0] not in '{["':
            break
        try:
            v = json.loads(s)
        except Exception:
            break
    return v


def tool_payload(result):
    texts = []
    for c in result.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            texts.append(c.get("text", ""))
    text = "\n".join(texts)
    if result.get("isError"):
        raise McpToolError(text or "tool reported an error")
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    v = decode_json_text(text)
    if isinstance(v, str) and len(texts) > 1:
        return [decode_json_text(t) for t in texts]
    return v


class McpClient:
    """Minimal MCP client: initialize, tools/list, tools/call."""

    def __init__(self, url, headers):
        self.url = url
        self.headers = headers
        self.rid = 0
        self.reset()

    def reset(self):
        self.session = None
        self.protocol = None
        self.ready = False

    def _post(self, message):
        h = dict(self.headers)
        h["Content-Type"] = "application/json"
        h["Accept"] = "application/json, text/event-stream"
        if self.session:
            h["Mcp-Session-Id"] = self.session
        if self.protocol:
            h["MCP-Protocol-Version"] = self.protocol
        status, hdrs, text = http_post(self.url, json.dumps(message), h)
        sid = header_get(hdrs, "mcp-session-id")
        if sid:
            self.session = sid
        return status, text

    def _request(self, method, params):
        self.rid += 1
        rid = self.rid
        if DEBUG:
            print("MCP ->", method, json.dumps(params)[:120])
        status, text = self._post(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        )
        if DEBUG:
            print("MCP <-", status, text[:300])
        if status >= 400:
            if status == 404 and self.session:
                self.reset()                  # the server forgot our session
            raise McpError("HTTP %d %s" % (status, text[:80]))
        msg = parse_rpc_reply(text, rid)
        if "error" in msg:
            # The server answered, so reconnecting won't help: don't retry.
            err = msg["error"]
            if isinstance(err, dict):
                err = err.get("message", err)
            raise McpToolError(str(err))
        return msg.get("result") or {}

    def connect(self):
        self.reset()
        res = self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "stackchan-stock-watchlist", "version": "1.0"},
        })
        self.protocol = res.get("protocolVersion") or MCP_PROTOCOL
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception as e:
            print("MCP: initialized notification failed:", e)
        self.ready = True
        info = res.get("serverInfo") or {}
        print("MCP connected:", info.get("name", "?"), info.get("version", ""),
              "| protocol", self.protocol, "| session", self.session)

    def request(self, method, params):
        if not self.ready:
            self.connect()
        return self._request(method, params)

    def list_tools(self):
        tools, cursor = [], None
        while True:
            res = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(res.get("tools") or [])
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name, arguments):
        res = self.request("tools/call", {"name": name, "arguments": arguments})
        return tool_payload(res)


# =============================================================================
# QUOTE DATA (tolerant of different JSON shapes)
# =============================================================================
SYMBOL_KEYS = ("symbol", "ticker", "sym")
PRICE_KEYS = ("price", "currentprice", "current", "last", "lastprice",
              "latestprice", "regularmarketprice", "c", "close")
CHANGE_KEYS = ("change", "pricechange", "netchange", "changeamount",
               "dailychange", "regularmarketchange", "d")
PCT_KEYS = ("changepercent", "percentchange", "changepct", "pctchange",
            "changespercentage", "changepercentage", "percentagechange",
            "regularmarketchangepercent", "percent", "dp")
TICKER_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"


def _norm_key(k):
    return str(k).lower().replace("_", "").replace("-", "").replace(" ", "")


def pick(d, keys):
    """First value in dict d whose key matches one of keys (ignoring case,
    '_', '-' and spaces). Also looks one level into nested dicts."""
    if not isinstance(d, dict):
        return None
    norm = {_norm_key(k): v for k, v in d.items()}
    for k in keys:
        if norm.get(k) is not None:
            return norm[k]
    for v in d.values():
        if isinstance(v, dict):
            nv = {_norm_key(k): x for k, x in v.items()}
            for k in keys:
                if nv.get(k) is not None:
                    return nv[k]
    return None


def to_float(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace("$", "").replace(",", "").replace("%", "").strip()
        try:
            return float(s)
        except Exception:
            return None
    return None


def is_ticker(s):
    if not isinstance(s, str) or not 1 <= len(s) <= 8 or s[0] not in TICKER_CHARS[:26]:
        return False
    for ch in s:
        if ch not in TICKER_CHARS:
            return False
    return True


def find_records(obj, depth=0):
    """Find the list of per-symbol records anywhere in a reply."""
    if depth > 4:
        return None
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if dicts and any(pick(x, SYMBOL_KEYS) is not None for x in dicts):
            return dicts
        for x in obj:
            r = find_records(x, depth + 1)
            if r:
                return r
        return None
    if isinstance(obj, dict):
        if pick(obj, SYMBOL_KEYS) is not None and pick(obj, PRICE_KEYS) is not None:
            return [obj]                                 # a single quote
        for v in obj.values():
            r = find_records(v, depth + 1)
            if r:
                return r
        if obj and all(is_ticker(k) for k in obj):        # {"AAPL": {...}} or {"AAPL": 227.5}
            out = []
            for k, v in obj.items():
                if isinstance(v, dict):
                    d = dict(v)
                    d.setdefault("symbol", k)
                    out.append(d)
                elif to_float(v) is not None:
                    out.append({"symbol": k, "price": v})
            return out or None
    return None


def extract_symbols(obj, depth=0):
    """Pull a list of symbols out of a reply. Returns None if none found."""
    if depth > 4:
        return None
    if isinstance(obj, str):
        parts = [p.strip().upper() for p in obj.replace(",", " ").split()]
        return parts if parts and all(is_ticker(p) for p in parts) else None
    if isinstance(obj, list):
        if not obj:
            return []
        if all(isinstance(x, str) for x in obj):
            return [x.strip().upper() for x in obj]
    recs = find_records(obj)
    if recs is not None:
        return [str(pick(r, SYMBOL_KEYS)).upper() for r in recs if pick(r, SYMBOL_KEYS) is not None]
    if isinstance(obj, dict):
        for v in obj.values():
            r = extract_symbols(v, depth + 1)
            if r is not None:
                return r
    return None


def normalize_quote(r):
    sym = pick(r, SYMBOL_KEYS)
    if sym is None:
        return None
    price = to_float(pick(r, PRICE_KEYS))
    chg = to_float(pick(r, CHANGE_KEYS))
    pct = to_float(pick(r, PCT_KEYS))
    if pct is None and chg is not None and price:
        prev = price - chg
        if prev:
            pct = chg / prev * 100
    if chg is None and pct is not None and price:
        chg = price - price / (1 + pct / 100)
    return {"symbol": str(sym).upper(), "price": price, "change": chg, "pct": pct}


# --- parsers for FusionStockWatchListMCP's actual replies ---------------------
ERROR_WORDS = ("error", "fail", "exception", "invalid", "unauthorized", "not found")


def looks_like_error(text):
    t = text.lower()
    return any(w in t for w in ERROR_WORDS)


def is_blank(data):
    if isinstance(data, str):
        return data.strip().lower() in ("", "null", "[]", "{}")
    return data is None or data == [] or data == {}


def parse_quotes(data):
    """GetStockWatchList replies with JSON text:
    {"Watchlist": [{"Symbol": "AAPL", "Name": "Apple Inc", "Price": 341.07,
                    "Change": 5.15, "ChangePercent": 1.5331}, ...]}
    Symbols the server has no quote for (e.g. a bogus ticker) are left out.
    An empty list may come back as {"Watchlist": []}, null, "" or a message."""
    if is_blank(data):
        return []
    if isinstance(data, dict) and "Watchlist" in data:
        items = data.get("Watchlist") or []
    elif isinstance(data, str):
        if looks_like_error(data):
            raise McpToolError(data.strip())
        print("Quotes reply treated as an empty list:", data[:120])
        return []
    else:                                            # unexpected shape: generic search
        items = find_records(data)
        if items is None:
            print("Unrecognised quotes reply:", repr(data)[:300])
            raise McpToolError("unrecognised quotes reply (see console)")
    return [q for q in (normalize_quote(r) for r in items if isinstance(r, dict)) if q]


def parse_symbols(data):
    """GetStockWatchListSymbols replies with plain text: "AAPL,INTC,TXN".
    Empty list: "" (or a message)."""
    if is_blank(data):
        return []
    if isinstance(data, list):
        return [str(x).strip().upper() for x in data if str(x).strip()]
    if isinstance(data, dict) and "Watchlist" in data:
        return [q["symbol"] for q in parse_quotes(data)]
    if not isinstance(data, str):                     # unexpected shape
        syms = extract_symbols(data)
        if syms is None:
            print("Unrecognised symbols reply:", repr(data)[:300])
            raise McpToolError("unrecognised symbols reply (see console)")
        return syms
    parts = [p.strip().upper() for p in data.split(",") if p.strip()]
    if parts and all(is_ticker(p) for p in parts):
        return parts
    if looks_like_error(data):
        raise McpToolError(data.strip())
    print("Symbols reply treated as an empty list:", data[:120])
    return []


def classify_reply(text):
    """Add/Remove always return isError=false, so read the message:
    'Symbol AAPL successfully added'                 -> ok
    'Symbol already exists'                          -> exists
    'Successfully removed MSFT from watch list'      -> ok
    'Error deleting symbol from watch list'          -> error"""
    t = text.lower()
    if "already" in t:
        return "exists"
    if looks_like_error(t):
        return "error"
    if "success" in t:
        return "ok"
    return "unknown"


# =============================================================================
# WATCHLIST SERVICE (shared by both screens)
# =============================================================================
TOOL_RULES = (                     # role, words to look for (name first, then description)
    ("add", ("add", "insert")),
    ("remove", ("remove", "delete")),
    ("quotes", ("quote", "price")),
    ("list", ("list", "get", "symbols", "watchlist")),
)
READ_ROLES = ("quotes", "list")
# Never auto-map a tool whose name contains one of these (e.g. RemoveAll...)
DESTRUCTIVE_WORDS = ("all", "clear", "reset", "purge", "wipe")
WRITE_WORDS = ("add", "insert", "remove", "delete", "set", "update")


def auto_mappable(tool, role):
    """Safety rules for automatic mapping: never pick a bulk/destructive
    tool, and for read roles only pick tools that take no required arguments
    and don't look like they change anything."""
    name = (tool.get("name") or "").lower()
    if any(w in name for w in DESTRUCTIVE_WORDS):
        return False
    if role in READ_ROLES:
        if any(w in name for w in WRITE_WORDS):
            return False
        if (tool.get("inputSchema") or {}).get("required"):
            return False
    return True


def symbol_arg(tool):
    """Which argument carries the symbol, and is it an array?"""
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    names = list(props.keys())
    arg = None
    for n in names:
        if "symbol" in n.lower() or "ticker" in n.lower():
            arg = n
            break
    if arg is None:
        req = schema.get("required") or []
        arg = req[0] if req else (names[0] if names else "symbol")
    is_array = (props.get(arg) or {}).get("type") == "array"
    return arg, is_array


def map_tools(tools):
    print("MCP tools on the server:")
    for t in tools:
        print("  -", t.get("name"), ":", (t.get("description") or "")[:70])
    roles, used = {}, []
    for role, words in TOOL_RULES:
        tool = None
        forced = TOOL_NAMES.get(role)
        if forced:
            for t in tools:
                if t.get("name") == forced:
                    tool = t
            if tool is None:
                print("TOOL_NAMES: no tool named", forced)
        else:
            for field in ("name", "description"):
                for t in tools:
                    if t.get("name") in used or not auto_mappable(t, role):
                        continue
                    text = (t.get(field) or "").lower()
                    if any(w in text for w in words):
                        tool = t
                        break
                if tool:
                    break
        if tool:
            used.append(tool["name"])
            arg, is_array = symbol_arg(tool)
            roles[role] = (tool["name"], arg, is_array)
    print("Tool mapping (role: name, symbol arg, is array):")
    for role, _ in TOOL_RULES:
        print("  ", role, ":", roles.get(role, "NOT FOUND"))
    return roles


class WatchlistService:
    def __init__(self):
        self.mcp = McpClient(MCP_URL, {MCP_API_KEY_HEADER: MCP_API_KEY})
        self.tools = None           # role -> (tool name, arg name, is_array)
        self.symbols = None         # last symbol list (may include bogus tickers)
        self.quoted = None          # set of symbols that had a quote last time
        self.quotes_stale = True    # set after add/remove

    def _call(self, role, symbol=None):
        """Call the tool for `role`. Reconnects and retries once on a
        transport error; never retries when the server did answer."""
        if "YOUR-" in MCP_URL or "YOUR-" in MCP_API_KEY:
            raise McpToolError("Set MCP_URL and MCP_API_KEY in CONFIG")
        for attempt in (1, 2):
            try:
                if self.tools is None:
                    self.tools = map_tools(self.mcp.list_tools())
                entry = self.tools.get(role)
                if entry is None:
                    raise McpToolError("no '%s' tool (see console)" % role)
                name, arg, is_array = entry
                args = {}
                if symbol is not None:
                    args[arg] = [symbol] if is_array else symbol
                return self.mcp.call_tool(name, args)
            except McpToolError:
                raise
            except Exception as e:
                log_exc("MCP call failed (attempt %d):" % attempt, e)
                self.mcp.reset()
                if attempt == 2:
                    raise

    def get_quotes(self):
        quotes = parse_quotes(self._call("quotes"))
        self.quoted = set(q["symbol"] for q in quotes if q["price"] is not None)
        self.quotes_stale = False
        return quotes

    def get_symbols(self):
        self.symbols = parse_symbols(self._call("list"))
        return self.symbols

    def rows(self, quotes):
        """Quotes, plus a 'no quote' row for listed symbols the server
        couldn't quote (e.g. a bogus ticker it accepted anyway)."""
        rows = list(quotes)
        seen = set(q["symbol"] for q in quotes)
        for sym in self.symbols or []:
            if sym not in seen:
                seen.add(sym)
                rows.append({"symbol": sym, "price": None, "change": None,
                             "pct": None, "missing": True})
        return rows

    def change(self, action, symbol):
        """Add or remove. Returns (kind, server message); kind is one of
        ok / exists / error / unknown (see classify_reply)."""
        data = self._call(action, symbol)
        self.quotes_stale = True
        text = data if isinstance(data, str) else json.dumps(data)
        return classify_reply(text), text.strip()


# =============================================================================
# SCREEN BASE CLASS
# =============================================================================
class Screen:
    """build(page) once; on_enter() when shown; tick() every loop pass
    (~10 ms) while shown; on_exit() when swiped away."""

    NAME = "Screen"

    def __init__(self, mgr):
        self.mgr = mgr
        self.page = None

    @property
    def svc(self):
        return self.mgr.service

    def build(self, page):
        pass

    def on_enter(self):
        pass

    def on_exit(self):
        pass

    def tick(self):
        pass

    def background(self):
        """Called every loop pass whether or not the screen is visible."""
        pass


# =============================================================================
# SCREEN 1: WATCHLIST
# =============================================================================
ROW_Y0 = 50
ROW_H = 25
COL_SYM = (12, 80)          # x, width
COL_PRICE = (92, 90)
COL_CHG = (186, 62)
COL_PCT = (250, 62)
TOGGLE_X = 100              # auto-refresh toggle, right of the title
TOGGLE_W = 64
PAUSED_MSG = "Auto refresh is off.\nTap Off to load quotes."


def fmt_price(p):
    if p is None:
        return "--"
    return "%.2f" % p if p < 10000 else "%.0f" % p


def fmt_signed(v, suffix=""):
    if v is None:
        return "--"
    return "%+.2f%s" % (v, suffix)


class WatchlistScreen(Screen):
    NAME = "Watchlist"

    def build(self, page):
        label(page, "Watchlist", 10, 4, ACCENT, lv.font_montserrat_18)
        # Auto-refresh On/Off toggle. SHORT_CLICKED (not PRESSED) so a swipe
        # that starts on it doesn't flip it. The callback only changes state;
        # any refresh it asks for runs from tick() in the main loop.
        self.auto = AUTO_REFRESH
        self.toggle_btn = button(page, "", TOGGLE_X, 3, TOGGLE_W, 24, TOGGLE_ON,
                                 lv.font_montserrat_14, self.toggle_refresh,
                                 lv.EVENT.SHORT_CLICKED)
        status_x = TOGGLE_X + TOGGLE_W + 4
        self.status = fixed_label(page, "", status_x, 8, SCREEN_W - 8 - status_x,
                                  TEXT_SOFT, lv.font_montserrat_12, lv.TEXT_ALIGN.RIGHT)

        # column headers
        right = lv.TEXT_ALIGN.RIGHT
        f12 = lv.font_montserrat_12
        fixed_label(page, "Symbol", COL_SYM[0], 30, COL_SYM[1], TEXT_DIM, f12)
        fixed_label(page, "Price", COL_PRICE[0], 30, COL_PRICE[1], TEXT_DIM, f12, right)
        fixed_label(page, "Change", COL_CHG[0], 30, COL_CHG[1], TEXT_DIM, f12, right)
        fixed_label(page, "%", COL_PCT[0], 30, COL_PCT[1], TEXT_DIM, f12, right)
        shape(page, 8, 46, 304, 1, 0x2A3A55)

        # rows: created once, then only their text changes
        self.rows = []
        for i in range(ROWS_PER_PAGE):
            y = ROW_Y0 + i * ROW_H
            stripe = shape(page, 4, y - 2, 312, ROW_H - 2, 0x131B2B, 4)
            show(stripe, False)
            f16, f14 = lv.font_montserrat_16, lv.font_montserrat_14
            sym = fixed_label(page, "", COL_SYM[0], y, COL_SYM[1], TEXT_MAIN, f16)
            price = fixed_label(page, "", COL_PRICE[0], y, COL_PRICE[1], TEXT_MAIN, f16, right)
            chg = fixed_label(page, "", COL_CHG[0], y + 2, COL_CHG[1], TEXT_SOFT, f14, right)
            pct = fixed_label(page, "", COL_PCT[0], y + 2, COL_PCT[1], TEXT_SOFT, f14, right)
            self.rows.append((stripe, sym, price, chg, pct))

        self.msg = fixed_label(page, "Loading..." if self.auto else PAUSED_MSG,
                               10, 110, 300, TEXT_SOFT,
                               lv.font_montserrat_16, lv.TEXT_ALIGN.CENTER)

        self.items = []
        self.have_data = False
        self.error = False
        self.page_i = 0
        self.next_fetch = time.ticks_ms()
        self.last_flip = time.ticks_ms()
        self.last_ok = None         # ticks_ms of the last successful refresh
        self.last_status = None
        self.style_toggle()

    # --- lifecycle -------------------------------------------------------
    def on_enter(self):
        self.last_status = None
        if not self.auto:
            return                      # paused: no network until toggled on
        now = time.ticks_ms()
        due = time.ticks_diff(self.next_fetch, now) <= 0
        if due or self.svc.quotes_stale or not self.have_data:
            # small delay so the swipe animation finishes before we block
            self.next_fetch = time.ticks_add(now, 600)

    def tick(self):
        now = time.ticks_ms()
        if self.auto:
            # a pick finished in the background: refresh soon instead of in 60 s
            if (self.svc.quotes_stale and not self.error
                    and time.ticks_diff(self.next_fetch, now) > 1500):
                self.next_fetch = time.ticks_add(now, 1000)
            if time.ticks_diff(now, self.next_fetch) >= 0:
                self.refresh()
                now = time.ticks_ms()
        pages = self.page_count()
        if pages > 1 and time.ticks_diff(now, self.last_flip) >= PAGE_MS:
            self.page_i = (self.page_i + 1) % pages
            self.last_flip = now
            self.render()
        self.update_status(now)

    # --- refresh toggle --------------------------------------------------
    def toggle_refresh(self):
        """LVGL callback: flip auto refresh. No network here; turning it on
        schedules a refresh that tick() runs from the main loop."""
        self.auto = not self.auto
        if self.auto:
            # refresh right away (short delay so the button repaints first)
            self.next_fetch = time.ticks_add(time.ticks_ms(), 300)
            if not self.have_data:
                self.msg.set_text("Loading...")
        elif not self.have_data:
            self.msg.set_text(PAUSED_MSG)
        print("Auto refresh", "on" if self.auto else "off")
        self.style_toggle()
        self.last_status = None         # redraw the status line

    def style_toggle(self):
        if self.auto:
            set_btn_text(self.toggle_btn, lv.SYMBOL.REFRESH + " On")
            color = TOGGLE_ON
        else:
            set_btn_text(self.toggle_btn, lv.SYMBOL.PAUSE + " Off")
            color = TOGGLE_OFF
        self.toggle_btn.set_style_bg_color(lv.color_hex(color), lv.PART.MAIN)

    # --- data ------------------------------------------------------------
    def refresh(self):
        if not wifi_connected():
            self.error = True
            if not self.have_data:
                self.msg.set_text("No Wi-Fi")
            self.next_fetch = time.ticks_add(time.ticks_ms(), RETRY_MS)
            return

        self.status.set_text(lv.SYMBOL.REFRESH + " Updating")
        self.last_status = None
        refresh_now()
        try:
            if self.svc.symbols is None:
                # once, so symbols without a quote can be shown too
                try:
                    self.svc.get_symbols()
                except Exception as e:
                    log_exc("Symbol list load failed (not fatal):", e)
            self.items = self.svc.rows(self.svc.get_quotes())
            self.have_data = True
            self.error = False
            self.last_ok = time.ticks_ms()
            if self.page_i >= self.page_count():
                self.page_i = 0
            self.msg.set_text("" if self.items else
                              "Watchlist is empty.\nSwipe left to pick symbols.")
            self.render()
            self.next_fetch = time.ticks_add(time.ticks_ms(), REFRESH_MS)
        except Exception as e:
            log_exc("Quote refresh failed:", e)
            self.error = True
            if not self.have_data:
                self.msg.set_text("Couldn't load quotes\n" + short_err(e))
            self.next_fetch = time.ticks_add(time.ticks_ms(), RETRY_MS)
        self.last_flip = time.ticks_ms()

    def page_count(self):
        return max(1, (len(self.items) + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)

    # --- drawing ---------------------------------------------------------
    def render(self):
        for i, (stripe, sym, price, chg, pct) in enumerate(self.rows):
            idx = self.page_i * ROWS_PER_PAGE + i
            if idx >= len(self.items):
                for lbl in (sym, price, chg, pct):
                    lbl.set_text("")
                show(stripe, False)
                continue
            q = self.items[idx]
            show(stripe, i % 2 == 1)
            sym.set_text(q["symbol"])
            if q.get("missing"):
                price.set_text("no quote")
                set_color(price, TEXT_DIM)
                chg.set_text("")
                pct.set_text("")
                continue
            move = q["change"] if q["change"] is not None else q["pct"]
            color = TEXT_SOFT if not move else UP_COLOR if move > 0 else DOWN_COLOR
            price.set_text(fmt_price(q["price"]))
            set_color(price, TEXT_MAIN)
            chg.set_text(fmt_signed(q["change"]))
            pct.set_text(fmt_signed(q["pct"], "%"))
            set_color(chg, color)
            set_color(pct, color)

    def update_status(self, now):
        text = ""
        pages = self.page_count()
        if pages > 1:
            text += "%d/%d  " % (self.page_i + 1, pages)
        if not self.auto:
            # paused: show how old the quotes on screen are
            text += "Paused"
            if self.last_ok is not None:
                text += ", " + fmt_age(time.ticks_diff(now, self.last_ok)) + " old"
            color = WARN_COLOR
        else:
            secs = max(0, time.ticks_diff(self.next_fetch, now) + 999) // 1000
            if self.error:
                text += "Retry "
            text += lv.SYMBOL.REFRESH + " %ds" % secs
            color = ERR_COLOR if self.error else TEXT_SOFT
        if text != self.last_status:
            self.last_status = text
            self.status.set_text(text)
            set_color(self.status, color)


# =============================================================================
# SCREEN 2: PICK (tap a symbol to add it; tap again to remove it)
# =============================================================================
GRID_COLS = 5
GRID_ROWS = 5
GRID_Y = 54                 # top of the symbol grid
BTN_W, BTN_H, BTN_GAP = 60, 30, 3


def unique(items):
    out = []
    for x in items:
        if x not in out:
            out.append(x)
    return out


class PickScreen(Screen):
    """Tabs of preset symbol buttons. A button is green when the symbol is
    in the watchlist. Taps are queued and sent one at a time from
    background(), so you can tap several symbols in a row."""

    NAME = "Pick"

    def build(self, page):
        self.buttons = {}           # symbol -> button (preset tabs + Other tab)
        self.preset = set()
        self.queue = []             # (action, symbol) not yet sent
        self.pending = set()        # symbols queued or in flight
        self.refresh_after = False  # re-read the list once the queue drains
        self.next_load = None
        self.other_syms = None      # what the Other tab currently shows
        self.tab = 0

        f12, f14 = lv.font_montserrat_12, lv.font_montserrat_14

        # tab bar
        names = [g[0] for g in SYMBOL_GROUPS] + [OTHER_TAB]
        tw = (SCREEN_W - 8 - 3 * (len(names) - 1)) // len(names)
        self.tabs = []
        for i, name in enumerate(names):
            self.tabs.append(button(page, name, 4 + i * (tw + 3), 4, tw, 28, TAB_OFF,
                                    f14, lambda i=i: self.show_tab(i)))

        self.status = fixed_label(page, "Tap a symbol to add or remove it", 8, 36, 222,
                                  TEXT_SOFT, f12)
        self.count_lbl = fixed_label(page, "", 232, 36, 80, TEXT_SOFT, f12,
                                     lv.TEXT_ALIGN.RIGHT)

        # one grid per tab; only the active one is shown
        grid_h = GRID_ROWS * (BTN_H + BTN_GAP)
        self.views = []
        for name, syms in SYMBOL_GROUPS:
            v = container(page, 0, GRID_Y, SCREEN_W, grid_h)
            if len(syms) > GRID_COLS * GRID_ROWS:
                print("SYMBOL_GROUPS: %s has more than %d symbols; extras ignored"
                      % (name, GRID_COLS * GRID_ROWS))
            for i, sym in enumerate(syms[: GRID_COLS * GRID_ROWS]):
                self.add_symbol_button(v, i, sym)
                self.preset.add(sym)
            self.views.append(v)
        self.other = container(page, 0, GRID_Y, SCREEN_W, grid_h)
        self.views.append(self.other)
        self.rebuild_other()
        self.show_tab(0)

    def add_symbol_button(self, parent, i, sym):
        x = 4 + (i % GRID_COLS) * (BTN_W + BTN_GAP) + 2
        y = (i // GRID_COLS) * (BTN_H + BTN_GAP)
        # SHORT_CLICKED, not PRESSED: a swipe that starts on a symbol must
        # not add or remove it.
        self.buttons[sym] = button(parent, sym, x, y, BTN_W, BTN_H, SYM_OFF,
                                   lv.font_montserrat_14, lambda s=sym: self.toggle(s),
                                   lv.EVENT.SHORT_CLICKED)

    # --- lifecycle -------------------------------------------------------
    def on_enter(self):
        self.restyle_all()
        if not self.queue and not self.refresh_after:
            self.next_load = time.ticks_add(time.ticks_ms(), 600)   # re-read the list

    def tick(self):
        if self.next_load is not None and time.ticks_diff(time.ticks_ms(), self.next_load) >= 0:
            self.next_load = None
            self.load()

    def background(self):
        # Sends queued taps one per loop pass (each blocks ~1 s), even if you
        # swiped to the Watchlist meanwhile. Touch is processed in between.
        if self.queue:
            action, sym = self.queue.pop(0)
            self.process(action, sym)
            if not self.queue:
                self.refresh_after = True
            return
        if self.refresh_after:
            self.refresh_after = False
            try:
                self.svc.get_symbols()
            except Exception as e:
                log_exc("Symbol list refresh failed:", e)
            self.after_list_change()

    # --- tabs ------------------------------------------------------------
    def show_tab(self, i):
        """Called from a tab's LVGL callback: only shows/hides, never deletes."""
        self.tab = i
        for j, v in enumerate(self.views):
            show(v, j == i)
        for j, t in enumerate(self.tabs):
            t.set_style_bg_color(lv.color_hex(TAB_ON if j == i else TAB_OFF), lv.PART.MAIN)

    def rebuild_other(self):
        """Other tab = watchlist symbols not in any group (e.g. added from
        Claude). Main loop only: it deletes widgets."""
        syms = [s for s in unique(self.svc.symbols or []) if s not in self.preset]
        if syms == self.other_syms:
            return
        self.other_syms = syms
        for sym in list(self.buttons):
            if sym not in self.preset:
                del self.buttons[sym]
        self.other.clean()
        cap = GRID_COLS * GRID_ROWS
        shown = syms if len(syms) <= cap else syms[: cap - 1]
        for i, sym in enumerate(shown):
            self.add_symbol_button(self.other, i, sym)
        if len(syms) > cap:
            i = cap - 1
            label(self.other, "+%d more" % (len(syms) - len(shown)),
                  10 + (i % GRID_COLS) * (BTN_W + BTN_GAP), (i // GRID_COLS) * (BTN_H + BTN_GAP) + 8,
                  TEXT_SOFT, lv.font_montserrat_12)
        if not syms:
            fixed_label(self.other, "Watchlist symbols that aren't on\nthe other tabs appear here.",
                        10, 50, 300, TEXT_DIM, lv.font_montserrat_14, lv.TEXT_ALIGN.CENTER)

    # --- styling ---------------------------------------------------------
    def style(self, sym):
        btn = self.buttons.get(sym)
        if btn is None:
            return
        if sym in self.pending:
            color = SYM_PENDING
        elif self.svc.symbols is not None and sym in self.svc.symbols:
            color = SYM_ON
        else:
            color = SYM_OFF
        btn.set_style_bg_color(lv.color_hex(color), lv.PART.MAIN)

    def restyle_all(self):
        for sym in self.buttons:
            self.style(sym)
        syms = self.svc.symbols
        if syms is None:
            self.count_lbl.set_text("")
        else:
            n = len(unique(syms))
            self.count_lbl.set_text("%d in list" % n)

    def after_list_change(self):
        self.rebuild_other()
        self.restyle_all()

    def set_status(self, text, color=TEXT_SOFT):
        self.status.set_text(text)
        set_color(self.status, color)

    # --- taps --------------------------------------------------------------
    def toggle(self, sym):
        """LVGL callback: decide the action and queue it (no network here)."""
        if self.svc.symbols is None:
            self.set_status("Still loading the watchlist...", WARN_COLOR)
            return
        if sym in self.pending:
            return
        action = "remove" if sym in self.svc.symbols else "add"
        self.queue.append((action, sym))
        self.pending.add(sym)
        self.next_load = None           # the queue re-reads the list afterwards
        self.style(sym)
        self.set_status("%s %s..." % ("Adding" if action == "add" else "Removing", sym))

    def process(self, action, sym):
        """Main loop: send one add/remove and update the local list from the
        server's message (the server always says isError=false)."""
        adding = action == "add"
        refresh_now()
        try:
            if not wifi_connected():
                raise McpError("no Wi-Fi")
            kind, text = self.svc.change(action, sym)
        except Exception as e:
            log_exc("%s %s failed:" % (action, sym), e)
            self.pending.discard(sym)
            self.style(sym)
            self.set_status("%s %s failed: %s" % ("Add" if adding else "Remove", sym,
                                                   short_err(e, 26)), ERR_COLOR)
            return
        print("%s %s -> %s | %s" % (action, sym, kind, text))

        syms = self.svc.symbols
        if adding:
            if kind in ("ok", "exists", "unknown") and sym not in syms:
                syms.append(sym)
            if kind == "ok":
                self.set_status("Added %s" % sym, OK_COLOR)
            elif kind == "exists":
                self.set_status("%s was already in the list" % sym, TEXT_SOFT)
            else:
                self.set_status("%s: %s" % (sym, text[:36]), ERR_COLOR if kind == "error" else WARN_COLOR)
        else:
            if kind == "ok" and sym in syms:
                syms.remove(sym)                  # one copy; the re-read catches duplicates
            elif kind == "error":                 # server says it isn't there
                while sym in syms:
                    syms.remove(sym)
            if kind == "ok":
                self.set_status("Removed %s" % sym, OK_COLOR)
            elif kind == "error":
                self.set_status("%s wasn't in the server's list" % sym, WARN_COLOR)
            else:
                self.set_status("%s: %s" % (sym, text[:36]), WARN_COLOR)
        self.pending.discard(sym)
        self.after_list_change()

    # --- symbol list -----------------------------------------------------
    def load(self):
        if self.queue or self.refresh_after:
            return
        if not wifi_connected():
            self.set_status("No Wi-Fi", ERR_COLOR)
            self.next_load = time.ticks_add(time.ticks_ms(), RETRY_MS)
            return
        self.set_status(lv.SYMBOL.REFRESH + " Loading watchlist...", TEXT_SOFT)
        refresh_now()
        try:
            self.svc.get_symbols()
            self.set_status("Tap a symbol to add or remove it", TEXT_SOFT)
        except Exception as e:
            log_exc("Loading symbols failed:", e)
            self.set_status("Load failed: %s" % short_err(e, 30), ERR_COLOR)
            self.next_load = time.ticks_add(time.ticks_ms(), RETRY_MS)
        self.after_list_change()


# =============================================================================
# SCREEN MANAGER (swipe navigation, page dots, main loop)
# =============================================================================
SCREENS = [WatchlistScreen, PickScreen]


class ScreenManager:
    def __init__(self, screen_classes):
        self.service = WatchlistService()
        self.screens = [cls(self) for cls in screen_classes]
        self.index = 0
        self.pending = None     # one deferred action (from a button tap)
        self.nav = 0            # +1 next, -1 previous (from a swipe)

    def run_later(self, fn):
        """Run fn from the main loop instead of inside an LVGL callback."""
        self.pending = fn

    # --- setup -----------------------------------------------------------
    def start(self):
        M5.begin()
        Widgets.setRotation(1)          # landscape 320 x 240
        m5ui.init()
        n = len(self.screens)
        for i, scr in enumerate(self.screens):
            scr.page = m5ui.M5Page(bg_c=BG)
            scr.build(scr.page)
            self.prepare_page(scr.page, i, n)
        self.screens[0].page.screen_load()
        self.screens[0].on_enter()

    def prepare_page(self, page, index, count):
        try:
            page.set_flag(lv.obj.FLAG.SCROLLABLE, False)   # scrolling would swallow swipes
        except Exception:
            flag_off(page, lv.obj.FLAG.SCROLLABLE)
        page.add_event_cb(self.on_gesture, lv.EVENT.GESTURE, None)
        d, gap = 6, 8
        x = (SCREEN_W - (count * d + (count - 1) * gap)) // 2
        for i in range(count):
            shape(page, x, 229, d, d, 0xFFFFFF, CIRCLE, 255 if i == index else 90)
            x += d + gap

    # --- navigation ------------------------------------------------------
    def on_gesture(self, e):
        indev = active_indev()
        if indev is None:
            return
        d = indev.get_gesture_dir()
        if d == lv.DIR.LEFT:
            self.nav = 1
        elif d == lv.DIR.RIGHT:
            self.nav = -1

    def switch(self, step):
        self.screens[self.index].on_exit()
        self.index = (self.index + step) % len(self.screens)
        new = self.screens[self.index]
        load_fn = getattr(lv, "screen_load_anim", None) or getattr(lv, "scr_load_anim", None)
        anims = getattr(lv, "SCR_LOAD_ANIM", None) or getattr(lv, "SCREEN_LOAD_ANIM", None)
        try:
            anim = getattr(anims, "MOVE_LEFT" if step > 0 else "MOVE_RIGHT")
            load_fn(new.page, anim, SWIPE_ANIM_MS, 0, False)
        except Exception:
            new.page.screen_load()
        new.on_enter()

    # --- main loop -------------------------------------------------------
    def loop_once(self):
        M5.update()                     # touch + LVGL processing
        if self.pending:
            fn, self.pending = self.pending, None
            fn()
        if self.nav:
            step, self.nav = self.nav, 0
            self.switch(step)
        self.screens[self.index].tick()
        for scr in self.screens:
            scr.background()
        time.sleep_ms(10)


manager = None


def setup():
    global manager
    manager = ScreenManager(SCREENS)
    manager.start()


def loop():
    manager.loop_once()


if __name__ == "__main__":
    try:
        setup()
        while True:
            loop()
    except (Exception, KeyboardInterrupt) as e:
        try:
            m5ui.deinit()
            from utility import print_error_msg

            print_error_msg(e)
        except ImportError:
            print("please update to latest firmware")
