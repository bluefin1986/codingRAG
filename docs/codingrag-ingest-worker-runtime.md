# codingRAG Ingest Worker Runtime

Registration-only ingest jobs become `pending` after files are staged or a configured
server directory is queued. A continuously running `scripts/library_import_worker.py`
process must consume those jobs. This worker registers document source content only;
it does not create chunks, embeddings, or index jobs.

Long-running ingest and reindex workers refresh PostgreSQL-backed domain
configuration once when they claim a job. Domain changes therefore apply to
the next claimed job, while a job already in progress uses one cached
configuration rather than querying PostgreSQL per document or for each
library-fallback lookup.

## Docker Compose

The Compose deployment already includes `library-import-worker`, using the same image
and PostgreSQL/SeaweedFS configuration as `app`:

```bash
docker compose up -d
docker compose ps app library-import-worker
docker compose logs -f library-import-worker
```

Browser uploads are staged at the fixed container path
`/app/output/ingest-jobs`. The API container writes those files and the ingest
worker reads them, so Compose mounts the same persistent host directory into
both containers. Set the host-side path in `.env` before deployment:

```bash
CODING_RAG_INGEST_STAGING_DIR=/Volumes/BDisk/bizData/codingrag/ingest-jobs
mkdir -p /Volumes/BDisk/bizData/codingrag/ingest-jobs
docker compose up -d --force-recreate app library-import-worker
```

`CODING_RAG_INGEST_STAGING_DIR` is a Compose interpolation variable, not an
application configuration value: Python continues reading and writing the
fixed mounted path. Existing failed upload jobs cannot be retried after their
previous unmounted container files have been lost; submit those uploads again
after recreating the services with this mount.

Do not also start a host-side worker against the same database unless intentionally
testing multiple consumers.

## VS Code Development On Port 8060

For the host-debug flow, first start the storage/database dependencies:

```bash
docker compose up -d postgres seaweedfs-master seaweedfs-volume seaweedfs-filer
```

Then launch the VS Code compound configuration:

```text
codingRAG API + ingest worker
```

It starts both `uvicorn api.app:app --port 8060 --reload` and the continuous Python
worker with `.env.debug`. Before launching, set the registry connection in `.env` or
your local `.env.debug` if it is not already configured:

```bash
CODING_RAG_DATABASE_URL=postgresql://codingrag:codingrag@localhost:5432/codingrag
```

Use the password/port selected in `.env` when they differ from Compose defaults.
Both processes must point at the same PostgreSQL and SeaweedFS instance.

For terminal-only host debugging, the equivalent manual operation is to keep a second
terminal running the worker beside Uvicorn. The launcher loads the host-side
`.env.debug` values (including the localhost SeaweedFS filer URL) and forwards worker
options such as `--ingest-job-id <job-id>`:

```bash
scripts/start_ingest_worker.sh
# Target a specific queued registration job when intentionally processing it:
# scripts/start_ingest_worker.sh --ingest-job-id <job-id>
```

## Validate A Pending Upload Job

Use a disposable QA knowledge base/domain already registered in the database. The
following creates one small registration-only upload job and verifies that the
running worker advances it from `pending` to a terminal status:

```bash
export API_BASE=http://127.0.0.1:8060
export DOMAIN=qa_ingest_runtime
printf '# worker runtime probe\n' > /tmp/qa_ingest_runtime_probe.md

curl -fsS -X POST "${API_BASE}/api/knowledge-bases/${DOMAIN}/ingest-jobs" \
  -H 'content-type: application/json' \
  -d '{"source_type":"files","batch_size":1}' \
  | tee /tmp/qa_ingest_job.json
export JOB_ID="$(python3 -c 'import json; print(json.load(open("/tmp/qa_ingest_job.json"))["id"])')"

curl -fsS -X POST "${API_BASE}/api/ingest-jobs/${JOB_ID}/files" \
  -F 'files=@/tmp/qa_ingest_runtime_probe.md' \
  -F 'relative_paths=runtime/qa_ingest_runtime_probe.md' \
  | python3 -m json.tool

while :; do
  curl -fsS "${API_BASE}/api/ingest-jobs/${JOB_ID}" > /tmp/qa_ingest_job_state.json
  python3 -m json.tool < /tmp/qa_ingest_job_state.json
  STATUS="$(python3 -c 'import json; print(json.load(open("/tmp/qa_ingest_job_state.json"))["status"])')"
  case "${STATUS}" in completed|failed|cancelled) break ;; esac
  sleep 1
done
test "${STATUS}" = completed
```

The upload response should show the queued job as `pending`; the final response
should show `status: completed` and its item as completed. Confirm the registered
document remains unindexed through its document status/index fields; this runtime
path does not invoke a reindex endpoint.
