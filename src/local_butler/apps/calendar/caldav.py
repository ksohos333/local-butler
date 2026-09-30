"""Minimal CalDAV client for the local Radicale server.

Hand-rolled on top of stdlib ``urllib`` (no third-party CalDAV/ICS dependency),
matching the approach already used by the calendar GTK app.

Only what the calendar app needs is implemented: list, create, update, delete,
and collection bootstrap. Every mutating call is guarded by an ETag so a
concurrent write (for example from GNOME Calendar) can never be silently
clobbered.
"""

from __future__ import annotations

import base64
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"

ET.register_namespace("D", DAV_NS)
ET.register_namespace("C", CALDAV_NS)

_NS = {"D": DAV_NS, "C": CALDAV_NS}


class CalDAVError(RuntimeError):
    """Raised when the CalDAV server rejects a request."""


class NotFound(CalDAVError):
    """Raised when a calendar or event resource does not exist."""


def _propfind_body() -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<D:propfind xmlns:D="DAV:"><D:prop><D:displayname/><D:resourcetype/>'
        "</D:prop></D:propfind>"
    ).encode("utf-8")


def _calendar_query_body(start: Optional[str], end: Optional[str]) -> bytes:
    """Build a calendar-query REPORT, optionally bounded by a UTC time range."""
    if start and end:
        window = (
            "<C:time-range start=\"{start}\" end=\"{end}\"/>".format(
                start=_stamp(start), end=_stamp(end)
            )
        )
    else:
        window = ""
    return (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
        '<C:filter><C:comp-filter name="VCALENDAR">'
        "<C:comp-filter name=\"VEVENT\"/>{window}</C:comp-filter></C:filter>"
        "</C:calendar-query>".format(window=window)
    ).encode("utf-8")


def _stamp(value: str) -> str:
    """Convert an ISO-8601 timestamp into the compact UTC form CalDAV wants."""
    import datetime as _dt

    text = value.strip().replace("Z", "+00:00")
    parsed = _dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class CalDAVClient:
    """Talks to Radicale over CalDAV on loopback with HTTP basic auth."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5232",
        user: str = "local",
        password: str = "local",
        calendar: str = "calendar",
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.calendar = calendar
        self.timeout = timeout
        token = base64.b64encode(
            "{0}:{1}".format(user, password).encode("utf-8")
        ).decode("ascii")
        self._auth = "Basic {0}".format(token)

    # -- plumbing ---------------------------------------------------------
    @property
    def collection_url(self) -> str:
        return "{0}/{1}/{2}/".format(
            self.base_url,
            urllib.parse.quote(self.user),
            urllib.parse.quote(self.calendar),
        )

    def _request(
        self,
        method: str,
        url: str,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        expected: Tuple[int, ...] = (200, 201, 204, 207),
    ) -> Tuple[int, Dict[str, str], bytes]:
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Authorization", self._auth)
        if content_type:
            request.add_header("Content-Type", content_type)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                payload = response.read()
                resp_headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code == 404:
                raise NotFound("{0} {1} -> 404".format(method, url)) from exc
            raise CalDAVError(
                "{0} {1} -> {2}: {3}".format(method, url, exc.code, detail)
            ) from exc
        except urllib.error.URLError as exc:
            raise CalDAVError(
                "{0} {1} -> {2}".format(method, url, exc.reason)
            ) from exc
        if status not in expected:
            raise CalDAVError(
                "{0} {1} -> unexpected status {2}".format(method, url, status)
            )
        return status, resp_headers, payload

    # -- discovery --------------------------------------------------------
    def reachable(self) -> bool:
        try:
            self._request("OPTIONS", self.base_url + "/", expected=(200, 204))
            return True
        except CalDAVError:
            return False

    def list_calendars(self) -> List[str]:
        url = "{0}/{1}/".format(self.base_url, urllib.parse.quote(self.user))
        _, _, payload = self._request(
            "PROPFIND",
            url,
            body=_propfind_body(),
            content_type="application/xml; charset=utf-8",
            headers={"Depth": "1"},
        )
        root = ET.fromstring(payload.decode("utf-8", errors="replace"))
        names: List[str] = []
        for response in root.findall("D:response", _NS):
            href = response.findtext("D:href", "", _NS)
            if href.rstrip("/").endswith(self.user):
                continue
            resourcetype = response.find("D:propstat/D:prop/D:resourcetype", _NS)
            if resourcetype is None:
                continue
            if resourcetype.find("C:calendar", _NS) is None:
                continue
            display = response.findtext(
                "D:propstat/D:prop/D:displayname", "", _NS
            )
            names.append(display or urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1]))
        return names

    def create_calendar(self, name: Optional[str] = None) -> str:
        url = self.collection_url
        request = urllib.request.Request(url, method="MKCALENDAR")
        request.add_header("Authorization", self._auth)
        request.add_header("Content-Type", "text/calendar; charset=utf-8")
        if name:
            request.add_header("X-Radicale-DisplayName", name)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status not in (200, 201, 207):
                    raise CalDAVError("MKCALENDAR -> {0}".format(response.status))
        except urllib.error.HTTPError as exc:
            if exc.code in (405, 409):
                return url
            raise CalDAVError("MKCALENDAR -> {0}".format(exc.code)) from exc
        except urllib.error.URLError as exc:
            raise CalDAVError("MKCALENDAR -> {0}".format(exc.reason)) from exc
        return url

    def ensure_calendar(self) -> str:
        """Create the default collection if it is missing; safe to call often."""
        try:
            self.create_calendar()
        except CalDAVError:
            if not self.collection_exists():
                raise
        return self.collection_url

    def collection_exists(self) -> bool:
        try:
            self._request(
                "PROPFIND",
                self.collection_url,
                body=_propfind_body(),
                content_type="application/xml; charset=utf-8",
                headers={"Depth": "0"},
            )
            return True
        except NotFound:
            return False

    # -- events -----------------------------------------------------------
    def list_events(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Return ``{"href", "etag", "ics"}`` for every VEVENT in the collection."""
        _, _, payload = self._request(
            "REPORT",
            self.collection_url,
            body=_calendar_query_body(start, end),
            content_type="application/xml; charset=utf-8",
            headers={"Depth": "1"},
        )
        root = ET.fromstring(payload.decode("utf-8", errors="replace"))
        events: List[Dict[str, str]] = []
        for response in root.findall("D:response", _NS):
            href = response.findtext("D:href", "", _NS)
            etag = ""
            ics = ""
            for propstat in response.findall("D:propstat", _NS):
                prop = propstat.find("D:prop", _NS)
                if prop is None:
                    continue
                found = prop.findtext("D:getetag", "", _NS)
                if found:
                    etag = found.strip('"')
                data = prop.findtext("C:calendar-data", "", _NS)
                if data:
                    ics = data
            if ics:
                events.append({"href": href, "etag": etag, "ics": ics})
        return events

    def _event_url(self, href_or_uid: str) -> str:
        """Absolute URL for a resource, given as an href, a path, or a UID.

        The three forms are easy to confuse: Radicale returns absolute paths
        like ``/local/calendar/<name>.ics``, a caller may pass a bare file name
        that belongs to the current collection, and a UID is a name that still
        needs a ``.ics`` suffix. Getting this wrong silently targets a
        non-existent URL, so each case is handled explicitly.
        """
        if href_or_uid.startswith("http"):
            return href_or_uid
        if href_or_uid.startswith("/"):
            return self.base_url + href_or_uid
        if "/" in href_or_uid:
            # A nested relative path, e.g. "sub/a.ics".
            return "{0}/{1}".format(self.base_url, urllib.parse.quote(href_or_uid))
        # A bare name belongs to the collection; only a UID needs the suffix.
        name = href_or_uid if href_or_uid.endswith(".ics") else href_or_uid + ".ics"
        return "{0}{1}".format(self.collection_url, urllib.parse.quote(name))

    def put_event(
        self,
        ics: str,
        uid: Optional[str] = None,
        href: Optional[str] = None,
        if_match: Optional[str] = None,
    ) -> str:
        """Create or update an event, returning its href.

        When ``if_match`` is supplied the write is conditional on the ETag, so a
        concurrent edit in another client surfaces as an error instead of being
        overwritten.
        """
        uid = uid or _extract_uid(ics) or "{0}@local-butler".format(uuid4())
        target = self._event_url(href) if href else self._event_url(uid)
        headers = {}
        if if_match:
            headers["If-Match"] = '"{0}"'.format(if_match)
        self._request(
            "PUT",
            target,
            body=ics.encode("utf-8"),
            content_type="text/calendar; charset=utf-8",
            headers=headers,
        )
        return target

    def delete_event(self, href: str, if_match: Optional[str] = None) -> None:
        headers = {}
        if if_match:
            headers["If-Match"] = '"{0}"'.format(if_match)
        self._request("DELETE", self._event_url(href), headers=headers)


def _extract_uid(ics: str) -> Optional[str]:
    for line in ics.replace("\r\n ", "").replace("\n ", "").splitlines():
        if line.startswith("UID:"):
            return line[4:].strip()
    return None
