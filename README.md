# Ministerien-Job-Monitor

Persönliches Projekt: aktuelle Stellenausschreibungen deutscher Bundes- (und später Landes-)ministerien an einem Ort.

## Prinzip

**Ein Adapter pro Quell-System**, nicht pro Haus. Jedes Ministerium ist ein Eintrag in `registry.yaml` und verweist auf `interamt`, `successfactors` oder `generic_html`.

## Stack

- Crawler: Python, httpx, BeautifulSoup, SQLite
- Schedule: GitHub Actions (täglich)
- Frontend: Next.js (App Router) + Tailwind in `web/`

## Schnellstart

```bash
python3 -m pip install -r requirements.txt
python3 crawler.py -v
```

Daten landen in `data/jobs.sqlite` und `data/jobs.json`.

Frontend:

```bash
cd web
npm install
npm run dev
```

## Interamt

Offizielle REST-API (`gate.interamt.de/interamtApi`, OpenAPI in der API-Doku) braucht `X-API-KEY` und ist mandantenbezogen.

Für öffentliche Embeds nutzen wir den verifizierten Widget-Feed:

`GET https://interamt.de/koop/app/webservice_v2?partner={partner_id}`

Neues Interamt-Haus: Registry-Eintrag mit `type: interamt` und `params.partner_id` — kein neuer Code.

## Akzeptanz

- `python crawler.py` schreibt mehrere Häuser in die DB und bricht bei Einzelfehlern nicht ab
- Interamt-Haus nur per Registry erweiterbar
- Frontend filtert nach Ebene / Land / Ministerium und markiert neue Einträge
