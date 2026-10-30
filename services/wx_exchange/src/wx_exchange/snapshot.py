"""Öffentlicher Grafana-Snapshot der Wochenprognose mit festem Kurzlink.

Ablauf je Durchgang: Dashboard „Prognose aktuell“ aus der eigenen Grafana holen, die Abfragen
der Wochenpanels für heute 00:00 bis Ende D+7 (Europe/Berlin) über ``/api/ds/query``
ausführen und als ``snapshotData`` einbetten, mit ``external: true`` als Snapshot hochladen
(Grafana reicht ihn an ``snapshots.raintank.io`` weiter) und das Ziel eines Kurzlinks auf die
neue Snapshot-Adresse umbiegen. Ein Snapshot ist ein interaktives Standbild (Tooltips, Zoom,
Legende), aktualisiert sich aber nicht; jeder hat eine neue Adresse, daher der Kurzlink.

Panels mit eigener Zeitüberschreibung (Gestern, Heute, Morgen) bleiben draußen: Ein Snapshot
hat einen absoluten Zeitraum, und dann ignoriert Grafana relative Panel-Zeiträume.
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

DASHBOARD_UID = "pdb-prognose-aktuell"
#: Wochenpanels (Last und Preis, Champion und Challenger) und das Quellen-Panel
PANELS = (67, 68, 69, 70, 71)
TZ = ZoneInfo("Europe/Berlin")
#: Standardwerte der Dashboard-Variablen im Snapshot (challenger kommt aus seiner Abfrage)
FIXED_VARS = {"tagesmittel_last": "aus"}


def week_range(now: datetime) -> tuple[datetime, datetime]:
    """Heute 00:00 bis Ende D+7 in Ortszeit (Sommer-/Winterzeit beachtet)."""
    local = now.astimezone(TZ)
    start = datetime(local.year, local.month, local.day, tzinfo=TZ)
    # Rechnen mit ZoneInfo geht über die Wanduhr: +8 Tage ist wieder 00:00 Ortszeit
    return start, start + timedelta(days=8)


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

    def snapshot(self, dashboard: dict[str, Any], name: str, expires: int) -> dict[str, Any]:
        body = {"dashboard": dashboard, "name": name, "expires": expires, "external": True}
        return dict(self._req("POST", "/api/snapshots", json=body))


def _variable_values(
    grafana: Grafana, dash: dict[str, Any], start: datetime, end: datetime
) -> dict[str, tuple[str, str]]:
    values = {k: (v, v) for k, v in FIXED_VARS.items()}
    for var in dash.get("templating", {}).get("list", []):
        if var["name"] != "challenger":
            continue
        res = grafana.query(
            [{"refId": "V", "datasource": var["datasource"], "rawSql": var["query"],
              "rawQuery": True, "format": "table"}],
            start,
            end,
        )
        frames = res["V"].get("frames", [])
        if not frames or not frames[0]["data"]["values"][0]:
            values["challenger"] = ("", "keiner")
            continue
        names = [f["name"] for f in frames[0]["schema"]["fields"]]
        cols = dict(zip(names, frames[0]["data"]["values"], strict=True))
        values["challenger"] = (str(cols["__value"][0]), str(cols["__text"][0]))
    return values


def _frames(
    grafana: Grafana, panel: dict[str, Any], values: dict[str, tuple[str, str]],
    start: datetime, end: datetime,
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


def build_snapshot(grafana: Grafana, now: datetime) -> dict[str, Any]:
    """Dashboard „Prognose aktuell“ (Schema v2) mit eingebetteten Daten der Wochenpanels.

    Grafana 13 lädt Snapshots im alten Schema mit ``snapshotData`` nicht mehr; die Oberfläche
    schickt das Schema v2, in dem jedes Panel eine Abfrage ``queryType: snapshot`` der
    Datenquelle ``grafana`` mit den Frames trägt. Das Schema v2 liefert Grafana selbst
    (``/apis/dashboard.grafana.app/v2beta1``), die Abfragen kommen aus dem alten Schema.
    """
    v1 = grafana.dashboard(DASHBOARD_UID)
    spec = copy.deepcopy(grafana.dashboard_v2(DASHBOARD_UID))
    start, end = week_range(now)
    values = _variable_values(grafana, v1, start, end)
    sources = {f"panel-{p['id']}": p for p in v1["panels"] if p["id"] in PANELS}
    elements = {}
    for name, el in spec["elements"].items():
        if name not in sources:
            continue
        panel = el["spec"]
        panel["title"] = interpolate(panel.get("title", ""), values)
        if panel.get("data"):
            data = panel["data"]["spec"]
            data["queries"] = [{
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
                            "snapshot": _frames(grafana, sources[name], values, start, end),
                        },
                    },
                },
            }]
            data["queryOptions"] = {
                k: v for k, v in data.get("queryOptions", {}).items()
                if k not in ("timeFrom", "timeShift", "hideTimeOverride")
            }
        elements[name] = el
    spec["elements"] = elements
    items = spec["layout"]["spec"]["items"]
    spec["layout"]["spec"]["items"] = [
        i for i in items if i["spec"]["element"]["name"] in elements
    ]
    stamp = now.astimezone(TZ)
    spec.update(
        title=f"Strompreis- und Lastprognose DE-LU · Stand {stamp:%d.%m.%Y %H:%M}",
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
    """Short.io: Ziel eines vorhandenen Links ändern (``POST /links/{id}``)."""

    client: httpx.Client
    api_key: str
    link_id: str

    def point_to(self, url: str) -> None:
        r = self.client.post(
            f"https://api.short.io/links/{self.link_id}",
            headers={"authorization": self.api_key},
            json={"originalURL": url},
        )
        r.raise_for_status()


@dataclass
class Shlink:
    """Shlink (selbst gehostet): ``PATCH /rest/v3/short-urls/{code}``."""

    client: httpx.Client
    base_url: str
    api_key: str
    short_code: str

    def point_to(self, url: str) -> None:
        r = self.client.patch(
            f"{self.base_url.rstrip('/')}/rest/v3/short-urls/{self.short_code}",
            headers={"X-Api-Key": self.api_key},
            json={"longUrl": url},
        )
        r.raise_for_status()


def publish(
    grafana: Grafana, shortlink: Shortlink | None, expires: int, now: datetime | None = None
) -> dict[str, Any]:
    """Ein Durchgang: Snapshot bauen, hochladen, Kurzlink umbiegen."""
    now = now or datetime.now(UTC)
    dash = build_snapshot(grafana, now)
    res = grafana.snapshot(dash, dash["title"], expires)
    url = str(res["url"])
    if shortlink is not None:
        shortlink.point_to(url)
    log.info("snapshot_published", url=url, shortlink=shortlink is not None)
    return {"url": url, "created": now.isoformat(timespec="seconds"), "expires": expires}


def next_run(now: datetime, at: str) -> datetime:
    """Nächster Zeitpunkt ``HH:MM`` Ortszeit nach ``now``."""
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
