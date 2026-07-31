/**
 * Strip LLM reasoning-chain wrappers (think / reasoning tags) from message
 * content before rendering.
 *
 * Mirrors the backend `_strip_think_blocks` so that:
 * - historical messages already persisted with leaked tags are cleaned up on
 *   the client (the backend fix only prevents NEW leaks);
 * - any residual bypass path (e.g. a non-streaming LLM call that does not go
 *   through the AssistantStreamCallback) is also covered.
 *
 * Order matters: complete blocks first, then a trailing unclosed open tag
 * (strip to end), finally any leftover orphan tags (a closing tag whose
 * opening tag was lost across a token boundary or a previous message).
 */

const THINK_BLOCK_RE = /<(think|reasoning)\b[^>]*>.*?<\/\1\s*>/gis;
const THINK_TRAILING_RE = /<(think|reasoning)\b[^>]*>.*$/gis;
const THINK_STRAY_RE = /<\/?(think|reasoning)\b[^>]*>/gi;

export function stripThinkBlocks(
  text: string | undefined | null,
): string {
  if (!text) return "";
  return text
    .replace(THINK_BLOCK_RE, "")
    .replace(THINK_TRAILING_RE, "")
    .replace(THINK_STRAY_RE, "");
}
