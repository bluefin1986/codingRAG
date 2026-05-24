# CRAG-QA-010 P0 Upload Lifecycle Result

- Executed: 2026-05-24 13:12-13:15 Asia/Shanghai
- Result: PARTIAL / BLOCKED for full `webkitdirectory` contract; PASS for the targeted registration lifecycle through the filename-path fallback
- QA domain: `qa_ingest_20260524_131237_domain` (disabled after validation)
- Ingest job: `4e5c2dc8-4757-4558-be6c-b1387f00f56a`
- Scope: generated QA files only; no HarmonyOS or iOS corpus was read or submitted

## Observations

| Check | Observed result | Verdict |
| --- | --- | --- |
| Initial job | `source_type=upload`, `operation=register`, `status=accepting` | PASS |
| Documented `relative_paths` multipart form | Two repeated `relative_paths` values returned HTTP `422`: `Input should be a valid list`; job remained `accepting` with 0 items | BLOCKER |
| Supported batch 1 | `button.md`, `network.md` staged through repeated `files` fields | PASS |
| Supported batch 2 fallback | Multipart filenames `pkg/widgets/card.md` and `pkg/network/client.md` staged with those relative paths | PASS with fallback |
| Unsupported input | `invalid.bin` returned HTTP `400`: `unsupported document extension: invalid.bin`; item was not added | PASS |
| Counts before completion | Job remained `accepting`, `pending=4`, `total_items=4` | PASS |
| Completion boundary | `POST /api/ingest-jobs/{id}/complete` changed state to `pending`, preserving four pending items | PASS |
| Targeted worker | `python3 scripts/library_import_worker.py --ingest-job-id 4e5c2dc8-4757-4558-be6c-b1387f00f56a` completed only this QA job; final summary `created=4`, `pending=0`, `indexing_triggered=false` | PASS |
| Relative path storage | Registered paths were `button.md`, `network.md`, `pkg/network/client.md`, `pkg/widgets/card.md` | PASS for fallback; blocked for intended form field |
| Registration-only indexing isolation | All four documents had `status=new`, `chunk_count=0`, `indexed_at=null`; chunks endpoints returned zero entries. `/api/index/jobs` reports `source=document-index-state`, `history_available=false`, and projects the four documents with `index_required=true`. | PASS: no automatic indexing observed |

## Runtime Risk

The configured SeaweedFS filer disconnected during each object upload. The worker completed registration using its local fallback and recorded each version with `storage_status=missing`. This does not invalidate the state-machine check, but storage health must be fixed before any broader document registration.

## Cleanup

- Disabled the four QA documents through the API.
- Disabled/deleted the QA domain through the API; subsequent domain lookup returned HTTP `404`.
- Removed the QA job staging directory under `output/ingest-jobs/`.
- Removed the four generated local fallback object files.
- Removed the generated `/tmp/codingrag-qa_ingest_20260524_131237` fixture root.
- The completed QA job and disabled database rows remain as audit records.

## Blocker

Full P0 acceptance is blocked until the backend accepts the frontend contract for repeated `relative_paths` multipart fields, or the frontend/backend contract is formally changed and revalidated. Do not proceed to HarmonyOS/iOS document sets on the basis of this run.

## Rerun After Fixes: 2026-05-24 13:24-13:26 Asia/Shanghai

- Result: PASS for the CRAG-QA-010 application-level rerun of the two prior blockers.
- QA domain: `qa_ingest_rerun_20260524_1325_domain` (disabled after validation)
- Ingest job: `dc4e8e60-e089-41fe-a5b6-fdf68b4a3b1b`
- Scope: four generated nested-path Markdown files only; no HarmonyOS or iOS corpus was read or submitted.

| Check | Observed result | Verdict |
| --- | --- | --- |
| Contract regression test | `python3 -m unittest tests/test_ingest_upload_contract.py -v` passed both acceptance and mismatch cases | PASS |
| Browser-shaped multipart upload | One request with four repeated `files` and paired repeated `relative_paths` returned HTTP `200` | PASS |
| Relative path preservation while accepting | Items remained `pending` under `status=accepting` with paths `guide/setup/configure.md`, `guide/setup/install.md`, `pkg/network/client.md`, and `pkg/widgets/card.md`; summary was `pending=4`, `total_items=4`, `indexing_triggered=false` | PASS |
| Completion boundary | `POST /api/ingest-jobs/{id}/complete` transitioned the isolated job to `pending`, preserving all four items | PASS |
| Targeted worker only | No continuous import worker was running; `set -a; source .env; source .env.debug; set +a; python3 scripts/library_import_worker.py --ingest-job-id dc4e8e60-e089-41fe-a5b6-fdf68b4a3b1b` completed this job with `created=4`, `pending=0`, `failed=0`, `indexing_triggered=false` | PASS |
| SeaweedFS original storage | Worker performed four `PUT` requests returning HTTP `201` and four read-back `GET` requests returning HTTP `200` at `http://localhost:8888`; all four document versions recorded `storage_backend=seaweedfs` and `storage_status=active` | PASS |
| Registration-only indexing isolation | Each document remained `status=new`, `index_required=true`, `chunk_count=0`, and `indexed_at=null`; each chunks endpoint returned `total=0`; index state projection contained only these unindexed document states | PASS |

### Rerun Cleanup

- Disabled the four QA document rows and disabled the QA domain; subsequent domain lookup returned HTTP `404`.
- Deleted the four generated SeaweedFS objects; each deletion returned HTTP `204` and immediate read-back returned HTTP `404`.
- Removed `output/ingest-jobs/dc4e8e60-e089-41fe-a5b6-fdf68b4a3b1b` and `/tmp/codingrag-qa_ingest_rerun_20260524_1325`.
- Retained the terminal job and disabled rows as audit records. Aggregate checks for `harmonyos` and `ios` remained unchanged at zero documents during this isolated rerun.

### Rerun Conclusion

The two blockers observed in the initial P0 run are resolved in the isolated application-level rerun: repeated multipart `relative_paths` are accepted and preserved, and the host-targeted worker stores source files in reachable SeaweedFS with `storage_status=active`. This rerun did not exercise the separate `server_dir`, cancellation/retry, or broader release-gate cases in the QA plan.
