# Image processing and Telegram delivery

## Diagnosis

Read-only inspection of recent production tasks showed two distinct reasons for text-only
previews: ENHANCE blocked because editing rights were not confirmed, and a generated image
rejected by Vision for additional unsupported statements, changed identifiers/logo and a
missing qualifier. These tasks had no final asset. Those safeguards remain active.

Implementation gaps made this hard to diagnose: promotional overlays and protected marks
were treated together; quality success was not distinguished from actual attachment delivery;
an R2 object could be uploaded without confirming the task_media pointer. This change adds
precise stage diagnostics and end-to-end delivery receipts, rather than weakening QC.

## Protected marks and authorized promotion cleanup

Vision reports separate `has_channel_overlay`, `has_source_copyright_mark`, and typed
`mark_regions`. Normal product/brand logos are not channel promotion. Copyright notices,
author credits, photographer signatures, media-rights marks, source URLs/handles, and unknown
marks are protected. Legacy ambiguous watermark classifications fail closed for cleanup.

In Image Tools, enable **Remove channel/promotion overlays** together with **Minimal changes**,
or select ENHANCE with promotion removal. Both flags remain compatible and remembered using
the existing per-user preference service. For a rights-blocked image, the review card now offers
**I confirm I have editing rights for this image** (Simplified Chinese: **我确认拥有此图片的编辑权限**).
Existing review cards can reach the same button through Image Tools, without an LLM call.
The existing `pipeline.owned_source_ids` and `pipeline.direct_uploads_owned` configuration remains
supported, but the confirmation button never changes these global settings. An option toggle
alone never grants editing rights; forwarded origins remain checked per image.

The selected flags explicitly request removing only identified channel-promotion rectangles.
`image_options.promotion_targets` can restrict these further to `source-index.region-id`;
empty selects all confidently identified safe promotion regions. Protected marks never qualify.
Unknown, overlapping, low-confidence, or unsafe regions go to IMAGE_POLICY review. A crop must
not intersect original meaningful content or conceal a scene that would need inventing.

For this mode, Image2 receives local crops, not the entire source. It returns patches composited
only into approved rectangles on the EXIF-corrected original canvas, saved losslessly at original
size. Rounded pixel boundaries must avoid protected marks; excessively large regions are
rejected. Every outside pixel (including alpha) is compared deterministically before Vision QC.
Source language, original text, UI, numbers, dates, products, composition, proportions and style
are preserved, even if the user's general locale/density preferences differ. This explicit
minimal-edit mode does not translate or redesign the source.

QC receives the source, composed output, original OCR and permission contract. It must approve
all existing checks plus `protected_marks_preserved`, `promotion_removal_valid`, and
`outside_regions_unchanged`. `watermarks_ok` permits only removal of contract-listed promotional
regions; a removed copyright/source/author mark still fails. Documentary restrictions remain:
no invented real-person/news scene, and source subjects are never regenerated for cleanup.
All other image actions continue to use the existing localized facts/density/prompt/QC pipeline.

## Asset and delivery contract

1. IMAGE_POLICY resolves an allowed action.
2. IMAGE2 produces the result; every edited/recreated/generated result must pass QC.
3. Only passing outputs are persisted as `asset_kind=final` by `services/storage.py` (AssetStore).
   R2 reference-counting, deduplication, budget and retention rules remain unchanged.
4. R2 upload/reuse must attach the object to its task_media row; missing pointers fail and
   unreferenced orphan objects are cleaned up. Review copies never become final attachments.
5. Draft delivery fetches actual R2 bytes and validates the image, wraps them in named BytesIO
   streams, then calls Telethon `send_file(..., force_document=False)` in batches of up to ten.
   It checks each returned message has `photo`, before reporting success.
6. One image carries the unchanged X draft as its caption; 2–10 images form an album with
   the draft on its first photo. The review card replies to the content bundle. Oversize captions
   and unconfirmed caption delivery fall back to an untruncated related text message. Text still
   arrives when an image fails. A stale final pointer cannot override a REVIEW decision/QC failure.

This uses the official [Telethon send_file documentation](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.uploads.UploadMethods.send_file).
Telegram may apply its own photo compression; exact pixel preservation describes the stored
final asset before Telegram's photo transport.

The review card and `draft_meta.delivery` distinguish **IMAGE_POLICY**, **IMAGE2**, **IMAGE_QC**,
**R2_UPLOAD**, **R2_FETCH**, and **TELEGRAM_SEND** failures, per image. A transport failure marks
a pending draft NEEDS_REVIEW; retrying delivery can clear the transport failure without
regenerating the draft. Processing diagnostics and terminal tasks remain unchanged.
No schema migration or production-data rewrite is needed.

## Verification

`tests/test_image_delivery.py` exercises the real Pipeline, AssetStore and TelegramBot boundaries
with in-memory R2/database fakes and a fake Telethon photo receipt. Both locales cover ordinary
images, permitted minimal cleanup, protected marks, Image2/QC failures, upload/fetch errors,
invalid R2 bytes, Telegram errors/document responses, stale pointers, retries, ordering and
callback preference persistence. These tests make no production writes or upstream paid calls.


## Explicit per-image confirmation and durable retry

The callback is `ia:<task_id>:<image_idx>` (under Telegram's 64-byte limit). Telegram's allowed-user
check runs first. Ops additionally checks the task owner, chat, draft status, image index, and the
specific IMAGE_POLICY rights-blocked result. Confirmation and queueing are one conditional SQL
update, guarding the current media hash/reason/status; stale or duplicate callbacks cannot queue
a second job. A full in-memory queue is covered by the existing database sweeper.

Authorization is stored in MySQL **tasks.envelope.image_edit_authorizations[image_idx]**, with
`task_id`, `image_idx`, `source_sha256`, `user_id` and `confirmed_at`. It is not stored in global
settings or remembered image preferences. Its hash must match the actual newly downloaded source,
and it is consulted only for requested scoped promotion cleanup. It never permits removal of
source/copyright/author marks or bypasses sensitive-content checks, rectangle guards or Vision QC.
Changing the task, image index, actor or source bytes invalidates this authorization.

The same atomic update sets `processing_mode=images_only` and `image_retry_indices=[image_idx]`.
The worker downloads/analyzes/processes/persists only this image, retaining its original index.
No intake cleaning, Jev, text evaluation, rewrite, or fact-packet LLM runs for this retry. Vision
analysis, its language check, Image2 edit and image QC remain active. Source analysis is refreshed;
failed fresh analysis is never rescued by old cached mark regions. The original X draft remains
byte-identical, and other images, their R2 assets and delivery receipts remain untouched.

AssetStore.release_task accepts an optional index filter, so replacement cannot free unrelated
assets. Draft delivery receives the same filter and sends only the selected final photo with the original X draft as its caption, followed
by a related review-card reply. The draft is not duplicated when caption delivery is confirmed. Failures include localized connection/timeout/invalid
image/HTTP status categories without exposing raw upstream payloads, source data or credentials.
For database compatibility the historical stored QC enum remains `QC`; review cards display
`IMAGE_QC` and new image verification events use `IMAGE_QC`.

`tests/test_image_authorization.py` covers the complete callback → durable scoped worker →
Image2 → QC → final R2 asset → real photo-send API boundary in both locales, with isolated
transports. It also checks ownership, stale/duplicate callbacks, hash changes, protected target
rejection, explicit rectangle selection, other-image preservation, and every failure stage.


## Promotion-free facts and risk-aware background edits

Text cleanup remains before Jev. Obvious TG promotion headers/footers also have a small
anchored deterministic rule, while the existing extraction model handles varied phrasing.
After Vision, `pipeline/image_content.py` derives `content_text` from raw OCR: identified
promotion strings/handles/URLs and standalone attribution metadata are excluded from publishing
evidence. Raw `extracted_text`, protected mark geometry and provenance stay intact for QC.
Promotion-related source_facts are filtered, fact packets/traceability and rewrite guards use
clean publishing evidence, and known promotion identifiers cannot be reintroduced in drafts.
Even legacy persisted analyses pass through the same evidence filtering.

MarkRegion now records `removal_risk` and a localized `removal_reason`. Vision distinguishes
plain screenshot/terminal background from functional UI controls, meaningful original text,
numbers, identifiers, subjects and protected marks. A tight promotional rectangle on a simple,
unambiguous UI background can be safe; merely being part of a screenshot is not a rejection
criterion. `content_occluded`, `protected` and `uncertain` edits still fail closed, and a safe flag
alone cannot override an explicit risk. Original source dimensions/outside pixels and all QC
checks continue to apply.

Image Tools includes a remembered **Allow an original information card if local cleanup is
unsafe** option (`info_card_fallback`). It defaults off and composes with MINIMAL_CHANGES and
REMOVE_OVERLAYS. Only explicit opt-in allows a NEW fact-verified, localized INFO_CARD when a
promotion patch is unsafe. It does not edit the source or claim to preserve its exact layout;
the review card identifies this fallback. Missing editing rights, sensitive sources or an
explicit selection of a protected attribution target cannot be bypassed. The generator receives
only the verified fact packet and editorial angle, never a documentary source photo. If a
fallback needs a new packet, extraction/verification runs once per image job; no evaluation or
text rewrite is repeated. Every new card still must pass Vision QC before final R2 storage.

Telegram uses named R2 byte streams: a single BytesIO for one photo, lists for albums, caption
on the first confirmed photo, `parse_mode=None` for untouched X draft text, and explicit photo
receipts. Captions conservatively fit within 1024 UTF-16 units; longer text is sent intact as a
related reply. Subsequent album batches do not duplicate captions. If the first photo's caption
is not confirmed, text is still delivered separately rather than silently lost. Pending tasks
continue to record per-image processing/transport failure stages and caption delivery receipts.

`tests/test_promotion_bundle.py` covers text/OCR promotion isolation, source/rights preservation,
empty-UI-background edits, risk-specific review, explicit card fallback, preference persistence,
caption+album grouping in both locales, oversize text and partial Telegram acknowledgements.
