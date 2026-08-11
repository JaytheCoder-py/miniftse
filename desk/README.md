# Deploying the ops desk to Hugging Face Spaces

`desk/data/` is committed to the repository (like `artefacts/`), so the deployed
container needs no build step — it copies what is already there and serves it. No
secrets or API keys are required: the desk runs the methodology assistant against
the deterministic offline backend by default (`MINIFTSE_LLM` unset), and there is no
writable volume — the desk only ever reads.

This needs the repository owner's own Hugging Face account, so the steps below are
written for the owner to run, not automated here.

1. Create a new Space at <https://huggingface.co/new-space>: pick a name, **Docker**
   as the Space SDK, and the free CPU-basic tier (sufficient for this app).
2. Add the Space as a git remote and push this repository to it:
   ```bash
   git remote add space https://huggingface.co/spaces/<owner>/<space-name>
   git push space feature/ops-desk:main   # or main, once this branch is merged
   ```
3. Hugging Face builds the repository's `Dockerfile` with a plain `docker build .`
   — there is no way to hand a Space a `--target`, which is exactly why the `desk`
   stage is the last stage in the file (see the Dockerfile's own comment on that
   stage): the default build target is the ops desk, not the index-builder image
   CI smoke-tests separately. It serves on port 7860, the Spaces convention.
4. Confirm cold start in the Space's build/run logs: uvicorn should report
   "Application startup complete" within a few seconds — `desk/data/` and `memos/`
   are already baked into the image, so there is no ten-year index rebuild at
   startup, only a snapshot load. Then confirm
   `https://<owner>-<space-name>.hf.space/healthz` returns
   `{"status": "ok", "snapshot_git_sha": "...", "loaded_at": "..."}`.
5. Put the Space's URL in the root `README.md`'s "Ops desk" section, replacing the
   `TODO` line.

A Space's router is the only thing that can reach the container, which is what makes
the Dockerfile's `desk` stage CMD safe to trust every forwarding hop
(`--proxy-headers --forwarded-allow-ips=*`) so `desk/limits.py`'s per-IP rate limiter
sees each visitor's own address instead of bucketing everyone under the router's.

**Known deviation:** the design spec says memo M2 is linked from `/day`; it is named
in a `<code>` block instead (`day.html`'s "Background reading" note) because no memo
route exists — a deliberate simplification, not an oversight.

To rebuild and redeploy after a change to the library or to `desk/`: rerun
`make desk-data` locally, commit the refreshed `desk/data/` tree, and push to the
`space` remote again — the same two-commit shape this repository already uses
(library/desk change, then a snapshot-data commit).
