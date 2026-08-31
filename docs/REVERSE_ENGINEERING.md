# Reverse engineering LinkedIn's profile transport

This document is the working record of how the endpoints in this repo were found, what
each one actually returns, and which of them are dead. It exists so the next person to
touch this code does not have to rediscover it from an empty terminal.

Everything here was established against live traffic and a captured HAR. Where something
is inference rather than observation, it says so.

---

## 1. There are three generations, and two of them are corpses

LinkedIn's web client has been through three profile APIs. All three still answer
requests, which is misleading, because two of them answer with tombstones.

| Generation | Endpoint | Status as observed |
|---|---|---|
| Legacy REST | `/voyager/api/identity/profiles/{slug}/profileView` | **410 Gone** |
| Legacy REST | `/voyager/api/identity/profiles/{slug}/{skills,educations}` | **410 Gone** |
| Legacy REST | `/voyager/api/identity/profiles/{slug}/positionGroups` | **200, real data** |
| Legacy REST | `/voyager/api/identity/profiles/{slug}/{certifications,languages,projects,honors,courses}` | 200, usually empty |
| Dash | `/voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity={slug}` | 200 briefly, then 401 (quota) |
| GraphQL | `/voyager/api/graphql?queryId=...` | 200 with a valid `queryId`; 500 without |
| Flagship RSC | `/flagship-web/in/{slug}/` + `/flagship-web/rsc-action/actions/component` | 200, complete data |

The practical consequence: **no single endpoint returns a whole profile any more.**
`positionGroups` gives experience. The RSC cards give everything. Nothing else is
load-bearing.

### The 410 trap

A retired sub-resource does not return a bare HTTP 410. It returns a Rest.li envelope:

```json
{"data":{"status":410},"included":[]}
```

Depending on the gateway this arrives under a 200 or a 410 on the wire. A client that
trusts the HTTP status records it as a successful fetch of an empty profile — strictly
worse than an error, because the section silently vanishes instead of failing over to
another strategy. `envelope_status()` in `app/linkedin/client.py` exists for this.

---

## 2. The cookie jar is the whole ballgame

Three separate findings, all of which look like "the account is banned" and none of which
are.

### 2.1 `lidc` and the self-redirect

Request an identity endpoint with only `li_at` and `JSESSIONID` and you get:

```
302 Found
Location: https://www.linkedin.com/voyager/api/identity/profiles/{slug}/profileView   <- the same URL
Set-Cookie: lidc="b=OGST00:s=O:..."
x-li-pop: afd-prod-ltx1-x
```

LinkedIn is pinning you to a datacenter. It redirects to the *identical* URL with a
`lidc` cookie attached, expecting the retry to carry it. Follow the redirect with a
cookie jar and the second request returns real data.

The failure mode if you don't: **infinite redirect loop**, `curl: (47) Maximum (30)
redirects followed`.

The subtle version of this bug: setting `cookie` as an explicit *header*. That overrides
curl's jar entirely, so `Set-Cookie` is accepted and then ignored on the retry, and you
loop forever while holding a perfectly valid session. This is why `build_headers()` in
`client.py` deliberately does not set a `cookie` key, and why a test asserts it never
does.

### 2.2 The domain scope matters

Cookies must be scoped the way the browser scopes them. This is not cosmetic — a
mis-scoped `li_at` reproduces the redirect loop:

| Cookie | Domain |
|---|---|
| `li_at`, `JSESSIONID`, `bscookie`, `li_theme`, `timezone` | `.www.linkedin.com` |
| `bcookie`, `lidc`, `liap`, `lang`, `dfpfpt`, `__cf_bm`, `UserMatchHistory` | `.linkedin.com` |

Observed directly: `li_at` on `.linkedin.com` → redirect loop. The same value on
`.www.linkedin.com` → a real response. The map lives in `COOKIE_DOMAINS` in
`app/config.py`.

### 2.3 `li_at` rotates

Three cookie exports taken minutes apart from the same browser had three *different*
`li_at` values. LinkedIn hands back a fresh one via `Set-Cookie` during normal use and
retires the previous one shortly after.

The symptom is distinctive and very easy to misread: a fresh cookie works for one or two
requests, then every endpoint returns 401 — including endpoints that worked seconds
earlier. It looks exactly like an account ban.

Two things follow, and this repo does both:

1. **Harvest rotated cookies from the jar after every response** and keep using the new
   value (`_harvest_cookies` in `client.py`, persisted by `cookie_store.py`).
2. **Serialize requests per session.** Two in-flight requests on one rotating credential
   race: the second sends a value the first has already invalidated. Requests across
   *different* sessions still run concurrently.

A corollary worth knowing: if a human is actively browsing LinkedIn in a browser with
the same account, their session rotates the cookie out from under the API. For a stable
demo, export the cookies and then leave the account alone.

---

## 3. The flagship RSC transport

This is what the live web app actually uses, and the only source that returns a complete
profile.

### 3.1 Shape

Two kinds of request, both calibrated against a captured profile page load:

```
GET  /flagship-web/in/{slug}/?skipRedirect=true
POST /flagship-web/rsc-action/actions/component?componentId={id}&sduiid={id}
```

The response is React Server Components wire format — a line-oriented stream with
base64-embedded payloads, parsed by `app/linkedin/rsc_parser.py` into a flat list of
strings. It is a *UI description*, not an API response: component ids, scale hints
(`"2x"`), and ICU plural templates sit inline with the actual values.

### 3.2 The component set

A profile page load requests these, all under
`com.linkedin.sdui.generated.profile.dsl.impl`:

| Component | Carries |
|---|---|
| `profileCardsAboveActivity` | About, featured posts |
| `profileCardsExperienceOnly` | Experience |
| `profileCardsBelowActivityPart1WithoutExp` | Education, licenses & certifications |
| `profileCardsBelowActivityPart2` | Recommendations |
| `profileCardsBelowActivityPart3` | Honors & awards, test scores |
| `profileCardsBelowActivityPart{4,5,6,7}` | Languages, interests, and other tail sections |
| `pymk…`, `browsemap…`, `product…RecommendedEntitySection` | Recommendation rails — no profile data, skipped |

**Which section lands in which `PartN` is not stable.** The names are LinkedIn's and they
are meaningless. This code fetches the whole set and lets the mappers pattern-match over
the union rather than trusting a bucket.

### 3.3 The navigation delta

`GET /flagship-web/in/{slug}/` returns **200 with a zero-length body** unless you send
the navigation context headers:

```
x-li-anchor-page-key: d_flagship3_feed
x-li-initial-url: /feed/
x-li-layout-tree: ["com.linkedin.sdui.flagshipnav.home.Home#0","a15eca…"]
```

RSC answers a client-side navigation with a *delta* against the layout the client claims
to already hold. Without these headers the server correctly concludes nothing needs to
change and sends nothing. An empty 200 reads like a silent block; it isn't.

### 3.4 Headers that are load-bearing

| Header | Why |
|---|---|
| `x-li-rsc-stream: true` | Selects RSC wire format. Without it you get HTML regardless of `accept`. |
| `x-li-application-version` / `x-li-track.clientVersion` | Must be the **SDUI** version (`0.2.x`), *not* the Voyager version (`1.13.x`). Different applications, validated separately. |
| `referer` | Must be the specific profile URL. A generic `/in/` referer reads as a cross-page fetch. |
| `x-li-page-instance`, `x-li-traceparent`, `x-li-pageforestid` | Tracing ids, generated fresh per request. Replaying one captured id on every request is itself a fingerprint. |

### 3.5 The three experience layouts

The flattened text stream renders positions three different ways, and a parser that
knows only one produces convincing garbage — swapped titles and companies rather than an
obvious failure.

```
Single position            Grouped employer            Bare company (older roles)
-----------------          -----------------           -------------------------
Founder, CEO and CTO       Google                      President of the United States
takeUforward · Full-time   Full-time · 3 yrs 5 mos     White House
Aug 2024 - Present · …     On-site                     Jan 2009 - Jan 2017 · 8 yrs 1 mo
Bangalore … · Remote       Software Engineer III
                           Jan 2024 - Jun 2025 · …
                           Bengaluru, Karnataka
```

Punctuation cannot tell these apart: a bare company name and a job title have identical
shape. The reliable signal is that **every organisation ships an accessibility label**,
`"Google logo"`. Harvesting those gives a roster of organisations on the profile, which
resolves the layout without guessing. `_company_names()` does this.

Two further traps, both found on real captures:

- **Older positions use year-only ranges** (`"1997 – 2004"`), identical in form to
  education's date line. Since education and experience share one pooled stream, a naive
  rule drops real jobs or invents fake ones. The test is *"was an employer named directly
  above this?"*, not *"is the range year-only?"*.
- **Role descriptions are their own text items** and can land exactly where a title is
  expected. Prose is rejected by length and terminal punctuation.

The dash in date ranges is an **EN DASH** (`–`), not a hyphen. Ruff's ambiguous-character
rules are disabled in `pyproject.toml` for exactly this reason — the patterns have to
match reality.

---

## 4. What is still blocked, and why

### 4.1 The session authenticates the API but not the web app

Observed repeatedly with valid, freshly exported cookies:

- `/voyager/api/me` → **200** with real data
- `/voyager/api/identity/profiles/{slug}/positionGroups` → **200** with real data
- `/feed/` → **302 to `/uas/login`**
- `/in/{slug}` (HTML) → **999** (LinkedIn's anti-automation status)
- `/flagship-web/in/{slug}/` → **200, empty body**

Dropping `__cf_bm` or reducing to only the auth cookies changes nothing. So the RSC
strategy cannot run on a session in this state, even though its implementation is
correct and fixture-verified. Whether this is an account-level restriction on the burner,
an IP reputation signal, or a deliberate split between API and web auth is **not
established** — it is the main open question in this codebase.

### 4.2 `queryId` and `decorationId` cannot be guessed

GraphQL needs a `queryId` like `voyagerIdentityDashProfiles.<32 hex>`. These are build
artifacts that rotate with every LinkedIn frontend deploy. `queries.yaml` ships
placeholders, and the strategies raise `ConfigError` and degrade rather than sending
garbage.

Ways to get real values, in order of preference:

1. **From a HAR.** `python scripts/extract_query_ids.py capture.har` — the capture must
   be of a *profile* page. A feed capture only yields feed and messaging query ids.
2. **From the JS bundles.** Attempted and documented as a dead end for the profile ids:
   the guest bundles reachable from `/login` (14 files, ~3.9 MB on `static.licdn.com`)
   contain no profile `queryId`s. Those live in the authenticated flagship bundles, whose
   URLs are only discoverable from an authenticated page — which brings back 4.1.

---

## 5. How to re-derive all of this

When LinkedIn changes something and the mappers go quiet, this is the loop:

```bash
# 1. Capture. Open a PROFILE page with devtools recording, then "Export HAR".
#    Chrome does not persist binary response bodies, so the main RSC stream will be
#    absent — the component POSTs (JSON-ish text) do survive.

# 2. Pull identifiers.
python scripts/extract_query_ids.py capture.har

# 3. Pull fixtures.
python scripts/har_to_fixtures.py capture.har tests/fixtures/

# 4. Re-run the offline suite. The mapper tests are the calibration record; if the
#    card format moved, they fail here rather than in production.
pytest -q
```

The fixtures in `tests/fixtures/rsc/` are the ground truth for the mappers. Read the
fixture before changing a mapper, not after.

---

## 6. Things that cost hours, condensed

- HTTP 200 is not success. Check content type, first byte, and the Rest.li envelope.
- An empty 200 can mean "your delta headers say you already have this".
- A redirect to the same URL is datacenter affinity, not auth failure.
- Blanket 401s on a healthy account usually mean a rotated credential, not a ban.
- One failing sub-request must never abort a strategy that already has partial data.
- A strategy's transport failure must never cool a *shared* session — it starves the
  strategies that would have worked. Cool on auth signals only.
