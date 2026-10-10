# Salla signature test case

`verify_salla_signature` (`src/ameen_workforce/webhook_signatures.py`) assumes Salla signs webhooks as
`X-Salla-Signature: hex(HMAC-SHA256(webhook secret, raw body))`. That comes from Salla's code samples, not from a
real delivery, so it must be confirmed once before the first Salla merchant goes live. The same capture also confirms
that the payload carries the top-level `merchant` field that tenant lookup relies on.

`tests/test_salla_signature.py` holds two tests:

| Test | What it proves | Runs |
|---|---|---|
| `test_salla_known_answer_vector` | The implementation matches a digest computed independently with OpenSSL. | Always |
| `test_salla_real_captured_delivery` | A real delivery from Salla verifies with the real secret. | Only when a capture is supplied |

## Capturing a real delivery

1. In the Salla Partners Portal, set the app's webhook security strategy to **Signature** and copy the webhook secret.
2. Point a test store's `order.status.updated` webhook at a request inspector you control (or at a dev Core instance
   with a temporary handler that writes the raw request), then change an order's status.
3. Save the request as a JSON file **outside the repository** (it contains customer data):

   ```json
   {
     "headers": {"X-Salla-Signature": "<header value exactly as received>"},
     "body_base64": "<base64 of the raw request body bytes, not re-serialized JSON>"
   }
   ```

4. Run the gate with the secret in the environment, never on the command line history:

   ```bash
   export SALLA_WEBHOOK_SECRET=...            # from a secrets file or manager
   SALLA_CAPTURED_DELIVERY=~/captures/salla-delivery.json python -m pytest -q tests/test_salla_signature.py
   ```

If the real-delivery test fails, do not onboard Salla merchants: check the security strategy (the `token` strategy is
deliberately unsupported) and the header name, then update `verify_salla_signature` and the known-answer vector
together. Delete the capture once the test has passed.
