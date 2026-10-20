# Beiträge zu PredictionDB

Beiträge sind willkommen. Bevor ein Beitrag übernommen werden kann, müssen die
folgenden Bedingungen akzeptiert werden. Sie stellen sicher, dass das Projekt
seinen Lizenzweg (Business Source License 1.1, später AGPL-3.0-or-later sowie
kommerzielle Lizenzen) rechtssicher gehen kann.

*An English summary follows the German text. The German version is authoritative.*

## Beitragsbedingungen

**Begriffe.** „Projektinhaber“ ist Daniel (the78mole) Glaser sowie jeder
Rechtsnachfolger, auf den die Rechte am Projekt übergehen (siehe Abschnitt 4).
„Beitrag“ ist jedes Werk, das Du dem Projekt zur Aufnahme anbietest, etwa Code,
Dokumentation, Konfiguration, Tests oder Daten, insbesondere über Pull Requests,
Patches, Issues oder Kommentare im Repository.

### 1. Annahme der Lizenz

Mit dem Einreichen eines Beitrags erklärst Du Dich damit einverstanden, dass
Dein Beitrag Teil des Licensed Work wird und unter der jeweils für das Projekt
geltenden Lizenz verbreitet wird, derzeit der Business Source License 1.1
(siehe [LICENSE](LICENSE)) mit der dort genannten Change License.

### 2. Einräumung von Nutzungsrechten

Du räumst dem Projektinhaber an Deinem Beitrag ein einfaches, weltweites,
zeitlich unbegrenztes, unwiderrufliches, unentgeltliches und
unterlizenzierbares Recht ein, den Beitrag auf alle bekannten und unbekannten
Nutzungsarten zu nutzen. Das umfasst insbesondere das Recht, ihn zu
vervielfältigen, zu bearbeiten, zu verbreiten, öffentlich zugänglich zu machen
und als Dienst zu betreiben, und zwar

- unter der jeweils geltenden Projektlizenz,
- unter jeder späteren Lizenz, auch einer anderen Open-Source-Lizenz, und
- unter kommerziellen oder proprietären Lizenzen, die der Projektinhaber Dritten
  erteilt.

Du behältst das Urheberrecht an Deinem Beitrag und darfst ihn auch selbst
weiterhin beliebig nutzen.

### 3. Patente

Soweit Dir Patente zustehen, die durch Deinen Beitrag allein oder in der
Kombination mit dem Projekt verletzt würden, räumst Du dem Projektinhaber und
allen Empfängern der Software eine weltweite, unentgeltliche, unwiderrufliche
Patentlizenz zur Nutzung des Beitrags im Rahmen des Projekts ein.

### 4. Übertragbarkeit

Der Projektinhaber darf die eingeräumten Rechte ganz oder teilweise auf Dritte
übertragen, insbesondere auf eine Gesellschaft, die das Projekt fortführt (zum
Beispiel die Bizzmark UG (haftungsbeschränkt)). Die Rechte gelten dann
unverändert gegenüber dem Rechtsnachfolger.

### 5. Zusicherungen

Du sicherst zu, dass

- der Beitrag Dein eigenes Werk ist oder Du die nötigen Rechte daran hast, um
  die obigen Rechte einzuräumen,
- ein Arbeitgeber oder Auftraggeber, der Rechte an Deinem Beitrag haben könnte,
  zugestimmt hat oder auf diese Rechte verzichtet hat, und
- Du Code, Daten oder andere Inhalte Dritter im Beitrag kenntlich machst,
  zusammen mit deren Herkunft und Lizenz.

Abgesehen davon wird der Beitrag „wie besehen“ bereitgestellt, ohne
Gewährleistung, soweit gesetzlich zulässig.

### 6. Namensnennung

Der Projektinhaber ist nicht verpflichtet, Deinen Beitrag aufzunehmen. Du
verzichtest auf eine Urhebernennung über die Git-Historie hinaus, soweit das
gesetzlich zulässig ist.

## So bestätigst Du die Bedingungen

Jeder Commit trägt eine `Signed-off-by`-Zeile mit Deinem Namen und Deiner
E-Mail-Adresse. Damit bestätigst Du die Beitragsbedingungen für diesen Commit:

```sh
git commit -s -m "..."
```

Zusätzlich hakst Du in der Beschreibung des Pull Requests die Bestätigung der
Beitragsbedingungen ab.

## Entwicklung

Wie die Umgebung aufgesetzt wird und welche Prüfungen vor einem Pull Request
laufen sollen, steht in der [README](README.md#entwicklung). Kurz gefasst:

```sh
uv sync --all-packages
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest
```

---

## English summary

By submitting a contribution (code, documentation, tests, data or other
material) you agree that

1. your contribution becomes part of the Licensed Work and is distributed under
   the project's current license (Business Source License 1.1, see
   [LICENSE](LICENSE)) and its Change License;
2. you grant the project owner, Daniel (the78mole) Glaser, and any successor in
   title a non-exclusive, worldwide, perpetual, irrevocable, royalty-free,
   sublicensable license to use, modify, distribute and relicense your
   contribution under the project license, any later license including other
   open source licenses, and commercial or proprietary licenses;
3. you grant a matching patent license for patents you hold that read on your
   contribution;
4. the project owner may transfer these rights, for example to a company
   continuing the project (such as Bizzmark UG (haftungsbeschränkt));
5. the contribution is your own work or you have the right to grant these
   rights, including consent from any employer, and you identify any
   third-party material and its license.

You keep the copyright in your contribution. Confirm these terms by signing off
every commit (`git commit -s`) and by ticking the confirmation in the pull
request description.
