# Ministerien-Job-Monitor

Persönliches Projekt: aktuelle Stellenausschreibungen deutscher Bundes- (und später Landes-)ministerien an einem Ort.

## Prinzip

**Ein Adapter pro Quell-System**, nicht pro Haus. Jedes Ministerium ist ein Eintrag in `crawler/registry.yaml` und verweist auf `interamt`, `successfactors` oder `generic_html`.

## Stack

- Crawler: Python in `crawler/` (httpx, BeautifulSoup, SQLite)
- Schedule: GitHub Actions (täglich)
- Frontend: Next.js (App Router) + Tailwind im Repo-Root (für Vercel)

## Schnellstart

```bash
cd crawler
python3 -m pip install -r requirements.txt
python3 crawler.py -v --db ../data/jobs.sqlite --json ../data/jobs.json
```

Daten landen in `data/jobs.sqlite` und `data/jobs.json`.

Frontend:

```bash
npm install
npm run dev
```

## Vercel

Das Frontend liegt im Repo-Root (`package.json`, `app/`). Der Python-Crawler unter `crawler/`, damit Vercel nicht fälschlich ein Python-Projekt erkennt.

## Interamt

Offizielle REST-API (`gate.interamt.de/interamtApi`) braucht `X-API-KEY` und ist mandantenbezogen.

Für öffentliche Embeds nutzen wir den verifizierten Widget-Feed:

`GET https://interamt.de/koop/app/webservice_v2?partner={partner_id}`

Neues Interamt-Haus: Registry-Eintrag mit `type: interamt` und `params.partner_id` — kein neuer Code.
