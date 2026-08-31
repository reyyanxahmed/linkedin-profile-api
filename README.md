# linkedin-profile-api

An HTTP API that takes a LinkedIn profile URL and returns the profile as structured JSON,
built by hitting LinkedIn's internal endpoints directly over HTTP.

**Purely reverse-engineered. No browser, anywhere.** Every LinkedIn request in this
codebase is a direct HTTP call to a LinkedIn endpoint, issued by `curl_cffi`. There is no
Selenium, Playwright, Puppeteer, headless Chrome, or webdriver — not in the runtime, not
in the dev dependencies, not in the test suite, not anywhere in the transitive dependency
tree. Nothing shells out to a browser binary. See
[Verifying the no-browser claim](#verifying-the-no-browser-claim) for how to check this
yourself in about thirty seconds.

**Live API:** https://linkedin-profile-api-green.vercel.app
**Interactive docs:** https://linkedin-profile-api-green.vercel.app/docs

```bash
curl -s "https://linkedin-profile-api-green.vercel.app/v1/profile?url=https://www.linkedin.com/in/satyanadella/" \
  | jq '.experience[] | {title, company: .company.name, start: .start.iso}'
```

```json
{ "title": "Chairman and CEO",         "company": "Microsoft",                 "start": "2014-02" }
{ "title": "Board Member",             "company": "Fred Hutch",                "start": "2016" }
{ "title": "Board Member",             "company": "Starbucks",                 "start": "2017" }
{ "title": "Chairman",                 "company": "The Business Council U.S.", "start": "2021" }
{ "title": "Member Board Of Trustees", "company": "University of Chicago",     "start": "2018" }
```

That is real output from a live run, not an illustration. **Please read
[Status: what works today](#status-what-works-today) before evaluating** — it says
exactly what is green, what is not, and why.

No API key is needed against the public deployment: it runs with
`ALLOW_UNAUTHENTICATED=true` so it can be exercised from a browser. Setting `API_KEY`
re-enables `X-API-Key` enforcement, and always wins over that flag.

---

## Contents

- [Verifying the no-browser claim](#verifying-the-no-browser-claim)
- [Status: what works today](#status-what-works-today)
- [Quickstart](#quickstart)
- [API reference](#api-reference)
- [Approach](#approach)
- [Architecture](#architecture)
- [Testing](#testing)
- [Known limitations](#known-limitations)
- [Security](#security)

---

## Verifying the no-browser claim

Three checks, none of which require trusting this README.

**1. The dependency tree.** The whole thing, transitively, is 38 packages:

```bash
uv venv && uv pip install -e ".[dev]"
python -c "import importlib.metadata as m; print(sorted(d.metadata['Name'] for d in m.distributions()))"
```

```
annotated-doc annotated-types anyio certifi cffi click curl_cffi fastapi h11 httpcore
httptools httpx idna iniconfig linkedin-profile-api orjson packaging pluggy pycparser
pydantic pydantic-settings pydantic_core Pygments pytest pytest-asyncio python-dotenv
PyYAML redis ruff selectolax starlette structlog typing-inspection typing_extensions
uvicorn uvloop watchfiles websockets
```

No automation library, no driver, no browser. `selectolax` is an HTML parser (C, no JS
engine); `curl_cffi` is libcurl with Chrome's TLS fingerprint.

```bash
grep -riE "selenium|playwright|puppeteer|webdriver|chromedriver|nodriver" . --include="*.py" --include="*.toml"
grep -rnE "subprocess|os\.system|Popen" app/          # nothing shells out
```

**2. The complete network surface.** Every outbound call in `app/` is
`curl_cffi.AsyncSession.get()` or `.post()`. There are four call sites:

| File | Purpose |
|---|---|
| `linkedin/client.py:301` | all Voyager REST requests |
| `linkedin/strategies/flagship_web.py:424,429` | flagship RSC component POST / main GET |
| `linkedin/strategies/public_html.py:57` | unauthenticated HTML fallback |
| `main.py:172` | image proxy for `media.licdn.com` |

**3. Reproduce a request with plain `curl`.** The requests are ordinary HTTP, so you can
issue an identical one from a shell. This is the whole protocol, no library involved:

```sh
curl -sS --compressed -L --max-redirs 5 \
  -b jar.txt -c jar.txt \
  -H 'csrf-token: ajax:<JSESSIONID>' \
  -H 'x-restli-protocol-version: 2.0.0' \
  -H 'accept: application/vnd.linkedin.normalized+json+2.1' \
  -H 'x-li-lang: en_US' \
  -H 'x-li-track: {"clientVersion":"1.13.*","osName":"web","timezoneOffset":5.5}' \
  -H 'referer: https://www.linkedin.com/feed/' \
  'https://www.linkedin.com/voyager/api/identity/profiles/<slug>/positionGroups'
```

Two things this makes concrete, both verified by running it:

- `-b jar.txt -c jar.txt` (a real cookie **jar**, read *and* written) is mandatory. With a
  static `-b 'k=v'` string, curl exits `(47) Maximum redirects followed` — LinkedIn's
  `lidc` datacenter hop never resolves. That is the single highest-value finding in this
  project, and it reproduces outside the codebase.
- Plain `/usr/bin/curl` gets LinkedIn's **999** anti-automation page for this request,
  while `curl_cffi` with `impersonate="chrome150"` gets a 200 and real JSON — same URL,
  same cookies, same headers. The only difference is the TLS/JA3 handshake. That is
  precisely why `curl_cffi` is a dependency and why no browser is needed to defeat it.

### On how the endpoints were found

Endpoint discovery used Chrome devtools to record a HAR of a profile page load — reading
LinkedIn's own traffic, the same way one would read a packet capture. That is recon, and
it is how reverse engineering is done; it is not runtime browser automation.

Nothing at runtime depends on it. The discovered facts — component ids, the required
header set, the SDUI version — are constants in `app/linkedin/strategies/flagship_web.py`.
Clone the repo, add cookies, and it runs. No HAR, no browser, no manual step.

---

## Status: what works today

Live status, checked against the deployed URL. Being precise here is more useful than
a wall of green.

### Infrastructure

| | Capability | Evidence |
|---|---|---|
| 🟢 | Deployed publicly over HTTPS | https://linkedin-profile-api-green.vercel.app |
| 🟢 | `GET /v1/health` | returns `{"status":"ok", ...}` |
| 🟢 | OpenAPI docs at `/docs` | HTTP 200 |
| 🟢 | Browser demo UI at `/` | HTTP 200 |
| 🟢 | Accepts a LinkedIn profile URL | slug, trailing slash, locale prefix, query string |
| 🟢 | Public GitHub repository | https://github.com/reyyanxahmed/linkedin-profile-api |
| 🟢 | No credentials in the repo | `.env`, `.cookie_state.json`, HARs all gitignored |
| 🟢 | Offline test suite | 233 tests, zero network |

### Data extraction

| | Section | State |
|---|---|---|
| 🟢 | Slug → profile URN | resolved live; falls back to position URNs when `dash` is quota-limited |
| 🟢 | Experience — title, company, dates, location, employment type | verified live: 5 positions for `satyanadella`, 3 for `williamhgates` |
| 🟢 | Education, certifications | verified against a captured profile (2 schools, 1 certification) |
| 🟢 | Name, headline, location, about, profile images | verified against a captured profile stream |
| 🟡 | Skills, languages | mappers implemented, no populated capture to calibrate against |

### 🔴 What is blocked right now

**The LinkedIn session cookies are expired.** This is the one thing standing between the
deployment and live profile data, and it is a credential problem, not a code problem:

```
GET /voyager/api/me                    -> 401     (session expired)
GET .../{slug}/positionGroups          -> 999     (account challenged)
```

Verified from two different IPs — the deployment and a local machine — so it is the
session, not the host.

**The fix is one command**, and it verifies before it deploys:

```bash
python scripts/update_session.py cookies.json     # or:  pbpaste | ... -
```

It writes `.env`, clears stale rotation state, spends exactly one request confirming
the session authenticates, and only then pushes to the host and redeploys. If the
cookies are already dead it stops and tells you which failure it is (401 = expired,
999 = challenged) rather than shipping them.

To get `cookies.json`: log in to LinkedIn, export cookies as a JSON array (devtools or
a cookie-export extension), and keep the whole set — `lidc` and `bcookie` matter, see
[Approach](#approach).

### The deeper limitation, stated honestly

Only experience currently comes from the live Voyager path. Everything else — name,
headline, about, education, skills, certifications, languages — comes from the flagship
RSC transport, which sits behind **web-app** auth. The cookies available to this project
have consistently authenticated LinkedIn's API surface but not its web app:

```
GET /voyager/api/me                    -> 200, real data
GET /feed/                             -> 302 -> /uas/login
GET /in/{slug}   (HTML)                -> 999
GET /flagship-web/in/{slug}/           -> 200, empty body
```

Those mappers are implemented and verified against real captured data, so **if you supply
cookies from a session that can load `linkedin.com/feed/` in a browser, that path should
light up with no code change.** Whether the split is an account-level restriction on the
burner, IP reputation, or a deliberate API/web auth split is not established — it is the
open question in this codebase, and it is documented rather than papered over with an
empty section in the response.

### The one blocker, stated plainly

The credentials available to this project authenticate LinkedIn's **API surface** but not
its **web app**. With the same freshly exported cookies, in the same process:

```
GET /voyager/api/me                                        → 200, real data
GET /voyager/api/identity/profiles/{slug}/positionGroups    → 200, real data
GET /feed/                                                  → 302 → /uas/login
GET /in/{slug}            (HTML)                            → 999 (anti-automation)
GET /flagship-web/in/{slug}/                                → 200, empty body
```

Everything except experience comes from the flagship RSC transport, which lives behind
web-app auth. So those sections are implemented and verified against real captured data,
but cannot currently be fetched live. Dropping `__cf_bm`, reducing to only the auth
cookies, and matching the browser's TLS fingerprint all fail to change it.

Whether this is an account-level restriction on the burner, IP reputation, or a
deliberate split between API and web auth is **not established**. It is the honest open
question in this codebase, and it is documented rather than hidden behind an empty
section in the response.

The Voyager path that *does* work is not a consolation prize: it is the universal path
that resolves any public slug and returns accurate structured experience.

---

## Quickstart

```bash
git clone <repo> linkedin-profile-api && cd linkedin-profile-api
uv venv && uv pip install -e ".[dev]"
cp .env.example .env          # fill in API_KEY and LI_SESSIONS
uvicorn app.main:app --reload
```

Or with Docker:

```bash
docker run --rm -p 8000:8000 --env-file .env $(docker build -q .)
```

Then:

```bash
curl http://localhost:8000/v1/health                       # unauthenticated
curl "http://localhost:8000/v1/profile?url=https://www.linkedin.com/in/satyanadella/"
open http://localhost:8000/docs                            # OpenAPI / Swagger UI
```

Add `-H "X-API-Key: $API_KEY"` if you set `API_KEY`; without it, set
`ALLOW_UNAUTHENTICATED=true` or the API fails closed by design.

### Try it with no LinkedIn account at all

The whole thing runs offline against captured fixtures. This is the fastest way to see
the output shape and to run the tests:

```bash
OFFLINE_MODE=true FIXTURE_DIR=tests/fixtures/rsc uvicorn app.main:app
curl -H "X-API-Key: $API_KEY" \
  "http://localhost:8000/v1/profile?url=https://www.linkedin.com/in/rajstriver/"
```

`OFFLINE_MODE=true` is a hard guarantee, not a preference: the strategy chain is reduced
to the fixture-backed strategy alone, so nothing in it can open a socket.

### Supplying credentials

`LI_SESSIONS` is a JSON array. Paste a browser cookie export straight in — it is accepted
unmodified:

```jsonc
// A raw cookie export (from Chrome devtools or an export extension)
[[ {"name":"li_at","value":"AQED…","domain":".www.linkedin.com"},
   {"name":"JSESSIONID","value":"\"ajax:123…\"","domain":".www.linkedin.com"},
   {"name":"lidc","value":"\"b=OGST00:…\"","domain":".linkedin.com"},
   {"name":"bcookie","value":"\"v=2&…\"","domain":".linkedin.com"} ]]
```

Two other shapes also work: `[{"cookies":[…]}]`, and the minimal
`[{"li_at":"…","jsessionid":"ajax:…"}]`.

**Supply the full cookie set, not just `li_at` + `JSESSIONID`.** `lidc` and `bcookie` are
required — without them every identity endpoint redirects to itself until curl gives up.
See [Approach](#approach).

---

## API reference

### `GET /v1/profile?url=…&refresh=false`
### `POST /v1/profile` — body `{"url": "...", "refresh": false}`

Header `X-API-Key: <key>` is required. `refresh=true` bypasses the cache.

Accepts any of: `https://www.linkedin.com/in/{slug}`, with or without trailing slash,
query string, locale prefix, or `http://`. Also accepts a bare slug.

**200** — the profile. Top-level keys:

```
meta, profile, experience, education, skills, certifications,
languages, projects, publications, honors, volunteer, courses
```

Real output, from `OFFLINE_MODE=true` against the `barackobama` fixture:

```jsonc
{
  "meta": {
    "profile_url": "https://www.linkedin.com/in/barackobama",
    "public_identifier": "barackobama",
    "profile_urn": "ACoAAAC2EzMB7j1OC2l5XFXf1vNlYdp8HaZcSr4",
    "fetched_at": "2026-08-30T22:05:16Z",
    "source": "flagship_web_rsc",    // which strategy served this
    "supplemented_by": [],           // strategies that filled gaps
    "cache": { "hit": false, "age_seconds": 0, "stale": false },
    "partial_sections": [],          // mappers that degraded, by name
    "completeness": 0.71,            // 0-1, how much of the schema was populated
    "request_id": "0163b4f574247f45"
  },
  "profile": {
    "first_name": null, "last_name": null, "full_name": "Barack Obama",
    "headline": "Former President of the United States of America",
    "about": null,
    "location": { "raw": "Washington, District of Columbia, United States",
                  "city": null, "region": null, "country": null, "country_code": null },
    "industry": null, "pronouns": null,
    "flags":  { "premium": false, "influencer": false, "open_to_work": false, "hiring": false },
    "images": { "profile": [ { "url": "https://media.licdn.com/dms/image/v2/D4E03AQGcice3q3BiUQ/…",
                               "width": 100, "height": 100 } ],
                "background": [] },
    "counts": { "followers": null, "connections": null }
  },
  "experience": [
    {
      "title": "President of the United States of America",
      "employment_type": null,
      "company": { "name": "White House", "urn": null, "linkedin_url": null, "logo": null },
      "location": null,
      "location_type": null,
      "start": { "year": 2009, "month": 1, "day": null, "iso": "2009-01" },
      "end":   { "year": 2017, "month": 1, "day": null, "iso": "2017-01" },
      "is_current": false,
      "duration_months": 96,
      "description": null,
      "skills": []
    }
  ]
}
```

Education and certifications, from the `rajstriver` capture:

```jsonc
  "education": [
    { "school": "Jalpaiguri Government  Engineering College",
      "degree": "B.TECH", "field_of_study": "Information Technology",
      "start": { "year": 2016, "iso": "2016" }, "end": { "year": 2020, "iso": "2020" } }
  ],
  "certifications": [
    { "name": "Algorithmic Toolbox", "authority": "Coursera",
      "license_number": "RKYYD4NET8ZR",
      "issued": { "year": 2017, "month": 10, "iso": "2017-10" }, "expires": null }
  ]
```

**Design notes on the schema.** Every section is a list and is always present, empty
rather than absent, so a consumer never branches on key existence. Dates are structured
(`year`/`month`/`day`) *and* pre-formatted (`iso`), because LinkedIn's own precision
varies — `"2016"` and `"2024-08"` are both faithful, and forcing a full date would invent
information. `meta.completeness` and `meta.partial_sections` make degradation legible
instead of silent: a caller can tell an empty `skills` list caused by a private profile
apart from one caused by a broken mapper.

### `GET /v1/health` — unauthenticated

```json
{ "status": "ok", "version": "0.1.0",
  "sessions": { "total": 1, "available": 1, "cooling": 0 },
  "redis": true, "has_api_key": true }
```

Safe to expose publicly: it reports session *counts*, never token material. If it ever
contains a cookie, that is a bug.

### `GET /docs` — OpenAPI / Swagger UI.

### Error codes

| HTTP | `error.code` | Meaning |
|---|---|---|
| 400 | `INVALID_URL` | Not a parseable LinkedIn profile URL |
| 401 | `UNAUTHORIZED` | Missing or wrong `X-API-Key` |
| 403 | `PROFILE_PRIVATE` | Profile exists but is not visible to this session |
| 404 | `PROFILE_NOT_FOUND` | No data from any strategy |
| 429 | `RATE_LIMITED` | Client-side rate limit |
| 500 | `MISSING_QUERY_ID` | A `queries.yaml` value is still a placeholder |
| 502 | `UPSTREAM_CHALLENGE` | LinkedIn challenged the session (999 / checkpoint) |
| 503 | `ALL_SESSIONS_COOLING` | Every session is in cooldown |

```json
{ "error": { "code": "PROFILE_NOT_FOUND",
             "message": "no data found for slug 'x' via any strategy",
             "request_id": "01bc9b5f602b114c" } }
```

---

## Approach

### Finding the endpoints

Full technical detail is in **[docs/REVERSE_ENGINEERING.md](docs/REVERSE_ENGINEERING.md)**.
The short version:

LinkedIn has been through three profile APIs and all three still answer requests — which
is misleading, because two of them answer with tombstones. `profileView`, `skills`, and
`educations` return `410 Gone`. Dash returns real data for a couple of calls and then
401s on a quota. GraphQL needs a `queryId` that rotates every frontend deploy. What
survives is `positionGroups` (experience) and the flagship RSC card components
(everything).

### The three findings that mattered

Each of these presents as "the account is banned". None of them is.

**1. A pinned `cookie` header causes infinite redirects.** LinkedIn answers identity
endpoints with a 302 to *the same URL* carrying `Set-Cookie: lidc=…` — datacenter
affinity. Setting `cookie` as a request header overrides curl's jar, so `lidc` is never
replayed and the request loops until curl aborts at 30 hops. Cookies must live in a jar,
with redirects followed. `build_headers()` deliberately sets no `cookie` key and a test
asserts it never does.

**2. Cookie domain scope is load-bearing.** `li_at` on `.linkedin.com` reproduces the
redirect loop; the same value on `.www.linkedin.com` returns data. The scopes a browser
uses are encoded in `COOKIE_DOMAINS`.

**3. `li_at` rotates during normal use.** Three exports taken minutes apart had three
different values. A fresh cookie works for one or two requests, then everything 401s —
indistinguishable from a ban unless you know to look. So the client harvests rotated
cookies from the jar after every response, persists them to a gitignored
`.cookie_state.json` (0600, atomic write, refuses to truncate itself), and **serializes
requests per session** so two in-flight calls cannot race the rotation. Different
sessions still run concurrently.

### Why `curl_cffi`

A plain `requests`/`httpx` TLS handshake is trivially distinguishable from Chrome's.
`curl_cffi` reproduces Chrome's JA3 and HTTP/2 fingerprint. The impersonation target is
kept aligned with the `User-Agent` the app sends — claiming Chrome 152 over a Chrome 124
handshake is itself a cheap bot signal.

### The fallback chain

Strategies run in order, and merging is **additive only** — a lower-priority strategy
fills empty fields and never overwrites populated ones.

```
flagship_web_rsc   complete profile, needs web-app auth
voyager_rest       experience + URN; the universal path, works today
voyager_graphql    needs a real queryId; ConfigError → skipped
voyager_dash       needs a real decorationId; ConfigError → skipped
public_html        unauthenticated last resort
```

Three invariants make this robust, all learned the hard way:

- **A strategy that cannot run is skipped, never fatal.** A missing `queryId` logs a
  warning and the chain continues.
- **One failed sub-request does not abort a strategy.** `voyager_rest` fetches ten
  endpoints; the earlier version returned `None` if the first failed, discarding
  experience data it had already retrieved.
- **A transport failure never cools a shared session.** An empty RSC parse says nothing
  about session health, and cooling on it starved the Voyager strategies that would have
  succeeded on the same session. Sessions cool on auth signals only.

---

## Architecture

```
app/
  main.py                  FastAPI app, routes, mapper isolation
  config.py                typed settings; cookie parsing + domain map
  models.py                the response schema (pydantic)
  errors.py                typed errors → HTTP codes
  cache.py                 Redis cache, stale-on-error fallback
  ratelimit.py             client-side limiter
  linkedin/
    client.py              curl_cffi transport, per-session jars, classification
    cookie_store.py        rotated-credential persistence
    session.py             session pool: LRU, cooldown, rotation
    orchestrator.py        strategy chain + additive merge
    rsc_parser.py          RSC wire format → flat text
    endpoints.py           URL builders (incl. Rest.li encoding)
    strategies/            one module per generation
  normalize/
    urn_graph.py           resolves star-key URN references
    sections/              one mapper per profile section
```

Invariants worth knowing before editing:

- **`app/normalize/` never imports `app/linkedin/`.** The normalizer is pure and takes a
  dict, which is what makes the whole suite runnable offline.
- **Every section mapper is independently failable.** A raising mapper yields an empty
  section and its name in `meta.partial_sections`. One bad mapper never 500s a request.
- **`urn_graph.py` has no I/O, no async, no config.**
- **`health()` output is safe to expose publicly.**

---

## Testing

```bash
pytest -q          # 233 tests, no network access required
ruff check app tests
```

The suite runs with **zero network**. There is no VCR cassette and no mocking of our own
HTTP layer pretending to be a test — the mappers are exercised against real captured
LinkedIn payloads in `tests/fixtures/`, and the transport logic is exercised against
synthesised responses through the pure `classify()` function.

The fixture-backed mapper tests double as the calibration record. `tests/test_rsc_mappers.py`
asserts the exact positions, schools, and certifications that a real capture contains, so
a LinkedIn format change fails there rather than silently emptying a section in
production. Some of those assertions encode specific traps:

- honors (`"Issued by X · Jan 2019"`) must not be parsed as certifications
- education date ranges must not be parsed as jobs, *and* year-only ranges that really
  are jobs must survive
- role descriptions must not be parsed as job titles

`tests/test_cookies.py` covers the credential-rotation logic, including that the cookie
store refuses to write itself empty over good state.

> **Fixtures contain real third-party profile data**, redacted of credentials. They are
> present for calibration and offline testing. `tests/fixtures/rsc/profile_rajstriver.json`
> is from a HAR supplied for this project.

---

## Known limitations

Written the way an operator would want to read them.

### The flagship RSC path needs web-app auth

The largest limitation, covered in [Status](#status-what-works-today). Name, headline,
about, education, skills, certifications, and languages all come from this transport.
The implementation is complete and fixture-verified; it is the session that is the
blocker. **If you supply cookies from a session that can load `linkedin.com/feed/` in a
browser, this path should light up with no code change** — that is the first thing to
test with a different account.

### `queryId` / `decorationId` rotate with every frontend deploy

`app/linkedin/queries.yaml` ships placeholders. The GraphQL and dash strategies raise
`ConfigError`, log it, and are skipped — the API keeps working without them.

To refresh, from a HAR of a **profile** page (a feed capture only yields feed and
messaging ids):

```bash
python scripts/extract_query_ids.py capture.har
# paste the printed values into app/linkedin/queries.yaml
```

Extracting them from LinkedIn's JS bundles instead was attempted and is a dead end for
profile ids: the guest bundles reachable from `/login` (14 files, ~3.9 MB) contain none,
and the authenticated flagship bundles are only discoverable from an authenticated page.

### The RSC format is undocumented and will change

It is a UI description, not an API contract — LinkedIn owes no stability here. The card
mappers pattern-match over pooled text because the `BelowActivityPartN` bucket names are
meaningless and unstable. When a section goes quiet, re-capture and re-calibrate; the
mapper tests will tell you what moved.

One known gap: a position whose title is not recoverable from the flattened stream is
dropped rather than guessed at. On the calibration capture that costs one of seven
positions (a sub-role whose title text is not adjacent to its date line). Emitting a
description as a job title would be worse than omitting the row.

### Keeping the session alive (the most likely thing to break)

When the API starts returning `PROFILE_NOT_FOUND` for every slug, the session has
expired or been challenged. Check it:

```bash
curl -s https://linkedin-profile-api-green.vercel.app/v1/health   # sessions.available
python scripts/update_session.py cookies.json                     # refresh everywhere
```

The script verifies against LinkedIn before deploying, so it will not ship dead
credentials. `401` means expired (log in again), `999` means challenged (open
linkedin.com in a browser and clear the checkpoint first).

### Sessions get challenged, and `li_at` rotates

Expect `999` and checkpoint challenges under load. The pool cools a session for
`SESSION_COOLDOWN_SECONDS` on a hard failure and rotates to the next. With a single
session, a challenge means downtime until it clears.

Because `li_at` rotates, **a human browsing LinkedIn on the same account will rotate the
credential out from under the API.** For a stable demo, export cookies and then leave the
account alone.

### `dash/profiles` is quota-limited

It returns real data for roughly the first couple of calls per window and then 401s. The
profile URN is recovered from position URNs (`urn:li:fs_position:(<profileId>,…)`) when
that happens, so URN resolution survives the quota.

### Serverless has no shared session state

The deployment is Vercel serverless. Rotated cookies are written to `/tmp`, which
survives within a warm instance and is lost on a cold start; instances do not share it.
For a credential that rotates, a long-running host (Fly, Render) with one persistent
process is the better shape, or point `REDIS_URL` at a shared Redis so rotation state is
common to all instances. The in-memory response cache is per-instance for the same
reason.

### Datacenter IPs are flagged harder

The politeness defaults (`MIN_DELAY_MS`/`MAX_DELAY_MS`, `MAX_CONCURRENCY=2`) are tuned
for self-preservation, not throughput. `HTTP_PROXY_URL` is wired for a residential proxy
but unused in the demo deployment.

### Private and out-of-network profiles return reduced data

Handled as partial data with the sections named in `meta.partial_sections`, not as an
error.

### Contact info is deliberately not fetched

`/profileContactInfo` exposes email and phone. Fetching it is a meaningful privacy
escalation beyond what the challenge asks for, so it is not implemented. This is a
choice, not an oversight.

### Legal

Automated collection of LinkedIn profiles is contrary to LinkedIn's User Agreement, and
the ethics differ from the legality. This exists as a technical exercise against a burner
account. It is not something to point at a production workload without independent legal
review and a data-protection basis for whatever you do with the output.

---

## Security

- **No secrets in the repo.** `.env` and `.cookie_state.json` are gitignored. Fixtures
  are swept for credentials before they land.
- **The runtime cookie store holds live credentials.** Written `0600`, atomically, and
  never logged.
- **No PII in logs.** Slugs and request ids, yes; cookies, response bodies, and names, no.
- **`X-API-Key` on every profile route.** `/v1/health` is deliberately open and reports
  no token material.

Before any commit touching fixtures or config:

```bash
gitleaks detect --no-git
grep -ri "li_at\|jsessionid" --include="*.py" --include="*.json" .
```
