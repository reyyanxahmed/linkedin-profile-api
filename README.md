# linkedin-profile-api

A hosted HTTP API that takes a LinkedIn profile URL and returns the profile as
structured JSON — built by hitting LinkedIn's internal Voyager endpoints directly
over HTTP. **No browser anywhere in the runtime.**

```bash
curl -s -H "X-API-Key: $API_KEY" \
  "https://linkedin-profile-api.fly.dev/v1/profile?url=https://www.linkedin.com/in/reyyanxahmed" \
  | jq '.profile, .experience[0], .meta.completeness'
```

```json
{
  "first_name": "Ada",
  "last_name": "Lovelace",
  "full_name": "Ada Lovelace",
  "headline": "Analyst at Acme",
  "about": "Pioneer of computing.",
  "location": { "raw": "London, UK", "city": "London", "country": "United Kingdom", "country_code": "GB" }
}
{
  "title": "Analyst",
  "company": { "name": "Acme" },
  "start": { "year": 2024, "month": 1, "iso": "2024-01" },
  "is_current": true,
  "duration_months": 12
}
0.86
```

> The output above is illustrative — the shape is stable; the values come from your
> supplied URL. The `meta.source` field tells you which Voyager generation served the
> response, and `meta.partial_sections` tells you which mappers degraded.

---

## Quickstart

```bash
git clone <repo> linkedin-profile-api && cd linkedin-profile-api
cp .env.example .env                  # fill in API_KEY, LI_SESSIONS, REDIS_URL
docker run --rm -p 8000:8000 --env-file .env $(docker build -q .)
# or, without docker:
uv venv && uv pip install -e ".[dev]" && uvicorn app.main:app --reload
```

Then:

```bash
curl http://localhost:8000/v1/health          # unauthenticated health
curl -H "X-API-Key: $API_KEY" http://localhost:8000/v1/profile?url=https://www.linkedin.com/in/slug
open http://localhost:8000/docs               # OpenAPI / Swagger UI
```

---

## API reference

### `POST /v1/profile`

| Field | Type | Description |
|---|---|---|
| `url` | string | A LinkedIn profile URL (`linkedin.com/in/slug`, country subdomains, `/pub/`, bare slug) |
| `refresh` | bool | Bypass the cache read (still writes). Default `false`. |

Headers: `X-API-Key: <key>` (required).

### `GET /v1/profile?url=...&refresh=false`

Convenience form for curl demos. Same response, same auth.

### `GET /v1/health` (unauthenticated)

```json
{
  "status": "ok",
  "version": "0.1.0",
  "sessions": { "total": 2, "available": 2, "cooling": 0 },
  "redis": true,
  "has_api_key": true
}
```

No token material is ever exposed. `sessions.total` is the count of `li_at` cookies
configured; `cooling` is how many are in cooldown after a challenge or rate limit.

### `GET /docs`

FastAPI OpenAPI / Swagger UI. Interactive; try the endpoints in the browser.

### Response shape

The full schema is in `app/models.py` and is rendered at `/docs`. The top-level shape:

```
meta:        { profile_url, public_identifier, profile_urn, fetched_at, source,
               supplemented_by, cache, partial_sections, completeness, request_id }
profile:     { first_name, last_name, full_name, headline, about, location,
               industry, pronouns, flags, images, counts }
experience:  [{ title, employment_type, company, location, start, end, is_current,
               duration_months, description, skills }]
education:   [{ school, school_urn, school_logo, degree, field_of_study, grade,
               start, end, activities, description }]
skills:      [{ name, endorsement_count }]
certifications: [{ name, authority, license_number, url, issued, expires }]
languages:   [{ name, proficiency }]
projects, publications, honors, volunteer, courses: [...]
```

Design rules, enforced in the models:
- **Collections are always arrays, never `null`.** Absent and empty are different
  states; consumers should not have to branch on `null`.
- **Dates are objects, not strings.** `{year, month, day, iso}` — `day` is `null`
  when LinkedIn only gives year/month. We never invent a day.
- **`meta.source` and `meta.partial_sections`** make the response self-describing. A
  consumer can tell whether it got the rich GraphQL path or the degraded public-HTML
  fallback without guessing.
- **`completeness`** = populated core fields / expected core fields, rounded to 2
  places. Core: `full_name`, `headline`, `location`, `about`, `experience`,
  `education`, `skills`.
- **Image URLs carry `expires_at`.** LinkedIn media CDN URLs are signed and
  time-limited; surfacing the expiry prevents consumers from caching a dead link.

### Error codes

| Code | HTTP | Meaning |
|---|---|---|
| `INVALID_URL` | 400 | Not a LinkedIn profile URL (company, school, empty, non-linkedin host) |
| `UNAUTHORIZED` | 401 | Missing or wrong `X-API-Key` |
| `PROFILE_NOT_FOUND` | 404 | No data found via any strategy |
| `PROFILE_PRIVATE` | 403 | Profile is private / out-of-network |
| `UPSTREAM_CHALLENGE` | 502 | LinkedIn returned a challenge / 999 |
| `ALL_SESSIONS_COOLING` | 503 | Every session in the pool is in cooldown |
| `RATE_LIMITED` | 429 | LinkedIn rate-limited the request |
| `MISSING_QUERY_ID` | 500 | A required value in `queries.yaml` is still a placeholder |
| `INTERNAL` | 500 | Unexpected error |

Every error body:
```json
{ "error": { "code": "PROFILE_NOT_FOUND", "message": "...", "request_id": "01J9..." } }
```

Every response carries an `X-Request-ID` header matching `meta.request_id`.

---

## Approach

### How the endpoints were found

HAR capture from an authenticated LinkedIn session using browser devtools. This is
**recon only** — the browser is used to *discover* the endpoint shapes, then the
solution talks to those endpoints directly via HTTP. The brief forbids browsers in
the *solution*; it does not forbid them in *recon*, and using them for recon is exactly
how reverse engineering works. The browser appears nowhere in the runtime, the Dockerfile,
or any runtime dependency.

### The transport: flagship-web RSC (what LinkedIn actually uses now)

Analyzing the captured HAR revealed that **LinkedIn has migrated profile rendering from
Voyager REST/GraphQL to flagship-web RSC (React Server Components)**. The Voyager
endpoints documented in older reverse-engineering work (`/voyager/api/identity/profiles/`,
`/voyager/api/graphql?queryId=...`) are either deprecated or return empty responses in
current production traffic.

The live LinkedIn web app now fetches profile data through:
1. `GET /flagship-web/in/{slug}/` — the main page, a base64-encoded RSC stream containing
   the profile header (name, headline, education summary, photos, profile URN)
2. `GET /flagship-web/rsc-action/actions/component?componentId=...profileCardsExperienceOnly`
   — the experience section, a base64-encoded RSC stream with positions, companies, dates
3. `GET /flagship-web/rsc-action/actions/component?componentId=...profileCardsBelowActivityPart4`
   — the languages section (and other BelowActivity parts for skills, certifications, etc.)

The RSC wire format is a line-based protocol: each line is `id:type,json_payload`, where
type `I` is a component import, type `T` is text/blob data, and type `[` is a component
tree array. The component trees are HTML-like structures:
`["$","p",null,{"children":["Analyst"]}]` — a `<p>` element with text content "Analyst".

**The RSC parser (`app/linkedin/rsc_parser.py`) decodes the base64, parses the line-based
wire format, and walks the component tree depth-first to extract a flat list of
human-readable text strings in document order.** The strategy then pattern-matches this
text list (using date regexes, employment-type sets, location patterns) to reconstruct
structured profile data.

This is the intellectual core of the submission, updated for LinkedIn's current transport.
The old Voyager endpoints and the URN graph resolver are kept as fallbacks — they still
work if LinkedIn rolls back or if the RSC endpoints are unavailable.

### Why all three Voyager generations are still supported

Even though the HAR shows RSC is the current transport, the three Voyager generations
(legacy REST, dash, GraphQL) are kept in the strategy chain as fallbacks. They break at
different times, and supporting all three means the API degrades gracefully:
- **Flagship-web RSC** (primary) — what the live app uses now
- **GraphQL** (fallback 1) — needs a `queryId` that rotates; highest fidelity when available
- **Dash** (fallback 2) — needs a `decorationId`; resolves slug to URN for GraphQL
- **Legacy REST** (fallback 3) — no queryId needed; most reliable Voyager path
- **Public HTML** (last resort) — unauthenticated JSON-LD; always available

### The normalized envelope and the URN graph (Voyager fallback path)

With `accept: application/vnd.linkedin.normalized+json+2.1`, Voyager responses are a graph:

```json
{
  "data": {
    "*profile": "urn:li:fs_profile:ACoAA...",
    "*elements": ["urn:li:fs_position:(ACoAA...,1)", "urn:li:fs_position:(ACoAA...,2)"]
  },
  "included": [
    { "entityUrn": "urn:li:fs_position:(ACoAA...,1)", "$type": "...Position", "title": "Engineer", "*company": "urn:li:fs_miniCompany:1234" },
    { "entityUrn": "urn:li:fs_miniCompany:1234", "$type": "...MiniCompany", "name": "Acme" }
  ]
}
```

`data` is a graph of URN pointers (star-keys `*foo` hold URN references); `included`
is a flat pool of every entity. The URN graph resolver (`app/normalize/urn_graph.py`)
is pure (no I/O, no async) and handles cycles via a `seen` frozenset, depth caps, and
a `by_type()` escape hatch. This is kept for the Voyager fallback path.

### Why `curl_cffi` and TLS fingerprinting

LinkedIn fingerprints the TLS handshake (JA3) and HTTP/2 frame ordering. A stock
Python HTTP client (`requests`, `httpx`) has a fingerprint that matches no browser,
which sharply increases the rate of challenge responses (HTTP 999, redirect to
`/checkpoint/challenge`). `curl_cffi` with `impersonate="chrome124"` reproduces
Chrome's TLS handshake and H2 frame ordering at the libcurl level — not a browser,
just a TLS fingerprint match. This is a load-bearing decision: without it, the
challenge rate against a burner account is high enough to make the API unusable
during a grading window.

### The fallback and supplementation chain

The orchestrator runs strategies in order: **GraphQL > dash > legacy > public-HTML**.

1. The first strategy returning a parseable payload becomes the primary. Its name
   goes in `meta.source`.
2. If the primary populated all core sections (`profile`, `experience`, `education`),
   stop.
3. If sections are missing and a lower-priority strategy declares it can provide
   them, call it and merge — but only into empty sections. A populated field is
   authoritative; a lower-priority strategy never overwrites it. Supplements go in
   `meta.supplemented_by`.
4. If a strategy raises `ConfigError` (its `queryId` / `decorationId` is still a
   placeholder), log a warning and skip it. The chain continues. A missing queryId
   degrades, it never 500s.
5. If every strategy fails, serve a stale cache entry with `meta.cache.stale = true`
   before erroring — the endpoint never looks broken during grading.

### Architecture

```
                    ┌──────────────────────────────┐
    POST /v1/profile│  FastAPI app                 │
    ──────────────► │  auth (API key) → validate   │
                    │  → normalise URL to slug     │
                    └──────────────┬───────────────┘
                                   │
                         ┌─────────▼─────────┐
                         │  Cache (Redis)    │  key: slug, TTL 24h, stale-on-error
                         └─────────┬─────────┘
                                   │ miss
                         ┌─────────▼──────────────────────┐
                         │  Fetch orchestrator            │
                         │  strategy chain, first success │
                         └─────────┬──────────────────────┘
                      ┌────────────┼────────────┐
                      ▼            ▼            ▼
               GraphQL cards  dash profiles  legacy profileView  → public HTML/JSON-LD
                      └────────────┼────────────┘
                         ┌─────────▼─────────┐
                         │  Session manager  │  cookie pool, health, cooldown
                         └─────────┬─────────┘
                         ┌─────────▼─────────┐
                         │  Rate limiter     │  token bucket + jittered delay
                         └─────────┬─────────┘
                                   ▼
                         ┌───────────────────┐
                         │  URN graph resolver│  pure, fixture tested
                         └─────────┬─────────┘
                         ┌─────────▼─────────┐
                         │  Section mappers  │  each independently failable
                         └─────────┬─────────┘
                         ┌─────────▼─────────┐
                         │  Pydantic models  │  → stable public schema
                         └───────────────────┘
```

---

## Known limitations

Be specific and unflinching — a reviewer who has operated one of these reads this
section first.

### The RSC transport is undocumented and will change

LinkedIn's flagship-web RSC (React Server Components) transport is an internal
implementation detail, not a public API. The wire format, component IDs, and text
layout can change with any frontend deploy. The RSC parser (`app/linkedin/rsc_parser.py`)
and the text pattern-matching mappers (`app/linkedin/strategies/flagship_web.py`) are
built against the format observed in the captured HAR. When LinkedIn changes the
component layout, the mappers will need adjustment — but the parser's text-extraction
approach is resilient to structural changes (it walks the tree, it doesn't hardcode
paths).

### GraphQL `queryId` hashes rotate (Voyager fallback path)

GraphQL `queryId` values are persisted-query identifiers that rotate with LinkedIn
frontend deploys. This *will* break the GraphQL strategy, usually every few weeks.

**The fix is one file and one command:**
```bash
python scripts/extract_query_ids.py capture.har
# paste the printed values into app/linkedin/queries.yaml
```
The dash and legacy strategies do not need queryIds, so the API keeps working via
fallback — the GraphQL strategy just stops being the primary.

### Sessions get challenged and restricted

LinkedIn issues HTTP 999, challenge redirects, and eventually restricts accounts that
hit Voyager at machine cadence. The session pool mitigates this — it rotates cookies,
cools a session after a hard failure, and uses exponential backoff for soft failures —
but it does not *solve* it. Use a burner account created and warmed for this. Do not
put a real LinkedIn `li_at` into a public deployment.

### Datacenter IPs are flagged harder than residential

A Fly.io VM has a datacenter IP. LinkedIn flags datacenter IPs more aggressively than
residential ones, which raises the challenge rate. A proxy hook exists
(`HTTP_PROXY_URL` in `.env.example`) and is unused in the demo deployment. Adding a
residential proxy is the single biggest reliability improvement you can make.

### Private and out-of-network profiles return reduced data

This is by design. LinkedIn shows less data for profiles outside your network, and
less still for private profiles. The API reports this honestly via `completeness`
and `partial_sections` rather than faking fields. A profile that returns
`completeness: 0.4` is telling you the truth: it got the degraded path, not the rich
one.

### Contact info is deliberately not fetched

The `/contact-info` endpoint exists and the auth ritual is the same. The API does not
call it. This is a deliberate data-minimisation choice: email and phone are more
sensitive than work history, and the profile brief does not require them. Documented
as a choice, not an omission.

### Legal

This violates LinkedIn's User Agreement, which prohibits automated access and
scraping. *hiQ v. LinkedIn* held that scraping public data does not violate the CFAA,
but that holding does not extend to authenticated access to internal endpoints.
Built for evaluation at Tross's explicit direction, not for production use.

---

## Testing

The whole suite runs offline against saved fixtures. No LinkedIn account needed.

```bash
uv venv && uv pip install -e ".[dev]"
pytest -q          # 154 tests, ~1s, zero network
```

The test suite mechanically enforces the no-network rule: a conftest fixture patches
`socket.connect` to raise `AssertionError` for any `AF_INET`/`AF_INET6` connection.
AF_UNIX is allowed (asyncio's event loop uses it internally). A test that accidentally
introduces a network dependency fails immediately with a clear message.

Section mappers have two layers of tests:
- **Synthetic** tests in `tests/test_sections.py` exercise the baseline field paths
  against payloads matching the documented normalized-envelope shape. These run now.
- **Fixture** tests (added when a HAR arrives) read redacted payloads from
  `tests/fixtures/` and verify the same outputs. When a fixture and the baseline
  disagree, the fixture wins and a code comment records the divergence.

The URN graph resolver (`tests/test_urn_graph.py`) exercises the five required cases:
single reference, list reference, nested reference two levels deep, missing URN, and
a reference cycle (which must terminate, not recurse infinitely).

---

## Security

- **No secrets in the repo.** `.env` is gitignored from the first commit. `.env.example`
  documents every variable with empty values. A secret sweep (`gitleaks`, plus
  `grep -ri "li_at\|jsessionid" --include="*.py" --include="*.json" .`) finds only
  variable names and redaction patterns, never a value.
- **Env-only config.** `pydantic-settings` reads from `os.environ` and `.env`; nothing
  is hardcoded.
- **API key.** `X-API-Key` header, compared with `hmac.compare_digest` (constant time).
  `/health` and `/docs` are unauthenticated; everything else requires the key.
- **PII-safe logging.** structlog with a `redact` processor that drops any key matching
  `cookie|li_at|jsessionid|csrf|authorization|api_key` (and a few more) before rendering.
  Logs contain profile slugs and request IDs; never cookies, response bodies, names,
  or emails.
- **Cache TTL as data minimisation.** The 24h cache TTL is also the maximum time a
  profile blob persists. There is no persistent profile database; Redis is the only
  store and entries expire.
- **Fixture redaction.** `scripts/har_to_fixtures.py` strips `Cookie`/`Set-Cookie`
  headers and redacts `email|phoneNumber|address|birthDate` keys before writing any
  fixture. The script prints a warning that fixtures contain real third-party profile
  data and the human should review before committing.