# Operations Guide: Receipt Fetching

## Enabling Receipt Fetching Post-Deployment

After deploying byoai-runtime with the receipts feature, enable receipt fetching by setting the `BYOAI_RECORDER_RECEIPTS` environment variable to `1` on the host running the agent runtime. This is off by default because it requires Coriqo to be deployed and accessible. The Recorder initialization respects this flag and starts the background receipt fetcher only when explicitly enabled, so receipts remain opt-in for deployments that do not yet have checkpoint infrastructure.

## Monitoring for Receipt Unavailability

The receipt fetcher runs in a background daemon thread at a configurable interval (default: 60 seconds). If the Coriqo `/api/v1/receipts/<event_hash>` endpoint returns `403` or `404` three times in a row for the same trace, that trace is marked `receipts_unavailable` in the verification report. Monitor logs for warning messages matching "receipts route answered 403|404" — these indicate the route is either not deployed yet or the device is not authorized to fetch receipts. Verify that:

- Coriqo has been deployed with migration 0314 or later (which adds the receipts endpoint).
- The device's `agent:runtime` role has read permission on the receipts route.
- Network connectivity from the runtime host to Coriqo is intact.

## Testing Receipt Flow: Post-Trace to Fetch and Verify

Once enabled, test the end-to-end flow in three steps:

1. **Post a trace**: Run an agent under byoai-recorder with a tool call. The trace is published via `publish_session()` and a row is written to the receipt store (next to the ledger as `<ledger>.receipts.json`).

2. **Issue a checkpoint on Coriqo**: Open the agent in Coriqo's UI and cut a regulatory checkpoint, which seals a batch of events. The sealed batch includes any event hashes that have been acknowledged since the last checkpoint.

3. **Fetch and verify**: Once the event is old enough (default: 5 minutes) and the checkpoint is sealed, run `BYOAI_RECORDER_RECEIPTS=1 python -m byoai.recorder.cli <ledger.db> --receipts <ledger.db.receipts.json> --receipt-overdue-after 3600 --allow-unchecked-receipts`. The receipt fetcher background pass will contact Coriqo, retrieve the sealed bundle for the trace, and store it. Verify output should show the receipt as `ok` and `with_receipt` count > 0. A clean report means the trace hash is cryptographically proven by the sealed checkpoint.
