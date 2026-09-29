// The visual-observation lane's presentation decisions, kept pure so the
// copy, the region wording and the link back to the original page are
// testable without a DOM. ``components/VisualClaims.tsx`` is the only
// place that turns these into markup.
//
// The lane exists because a generated visual observation must never reach
// the quote channel (spec thread-attachment-source-read lines 58 and 65):
// the citation chips place a span with ``answer.indexOf(c.quote)``, and a
// visual claim has no quote to place. Nothing here produces one.

import {
  attachmentSourceRef,
  formatUnitLocator,
  threadAttachmentUrl,
} from "./sourceLocator";
import type {
  AttachmentSourceRef,
  VisualClaim,
  VisualObservationScope,
} from "./types";

/** The surface's own heading. Names what the rows ARE — a model-generated
 * visual observation — and what they are NOT, so a reader cannot take one
 * for a quoted source span. */
export const VISUAL_CLAIMS_HEADING = "视觉观察（模型生成，非原文引用）";

/** One line under the heading: the observation is a conclusion about the
 * page image, not a fragment of the file's text. */
export const VISUAL_NOT_A_QUOTE_NOTE =
  "以下内容由视觉模型观察该页图像生成，是观察结论，不是原文引用；点行内链接回到原始位置。";

/** The row's own dispatch key, as the host's ``VISUAL_EVIDENCE_KIND``
 * sends it. A row tagged anything else belongs to a channel this client
 * does not render. */
const VISUAL_CLAIM_KIND = "visual_observation";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFilledString(value: unknown): value is string {
  return typeof value === "string" && value !== "";
}

/** Where the observation says it came from. The two fields the display
 * reads aloud — the attachment and the target — must be non-empty; a row
 * that cannot name either is not a claim about a page. */
function claimProvenance(value: Record<string, unknown>) {
  if (
    !isFilledString(value.attachment_id) ||
    !isFilledString(value.detail) ||
    typeof value.version !== "string" ||
    typeof value.provider !== "string" ||
    typeof value.model_version !== "string" ||
    typeof value.page_image_sha256 !== "string" ||
    value.page_image_sha256.trim() === "" ||
    typeof value.filename !== "string"
  ) {
    return null;
  }
  return {
    attachment_id: value.attachment_id,
    detail: value.detail,
    version: value.version,
    provider: value.provider,
    model_version: value.model_version,
    page_image_sha256: value.page_image_sha256,
    filename: value.filename,
  };
}

/** The row's ordinals and its cost. ``unit`` and ``page`` are both
 * one-based, so a zero or fractional one locates nothing. */
function claimCounts(value: Record<string, unknown>) {
  if (
    !Number.isInteger(value.unit) ||
    (value.unit as number) < 1 ||
    !Number.isInteger(value.page) ||
    (value.page as number) < 1 ||
    !Number.isInteger(value.observation_version) ||
    !Number.isInteger(value.outbound_calls) ||
    (value.outbound_calls as number) < 0 ||
    typeof value.reused !== "boolean"
  ) {
    return null;
  }
  return {
    unit: value.unit as number,
    page: value.page as number,
    observation_version: value.observation_version as number,
    outbound_calls: value.outbound_calls as number,
    reused: value.reused,
  };
}

/** `undefined` marks a region this client cannot name, which is different
 * from `null` = the whole rendered page: a bounds list of the wrong length
 * or an out-of-range coordinate is not a region claim. */
function parseScope(
  value: unknown,
): VisualObservationScope | null | undefined {
  if (value === null) return null;
  if (
    !Array.isArray(value) ||
    value.length !== 4 ||
    !value.every(
      (bound) => typeof bound === "number" && bound >= 0 && bound <= 1,
    )
  ) {
    return undefined;
  }
  return value as VisualObservationScope;
}

/**
 * Narrow one wire row to a renderable ``VisualClaim``, else null.
 *
 * The wire is ``unknown`` (SSE payload and GET history row alike), and this
 * row is the operator's only evidence that a claim came from a real page.
 * A row missing its identity, page, target or raster hash cannot carry
 * that claim, so it renders nothing rather than a half-row with
 * ``undefined`` in the provenance line.
 *
 * A row that passes is REBUILT from the contract keys, never cast: an entry
 * carrying something the contract does not list — a ``quote``, above all —
 * loses it here. That is what makes the lane structurally unable to reach
 * the chip layer, which places a span with ``answer.indexOf(c.quote)``.
 */
export function parseVisualClaim(value: unknown): VisualClaim | null {
  if (!isRecord(value) || value.kind !== VISUAL_CLAIM_KIND) return null;
  const provenance = claimProvenance(value);
  const counts = claimCounts(value);
  const scope = parseScope(value.scope);
  if (provenance === null || counts === null || scope === undefined) {
    return null;
  }
  // Assembled in the host's declared key order rather than by spreading
  // the validation helpers: the row IS the contract, and a reader
  // comparing it against the wire sees the same key sequence.
  return {
    kind: VISUAL_CLAIM_KIND,
    attachment_id: provenance.attachment_id,
    version: provenance.version,
    unit: counts.unit,
    page: counts.page,
    observation_version: counts.observation_version,
    scope,
    provider: provenance.provider,
    model_version: provenance.model_version,
    page_image_sha256: provenance.page_image_sha256,
    detail: provenance.detail,
    filename: provenance.filename,
    reused: counts.reused,
    outbound_calls: counts.outbound_calls,
  };
}

/** Every renderable row in a wire list; malformed entries drop out rather
 * than failing the turn. */
export function parseVisualClaims(value: unknown): VisualClaim[] {
  if (!Array.isArray(value)) return [];
  const out: VisualClaim[] = [];
  for (const entry of value) {
    const claim = parseVisualClaim(entry);
    if (claim !== null) out.push(claim);
  }
  return out;
}

/** The rows of a ``visual_claims`` SSE payload, whose body is
 * ``{"visual_claims": [row, …]}``. A payload that is not that shape
 * carries no rows — an empty list, never a crash. */
export function parseVisualClaimFrame(payload: unknown): VisualClaim[] {
  if (!isRecord(payload)) return [];
  return parseVisualClaims(payload.visual_claims);
}

/** The turn's visual claims. The host always sends an array (empty when
 * the run made no claim), and a peer that predates the lane sends no field
 * at all — which reads as none rather than as an unknown number. Parsed
 * here rather than trusted because a GET history row is unvalidated JSON. */
export function turnVisualClaims(turn: { visual_claims?: unknown }): VisualClaim[] {
  return parseVisualClaims(turn.visual_claims ?? []);
}

/** The physical page the model read. A visual claim is always about a
 * rendered page image, so it reads as a page; the ``unit`` ordinal belongs
 * to the locator that opens the source, not to this label. */
export function visualClaimPageLabel(claim: VisualClaim): string {
  return formatUnitLocator("page", claim.page);
}

/** The exact chart / series / category target the observation answers,
 * shown as a labelled value rather than as spanned prose. */
export function visualClaimTargetLabel(claim: VisualClaim): string {
  return `目标：${claim.detail}`;
}

/** Whether this run read a stored observation or inspected the page again.
 * The call count is a separate label (``visualClaimCallsLabel``) so a
 * reuse never has to imply a number the row did not report. */
export function visualClaimProvenanceLabel(claim: VisualClaim): string {
  return claim.reused ? "复用已存观察" : "本次新读取页面";
}

/** Outbound attempts this read cost the run — ``0`` is the reuse case and
 * stays visible as ``0``, because "no call happened" is the claim. */
export function visualClaimCallsLabel(claim: VisualClaim): string {
  return `视觉调用 ${claim.outbound_calls} 次`;
}

/** Which persisted observation version answered, from which provider and
 * model. Provenance the operator can check against the attachment. */
export function visualClaimEvidenceLabel(claim: VisualClaim): string {
  return `观察 v${claim.observation_version} · ${claim.provider}/${claim.model_version}`;
}

/** The region that was read, or the whole rendered page when no reliable
 * box existed. Bounds are already normalized to the page image, so this
 * client prints them as given and rescales nothing. */
export function visualClaimScopeLabel(
  scope: VisualObservationScope | null,
): string {
  if (scope === null) return "整页（无可信区域框）";
  return `区域 ${scope.map((bound) => bound.toFixed(2)).join(", ")}`;
}

/** The locator this claim points at — the same ``source_ref`` shape an
 * attachment citation resolves, built through the shared helper so a
 * citation and a claim naming one unit are one locator, not two. */
export function visualClaimSourceRef(claim: VisualClaim): AttachmentSourceRef {
  return attachmentSourceRef(claim);
}

/** Where the link lands: the host's thread attachment door, the one address
 * that names the source this claim came from. The page raster the
 * observation was read from has no durable HTTP seam of its own yet, so the
 * link names the attachment through the shared door builder and carries the
 * exact ``source_ref`` (``data-source-ref``) for the unit rather than
 * fabricating an image route. */
export function visualClaimSourceUrl(
  threadId: string,
  claim: VisualClaim,
): string {
  return threadAttachmentUrl(threadId, claim.attachment_id);
}
