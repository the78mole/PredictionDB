"""Grafana-Snapshot: Zeitraum, Variablen, Aufbau im Schema v2, Kurzlink."""

from datetime import UTC, datetime
from typing import Any

import httpx

from wx_exchange import snapshot


def test_week_range_spans_dst_change() -> None:
    # 25.10.2026: Ende der Sommerzeit, der Bereich hat dann 8 Tage + 1 Stunde
    start, end = snapshot.week_range(datetime(2026, 10, 24, 22, 30, tzinfo=UTC))
    assert start.isoformat() == "2026-10-25T00:00:00+02:00"
    assert end.isoformat() == "2026-11-02T00:00:00+01:00"
    # gleiche tzinfo: Python zieht Wanduhren ab, daher über UTC
    assert (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() == 8 * 86400 + 3600


def test_next_run_today_or_tomorrow() -> None:
    morning = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)  # 10:00 Ortszeit
    assert snapshot.next_run(morning, "14:30").isoformat() == "2026-10-30T14:30:00+01:00"
    evening = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
    assert snapshot.next_run(evening, "14:30").isoformat() == "2026-10-31T14:30:00+01:00"


def test_interpolate_value_and_text() -> None:
    vals = {"challenger": ("v-123", "de_lu_v2 frei 29.10.")}
    sql = "p.model_version = '${challenger}' OR x = $challenger"
    assert snapshot.interpolate(sql, vals) == "p.model_version = 'v-123' OR x = v-123"
    assert snapshot.interpolate("· ${challenger:text}", vals) == "· de_lu_v2 frei 29.10."


def _frame(*names: str) -> dict[str, Any]:
    return {
        "schema": {"fields": [{"name": n, "type": "string"} for n in names]},
        "data": {"values": [["v-123"], ["Challenger X"]]},
    }


class FakeGrafana(snapshot.Grafana):
    def __init__(self) -> None:
        super().__init__("http://grafana", httpx.Client(), "admin", "")
        self.queries: list[dict[str, Any]] = []

    def dashboard(self, uid: str) -> dict[str, Any]:
        ds = {"type": "grafana-postgresql-datasource", "uid": "pdb-postgres"}
        return {
            "templating": {"list": [{"name": "challenger", "datasource": ds, "query": "SELECT"}]},
            "panels": [
                {
                    "id": 69,
                    "datasource": ds,
                    "title": "Woche · Challenger ${challenger:text}",
                    "targets": [{"refId": "B", "rawSql": "WHERE v = '${challenger}'"}],
                },
                {"id": 59, "datasource": ds, "title": "Heute", "targets": []},
            ],
        }

    def dashboard_v2(self, uid: str) -> dict[str, Any]:
        def el(title: str) -> dict[str, Any]:
            return {
                "kind": "Panel",
                "spec": {
                    "title": title,
                    "data": {
                        "kind": "QueryGroup",
                        "spec": {
                            "queries": [],
                            "queryOptions": {"timeFrom": "now/d", "maxDataPoints": 500},
                        },
                    },
                },
            }

        def item(name: str) -> dict[str, Any]:
            return {"kind": "GridLayoutItem", "spec": {"element": {"name": name}}}

        return {
            "title": "Prognose aktuell",
            "elements": {
                "panel-69": el("Woche · Challenger ${challenger:text}"),
                "panel-59": el("Heute"),
            },
            "layout": {
                "kind": "GridLayout",
                "spec": {"items": [item("panel-69"), item("panel-59")]},
            },
            "variables": [{"kind": "QueryVariable"}],
            "links": [{"title": "intern"}],
            "timeSettings": {"from": "now/d", "to": "now+7d/d", "autoRefresh": "5m"},
        }

    def snapshots(self) -> list[dict[str, Any]]:
        stand = "Strompreis- und Lastprognose DE-LU · Stand"
        return [
            {
                "name": f"{stand} 29.10.2026 14:30",
                "external": True,
                "externalUrl": "https://snapshots.raintank.io/dashboard/snapshot/alt",
                "created": "2026-10-29T13:30:00Z",
                "expires": "2026-11-28T13:30:00Z",
            },
            {
                "name": f"{stand} 01.10.2026 14:30",
                "external": True,
                "externalUrl": "https://snapshots.raintank.io/dashboard/snapshot/abgelaufen",
                "created": "2026-09-30T12:30:00Z",
                "expires": "2026-10-30T12:30:00Z",
            },
            {
                "name": "Etwas anderes",
                "external": True,
                "externalUrl": "https://x",
                "created": "2026-10-29T10:00:00Z",
                "expires": "2026-11-28T10:00:00Z",
            },
        ]

    def query(
        self, targets: list[dict[str, Any]], start: datetime, end: datetime
    ) -> dict[str, Any]:
        self.queries.extend(targets)
        t = targets[0]
        if t["refId"] == "V":
            return {"V": {"frames": [_frame("__value", "__text")]}}
        return {
            t["refId"]: {
                "frames": [{"schema": {"fields": [{"name": "Time"}]}, "data": {"values": [[1]]}}]
            }
        }


def test_build_snapshot_embeds_frames_in_v2() -> None:
    g = FakeGrafana()
    spec = snapshot.build_snapshot(g, datetime(2026, 10, 30, 13, 30, tzinfo=UTC))
    assert list(spec["elements"]) == ["panel-69", "panel-staende"]
    names = [i["spec"]["element"]["name"] for i in spec["layout"]["spec"]["items"]]
    assert names == ["panel-69", "panel-staende"]
    # frühere Stände: nur eigene, nicht abgelaufene, mit Link und Ablaufdatum
    staende = spec["elements"]["panel-staende"]["spec"]["vizConfig"]["spec"]["options"]
    assert staende["content"].splitlines()[2:] == [
        "| [Do 29.10.2026 14:30](https://snapshots.raintank.io/dashboard/snapshot/alt)"
        " | 28.11.2026 |"
    ]
    panel = spec["elements"]["panel-69"]["spec"]
    assert panel["title"] == "Woche · Challenger Challenger X"
    data = panel["data"]["spec"]
    assert data["queryOptions"] == {"maxDataPoints": 500}
    q = data["queries"][0]["spec"]["query"]
    assert q["datasource"] == {"name": "grafana"}
    assert q["spec"]["queryType"] == "snapshot"
    assert q["spec"]["snapshot"][0]["schema"]["fields"][0]["config"] == {}
    assert g.queries[-1]["rawSql"] == "WHERE v = 'v-123'"
    assert spec["variables"] == [] and spec["links"] == []
    assert spec["timeSettings"]["from"] == "2026-10-29T23:00:00.000Z"
    assert spec["timeSettings"]["to"] == "2026-11-06T22:59:59.999Z"
    assert spec["timeSettings"]["hideTimepicker"] is True
    assert spec["title"].endswith("Stand 30.10.2026 14:30")


def test_publish_points_shortlink_at_snapshot() -> None:
    class G(FakeGrafana):
        def snapshot(self, dashboard: dict[str, Any], name: str, expires: int) -> dict[str, Any]:
            assert expires == 3600
            return {"url": "https://snapshots.raintank.io/dashboard/snapshot/abc"}

    class Link:
        target = ""

        def point_to(self, url: str) -> None:
            self.target = url

    link = Link()
    out = snapshot.publish(G(), link, 3600, datetime(2026, 10, 30, 13, 30, tzinfo=UTC))
    assert link.target == out["url"] == "https://snapshots.raintank.io/dashboard/snapshot/abc"


def test_shortio_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/links/expand":
            return httpx.Response(200, json={"idString": "lnk_1"})
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snapshot.ShortIo(client, "sk_x", "go.example.org", "strom").point_to("https://e/s")
        snapshot.Shlink(client, "https://s.example.org/", "k", "strom").point_to("https://e/s")
    assert seen[0].url.params["domain"] == "go.example.org"
    assert seen[0].url.params["path"] == "strom"
    assert seen[1].url == "https://api.short.io/links/lnk_1"
    assert seen[1].headers["authorization"] == "sk_x"
    assert b'"originalURL"' in seen[1].content
    assert seen[2].method == "PATCH"
    assert seen[2].url == "https://s.example.org/rest/v3/short-urls/strom"
    assert seen[2].headers["x-api-key"] == "k"


def test_shortio_creates_missing_link() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/links/expand":
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"idString": "lnk_2"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        snapshot.ShortIo(client, "sk_x", "go.example.org", "strom").point_to("https://e/s")
    assert [r.method for r in seen] == ["GET", "POST"]
    assert seen[1].url == "https://api.short.io/links"
    assert b'"path":"strom"' in seen[1].content.replace(b" ", b"")
