# Demo script (~15 minutes)

Audience: a customer sponsor and their technical colleagues. Every scene maps to a requirement the customer stated.
All data is synthetic. The UI shows a "Synthetic demo data" badge and which services are live vs mock.
Running it against real Azure services (play a call, a Teams call, 800 calls)? Use [live-demo.md](live-demo.md). Want to click through every scenario yourself, with a screenshot at each step? Follow [walkthrough.md](walkthrough.md).

## Pre-flight (5 minutes before)
1. `scripts\run_all.bat` (or backend + frontend separately). Open http://localhost:5173.
2. Top bar chips: confirm which services are **Live** (Azure Speech, Foundry model, ...) and which are **Mock**. Say so out loud - it keeps the demo honest.
3. `scripts\seed_demo.bat` fills the inbox so there is always something to review. To start from an empty inbox instead, use **Live demo -> Start clean -> Reset demo data**.
4. Policies page: US = transcript **off**, EU = **on** (these are example policies, not any real firm's).

## Scenes

| # | Scene | Requirement | Do this | What to point out |
|---|---|---|---|---|
| 1 | **Phone call -> CRM** | Start from a phone call; end with the summary and the CRM association | Sources -> run the Teams call (`pb-teams-equity-class-c`) or upload the real Teams VTT from the mock call. Watch the pipeline stages tick over in the Inbox. Open the item and press **Approve & send to CRM**. | One click. Stage timeline with durations (a few minutes target). CRM tab shows the call note, tasks, products, topics - and no transcript. |
| 2 | **Same pipeline, other channels** | The same pipeline for Zoom and other channels | Run `pb-zoom-equity-income-advisor` and `pb-genesys-inbound-service`. | Same output shape. Transcript source differs: Zoom = audio via central Speech; Genesys = platform transcript if free (cost line says "no transcription fee"). |
| 3 | **Product precision** | Exact product and share class: Class C must never become Class A | Open the hero call: the product is **Contoso Equity Fund Class C**; Class A was only a comparison. | Candidates list with share class prominent; confidence; evidence quote. |
| 4 | **Top-3 fallback** | When unsure, offer the top three candidates to pick from | Run `pb-ambiguous-equity-fund`. The speaker never said the class. Pick one candidate; approve. | The system refused to guess. One extra click only where it is genuinely unsure. |
| 5 | **Templates** | Two selectable summary templates | On a review screen switch the template (Professional Buyer <-> Retail). | Same call, different structured sections; every section maps to a CRM field for the downstream analytics team. |
| 6 | **Privacy + relationship context** | Omit card and medical details; keep non-sensitive relationship context | Run `retail-teams-relationship-privacy`. | Card number, medical disclosure and football chat are absent from the note; "Seahawks fan" is kept. "What the privacy policy did" lists categories only - never the text. |
| 7 | **Market toggle** | Transcript retention switchable per market | Policies: US = off, EU = on. Run the same fixture with market US, then EU (Sources -> market selector). | US: transcript discarded, quotes stripped. EU: redacted transcript retained in the **vault** with an expiry. CRM holds no transcript in either case. Run "retention sweep". |
| 8 | **Spanish** | English and Spanish calls | Run `pb-teams-spanish-clase-c`. | Spanish audio/transcript in, English CRM note out; "clase C" resolves to Class C. Note: the no-LLM engine builds the note from structured elements; the LLM engine writes richer free text. |
| 9 | **Improvement over time** | Show improvement over time | Run `pb-teams-legacy-blue-chip` (the customer uses an old fund name the catalogue has never seen). The product shows **no candidate**: search "equity fund class c", pick it, approve. Run the same call again (Sources -> run). | First run: needs a pick (the system refuses to guess). Second run: resolved automatically by the **learned alias** (`learned_alias` reason), no review reasons, auto-approve eligible. The Catalogue page lists the learned alias. |
| 10 | **Scoreboard and cost** | Measurable accuracy and cost per call | Scoreboard -> Run baseline; then Run with learning. | Product top-1/top-3, share-class confusion, topic F1, leaks = 0, latency p50/p95, cost per call (**illustrative**). Before/after chart: the two legacy-name calls flip from needs-pick to resolved after learning. Say plainly that the golden set is a development set, not held-out accuracy. |
| 11 (stretch) | **Mobile in-person** | In-person capture matters | On a phone open `/capture`, record 20 s, go offline, record another, reconnect. | Offline queue, auto-upload, then the same review screen. |

## If something goes wrong
- **Live service error** -> set `VOICECRM_DEMO_MODE=true` (default): the affected service falls back to mock and the chip turns grey.
- **A call shows "needs review" unexpectedly** -> that is the product working: open "review reasons" and explain.
- **Nothing in the inbox** -> `scripts\seed_demo.bat`.

## Questions to expect
- *Why not use Genesys / Zoom transcripts?* Cost and control: Genesys bills per minute beyond a fair-use allowance; Zoom's entitlement is unconfirmed. The policy table lets the customer choose per channel.
- *Where does the transcript live?* Vault (Blob), per market, redacted, with an expiry - never in Dataverse.
- *What does a call cost?* See the Scoreboard; figures are illustrative until validated against current Azure pricing.
