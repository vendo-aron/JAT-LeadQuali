# Stripe event fixtures

These JSON files are **hand-built, not captured.** There is no Stripe key and no Stripe CLI
in this repository's development environment, and a captured event would have to be stored
byte-exactly for ever with a real customer identifier in it.

Each one is shaped from the installed SDK's own type definitions (`stripe` 15.6.1:
`stripe.Event`, `stripe.Subscription`, `stripe.Invoice`) and carries only the fields this
system reads — `id`, `type`, and `data.object` with a `customer`, an `id` and a `status`.
Nothing else is populated, deliberately: a field invented here is a field a test might come
to assert on, and an assertion on an invented value proves nothing about Stripe.

They are consumed as **raw bytes**, because that is what a webhook signature covers.
`tests/unit/test_api_webhooks.py` signs them with a known `whsec_` secret at test time; no
secret and no signature is stored here.

`api_version` is pinned to the version `leadquali.config.DEFAULT_STRIPE_API_VERSION` pins,
so a bump of one without the other is visible.

| file | what it stands for |
|---|---|
| `subscription_created.json` | a new subscription going active |
| `subscription_updated_active.json` | a subscription becoming active again |
| `subscription_deleted.json` | a cancellation taking effect — the tenant is suspended |
| `invoice_payment_failed.json` | a failed payment — the seven-day grace period starts |
| `invoice_payment_succeeded.json` | a payment landing — the grace period is cleared |
| `unhandled_event.json` | an event type we store, log and do not act on |
