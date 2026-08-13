# Deploying the ops desk to Google Cloud Run

`desk/data/` is committed to the repository (like `artefacts/`), so the deployed
container needs no build step — it copies what is already there and serves it. No
secrets or API keys are required: the desk runs the methodology assistant against
the deterministic offline backend by default (`MINIFTSE_LLM` unset), and there is no
writable volume — the desk only ever reads.

This needs the repository owner's own Google Cloud account, so the steps below are
written for the owner to run, not automated here.

**Why not Hugging Face Spaces, which this runbook used to describe.** Hugging Face
now gates Docker and Gradio Spaces behind a PRO subscription — <https://huggingface.co/pricing>
lists "Host ZeroGPU, Gradio & Docker Spaces" as a PRO ($9/month) feature, and Static
is the only SDK left on the free plan. A Static Space cannot host this desk without
amputating it: ten of its twelve application routes are pure reads off the precomputed
snapshot and would export to files fine — `/draft/render` included, since its inputs are
a closed set of questions crossed with one boolean — but `/ask/query` runs live retrieval
over `ground_rules/` and `memos/` in Python, and `/chaos/run` re-runs the validation
engine against a fresh baseline copy. Those two are the reason the desk is a desk. See
`DECISIONS.md` D-016 for the full comparison, including why Cloud Run beat Render on
the same criterion the original design spec used to reject Render.

## What the container actually needs

Measured on the committed snapshot, so the sizing flags below are evidence rather than
guesses:

| | measured |
|---|---|
| Resident memory, every page rendered plus a live `/ask` | 137 MB |
| Resident memory after six live chaos drills | 140 MB |
| Live drill latency | 0.12 s (the handler's own timeout budget is 10 s) |
| Snapshot on disk (`desk/data/`) | 852 KB |

512 MiB is therefore roughly 3.5× headroom, not a squeeze.

## Steps

1. Create (or pick) a Google Cloud project and enable billing on it. The always-free
   Cloud Run tier still requires a billing account attached — check the current
   allotments at <https://cloud.google.com/run/pricing> rather than trusting a number
   written down here, because they move. Then enable the three APIs a source deploy
   touches:
   ```bash
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
   ```

2. Deploy straight from this working directory. **No git remote is required** — this is
   the reason Cloud Run suits this repository specifically, which at time of writing has
   no remote configured at all:
   ```bash
   gcloud run deploy miniftse-desk \
     --source . \
     --region europe-west1 \
     --port 7860 \
     --memory 512Mi \
     --cpu 1 \
     --min-instances 0 \
     --max-instances 2 \
     --concurrency 40 \
     --allow-unauthenticated
   ```

   Each flag that is not obvious, and why:

   - `--source .` uploads this directory and builds it with Cloud Build. Cloud Build
     uses a `Dockerfile` when one is present rather than falling back to buildpacks,
     and — exactly as with the Hugging Face Space this runbook used to describe — there
     is no way to hand it a `--target`. That is why the `desk` stage is the last stage
     in the Dockerfile (see that stage's own comment): the default build target is the
     ops desk, not the index-builder image CI smoke-tests separately.
   - `--port 7860` because Cloud Run injects a `PORT` environment variable and routes to
     8080 by default, while the `desk` stage's `CMD` hardcodes 7860. Telling Cloud Run
     where to knock is deliberately preferred over rewriting the `CMD` to expand
     `$PORT`: the `CMD` is exec-form, so `$PORT` would not expand without dropping to
     shell form, and `EXPOSE 7860` stays honest for any other host.
   - `--min-instances 0` is what keeps this free. The service scales to zero when idle
     and cold-starts on the next request. The design spec rejected Render's free tier
     because "Render sleeps and a cold start loses the visitor" — that reasoning still
     stands, and Cloud Run is the option that honours it at zero cost: Render's free
     tier takes about a minute to wake, while this container has a snapshot load the
     test suite already pins under two seconds, plus image pull.
   - `--max-instances 2` is the actual spend guard, and it matters more than usual here
     — see the rate-limiting caveat below.
   - `--allow-unauthenticated` because it is a public portfolio page. Everything it
     serves is a published index figure or a validation-suite fixture.

   `europe-west1` (Belgium) is the nearest Tier 1-priced region to a London audience;
   London itself (`europe-west2`) is Tier 2 and costs more past the free allotment.
   Confirm the current tier list on the pricing page before committing to a region.

3. Confirm the deploy. `gcloud` prints the service URL; the snapshot identity is the
   thing worth checking, not just that something answers:
   ```bash
   curl -s "$(gcloud run services describe miniftse-desk --region europe-west1 --format='value(status.url)')/healthz"
   ```
   That should return `{"status": "ok", "snapshot_git_sha": "...", "loaded_at": "..."}`,
   and the sha should match `git rev-parse HEAD` locally. Cold start is visible in the
   Cloud Run logs — uvicorn reports "Application startup complete"; `desk/data/` and
   `memos/` are baked into the image, so there is no ten-year index rebuild at startup,
   only a snapshot load.

4. Set a billing budget alert on the project. `--max-instances 2` bounds concurrency,
   but a budget alert is the thing that tells you if an assumption here was wrong.

5. Put the service URL in the root `README.md`'s "Ops desk" section, replacing the
   `TODO` line.

## Rate limiting behind Cloud Run — a live caveat, not a theoretical one

`desk/limits.py`'s per-IP token bucket keys on `request.client.host`. Behind any proxy
that is the proxy's own address unless the ASGI server is told to trust
`X-Forwarded-For`, which is what the `desk` stage's `CMD` does with `--proxy-headers
--forwarded-allow-ips=*`.

**`--forwarded-allow-ips=*` makes that key client-controlled.** uvicorn's
`_TrustedHosts.get_trusted_client_address` returns `x_forwarded_for_hosts[0]` — the
*leftmost* entry — whenever `always_trust` is set, and proxies append rather than
replace, so the leftmost entry is whatever the caller sent. Measured against a real
uvicorn running the deployed flags, on uvicorn 0.52.1:

| probe | result |
|---|---|
| 65 requests, one fixed forged `X-Forwarded-For` | 60 served, then 5 × 429 — bucket honoured |
| 65 requests, rotating forged `X-Forwarded-For` | 65 served, **zero 429** — limiter fully evaded |
| forged value with a real peer appended after it | uvicorn takes the forged one |

Google Cloud appends the caller's address to any `X-Forwarded-For` already present
rather than replacing it, so this is reachable on Cloud Run, not just in theory. The
consequence is bounded — the desk is read-only, there is nothing to exfiltrate and
nothing to corrupt — but an evaded limiter on a per-request-billed platform is a spend
problem, which is why `--max-instances` above is doing real work.

Confirm it on your own deployed service before deciding whether to change anything:

```bash
URL=$(gcloud run services describe miniftse-desk --region europe-west1 --format='value(status.url)')
for i in $(seq 1 65); do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST "$URL/chaos/run" \
    -H "X-Forwarded-For: 198.51.100.$((i % 256))" \
    --data "fault_id=not-a-real-fault&seed=1"
done | sort | uniq -c
```

`fault_id` is deliberately invalid: `enforce_rate_limit` is a route dependency, so it
spends a token before the handler body rejects the input, which makes this cheap. If
every line reads `400` and none reads `429`, the limiter is bypassable on your
deployment and the fix is to stop trusting every hop — replace `--forwarded-allow-ips=*`
with the actual peer address Cloud Run presents to the container, so uvicorn walks the
header from the right and returns the first untrusted entry instead of the leftmost. The
correct value has to be read off the live service; guessing the hop count is how this
class of bug gets reintroduced.

See `DECISIONS.md` D-017.

## Known deviation

The design spec says memo M2 is linked from `/day`; it is named in a `<code>` block
instead (`day.html`'s "Background reading" note) because no memo route exists — a
deliberate simplification, not an oversight.

## Redeploying after a change

Rerun `make desk-data` locally, commit the refreshed `desk/data/` tree, and run the
same `gcloud run deploy` command again — the same two-commit shape this repository
already uses (library/desk change, then a snapshot-data commit). Cloud Run keeps the
previous revision, so a bad deploy is one `gcloud run services update-traffic
miniftse-desk --to-revisions=<previous>=100` away from being undone.
