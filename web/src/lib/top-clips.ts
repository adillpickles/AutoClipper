/** The top 10 are rendered as vertical clips automatically (PRD § 4). */
export const TOP_CLIPS = 10;

/**
 * One sentence that matches the results screen's "Top N moments" (#217): every ranked moment
 * was scored, and the top `TOP_CLIPS` of them become clips.
 */
export function momentsSummary(rankedCount: number): string {
  if (rankedCount > TOP_CLIPS) {
    return `Scored ${rankedCount} moments · picked the top ${TOP_CLIPS}.`;
  }
  if (rankedCount === 1) return "Picked the top moment.";
  return `Picked the top ${rankedCount} moments.`;
}
