"""Öffentlicher Grafana-Snapshot der Wochenprognose mit festem Kurzlink.

Ablauf je Durchgang: Dashboard „Prognose öffentlich“ aus der eigenen Grafana holen (nur
Standard-Panels, raintank kennt keine Zusatz-Plugins), die Abfragen der Wochenpanels für heute
00:00 bis Ende D+7 (Europe/Berlin) über ``/api/ds/query`` ausführen und einbetten, mit
``external: true`` als Snapshot hochladen (Grafana reicht ihn an ``snapshots.raintank.io``
weiter) und das Ziel eines Kurzlinks auf die neue Snapshot-Adresse umbiegen. Ein Snapshot ist
ein interaktives Standbild (Tooltips, Zoom, Legende), aktualisiert sich aber nicht; jeder hat
eine neue Adresse, daher der Kurzlink. Jeder Snapshot listet die früheren, noch nicht
abgelaufenen Stände mit Link, damit man Prognosen später mit dem Eingetretenen vergleichen kann.

Panels mit eigener Zeitüberschreibung wertet Grafana im Snapshot nicht aus (absoluter Zeitraum,
dann gilt ``timeFrom`` nicht). Die vier Panels „Morgen“ (Preis und Last mit D+1 bis D+7, je
Champion und Challenger) laufen daher für den Liefertag nach dem Stand und werden zu einem
Trend-Panel umgebaut: x-Achse in Stunden seit 0 Uhr, beschriftet mit der Uhrzeit (Wertzuordnung
je Viertelstunde, an Tagen der Zeitumstellung mit doppelter bzw. fehlender Stunde). Ein
Zeitreihen-Panel hätte sonst den Wochenbereich als x-Achse. Gestern und Heute bleiben draußen.

@relation(SWA-WXX-SNAPSHOT, scope=file)
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from predictiondb_core.log import get_logger

log = get_logger(__name__)

DASHBOARD_UID = "pdb-prognose-oeffentlich"
#: Panels „Morgen“: Preis Champion, Preis Challenger, Last Champion, Last Challenger
DAY_PANELS = (60, 62, 73, 75)
#: Wochenpanels (Last und Preis, Champion und Challenger), „Morgen“ und das Quellen-Panel
PANELS = (67, 68, 69, 70, *DAY_PANELS, 71)
#: x-Feld der Trend-Panels „Morgen“ (Stunden seit 0 Uhr Ortszeit)
X_FIELD = "Uhrzeit"
TZ = ZoneInfo("Europe/Berlin")
#: Namensanfang aller veröffentlichten Stände (Titel und Name des Snapshots)
TITLE = "Strompreis- und Lastprognose DE-LU · Stand"
WOCHENTAG = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
#: Standardwerte der Dashboard-Variablen im Snapshot (die übrigen kommen aus ihren Abfragen)
FIXED_VARS = {"tagesmittel_last": "aus"}


def week_range(now: datetime) -> tuple[datetime, datetime]:
    """Heute 00:00 bis Ende D+7 in Ortszeit (Sommer-/Winterzeit beachtet).

    @relation(SWR-WXX-050, scope=function)
    """
    local = now.astimezone(TZ)
    start = datetime(local.year, local.month, local.day, tzinfo=TZ)
    # Rechnen mit ZoneInfo geht über die Wanduhr: +8 Tage ist wieder 00:00 Ortszeit
    return start, start + timedelta(days=8)


def day_range(now: datetime) -> tuple[datetime, datetime]:
    """Der Liefertag nach ``now`` (morgen) von 0 bis 24 Uhr Ortszeit.

    @relation(SWR-WXX-052, scope=function)
    """
    start = week_range(now)[0] + timedelta(days=1)
    return start, start + timedelta(days=1)


def clock_mapping(start: datetime, end: datetime) -> dict[str, Any]:
    """Wertzuordnung Stunden seit ``start`` → Uhrzeit ``HH:MM`` je Viertelstunde.

    @relation(SWR-WXX-052, scope=function)
    """
    # über UTC: Python zieht bei gleicher tzinfo Wanduhren ab (23 bzw. 25 h bei Zeitumstellung)
    hours = (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600
    options = {}
    for k in range(int(hours * 4) + 1):
        h = k / 4
        t = (start.astimezone(UTC) + timedelta(hours=h)).astimezone(TZ)
        options[f"{h:g}"] = {"text": "24:00" if k == hours * 4 else f"{t:%H:%M}", "index": k}
    return {"type": "value", "options": options}


def day_frame(frames: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any]:
    """Frames (Zeitreihen) zu einem Frame mit Stunden seit ``start`` als erstem Feld.

    Das Trend-Panel braucht genau einen Frame mit aufsteigender Zahlenspalte als x. Werte
    außerhalb von [start, end) fallen weg, fehlende Zeitpunkte einer Reihe sind ``None``.

    @relation(SWR-WXX-052, scope=function)
    """
    t0, t1 = start.timestamp() * 1000, end.timestamp() * 1000
    cols: dict[str, dict[float, Any]] = {}
    for f in frames:
        fields, values = f["schema"]["fields"], f["data"]["values"]
        ti = next((i for i, x in enumerate(fields) if x.get("type") == "time"), None)
        if ti is None:
            continue
        for i, x in enumerate(fields):
            if i == ti or x.get("type") != "number":
                continue
            col = cols.setdefault(x["name"], {})
            for t, v in zip(values[ti], values[i], strict=True):
                if t0 <= t < t1:
                    col[(t - t0) / 3_600_000] = v
    hours = sorted({h for c in cols.values() for h in c})
    mapping = clock_mapping(start, end)
    # Achse immer über den ganzen Tag, auch wenn späte Stunden (noch) fehlen
    x_config = {
        "unit": "none",
        "min": 0,
        "max": max(len(mapping["options"]) - 1, 0) / 4,
        "mappings": [mapping],
    }
    return {
        "schema": {
            "fields": [
                {"name": X_FIELD, "type": "number", "config": x_config},
                *({"name": n, "type": "number", "config": {}} for n in cols),
            ]
        },
        "data": {"values": [hours, *([c.get(h) for h in hours] for c in cols.values())]},
    }


def _as_trend(panel: dict[str, Any]) -> None:
    """Zeitreihen-Panel (Schema v2) auf Trend umstellen: x-Feld, Lücken überbrücken."""
    viz = panel["vizConfig"]
    viz["group"] = "trend"
    spec = viz.setdefault("spec", {})
    spec.setdefault("options", {})["xField"] = X_FIELD
    defaults = spec.setdefault("fieldConfig", {}).setdefault("defaults", {})
    # Preise in Viertelstunden, Prognosen stündlich: im gemeinsamen Frame Lücken je Reihe
    defaults.setdefault("custom", {})["spanNulls"] = True


def _ms(t: datetime) -> str:
    return str(int(t.timestamp() * 1000))


def interpolate(text: str, values: dict[str, tuple[str, str]]) -> str:
    """``${name}``, ``${name:text}`` und ``$name`` durch Wert bzw. Text ersetzen."""
    for name, (value, label) in values.items():
        text = text.replace("${" + name + ":text}", label)
        text = text.replace("${" + name + "}", value).replace("$" + name, value)
    return text


@dataclass
class Grafana:
    """Lesen und Snapshot-Upload über die HTTP-API der eigenen Grafana."""

    url: str
    client: httpx.Client
    user: str
    password: str

    def _req(self, method: str, path: str, **kwargs: Any) -> Any:
        r = self.client.request(
            method, self.url.rstrip("/") + path, auth=(self.user, self.password), **kwargs
        )
        r.raise_for_status()
        return r.json()

    def dashboard(self, uid: str) -> dict[str, Any]:
        return dict(self._req("GET", f"/api/dashboards/uid/{uid}")["dashboard"])

    def dashboard_v2(self, uid: str) -> dict[str, Any]:
        path = f"/apis/dashboard.grafana.app/v2beta1/namespaces/default/dashboards/{uid}"
        return dict(self._req("GET", path)["spec"])

    def query(
        self, targets: list[dict[str, Any]], start: datetime, end: datetime
    ) -> dict[str, Any]:
        body = {
            "from": _ms(start),
            "to": _ms(end),
            "queries": [{"intervalMs": 3_600_000, "maxDataPoints": 2000, **t} for t in targets],
        }
        return dict(self._req("POST", "/api/ds/query", json=body)["results"])

    def snapshots(self) -> list[dict[str, Any]]:
        """Snapshots dieser Grafana, auch die extern hochgeladenen (mit ``externalUrl``)."""
        return list(self._req("GET", "/api/dashboard/snapshots", params={"limit": 1000}))

    def snapshot(self, dashboard: dict[str, Any], name: str, expires: int) -> dict[str, Any]:
        body = {"dashboard": dashboard, "name": name, "expires": expires, "external": True}
        return dict(self._req("POST", "/api/snapshots", json=body))


def _variable_values(
    grafana: Grafana, dash: dict[str, Any], start: datetime, end: datetime
) -> dict[str, tuple[str, str]]:
    """Abfrage-Variablen in Listenreihenfolge auflösen (erster Wert, wie beim Laden).

    Spätere Variablen dürfen frühere verwenden (``lauf_*_challenger`` nutzt ``challenger``).
    """
    values = {k: (v, v) for k, v in FIXED_VARS.items()}
    for var in dash.get("templating", {}).get("list", []):
        if var.get("type") != "query" or var["name"] in values:
            continue
        res = grafana.query(
            [
                {
                    "refId": "V",
                    "datasource": var["datasource"],
                    "rawSql": interpolate(var["query"], values),
                    "rawQuery": True,
                    "format": "table",
                }
            ],
            start,
            end,
        )
        frames = res["V"].get("frames", [])
        if not frames or not frames[0]["data"]["values"][0]:
            values[var["name"]] = ("", "keiner")
            continue
        names = [f["name"] for f in frames[0]["schema"]["fields"]]
        cols = dict(zip(names, frames[0]["data"]["values"], strict=True))
        first = frames[0]["data"]["values"][0][0]
        value = cols.get("__value", [first])[0]
        values[var["name"]] = (str(value), str(cols.get("__text", [value])[0]))
    return values


def _frames(
    grafana: Grafana,
    panel: dict[str, Any],
    values: dict[str, tuple[str, str]],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    targets = [
        {"datasource": panel.get("datasource"), **t, "rawSql": interpolate(t["rawSql"], values)}
        for t in panel.get("targets", [])
        if "rawSql" in t
    ]
    if not targets:
        return []
    res = grafana.query(targets, start, end)
    errors = {k: r["error"] for k, r in res.items() if r.get("error")}
    if errors:
        raise RuntimeError(f"Panel {panel['id']}: {errors}")
    frames = [f for t in targets for f in res.get(t["refId"], {}).get("frames", [])]
    for f in frames:
        for field in f["schema"]["fields"]:
            field.setdefault("config", {})
    return frames


def _parse(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def history(grafana: Grafana, now: datetime) -> str:
    """Markdown-Liste der früheren, noch gültigen öffentlichen Stände (neueste zuerst).

    @relation(SWR-WXX-050, scope=function)
    """
    rows = []
    for s in grafana.snapshots():
        url, name, expires = s.get("externalUrl"), s.get("name", ""), s.get("expires", "")
        if not (s.get("external") and url and name.startswith(TITLE)):
            continue
        # Grafana meldet „läuft nie ab“ als Jahr 2100 oder später
        if expires and _parse(expires) <= now:
            continue
        rows.append((_parse(s["created"]), url, expires))
    if not rows:
        return "Noch keine früheren Stände."
    lines = ["| Stand | läuft ab |", "| --- | --- |"]
    for created, url, expires in sorted(rows, reverse=True):
        end = _parse(expires).astimezone(TZ).strftime("%d.%m.%Y") if expires else "nie"
        local = created.astimezone(TZ)
        day = WOCHENTAG[local.weekday()]
        lines.append(f"| [{day} {local:%d.%m.%Y %H:%M}]({url}) | {end} |")
    return "\n".join(lines)


def _history_element(content: str) -> dict[str, Any]:
    return {
        "kind": "Panel",
        "spec": {
            "id": 999,
            "title": "Frühere Prognosestände (zum Vergleich mit dem Eingetretenen)",
            "description": "",
            "links": [],
            "data": {
                "kind": "QueryGroup",
                "spec": {"queries": [], "transformations": [], "queryOptions": {}},
            },
            "vizConfig": {
                "kind": "VizConfig",
                "group": "text",
                "version": "",
                "spec": {
                    "options": {"mode": "markdown", "content": content},
                    "fieldConfig": {"defaults": {}, "overrides": []},
                },
            },
        },
    }


def build_snapshot(grafana: Grafana, now: datetime) -> dict[str, Any]:
    """Dashboard „Prognose öffentlich“ (Schema v2) mit eingebetteten Daten der Wochenpanels.

    Grafana 13 lädt Snapshots im alten Schema mit ``snapshotData`` nicht mehr; die Oberfläche
    schickt das Schema v2, in dem jedes Panel eine Abfrage ``queryType: snapshot`` der
    Datenquelle ``grafana`` mit den Frames trägt. Das Schema v2 liefert Grafana selbst
    (``/apis/dashboard.grafana.app/v2beta1``), die Abfragen kommen aus dem alten Schema.

    @relation(SWR-WXX-050, scope=function)
    """
    v1 = grafana.dashboard(DASHBOARD_UID)
    spec = copy.deepcopy(grafana.dashboard_v2(DASHBOARD_UID))
    start, end = week_range(now)
    values = _variable_values(grafana, v1, start, end)
    sources = {f"panel-{p['id']}": p for p in v1["panels"] if p["id"] in PANELS}
    day_start, day_end = day_range(now)
    day = f"{WOCHENTAG[day_start.weekday()]} {day_start:%d.%m.%Y}"
    elements = {}
    for name, el in spec["elements"].items():
        if name not in sources:
            continue
        panel = el["spec"]
        panel["title"] = interpolate(panel.get("title", ""), values)
        source = sources[name]
        if source["id"] in DAY_PANELS:
            raw = _frames(grafana, source, values, day_start, day_end)
            frames = [day_frame(raw, day_start, day_end)]
            panel["title"] += f" · Liefertag {day}"
            _as_trend(panel)
        else:
            frames = _frames(grafana, source, values, start, end)
        if panel.get("data"):
            data = panel["data"]["spec"]
            data["queries"] = [
                {
                    "kind": "PanelQuery",
                    "spec": {
                        "hidden": False,
                        "refId": "A",
                        "query": {
                            "kind": "DataQuery",
                            "group": "grafana",
                            "version": "v0",
                            "datasource": {"name": "grafana"},
                            "spec": {
                                "queryType": "snapshot",
                                "snapshot": frames,
                            },
                        },
                    },
                }
            ]
            data["queryOptions"] = {
                k: v
                for k, v in data.get("queryOptions", {}).items()
                if k not in ("timeFrom", "timeShift", "hideTimeOverride")
            }
        elements[name] = el
    spec["elements"] = elements
    items = spec["layout"]["spec"]["items"]
    items = [i for i in items if i["spec"]["element"]["name"] in elements]
    content = history(grafana, now)
    elements["panel-staende"] = _history_element(content)
    bottom = max((i["spec"].get("y", 0) + i["spec"].get("height", 0) for i in items), default=0)
    items.append(
        {
            "kind": "GridLayoutItem",
            "spec": {
                "x": 0,
                "y": bottom,
                "width": 24,
                "height": min(4 + content.count("\n"), 20),
                "element": {"kind": "ElementReference", "name": "panel-staende"},
            },
        }
    )
    spec["layout"]["spec"]["items"] = items
    stamp = now.astimezone(TZ)
    spec.update(
        title=f"{TITLE} {stamp:%d.%m.%Y %H:%M}",
        links=[],
        variables=[],
        editable=False,
    )
    spec["timeSettings"] = {
        **spec.get("timeSettings", {}),
        "from": start.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "to": (end - timedelta(milliseconds=1))
        .astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "hideTimepicker": True,
        "autoRefresh": "",
        "timezone": "Europe/Berlin",
    }
    # uid bleibt: Grafana prüft beim Anlegen das Leserecht am Quell-Dashboard
    spec["uid"] = DASHBOARD_UID
    return dict(spec)


class Shortlink(Protocol):
    def point_to(self, url: str) -> None: ...


@dataclass
class ShortIo:
    """Short.io: Link ``domain/path`` umbiegen, beim ersten Mal anlegen.

    Die Link-ID kommt aus ``GET /links/expand``; fehlt der Link (404), legt ``POST /links`` ihn an.
    """

    client: httpx.Client
    api_key: str
    domain: str
    path: str

    def point_to(self, url: str) -> None:
        """@relation(SWR-WXX-051, scope=function)"""
        api = "https://api.short.io/links"
        headers = {"authorization": self.api_key}
        r = self.client.get(
            f"{api}/expand", headers=headers, params={"domain": self.domain, "path": self.path}
        )
        if r.status_code == 404:
            body = {"domain": self.domain, "path": self.path, "originalURL": url}
            self.client.post(api, headers=headers, json=body).raise_for_status()
            return
        r.raise_for_status()
        link_id = r.json()["idString"]
        self.client.post(
            f"{api}/{link_id}", headers=headers, json={"originalURL": url}
        ).raise_for_status()


@dataclass
class Shlink:
    """Shlink (selbst gehostet): ``PATCH /rest/v3/short-urls/{code}``."""

    client: httpx.Client
    base_url: str
    api_key: str
    short_code: str

    def point_to(self, url: str) -> None:
        """@relation(SWR-WXX-051, scope=function)"""
        r = self.client.patch(
            f"{self.base_url.rstrip('/')}/rest/v3/short-urls/{self.short_code}",
            headers={"X-Api-Key": self.api_key},
            json={"longUrl": url},
        )
        r.raise_for_status()


def publish(
    grafana: Grafana, shortlink: Shortlink | None, expires: int, now: datetime | None = None
) -> dict[str, Any]:
    """Ein Durchgang: Snapshot bauen, hochladen, Kurzlink umbiegen.

    @relation(SWR-WXX-050, SWR-WXX-051, scope=function)
    """
    now = now or datetime.now(UTC)
    dash = build_snapshot(grafana, now)
    res = grafana.snapshot(dash, dash["title"], expires)
    url = str(res["url"])
    if shortlink is not None:
        shortlink.point_to(url)
    log.info("snapshot_published", url=url, shortlink=shortlink is not None)
    return {"url": url, "created": now.isoformat(timespec="seconds"), "expires": expires}


def next_run(now: datetime, at: str) -> datetime:
    """Nächster Zeitpunkt ``HH:MM`` Ortszeit nach ``now``.

    @relation(SWR-WXX-050, scope=function)
    """
    hour, minute = (int(x) for x in at.split(":"))
    local = now.astimezone(TZ)
    run = datetime(local.year, local.month, local.day, hour, minute, tzinfo=TZ)
    if run <= local:
        day = run.date() + timedelta(days=1)
        run = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
    return run


def sleep_until(t: datetime) -> None:
    while (left := (t - datetime.now(UTC)).total_seconds()) > 0:
        time.sleep(min(left, 300))
