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
the existing per-user preference service. Confirm ownership/authorization using the existing
`pipeline.owned_source_ids` or `pipeline.direct_uploads_owned` configuration; an option toggle
alone never grants editing rights. Forwarded origins must be authorized per image.

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
6. Images are sent before the review card and unchanged X draft. Text still arrives when an
   image fails. A stale final pointer cannot override a REVIEW decision/QC failure.

This uses the official [Telethon send_file documentation](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.uploads.UploadMethods.send_file).
Telegram may apply its own photo compression; exact pixel preservation describes the stored
final asset before Telegram's photo transport.

The review card and `draft_meta.delivery` distinguish **IMAGE_POLICY**, **IMAGE2**, **QC**,
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
