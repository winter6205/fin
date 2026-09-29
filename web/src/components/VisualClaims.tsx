// The turn's visual-observation rows: what the run looked at on the page,
// which provenance backs it, and a link back to that original location.
//
// This is NOT the citation channel. ``CitationChips.tsx`` places a chip
// with ``answer.indexOf(c.quote)`` precisely because a verified citation is
// a verbatim span of the answer; a visual claim carries no quote at all —
// the wire row holds provenance and deliberately no answer text — so it
// must never enter that path (spec thread-attachment-source-read lines 58
// and 65: a model-generated visual description is never presented as a
// verbatim quote). The copy below says what the rows are, so a reader
// cannot mistake one for a quoted source span.

import { attachmentDisplayName } from "../sourceLocator";
import {
  VISUAL_CLAIMS_HEADING,
  VISUAL_NOT_A_QUOTE_NOTE,
  visualClaimCallsLabel,
  visualClaimEvidenceLabel,
  visualClaimPageLabel,
  visualClaimProvenanceLabel,
  visualClaimScopeLabel,
  visualClaimSourceRef,
  visualClaimSourceUrl,
  visualClaimTargetLabel,
} from "../visualClaims";
import type { VisualClaim } from "../types";

function VisualClaimRow({
  threadId,
  claim,
}: {
  threadId: string;
  claim: VisualClaim;
}) {
  const name = attachmentDisplayName(claim.attachment_id, claim.filename);
  const page = visualClaimPageLabel(claim);
  return (
    <li
      data-testid="visual-claim-row"
      // The raster this observation was made from: what makes the claim
      // about a specific rendered page rather than about the model's
      // memory of it. Kept on the row so the provenance is inspectable.
      data-page-image-sha256={claim.page_image_sha256}
      className="border-t border-black first:border-t-0 px-2 py-1.5 text-xs font-mono text-gray-700"
    >
      <a
        data-testid="visual-claim-source"
        href={visualClaimSourceUrl(threadId, claim)}
        data-source-ref={JSON.stringify(visualClaimSourceRef(claim))}
        aria-label={`回到原始位置：${name} ${page}`}
        className="underline decoration-dotted underline-offset-2 hover:text-black focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:ring-offset-1 focus:ring-offset-[#FAFAF7]"
      >
        {name} {page}
      </a>
      <span data-testid="visual-claim-target"> · {visualClaimTargetLabel(claim)}</span>
      <span data-testid="visual-claim-scope">
        {" "}
        · {visualClaimScopeLabel(claim.scope)}
      </span>
      <span data-testid="visual-claim-provenance">
        {" "}
        · {visualClaimProvenanceLabel(claim)} · {visualClaimCallsLabel(claim)}
      </span>
      <span data-testid="visual-claim-evidence">
        {" "}
        · {visualClaimEvidenceLabel(claim)}
      </span>
    </li>
  );
}

export default function VisualClaims({
  threadId,
  claims,
}: {
  threadId: string;
  claims: VisualClaim[];
}) {
  if (claims.length === 0) return null;
  return (
    <section
      data-testid="visual-claims"
      aria-label={VISUAL_CLAIMS_HEADING}
      className="mt-2 border border-black bg-card"
    >
      <p className="px-2 py-1.5 text-xs font-mono font-medium border-b border-black">
        {VISUAL_CLAIMS_HEADING}
      </p>
      <p className="px-2 py-1.5 text-xs text-gray-600 border-b border-black">
        {VISUAL_NOT_A_QUOTE_NOTE}
      </p>
      <ul>
        {claims.map((claim, i) => (
          <VisualClaimRow
            key={`${claim.attachment_id}-${claim.unit}-${i}`}
            threadId={threadId}
            claim={claim}
          />
        ))}
      </ul>
    </section>
  );
}
