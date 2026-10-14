# V7 implementation ambiguities

Entries are appended as they are found, numbered AMB-01, AMB-02, ... Format of one entry:

`## AMB-nn — <title>`, then: spec section, literal text, options, classification
(`implementation clarification` | `normative amendment required`), and what the code does now.

## AMB-01 — Shape of the Binance announcement catalog response for transport-level ID extraction

- Spec: S-1.7 M3-3, EC-8, DEC-45, section 15.
- Literal text: "the minimum mechanical parsing needed to extract the article ID / code / URL from the raw catalog response".
- Options: (a) collect every `id` / `code` / `url` key anywhere (would also pick up response status codes and catalog ids); (b) collect only from entries of `articles` lists.
- Classification: implementation clarification.
- Code now: `forward_store.extract_transport_id` reads only the entries of lists under an `articles` key (any depth), returns `code` (else `id`) and `url` as strings, de-duplicated in order; never titles, bodies or dates. The real shape is to be confirmed when the recorder is built (Task 18).

## AMB-02 — N2 bucket listing pagination

- Spec: S-1.7 N2.
- Literal text: "`delimiter=/` and `prefix=data/futures/um/...`" and "Refused: every other host, path or parameter".
- Options: (a) allow only `delimiter` and `prefix`; (b) also allow S3 paging parameters (`marker`, `max-keys`), which a listing of more than 1000 keys needs in practice.
- Classification: normative amendment required (if paging is needed).
- Code now: `netguard.check` allows exactly `delimiter` and `prefix`; any other N2 parameter is refused.

## AMB-03 — Numeric materiality threshold for a funding source conflict

- Spec: S-2.6 (Overlap), DEC-32.
- Literal text: "keys, rates and timestamps are compared. If they agree, archive provenance is kept. A material conflict is reported as a data-integrity blocker."
- Options: (a) any rate difference; (b) a tolerance. The spec gives no numeric materiality threshold.
- Classification: implementation clarification.
- Code now: `funding.merge` matches REST to archive keys within the frozen +-60 s tolerance; rates are parsed to float and conflict when `abs(archive - rest) > 1e-12` (`funding.RATE_ATOL`). Also an `IntegrityBlocker`: a REST record matching several archive records, two REST records mapping to the same archive settlement, or two records of one source within 60 s. On agreement the archive row is kept unchanged (no field such as `mark_at_settle` is borrowed from REST); REST rows with no archive key within 60 s fill the gap with `source = "rest"`. Kline conflicts (S-2.2 DEC-33) use exact equality of every value column after parsing.
